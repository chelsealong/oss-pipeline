#!/usr/bin/env python3
"""Local Codex queue and shared circuit breaker. No model calls on import."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'state/runtime.json'
DATA = ROOT / '.runtime'

def config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {'enabled': False, 'backend': 'codex-local'}

def local():
    # Historical name: both managed backends use this queue, never Claude.
    return config().get('backend') in ('codex-local', 'codex-cloud')

@contextlib.contextmanager
def db():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / 'queue.sqlite3', timeout=20)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY, identity TEXT UNIQUE, kind TEXT, repo TEXT,
        number INTEGER, note TEXT, status TEXT, created REAL, updated REAL,
        result TEXT DEFAULT '', attempts INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS calls (at REAL, kind TEXT);
    ''')
    try:
        yield c
        c.commit()
    finally:
        c.close()

def getmeta(key, default=None):
    with db() as c:
        row = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default

def setmeta(key, value):
    with db() as c:
        c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

def checkpoint():
    if config().get('backend')=='codex-cloud':
        from cloud_store import save
        try:save()
        except Exception as error:
            raise PersistenceError('Cloud checkpoint unavailable; keep task state for recovery') from error

def ready(*, require_worker=True, check_budget=True):
    cfg = config()
    if not cfg.get('enabled') or not local():
        return False, 'runtime disabled'
    pause = getmeta('pause', {})
    if pause.get('until', 0) > time.time():
        return False, pause.get('reason', 'circuit open')
    health = getmeta('health', {})
    if time.time() - health.get('at', 0) > 3600 or not health.get('ok'):
        return False, 'Codex health check required'
    if require_worker and time.time() - getmeta('worker_heartbeat', 0) > 120:
        return False, 'worker heartbeat stale'
    with db() as c:
        spent = c.execute("SELECT count(*) FROM calls WHERE kind='codex' AND at>?", (time.time()-18000,)).fetchone()[0]
    if check_budget and spent >= cfg.get('codex_sessions_per_5h', 45):
        return False, 'Codex session budget reached; judge paused too'
    return True, ''

def _meta(c, key, default=None):
    row=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default

def _putmeta(c, key, value):
    c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value)))

def task_state(task_id, **changes):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        state=_meta(c,f'task:{task_id}',{})
        if changes:
            state.update(changes)
            _putmeta(c,f'task:{task_id}',state)
    return state

def repo_key(repo):
    import scan
    return next((k for k,v in scan.REPOS.items()
                 if repo in (k,v.get('implements_in') or v['upstream'])),repo)

def _room(c, kind, repo=None):
    rows=c.execute("SELECT kind,repo FROM tasks WHERE status IN ('queued','running','retry_wait')").fetchall()
    cfg=config();limit=cfg.get('max_pending',8)
    reserve=cfg.get('response_slots',1) if kind=='fix' else cfg.get('fix_slots',1)
    if len(rows)>=limit or sum(r['kind']==kind for r in rows)>=max(1,limit-reserve):
        return False,'queue full'
    if repo and sum(repo_key(r['repo'])==repo_key(repo) for r in rows)>=cfg.get('max_pending_per_repo',limit):
        return False,'repository pending share reached'
    return True,''

def room(kind='fix', repo=None):
    ok, why = ready()
    if not ok:
        return ok, why
    with db() as c:
        return _room(c,kind,repo)

def _dispatch_budget(c):
    today=time.strftime('%Y-%m-%d',time.gmtime())
    budget=_meta(c,'dispatch_budget')
    if budget is None:
        # Preserve spending made by the old admission-time accounting at cutover.
        try:budget=json.loads((ROOT/'state/dispatch-budget.json').read_text())
        except (OSError,ValueError):budget={}
    if budget.get('date')!=today:budget={'date':today,'used':{}}
    return budget

def dispatch_headroom(key):
    import watch,scan
    with db() as c:
        budget=_dispatch_budget(c)
        if budget['used'].get(key,0)>=watch.DISPATCH_BUDGET.get(key,watch.DEFAULT_BUDGET):
            return False,'daily viable-task budget reached'
        starts=_meta(c,'fix_sessions',[])
    if sum(s['repo']==key and s['at']>time.time()-18000 for s in starts)>=scan.session_share(key):
        return False,'repository five-hour session share reached'
    return True,''

def publication_holds():
    """A creation denial needs positive clearance, not merely elapsed time."""
    with db() as c:
        rows=c.execute("""SELECT repo, MAX(updated) AS denied_at FROM tasks
            WHERE kind='fix' AND status='error'
            AND result LIKE '%correct permissions to execute%CreatePullRequest%'
            GROUP BY repo""").fetchall()
        return {row['repo']:max(time.time()+86400,row['denied_at']+86400)
            for row in rows if _meta(c,'publication_clearance:'+row['repo'],{}).get('denied_at')!=row['denied_at']}

def enqueue(kind, repo, number, note='', *, only_new=False):
    ok, _ = ready()
    if not ok:
        return False
    if kind=='fix' and publication_holds().get(repo,0)>time.time():
        return False
    if kind=='fix' and not dispatch_headroom(repo)[0]:return False
    # One durable task per issue. Safe, expired skips can reuse that task;
    # publication ambiguity and human gates must never be replayed here.
    digest = hashlib.sha256(note.encode()).hexdigest()[:20] if kind == 'respond' else ''
    identity = f'{kind}:{repo}:{int(number)}:{digest}'
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT * FROM tasks WHERE identity=?', (identity,)).fetchone()
        if existing:
            state=_meta(c,f"task:{existing['id']}",{})
            if (kind=='fix' and existing['status']=='skipped'
                    and existing['updated']<time.time()-86400*3
                    and existing['attempts']<3 and not state.get('publication_started')
                    and _room(c,kind,repo)[0]):
                c.execute("UPDATE tasks SET status='queued',updated=? WHERE id=?",(time.time(),existing['id']))
                state.update(phase='queued',terminal=False,retry_after=0)
                _putmeta(c,f"task:{existing['id']}",state)
                return True
            if only_new:return False
            return existing['status'] in ('queued', 'running', 'done', 'skipped', 'blocked')
        if not _room(c,kind,repo)[0]:
            return False
        now = time.time()
        c.execute('INSERT INTO tasks(identity,kind,repo,number,note,status,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                  (identity, kind, repo, int(number), note, 'queued', now, now))
    return True

def reserve_call(kind, *, task=None, phase=None):
    ok, why = ready()
    if not ok:
        return False, why
    cfg = config()
    seconds, limit = (3600, cfg.get('judge_requests_per_hour', 120)) if kind == 'judge' else (18000, cfg.get('codex_sessions_per_5h', 45))
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        n = c.execute('SELECT count(*) FROM calls WHERE kind=? AND at>?', (kind, time.time()-seconds)).fetchone()[0]
        if n >= limit:
            return False, f'{kind} call budget reached ({limit})'
        if task and task['kind']=='fix' and phase=='generation':
            import watch,scan
            key=task['repo'];state=_meta(c,f"task:{task['id']}",{})
            budget=_dispatch_budget(c)
            starts=[s for s in _meta(c,'fix_sessions',[]) if s['at']>time.time()-18000]
            if not state.get('generation_started'):
                if sum(s['repo']==key for s in starts)>=scan.session_share(key):
                    return False,'repository five-hour session share reached'
                if state.get('dispatch_paid_date')!=budget['date']:
                    cap=watch.DISPATCH_BUDGET.get(key,watch.DEFAULT_BUDGET)
                    if budget['used'].get(key,0)>=cap:return False,'daily viable-task budget reached'
                    budget['used'][key]=budget['used'].get(key,0)+1
                starts.append({'repo':key,'task':task['id'],'at':time.time()})
                state.update(generation_started=time.time(),dispatch_paid_date=budget['date'])
                _putmeta(c,f"task:{task['id']}",state)
                _putmeta(c,'fix_sessions',starts)
                _putmeta(c,'dispatch_budget',budget)
        c.execute('INSERT INTO calls VALUES (?,?)', (time.time(), kind))
        c.execute('DELETE FROM calls WHERE at<?', (time.time()-86400*7,))
    checkpoint()  # Persist spending BEFORE a cloud model request.
    return True, ''

class Paused(RuntimeError):
    pass

class PersistenceError(RuntimeError):
    pass

def require_judge():
    ok, why = reserve_call('judge')
    if not ok:
        # Propagate, never turn an infrastructure pause into a final verdict.
        raise Paused(why)

def pause(reason, seconds=1800):
    setmeta('pause', {'until': time.time()+seconds, 'reason': reason})

@contextlib.contextmanager
def lock(name):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / (name+'.lock')).open('w') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f'{name} already running')
        yield

def status():
    with db() as c:
        counts = dict(c.execute('SELECT status,count(*) FROM tasks GROUP BY status').fetchall())
        recent = [dict(r) for r in c.execute('SELECT id,kind,repo,number,status,result,updated FROM tasks ORDER BY id DESC LIMIT 8')]
        calls = dict(c.execute('SELECT kind,count(*) FROM calls WHERE at>? GROUP BY kind', (time.time()-3600,)).fetchall())
        budget=_dispatch_budget(c)
        active=[dict(r) for r in c.execute("SELECT id,repo,number,kind,status FROM tasks WHERE status IN ('running','queued','retry_wait','publication_wait','capacity_wait','human_wait','validation_wait')")]
        for task in active:task['execution']=_meta(c,f"task:{task['id']}",{})
        throughput={kind:dict(c.execute('SELECT status,count(*) FROM tasks WHERE kind=? AND updated>? GROUP BY status',
                    (kind,time.time()-86400)).fetchall()) for kind in ('fix','respond')}
        followups=[{'key':r['key'],**json.loads(r['value'])} for r in c.execute(
            "SELECT key,value FROM meta WHERE key LIKE 'coordination:%' OR key LIKE 'followup:%'")]
        publication_outcomes={'created':0,'updated':0,'reconciled':0}
        for row in c.execute("SELECT key,value FROM meta WHERE key LIKE 'task:%'"):
            state=json.loads(row['value'])
            if state.get('published_at',0)>time.time()-86400:
                publication_outcomes[state.get('publication_outcome','reconciled')]+=1
    followups.extend({'key':'publication:'+repo,'status':'needs_human','repo':repo,'requires_clearance':True,
        'reason':'Upstream rejected ordinary PR creation; inspect permissions/concurrent PR cap. Time passing or draft creation does not clear the hold; verify a newer ordinary creation.'}
        for repo,until in publication_holds().items())
    return {'config': config(), 'ready': ready(), 'health': getmeta('health'),
            'pause': getmeta('pause'), 'tasks': counts, 'recent': recent, 'calls_last_hour': calls,
            'active':active,'dispatch_budget':budget,'throughput_24h':throughput,'human_followups':followups,
            'publication_outcomes_24h':publication_outcomes,
            'runner_failure':getmeta('runner_failure'),
            'worker_heartbeat': getmeta('worker_heartbeat'), 'detector_heartbeat': getmeta('detector_heartbeat')}

if __name__ == '__main__':
    print(json.dumps(status(), indent=2))
