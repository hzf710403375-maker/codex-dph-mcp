"""Pinned, portable task context. Does not access any agent's chat history."""
import copy
import hashlib
import json
from pathlib import PurePosixPath

from .git import git

MAX_DOCUMENT = 64 * 1024
MAX_BUNDLE = 256 * 1024
DEFAULT_DOCUMENTS = ('AGENTS.md', '.ai-workflow/requirements.md',
                     '.ai-workflow/architecture.md', '.ai-workflow/acceptance.md')
MANIFEST = '.ai-workflow/context.json'


class ContextError(ValueError):
    pass


def serialize(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(serialize(value).encode('utf-8')).hexdigest()


def bounded(bundle):
    if len(serialize(bundle).encode('utf-8')) > MAX_BUNDLE:
        raise ContextError('Context bundle exceeds 256 KiB; shorten summaries/documents/history before creating a new task')
    return bundle


def validate_fields(value):
    if not isinstance(value, dict) or set(value)-{'requirements_summary','design_decisions','constraints','documents'}:
        raise ContextError('Context allows only requirements_summary, design_decisions, constraints and documents')
    if not isinstance(value.get('requirements_summary',''),str):
        raise ContextError('Context requirements_summary must be a string')
    for field in ('design_decisions','constraints','documents'):
        items=value.get(field,[])
        if not isinstance(items,list) or len(items)>128 or any(not isinstance(item,str) or not item.strip() for item in items):
            raise ContextError(f'Context {field} must contain at most 128 nonempty strings')
    if len(serialize(value).encode('utf-8'))>MAX_BUNDLE:
        raise ContextError('Context input exceeds 256 KiB')
    return value


def path_name(value):
    path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\\' in value or not path.parts or '\x00' in value:
        raise ContextError('Context document paths must stay inside the repository')
    for part in path.parts:
        lower=part.lower()
        if lower in {'.git','.ssh','.aws','.azure','.gnupg','.kube','.docker','.netrc','.npmrc','.pypirc','.git-credentials','auth.json','credentials','credentials.json','application_default_credentials.json','id_rsa','id_dsa','id_ecdsa','id_ecdsa_sk','id_ed25519','id_ed25519_sk','identity'} or lower.startswith('.env') or lower.endswith(('.pem','.key','.p12','.pfx')):
            raise ContextError(f'Sensitive context path is prohibited: {value}')
    return str(path)


def document(root, commit, name, *, optional=False):
    name=path_name(name)
    # Literal pathspec prevents wildcard/magic paths from selecting unintended blobs.
    entry=git(root,'ls-tree','-z',commit,'--',':(literal)'+name)
    if not entry:
        if optional:
            return None
        raise ContextError(f'Context document is not tracked at the base commit: {name}')
    header,actual=entry.rstrip('\0').split('\t',1)
    mode,kind,oid=header.split()
    if actual!=name or mode not in {'100644','100755'} or kind!='blob':
        raise ContextError(f'Context document must be a regular tracked file: {name}')
    if int(git(root,'cat-file','-s',oid))>MAX_DOCUMENT:
        raise ContextError(f'Context document exceeds 64 KiB: {name}')
    try:
        content=git(root,'cat-file','blob',oid,raw=True).decode('utf-8')
    except UnicodeDecodeError as e:
        raise ContextError(f'Context document must be UTF-8: {name}') from e
    if '\x00' in content:
        raise ContextError(f'Binary context document is unsupported: {name}')
    return dict(path=name,source_commit=commit,git_blob=oid,sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),content=content)


def build(root, commit, task_context):
    task=validate_fields(task_context)
    source=document(root,commit,MANIFEST,optional=True)
    project={}
    if source:
        try:
            project=validate_fields(json.loads(source['content']))
        except (ValueError,TypeError) as e:
            raise ContextError(f'Invalid {MANIFEST}: {e}') from e
    documents=[]
    by_path={}
    for name in DEFAULT_DOCUMENTS:
        item=document(root,commit,name,optional=True)
        if item:
            by_path[item['path']]=item
    for name in project.get('documents',[])+task.get('documents',[]):
        name=path_name(name)
        if name not in by_path:
            by_path[name]=document(root,commit,name)
        if len(by_path)>32:
            raise ContextError('Context bundle may include at most 32 documents')
    documents=list(by_path.values())
    return bounded(dict(schema_version=1,source_commit=commit,
        project_requirements_summary=project.get('requirements_summary',''),
        requirements_summary=task.get('requirements_summary',''),
        design_decisions=list(dict.fromkeys(project.get('design_decisions',[])+task.get('design_decisions',[]))),
        constraints=list(dict.fromkeys(project.get('constraints',[])+task.get('constraints',[]))),
        documents=documents,review_history=[],
        manifest_source={k:source[k] for k in ('path','source_commit','git_blob','sha256')} if source else None))


def with_history(bundle, history):
    result=copy.deepcopy(bundle)
    result['review_history']=history
    return bounded(result)


def execution_view(bundle, spec, *, archive_path, max_bytes=32768):
    """Small execution view; immutable full bundle remains available for audit/retrieval."""
    if not bundle:
        return None
    result={k:copy.deepcopy(bundle.get(k)) for k in ('schema_version','source_commit',
        'project_requirements_summary','requirements_summary','constraints','design_decisions','manifest_source')}
    result.update(archive_path=archive_path,documents=[],review_history=[])
    for item in bundle['documents']:
        result['documents'].append({k:v for k,v in item.items() if k!='content'})
        result['documents'][-1]['retrieval']={'git_blob':item['git_blob'],
            'argv':['git','cat-file','blob',item['git_blob']]}
    reviews=bundle['review_history']
    for i,review in enumerate(reviews):
        if i==len(reviews)-1:
            result['review_history'].append(copy.deepcopy(review))
        else:
            summary={k:copy.deepcopy(review.get(k)) for k in ('review_id','run_id','candidate_commit','decision')}
            summary.update(issue_previews=[],issue_count=len(review['issues']),historical=True,
                failed_check_count=sum(check.get('exit_code',0)!=0 for check in review.get('checks',[])))
            result['review_history'].append(summary)
    result['history_note']='Older reviews are historical, not automatically resolved; full details are in archive_path'
    def size():
        return len(serialize(result).encode('utf-8'))
    if size()>max_bytes:
        raise ContextError('Execution context mandatory fields exceed budget; shorten summaries/constraints/latest review')
    selected=set(spec.get('context',{}).get('documents',[]))
    # Project rules first, then task-explicit references. Other documents stay on demand.
    ordered=sorted(enumerate(bundle['documents']),key=lambda pair:pair[1]['path']!='AGENTS.md')
    for index,item in ordered:
        if item['path']!='AGENTS.md' and item['path'] not in selected:
            continue
        result['documents'][index]['content']=item['content']
        if size()>max_bytes:
            del result['documents'][index]['content']
    # Spend only remaining space on historical previews, newest older review first.
    for index in reversed(range(max(0,len(reviews)-1))):
        previews=result['review_history'][index]['issue_previews']
        for issue in reviews[index]['issues']:
            previews.append(issue[:160])
            if size()>max_bytes:
                previews.pop()
                break
    return result
