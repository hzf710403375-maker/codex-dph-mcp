import json
import pytest
from codex_dph.context import execution_view, ContextError


def fixture_bundle():
    return dict(schema_version=1,source_commit='a'*40,project_requirements_summary='project goal',
                requirements_summary='task goal',constraints=['Do not add dependencies'],design_decisions=['Use SQLite'],
                documents=[dict(path='AGENTS.md',source_commit='a'*40,git_blob='b'*40,sha256='c'*64,content='Follow project rules'),
                dict(path='docs/large.md',source_commit='a'*40,git_blob='d'*40,sha256='e'*64,content='unused context '*4000)],
                review_history=[dict(run_id=str(i),candidate_commit='f'*40,decision='changes_requested',criteria=['fail'],issues=['issue '+str(i)+' details '*80],checks=[]) for i in range(5)])


def test_execution_view_is_small_and_retains_constraints_and_latest_review():
    bundle=fixture_bundle()
    view=execution_view(bundle,{},archive_path='/local/context.full.json')
    assert len(json.dumps(view,ensure_ascii=False).encode())<=32768
    assert view['constraints']==bundle['constraints']
    assert view['documents'][0]['content']=='Follow project rules'
    assert 'content' not in view['documents'][1]
    assert view['documents'][1]['retrieval']['git_blob']=='d'*40
    assert view['review_history'][-1]['issues']==bundle['review_history'][-1]['issues']
    assert len(view['review_history'][0]['issue_previews'][0])<=160
    assert view['archive_path']=='/local/context.full.json'
    assert len(bundle['documents'][1]['content'])>32768


def test_explicit_document_is_included_if_budget_permits():
    bundle=fixture_bundle();bundle['documents'][1]['content']='Relevant spec'
    view=execution_view(bundle,{'context':{'documents':['docs/large.md']}},archive_path='/archive')
    assert view['documents'][1]['content']=='Relevant spec'
    assert fixture_bundle()['documents'][1]['content'].startswith('unused context')


def test_large_mandatory_context_fails_clearly():
    bundle=fixture_bundle();bundle['constraints']=['x'*40000]
    with pytest.raises(ContextError,match='mandatory'):
        execution_view(bundle,{},archive_path='/archive')


def test_large_agents_file_falls_back_to_exact_retrieval():
    bundle=fixture_bundle();bundle['documents'][0]['content']='rules '*7000
    view=execution_view(bundle,{},archive_path='/archive')
    assert 'content' not in view['documents'][0]
    assert view['documents'][0]['retrieval']['argv']==['git','cat-file','blob','b'*40]


def test_old_review_previews_cannot_starve_latest_actionable_review():
    bundle=fixture_bundle()
    bundle['review_history']=[dict(run_id=str(i),candidate_commit='f'*40,decision='changes_requested',criteria=['fail']*35,
        issues=['旧问题'*50 for _ in range(35)],checks=[]) for i in range(3)]
    view=execution_view(bundle,{},archive_path='/archive')
    assert len(json.dumps(view,ensure_ascii=False).encode())<=32768
    assert view['review_history'][-1]['issues']==bundle['review_history'][-1]['issues']
    assert view['review_history'][0]['issue_count']==35
    assert view['archive_path']=='/archive'
