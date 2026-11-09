import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codex_dph.service import Service, Conflict, Invalid
from codex_dph.worker import Worker


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@local', *args], cwd=root, text=True).strip()


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'hello.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    service = Service(tmp_path / 'state')
    project = service.register(repo, {'ok': [sys.executable, '-c', 'print("checked")']})
    spec = {'project_id': project['project_id'], 'title': 'change', 'objective': 'update hello',
            'allowed_paths': ['hello.txt', 'new.txt'], 'acceptance_criteria': ['hello updated'], 'check_ids': ['ok']}
    return service, repo, spec


def test_idempotency_and_dirty_workspace(setup):
    s, repo, spec = setup
    first = s.submit(spec, 'one')
    assert s.submit(spec, 'one')['run_id'] == first['run_id']
    with pytest.raises(Conflict):
        s.submit(dict(spec, title='different'), 'one')
    (repo / 'dirty').write_text('x')
    with pytest.raises(Invalid):
        s.submit(spec, 'two')


def test_input_validation(setup):
    s, _, spec = setup
    for changes in [{'allowed_paths': ['../escape']}, {'check_ids': ['unknown']}, {'acceptance_criteria': []}]:
        with pytest.raises(Invalid):
            s.submit(dict(spec, **changes), 'key')


def test_success_review_and_revision(setup):
    s, repo, spec = setup
    run = s.submit(spec, 'one')
    Worker(s, command=[sys.executable, str(Path(__file__).with_name('fake_dsh.py'))]).once()
    ready = s.get(run['run_id'])
    assert ready['state'] == 'awaiting_review', ready
    assert (repo / 'hello.txt').read_text() == 'base\n'
    assert 'new.txt' in s.artifacts(run['run_id'])['files']
    assert s.artifacts(run['run_id'])['checks'][0]['exit_code'] == 0
    with pytest.raises(Conflict):
        s.review(run['run_id'], 'wrong', 'accepted', ['pass'], [], 'r0')
    review = s.review(run['run_id'], ready['candidate_commit'], 'changes_requested', ['fail'], ['fix hello'], 'r1')
    again = s.revise(run['run_id'], review['review_id'], 'revision')
    assert again['run_id'] != run['run_id']
    Worker(s, command=[sys.executable, str(Path(__file__).with_name('fake_dsh.py'))]).once()
    fixed = s.get(again['run_id'])
    s.review(fixed['run_id'], fixed['candidate_commit'], 'accepted', ['pass'], [], 'r2')
    assert s.get(fixed['run_id'])['state'] == 'accepted'


def test_failed_and_truncated_output(setup):
    s, _, spec = setup
    run = s.submit(spec, 'one')
    Worker(s, command=[sys.executable, '-c', 'print("{}")']).once()
    assert s.get(run['run_id'])['state'] == 'failed'


def test_external_change_invalidates_review(setup):
    s, _, spec = setup
    run = s.submit(spec, 'one')
    Worker(s, command=[sys.executable, str(Path(__file__).with_name('fake_dsh.py'))]).once()
    ready = s.get(run['run_id'])
    (Path(ready['workspace']) / 'hello.txt').write_text('tampered')
    with pytest.raises(Conflict):
        s.review(run['run_id'], ready['candidate_commit'], 'accepted', ['pass'], [], 'review')


def test_checks_cannot_be_skipped(setup):
    s, _, spec = setup
    project = s.projects()[0]
    s.configure_checks(project['project_id'], {'bad': [sys.executable, '-c', 'raise SystemExit(2)']})
    run = s.submit(dict(spec, check_ids=['bad']), 'one')
    Worker(s, command=[sys.executable, str(Path(__file__).with_name('fake_dsh.py'))]).once()
    assert s.get(run['run_id'])['state'] == 'blocked'


def test_cancel_queued(setup):
    s, _, spec = setup
    run = s.submit(spec, 'one')
    assert s.cancel(run['run_id'])['state'] == 'cancelled'
    assert Worker(s).once() is False


def test_cancellation_stops_writes(setup):
    import threading
    s, _, spec = setup
    run = s.submit(spec, 'one')
    script = 'import time; from pathlib import Path\nwhile True:\n Path("hello.txt").write_text(str(time.time())); time.sleep(.05)'
    worker = Worker(s, command=[sys.executable, '-c', script])
    thread = threading.Thread(target=worker.once)
    thread.start()
    deadline = time.monotonic() + 10
    while s.get(run['run_id'])['state'] != 'running' and time.monotonic() < deadline:
        time.sleep(.02)
    s.cancel(run['run_id'])
    thread.join(10)
    assert not thread.is_alive()
    result = s.get(run['run_id'])
    assert result['state'] == 'cancelled', result
    path = Path(result['workspace']) / 'hello.txt'
    content = path.read_bytes()
    time.sleep(.15)
    assert path.read_bytes() == content


def test_worker_recovers_without_replaying(setup):
    s, _, spec = setup
    run = s.submit(spec, 'one')
    s.update(run['run_id'], state='running')
    Worker(s).recover()
    assert s.get(run['run_id'])['state'] == 'interrupted'
    assert Worker(s).once() is False


def test_cannot_fork_multiple_revisions(setup):
    s, _, spec = setup
    r = s.submit(spec,'one')
    w = Worker(s,command=[sys.executable,str(Path(__file__).with_name('fake_dsh.py'))])
    w.once()
    ready = s.get(r['run_id'])
    review = s.review(r['run_id'],ready['candidate_commit'],'changes_requested',['fail'],['fix'],'r')
    first = s.revise(r['run_id'],review['review_id'],'retry')
    assert s.revise(r['run_id'],review['review_id'],'retry')['run_id'] == first['run_id']
    with pytest.raises(Conflict):
        s.revise(r['run_id'],review['review_id'],'other')


def test_failed_checks_allow_revision_but_not_acceptance(setup):
    s, _, spec = setup
    p = s.projects()[0]
    s.configure_checks(p['project_id'], {'bad':[sys.executable,'-c','raise SystemExit(2)']})
    r = s.submit(dict(spec,check_ids=['bad']),'one')
    Worker(s,command=[sys.executable,str(Path(__file__).with_name('fake_dsh.py'))]).once()
    ready = s.get(r['run_id'])
    with pytest.raises(Invalid):
        s.review(r['run_id'],ready['candidate_commit'],'accepted',['pass'],[],'accept')
    review = s.review(r['run_id'],ready['candidate_commit'],'changes_requested',['fail'],['fix failed check'],'reject')
    assert s.revise(r['run_id'],review['review_id'],'fix')['state'] == 'queued'


def test_cancel_unknown_is_explicit(setup):
    s, _, _ = setup
    with pytest.raises(Invalid):
        s.cancel('unknown')


def test_cancel_wins_completion_race(setup):
    s, _, spec = setup
    r = s.submit(spec,'one')
    original = s.update
    def concurrent_update(run_id, **fields):
        if fields.get('state') == 'awaiting_review':
            s.cancel(run_id)
        return original(run_id,**fields)
    s.update = concurrent_update
    Worker(s,command=[sys.executable,str(Path(__file__).with_name('fake_dsh.py'))]).once()
    assert s.get(r['run_id'])['state'] == 'cancelled'


def test_interrupted_reconciliation_unlocks_project(setup):
    s, _, spec = setup
    r = s.submit(spec,'one')
    s.update(r['run_id'],state='running')
    Worker(s).recover()
    s.reconcile(r['run_id'])
    assert s.get(r['run_id'])['state'] == 'cancelled'
    assert s.submit(spec,'next')['state'] == 'queued'



def test_large_prompt_survives_delayed_stdin_reader(tmp_path, monkeypatch):
    from codex_dph import process
    # No detached children in this case; keep this regression about stdin delivery.
    monkeypatch.setattr(process, 'stop_owned', lambda token: None)
    script = 'import sys,time; time.sleep(.3); data=sys.stdin.buffer.read(); print(len(data))'
    payload = 'x' * 25_000
    result = process.execute([sys.executable, '-c', script], str(tmp_path), payload,
                             tmp_path/'out', tmp_path/'err', 2, lambda: False, lambda pid: None)
    assert result == 0
    assert (tmp_path/'out').read_text().strip() == '25000'

def test_detached_descendant_is_stopped(tmp_path):
    from codex_dph.process import execute
    output = tmp_path/'escaped'
    child = f'from pathlib import Path; import time; time.sleep(.8); Path({str(output)!r}).write_text("escaped")'
    script = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); time.sleep(.2)'
    result = execute([sys.executable,'-c',script],str(tmp_path),'',tmp_path/'out',tmp_path/'err',5,lambda:False,lambda pid:None)
    assert result == 0, (tmp_path/'err').read_text()
    time.sleep(1)
    assert not output.exists()


def test_unicode_symlink_delivery_rejected(setup):
    s, _, spec = setup
    r = s.submit(dict(spec,allowed_paths=['**']),'one')
    script = 'import json; from pathlib import Path; Path("中文链接").symlink_to("/etc/passwd"); print(json.dumps({"type":"final","reason":{"kind":"completed"}}))'
    Worker(s,command=[sys.executable,'-c',script]).once()
    assert s.get(r['run_id'])['state'] == 'blocked'
    assert s.get(r['run_id'])['candidate_commit'] is None


def test_revision_replay_after_completion(setup):
    s, _, spec = setup
    r = s.submit(spec,'one')
    w = Worker(s,command=[sys.executable,str(Path(__file__).with_name('fake_dsh.py'))])
    w.once()
    ready = s.get(r['run_id'])
    review = s.review(r['run_id'],ready['candidate_commit'],'changes_requested',['fail'],['fix'],'r')
    fixed = s.revise(r['run_id'],review['review_id'],'same-key')
    w.once()
    assert s.revise(r['run_id'],review['review_id'],'same-key')['run_id'] == fixed['run_id']


def test_event_log_available_while_executor_runs(setup):
    import threading
    s, _, spec = setup
    r = s.submit(spec, 'live-events')
    script = 'import json,time; print(json.dumps({"type":"session","sessionId":"live"}),flush=True); time.sleep(1); print(json.dumps({"type":"final","reason":{"kind":"completed"}}),flush=True)'
    thread = threading.Thread(target=Worker(s,command=[sys.executable,'-c',script]).once)
    thread.start()
    try:
        deadline = time.monotonic()+5
        seen = []
        while time.monotonic()<deadline and thread.is_alive():
            seen = s.events(r['run_id'])['events']
            if seen:
                break
            time.sleep(.03)
        assert seen and seen[0]['event']['type'] == 'session'
        assert s.get(r['run_id'])['state'] == 'running'
    finally:
        thread.join(10)


def test_waiting_for_review_does_not_consume_execution_budget(setup):
    s, _, spec = setup
    r = s.submit(spec,'one')
    w = Worker(s,command=[sys.executable,str(Path(__file__).with_name('fake_dsh.py'))])
    w.once()
    ready = s.get(r['run_id'])
    s.update(r['run_id'],total_started_at=time.time()-10000)
    review = s.review(r['run_id'],ready['candidate_commit'],'changes_requested',['fail'],['fix'],'r')
    revision = s.revise(r['run_id'],review['review_id'],'again')
    w.once()
    assert s.get(revision['run_id'])['state'] == 'awaiting_review'


def test_process_inspection_denial_is_not_success(monkeypatch):
    import os
    import psutil
    from types import SimpleNamespace
    from codex_dph.process import stop_owned
    class Denied:
        def uids(self):
            return SimpleNamespace(real=os.getuid())
        def environ(self):
            raise psutil.AccessDenied(123)
    monkeypatch.setattr(psutil,'process_iter',lambda *args:[Denied()])
    with pytest.raises(RuntimeError,match='Cannot confirm'):
        stop_owned('token')


def test_terminal_state_includes_settled_execution_time(setup):
    s, _, spec = setup
    r = s.submit(spec,'one')
    original = s.update
    observed = []
    def capture(run_id,**fields):
        result = original(run_id,**fields)
        if fields.get('state') == 'awaiting_review':
            observed.append(result['execution_seconds'])
        return result
    s.update = capture
    Worker(s,command=[sys.executable,str(Path(__file__).with_name('fake_dsh.py'))]).once()
    assert len(observed)==1 and observed[0]>0


def test_event_backlog_does_not_starve_cancellation(setup):
    import threading
    s, _, spec = setup
    run = s.submit(spec,'event-backlog')
    real_event = s.event
    entered = threading.Event()
    def slow_storage(run_id,event):
        entered.set()
        time.sleep(.003)
        real_event(run_id,event)
    s.event = slow_storage
    script = 'import json,time; print("\\n".join(json.dumps({"type":"text","text":"progress"}) for _ in range(1200)),flush=True); time.sleep(30)'
    thread = threading.Thread(target=Worker(s,command=[sys.executable,'-c',script]).once)
    thread.start()
    try:
        assert entered.wait(5)
        s.cancel(run['run_id'])
        thread.join(3)
        assert not thread.is_alive(), 'event ingestion prevented cancellation'
        assert s.get(run['run_id'])['state'] == 'cancelled'
    finally:
        s.cancel(run['run_id'])
        thread.join(10)
