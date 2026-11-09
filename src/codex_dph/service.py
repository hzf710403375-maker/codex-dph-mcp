import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .git import git, clean
from . import context as task_context


class Invalid(ValueError):
    pass


class Conflict(Invalid):
    pass


def uid():
    return uuid.uuid4().hex


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class Service:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        self.db = self.home / 'state.sqlite3'
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, root TEXT UNIQUE, checks TEXT);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, project TEXT, state TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, fingerprint TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, run TEXT, data TEXT);
            ''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def projects(self):
        with self.connect() as c:
            return [dict(project_id=r['id'], root=r['root'], checks=json.loads(r['checks']))
                    for r in c.execute('SELECT * FROM projects')]

    def register(self, root, checks=None):
        root = Path(root).resolve()
        try:
            top = Path(git(root, 'rev-parse', '--show-toplevel')).resolve()
            if top != root:
                raise Invalid('Register the repository root')
            if git(root, 'rev-parse', '--show-superproject-working-tree'):
                raise Invalid('Submodules are unsupported')
            common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
            root = Path(git(common.parent, 'rev-parse', '--show-toplevel')).resolve()
            git(root, 'rev-parse', 'HEAD')
        except (ValueError, OSError) as e:
            raise Invalid(f'Requires a Git repository with an initial commit: {e}') from e
        self.validate_checks(checks or {})
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO projects VALUES (?,?,?)', (uid(), str(root), encoded(checks or {})))
        return next(p for p in self.projects() if p['root'] == str(root))

    @staticmethod
    def validate_checks(checks):
        if not isinstance(checks, dict) or any(not isinstance(k, str) or not k or not isinstance(v, list)
            or not v or any(not isinstance(a, str) or not a for a in v) for k, v in checks.items()):
            raise Invalid('Checks must map names to nonempty argv arrays')

    def configure_checks(self, project_id, checks):
        self.validate_checks(checks)
        with self.connect() as c:
            if not c.execute('UPDATE projects SET checks=? WHERE id=?', (encoded(checks), project_id)).rowcount:
                raise Invalid('Unknown project')

    def project(self, project_id):
        for p in self.projects():
            if p['project_id'] == project_id:
                return p
        raise Invalid('Unknown registered project')

    def list_runs(self, project_id=None, limit=50, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise Invalid('Invalid pagination')
        with self.connect() as c:
            rows = c.execute('SELECT data FROM runs WHERE (? IS NULL OR project=?) ORDER BY rowid DESC LIMIT ? OFFSET ?',
                             (project_id, project_id, limit, offset))
            return [self._status_data(json.loads(r[0])) for r in rows]

    def get(self, run_id):
        with self.connect() as c:
            row = c.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise Invalid('Unknown run')
            return json.loads(row[0])

    def update(self, run_id, **fields):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise Invalid('Unknown run')
            data = json.loads(row[0])
            if data.get('cancel_requested') and fields.get('state') in {'running','checking','awaiting_review'}:
                fields['state'] = 'cancelled' if fields['state'] == 'awaiting_review' else 'cancelling'
            data.update(fields, updated_at=time.time())
            c.execute('UPDATE runs SET state=?, data=? WHERE id=?', (data['state'], encoded(data), run_id))
        return data

    def event(self, run_id, event):
        with self.connect() as c:
            c.execute('INSERT INTO events(run,data) VALUES (?,?)', (run_id, encoded(event)))

    def events(self, run_id, after_sequence=0, limit=100):
        self.get(run_id)
        if not 1 <= limit <= 500 or after_sequence < 0:
            raise Invalid('Invalid event pagination')
        with self.connect() as c:
            result = [dict(sequence=r['seq'], event=json.loads(r['data'])) for r in
                      c.execute('SELECT * FROM events WHERE run=? AND seq>? ORDER BY seq LIMIT ?', (run_id, after_sequence, limit))]
        return dict(events=result, next_sequence=result[-1]['sequence'] if result else after_sequence)

    def _request(self, c, key, payload):
        if not isinstance(key, str) or not key.strip():
            raise Invalid('A nonempty idempotency key is required')
        fingerprint = hashlib.sha256(encoded(payload).encode()).hexdigest()
        row = c.execute('SELECT * FROM requests WHERE key=?', (key,)).fetchone()
        if row:
            if row['fingerprint'] != fingerprint:
                raise Conflict('Idempotency key reused with different input')
            return fingerprint, json.loads(row['result'])
        return fingerprint, None

    def submit(self, spec, key, *, parent=None):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            fingerprint, old = self._request(c, 'submit:'+key, [spec, parent['run_id'] if parent else None])
            if old:
                return self.get(old['run_id'])
            if parent and c.execute("SELECT 1 FROM runs WHERE json_extract(data,'$.parent_run')=?",(parent['run_id'],)).fetchone():
                raise Conflict('A revision already exists for this run')
            p = self.project(spec.get('project_id'))
            if not parent and not clean(p['root']):
                raise Invalid('Main workspace must be clean, including untracked files')
            for field in ('title', 'objective'):
                if not isinstance(spec.get(field), str) or not spec[field].strip():
                    raise Invalid(f'{field} is required')
            for field in ('allowed_paths', 'acceptance_criteria'):
                if not isinstance(spec.get(field), list) or not spec[field] or any(not isinstance(x, str) or not x for x in spec[field]):
                    raise Invalid(f'{field} requires nonempty strings')
            for path in spec['allowed_paths']:
                if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path:
                    raise Invalid('Allowed paths must be repository relative')
            ids = spec.get('check_ids', [])
            if not isinstance(ids, list) or any(not isinstance(k, str) or k not in p['checks'] for k in ids):
                raise Invalid('Unknown check ID')
            timeout = spec.get('timeout_seconds', 1800)
            rounds = spec.get('max_revision_rounds', 3)
            if type(timeout) is not int or not 1 <= timeout <= 7200 or type(rounds) is not int or not 0 <= rounds <= 10:
                raise Invalid('Invalid time or revision limit')
            base = git(p['root'], 'rev-parse', '--verify', '--end-of-options', spec.get('base_commit', 'HEAD')+'^{commit}')
            try:
                if parent and parent.get('context_bundle'):
                    bundle=parent['context_bundle']
                else:
                    if parent:
                        original=parent
                        while original.get('parent_run'):
                            original=self.get(original['parent_run'])
                        bundle=task_context.build(p['root'],original['base_commit'],original['spec'].get('context',{}))
                    else:
                        bundle=task_context.build(p['root'],base,spec.get('context',{}))
                if parent:
                    history=[]
                    ancestor=parent
                    while ancestor:
                        for row in c.execute('SELECT data FROM reviews WHERE run=? ORDER BY rowid',(ancestor['run_id'],)):
                            review=json.loads(row[0])
                            review['checks']=[{k:check[k] for k in ('check_id','exit_code')} for check in ancestor['checks']]
                            history.append(review)
                        previous=ancestor.get('parent_run')
                        ancestor=self.get(previous) if previous else None
                    bundle=task_context.with_history(bundle,list(reversed(history)))
            except task_context.ContextError as e:
                raise Invalid(str(e)) from e
            run_id = uid()
            data = dict(run_id=run_id, task_id=parent['task_id'] if parent else uid(),
                        task_revision=1, attempt=parent['attempt']+1 if parent else 0,
                        project_id=p['project_id'], state='queued', spec=spec, base_commit=base,
                        workspace=parent['workspace'] if parent else str(self.home/'worktrees'/run_id),
                        candidate_commit=None, context_bundle=bundle, context_hash=task_context.digest(bundle), checks=[], check_config={k:p['checks'][k] for k in ids},
                        timeout_seconds=timeout, max_revision_rounds=rounds,
                        created_at=time.time(), updated_at=time.time(), cancel_requested=False,
                        execution_seconds=parent.get('execution_seconds',0) if parent else 0,
                        parent_run=parent['run_id'] if parent else None)
            c.execute('INSERT INTO runs VALUES (?,?,?,?)', (run_id, p['project_id'], 'queued', encoded(data)))
            c.execute('INSERT INTO requests VALUES (?,?,?)', ('submit:'+key, fingerprint, encoded({'run_id':run_id})))
        return data

    def cancel(self, run_id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise Invalid('Unknown run')
            r = json.loads(row[0])
            if r['state'] in {'queued', 'awaiting_review', 'changes_requested'}:
                r['state'] = 'cancelled'
            elif r['state'] in {'preparing', 'running', 'checking', 'cancelling'}:
                r['state'] = 'cancelling'
            else:
                return r
            r['cancel_requested'] = True
            c.execute('UPDATE runs SET state=?,data=? WHERE id=?', (r['state'], encoded(r), run_id))
        return r

    def artifacts(self, run_id):
        r = self.get(run_id)
        return {k:r.get(k) for k in ('run_id','task_revision','spec','base_commit','candidate_commit','files','checks','error','workspace','session_id','context_bundle','context_hash','context_metrics','execution_context_hash','context_archive')}

    def diff(self, run_id, offset=0, limit=20000):
        r = self.get(run_id)
        if not r['candidate_commit'] or not 1 <= limit <= 50000 or offset < 0:
            raise Invalid('Snapshot required and pagination must be bounded')
        content = git(r['workspace'], 'diff', '--no-ext-diff', '--no-textconv', r['base_commit'], r['candidate_commit'])
        return dict(text=content[offset:offset+limit], next_offset=min(len(content),offset+limit), total=len(content))

    def review(self, run_id, candidate_commit, decision, criteria, issues, key):
        payload = [run_id, candidate_commit, decision, criteria, issues]
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            fingerprint, old = self._request(c, 'review:'+key, payload)
            if old:
                return old
            r = self.get(run_id)
            if r.get('cancel_requested'):
                raise Conflict('Cancelled run cannot be reviewed')
            if r['state'] not in {'awaiting_review','blocked'} or not candidate_commit or r['candidate_commit'] != candidate_commit:
                raise Conflict('Review must target the awaiting snapshot')
            if not clean(r['workspace']) or git(r['workspace'], 'rev-parse', 'HEAD') != candidate_commit:
                raise Conflict('Workspace changed after validation')
            if decision not in {'accepted', 'changes_requested'} or len(criteria) != len(r['spec']['acceptance_criteria']):
                raise Invalid('Provide a decision for every acceptance criterion')
            if any(x not in {'pass','fail'} for x in criteria) or any(not isinstance(x,str) or not x.strip() for x in issues):
                raise Invalid('Invalid criteria or issues')
            if decision == 'accepted' and (r['state'] != 'awaiting_review' or issues or any(x != 'pass' for x in criteria) or any(x['exit_code'] != 0 for x in r['checks'])):
                raise Invalid('Blocking issues or failed checks prevent acceptance')
            if decision == 'changes_requested' and not issues:
                raise Invalid('Revision requires specific issues')
            result = dict(review_id=uid(), **dict(zip(['run_id','candidate_commit','decision','criteria','issues'],payload)))
            c.execute('INSERT INTO reviews VALUES (?,?,?)', (result['review_id'],run_id,encoded(result)))
            r['state'] = decision
            c.execute('UPDATE runs SET state=?,data=? WHERE id=?', (decision,encoded(r),run_id))
            c.execute('INSERT INTO requests VALUES (?,?,?)', ('review:'+key,fingerprint,encoded(result)))
        return result

    def revise(self, run_id, review_id, key):
        with self.connect() as c:
            prior = c.execute('SELECT result FROM requests WHERE key=?',('submit:revision:'+key,)).fetchone()
            if prior:
                result = self.get(json.loads(prior[0])['run_id'])
                if result.get('parent_run') != run_id or result['spec'].get('review_id') != review_id:
                    raise Conflict('Revision key reused with different review')
                return result
        r = self.get(run_id)
        with self.connect() as c:
            row = c.execute('SELECT data FROM reviews WHERE id=? AND run=?', (review_id,run_id)).fetchone()
        if not row or r['state'] != 'changes_requested' or r['attempt'] >= r['max_revision_rounds']:
            raise Conflict('Revision not allowed or limit reached')
        if not clean(r['workspace']) or git(r['workspace'],'rev-parse','HEAD') != r['candidate_commit']:
            raise Conflict('Workspace changed after review')
        spec = dict(r['spec'], base_commit=r['candidate_commit'], revision_feedback=json.loads(row[0])['issues'], review_id=review_id)
        return self.submit(spec, 'revision:'+key, parent=r)

    def reconcile(self, run_id):
        import fcntl
        import psutil
        with open(self.home/'worker.lock','a+') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict('Stop the worker before reconciliation')
            r = self.get(run_id)
            if r['state'] != 'interrupted':
                raise Conflict('Only interrupted runs can be reconciled')
            from .process import owned_processes
            try:
                if owned_processes(run_id):
                    raise Conflict('An execution process remains; stop it before reconciliation')
            except PermissionError as e:
                raise Conflict('Cannot confirm process ownership; reconciliation refused') from e
            if r.get('pid') and psutil.pid_exists(r['pid']):
                raise Conflict('Recorded PID still exists; manual identity reconciliation is required')
            return self.update(run_id,state='cancelled',cancel_requested=True,unsafe_lock=False,
                               error='Explicitly reconciled; workspace retained')

    def context(self, run_id, offset=0, limit=20000):
        run=self.get(run_id)
        if type(offset) is not int or type(limit) is not int or offset<0 or not 1<=limit<=50000:
            raise Invalid('Context pagination must use offset>=0 and limit 1..50000')
        if not run.get('context_bundle'):
            raise Invalid('This older task has no frozen context bundle')
        text=task_context.serialize(run['context_bundle'])
        return dict(text=text[offset:offset+limit],next_offset=min(len(text),offset+limit),total=len(text),context_hash=run['context_hash'])

    @staticmethod
    def _status_data(run):
        result=dict(run)
        bundle=result.pop('context_bundle',None)
        if bundle:
            result['context_summary']=dict(source_commit=bundle['source_commit'],
                document_paths=[d['path'] for d in bundle['documents']],
                review_count=len(bundle['review_history']))
        return result

    def status(self, run_id):
        return self._status_data(self.get(run_id))
