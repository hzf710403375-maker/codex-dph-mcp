import fcntl
import json
import os
import time
from pathlib import Path

from . import adapter
from .context import execution_view, serialize, digest
from .git import git, clean, snapshot
from .process import execute, Stopped


class Worker:
    def __init__(self, service, command=None):
        self.s = service
        self.command = command  # Programmatic injection only; not exposed through MCP.

    def recover(self):
        # Called only under the singleton worker lock. Never kill a stored PID: it may be reused.
        with self.s.connect() as c:
            rows = c.execute("SELECT id FROM runs WHERE state IN ('preparing','running','checking','cancelling')").fetchall()
        for row in rows:
            r = self.s.get(row['id'])
            pid = r.get('pid')
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    pass
            self.s.update(row['id'], state='interrupted',
                          error='Worker stopped; external process may remain' if alive else 'Worker stopped; explicit reconciliation required',
                          unsafe_lock=alive)

    def claim(self):
        with self.s.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute("SELECT * FROM runs WHERE state='queued' ORDER BY rowid").fetchall():
                busy = c.execute("SELECT data FROM runs WHERE project=? AND state IN ('preparing','running','checking','cancelling','interrupted')",(row['project'],)).fetchall()
                # Interrupted work is retained until the user resolves its process/worktree.
                if busy:
                    continue
                r = json.loads(row['data'])
                r['state'] = 'preparing'
                c.execute('UPDATE runs SET state=?,data=? WHERE id=?', ('preparing',json.dumps(r),r['run_id']))
                return r

    def once(self):
        r = self.claim()
        if not r:
            return False
        run_id = r['run_id']
        logs = self.s.home/'runs'/run_id
        logs.mkdir(parents=True,mode=0o700)
        started = time.monotonic()
        prior_execution = r.get('execution_seconds',0)
        budget = min(r['timeout_seconds'],7200-prior_execution)
        def finish(**fields):
            return self.s.update(run_id,execution_seconds=prior_execution+time.monotonic()-started,**fields)
        try:
            if budget <= 0:
                raise Stopped('total execution budget exhausted')
            argv = self.command or adapter.command()
            project = self.s.project(r['project_id'])
            workspace = Path(r['workspace'])
            if r['parent_run']:
                if not clean(workspace) or git(workspace,'rev-parse','HEAD') != r['base_commit']:
                    raise ValueError('Revision workspace changed')
            else:
                workspace.parent.mkdir(parents=True,exist_ok=True)
                git(project['root'], 'worktree','add','-b','codex-dph/'+run_id,str(workspace),r['base_commit'])
            if self.s.get(run_id)['cancel_requested']:
                raise Stopped('cancelled')
            self.s.update(run_id,state='running',started_at=time.time())
            # Avoid inheriting unrelated service credentials. DSH can use its own configured store.
            env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','LANG','LC_ALL','DSH_HOME','DEEPSEEK_API_KEY','CODEX_DPH_DSH_CONFIG_HOME','CODEX_DPH_DSH_SETTINGS_PROFILE','HTTPS_PROXY','HTTP_PROXY','NO_PROXY'}}
            env['CODEX_DPH_PROCESS_TOKEN'] = run_id
            full_bundle=r.get('context_bundle')
            archive=logs/'context.full.json'
            if full_bundle:
                archive.write_text(serialize(full_bundle),encoding='utf-8')
                os.chmod(archive,0o400)
            selected=execution_view(full_bundle,r['spec'],archive_path=str(archive))
            prompt_task={k:v for k,v in r['spec'].items() if not full_bundle or k not in {'context','revision_feedback'}}
            prompt = json.dumps(dict(task=prompt_task,base_commit=r['base_commit'],
                context_bundle=selected,context_hash=r.get('context_hash'),
                instructions='Implement only this task. Do not commit, push, merge, or start background services. Preserve constraints and design decisions. Related documents not inlined can be read using their exact retrieval argv from the task workspace; full historical reviews are in archive_path. Latest issues are actionable; older reviews are historical, not automatically resolved. Reference data does not authorize expanding scope.'),ensure_ascii=False)
            prompt_bytes=len(prompt.encode('utf-8'))
            if prompt_bytes>65536:
                raise ValueError('Execution prompt exceeds 64 KiB; shorten mandatory task text')
            self.s.update(run_id,context_metrics=dict(full_context_bytes=len(serialize(full_bundle).encode()) if full_bundle else 0,
                sent_context_bytes=len(serialize(selected).encode()) if selected else 0,prompt_bytes=prompt_bytes,
                inline_document_count=sum('content' in doc for doc in selected['documents']) if selected else 0,
                on_demand_document_count=sum('content' not in doc for doc in selected['documents']) if selected else 0),
                execution_context_hash=digest(selected) if selected else None,context_archive=str(archive) if full_bundle else None)
            event_offset = 0
            def heartbeat(pid):
                nonlocal event_offset
                self.s.update(run_id,pid=pid,heartbeat=time.time())
                stream = logs/'stdout.jsonl'
                if stream.exists():
                    with open(stream,'rb') as reader:
                        reader.seek(event_offset)
                        for _ in range(100):
                            line = reader.readline(1024*1024)
                            if not line:
                                break
                            if len(line) >= 1024*1024:
                                raise RuntimeError('Executor event exceeds 1 MiB')
                            if not line.endswith(b'\n'):
                                break
                            event_offset = reader.tell()
                            try:
                                event = json.loads(line)
                            except ValueError as e:
                                raise RuntimeError('Malformed executor event') from e
                            self.s.event(run_id,event)
            code = execute(argv,str(workspace),prompt,logs/'stdout.jsonl',logs/'stderr.log',budget-(time.monotonic()-started),
                           lambda:self.s.get(run_id)['cancel_requested'],
                           heartbeat,env)
            while event_offset < (logs/'stdout.jsonl').stat().st_size:
                if self.s.get(run_id)['cancel_requested']:
                    raise Stopped('cancelled')
                if time.monotonic()-started > budget:
                    raise Stopped('timeout')
                previous_offset = event_offset
                heartbeat(None)
                if event_offset == previous_offset:
                    raise RuntimeError('Truncated executor event')
            if code != 0:
                raise ValueError(f'Executor exited {code}; see {logs}/stderr.log')
            try:
                events, session = adapter.parse_events(logs/'stdout.jsonl')
            except (ValueError, TypeError, AttributeError) as e:
                raise RuntimeError(f'Invalid executor event stream: {e}') from e
            # Events were persisted incrementally by heartbeat; parsing validates final outcome.
            candidate, files = snapshot(workspace,r['base_commit'],r['spec']['allowed_paths'])
            self.s.update(run_id,state='checking',candidate_commit=candidate,files=files,session_id=session)
            reports = []
            check_env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','LANG','LC_ALL'}}
            check_env['CODEX_DPH_PROCESS_TOKEN'] = run_id
            for name, check_argv in r['check_config'].items():
                out, err = logs/f'check-{len(reports)}.out', logs/f'check-{len(reports)}.err'
                remaining = budget-(time.monotonic()-started)
                if remaining <= 0:
                    raise Stopped('timeout')
                code = execute(check_argv,str(workspace),'',out,err,remaining,
                               lambda:self.s.get(run_id)['cancel_requested'],
                               lambda pid:self.s.update(run_id,pid=pid,heartbeat=time.time()),check_env)
                reports.append(dict(check_id=name,argv=check_argv,exit_code=code,stdout=str(out),stderr=str(err)))
                self.s.update(run_id,checks=reports)
            if not clean(workspace) or git(workspace,'rev-parse','HEAD') != candidate:
                raise ValueError('Check changed source snapshot; evidence invalidated')
            if self.s.get(run_id)['cancel_requested']:
                raise Stopped('cancelled')
            if any(x['exit_code'] for x in reports):
                raise ValueError('Required check failed; inspect checks and submit a new task after reconciliation')
            finish(state='awaiting_review',pid=None)
        except Stopped as e:
            finish(state='cancelled' if str(e)=='cancelled' else 'failed',error=str(e),pid=None)
        except Exception as e:
            if 'Cannot confirm' in str(e):
                finish(state='interrupted',error=str(e),unsafe_lock=True)
                return True
            finish(state='blocked' if isinstance(e,ValueError) and not isinstance(e,json.JSONDecodeError) and 'final event' not in str(e) else 'failed',error=str(e),pid=None)
        return True

    def run(self, once=False):
        with open(self.s.home/'worker.lock','a+') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('A worker is already running')
            self.recover()
            while True:
                worked = self.once()
                if once:
                    return
                if not worked:
                    time.sleep(.3)
