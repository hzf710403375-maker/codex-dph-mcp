import hashlib
import json
import sys
from pathlib import Path

import pytest

from codex_dph.service import Invalid
from codex_dph.worker import Worker
from test_workflow import setup, git


def commit_docs(repo, files):
    for name, text in files.items():
        p=repo/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(text)
    git(repo,'add','.')
    git(repo,'commit','-qm','project context')


def test_project_and_task_context_are_merged_and_frozen(setup):
    s,repo,spec=setup
    text='  Important design\n\n'
    commit_docs(repo,{'AGENTS.md':'Use existing patterns\n','docs/design.md':text,
        '.ai-workflow/context.json':json.dumps({'requirements_summary':'Project goal',
        'design_decisions':['Use SQLite'],'constraints':['No new dependencies'],'documents':['docs/design.md']})})
    run=s.submit(dict(spec,context={'requirements_summary':'Task background','constraints':['Keep API stable']}),'one')
    bundle=s.artifacts(run['run_id'])['context_bundle']
    assert bundle['project_requirements_summary']=='Project goal'
    assert bundle['requirements_summary']=='Task background'
    assert bundle['constraints']==['No new dependencies','Keep API stable']
    assert bundle['design_decisions']==['Use SQLite']
    document=next(d for d in bundle['documents'] if d['path']=='docs/design.md')
    assert document['content']==text
    assert document['sha256']==hashlib.sha256(text.encode()).hexdigest()
    commit_docs(repo,{'docs/design.md':'new design'})
    assert s.artifacts(run['run_id'])['context_bundle']==bundle


@pytest.mark.parametrize('context',[
    {'documents':['../outside']},{'documents':['.env.local']},{'documents':['docs/missing.md']},
    {'constraints':'not a list'},{'review_history':[{'decision':'accepted'}]},
])
def test_invalid_context_is_rejected(setup,context):
    s,_,spec=setup
    with pytest.raises(Invalid):
        s.submit(dict(spec,context=context),'invalid')
    assert s.list_runs()==[]


def test_symlink_and_large_document_rejected(setup):
    s,repo,spec=setup
    (repo/'linked.md').symlink_to('/etc/passwd')
    git(repo,'add','linked.md');git(repo,'commit','-qm','symlink')
    with pytest.raises(Invalid):
        s.submit(dict(spec,context={'documents':['linked.md']}),'symlink')
    commit_docs(repo,{'large.md':'a'*65537})
    with pytest.raises(Invalid):
        s.submit(dict(spec,context={'documents':['large.md']}),'large')


def test_revisions_preserve_all_reviews_and_deliver_context(setup,tmp_path):
    s,repo,spec=setup
    commit_docs(repo,{'.ai-workflow/architecture.md':'Keep this architecture\n'})
    capture=tmp_path/'captured.json'
    script=f'import sys,json; from pathlib import Path; Path({str(capture)!r}).write_text(sys.stdin.read()); Path(".ai-workflow/architecture.md").write_text("Execution-side update"); print(json.dumps({{"type":"final","reason":{{"kind":"completed"}}}}))'
    worker=Worker(s,command=[sys.executable,'-c',script])
    r=s.submit(dict(spec,allowed_paths=spec['allowed_paths']+['.ai-workflow/architecture.md']),'first')
    original=s.artifacts(r['run_id'])['context_bundle']['documents']
    for i in range(2):
        worker.once()
        ready=s.get(r['run_id'])
        review=s.review(r['run_id'],ready['candidate_commit'],'changes_requested',['fail'],[f'issue-{i}'],f'review-{i}')
        r=s.revise(r['run_id'],review['review_id'],f'fix-{i}')
    worker.once()
    prompt=json.loads(capture.read_text())
    archive=json.loads(Path(prompt['context_bundle']['archive_path']).read_text())
    history=archive['review_history']
    assert [h['issues'] for h in history]==[['issue-0'],['issue-1']]
    assert len({h['run_id'] for h in history})==2
    assert archive['documents']==original
    assert 'No new dependencies' not in json.dumps(prompt)
    artifact=s.artifacts(r['run_id'])
    assert archive==artifact['context_bundle']
    assert prompt['context_bundle']['review_history'][-1]['issues']==['issue-1']
    assert prompt['context_hash']==artifact['context_hash']


def test_context_retrieval_is_bounded(setup):
    s,_,spec=setup
    r=s.submit(spec,'one')
    result=s.context(r['run_id'],offset=0,limit=20)
    assert len(result['text'])==20
    assert result['next_offset']==20
    assert result['total']>20
    with pytest.raises(Invalid):
        s.context(r['run_id'],limit=1000000)


def test_bundle_and_document_count_limits(setup):
    s,repo,spec=setup
    commit_docs(repo,{f'docs/large-{i}.md':'x'*60000 for i in range(5)})
    with pytest.raises(Invalid,match='256 KiB'):
        s.submit(dict(spec,context={'documents':[f'docs/large-{i}.md' for i in range(5)]}),'too-big')
    commit_docs(repo,{f'docs/small-{i}.md':'x' for i in range(33)})
    with pytest.raises(Invalid,match='32 documents'):
        s.submit(dict(spec,context={'documents':[f'docs/small-{i}.md' for i in range(33)]}),'too-many')


def test_invalid_utf8_and_manifest_rejected(setup):
    s,repo,spec=setup
    (repo/'binary.md').write_bytes(b'\xff\xfe')
    git(repo,'add','.');git(repo,'commit','-qm','binary')
    with pytest.raises(Invalid,match='UTF-8'):
        s.submit(dict(spec,context={'documents':['binary.md']}),'binary')
    commit_docs(repo,{'.ai-workflow/context.json':'not JSON'})
    with pytest.raises(Invalid,match='Invalid .ai-workflow/context.json'):
        s.submit(spec,'manifest')


def test_old_task_without_bundle_remains_runnable(setup):
    s,_,spec=setup
    r=s.submit(spec,'old')
    s.update(r['run_id'],context_bundle=None,context_hash=None)
    Worker(s,command=[sys.executable,str(Path(__file__).with_name('fake_dsh.py'))]).once()
    assert s.get(r['run_id'])['state']=='awaiting_review'
    with pytest.raises(Invalid,match='older task'):
        s.context(r['run_id'])


@pytest.mark.parametrize('name',['.aws/credentials','credentials','keys/id_ecdsa','.netrc'])
def test_common_credential_files_are_not_included(setup,name):
    s,repo,spec=setup
    commit_docs(repo,{name:'sensitive placeholder'})
    with pytest.raises(Invalid,match='Sensitive context path'):
        s.submit(dict(spec,context={'documents':[name]}),'secret')


def test_legacy_revision_uses_original_input_documents(setup):
    s,repo,spec=setup
    commit_docs(repo,{'.ai-workflow/architecture.md':'Original design'})
    r=s.submit(dict(spec,allowed_paths=['.ai-workflow/architecture.md']),'legacy')
    s.update(r['run_id'],context_bundle=None,context_hash=None)
    script='import json; from pathlib import Path; Path(".ai-workflow/architecture.md").write_text("Executor rewrite"); print(json.dumps({"type":"final","reason":{"kind":"completed"}}))'
    Worker(s,command=[sys.executable,'-c',script]).once()
    ready=s.get(r['run_id'])
    review=s.review(r['run_id'],ready['candidate_commit'],'changes_requested',['fail'],['fix'],'r')
    fixed=s.revise(r['run_id'],review['review_id'],'fix')
    assert fixed['context_bundle']['documents'][0]['content']=='Original design'


def test_status_and_list_do_not_repeat_full_document_contents(setup):
    s,repo,spec=setup
    commit_docs(repo,{'AGENTS.md':'Context document that should not be repeated on polling'})
    r=s.submit(spec,'one')
    assert 'context_bundle' not in s.status(r['run_id'])
    assert 'context_bundle' not in s.list_runs()[0]
    assert s.status(r['run_id'])['context_hash']==r['context_hash']
    assert s.artifacts(r['run_id'])['context_bundle']['documents'][0]['content'].startswith('Context document')
