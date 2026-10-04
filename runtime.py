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

def ready(*, require_worker=True):
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
    if spent >= cfg.get('codex_sessions_per_5h', 45):
        return False, 'Codex session budget reached; judge paused too'
    return True, ''

def room(kind='fix'):
    ok, why = ready()
    if not ok:
        return ok, why
    with db() as c:
        n = c.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]
    limit = config().get('max_pending', 8)
    # Reserve one admission slot for feedback; priority alone cannot help a
    # maintainer response if a queue full of new issues prevents admission.
    if kind == 'fix':
        limit = max(1, limit - config().get('response_slots', 1))
    return (n < limit, 'queue full' if n >= limit else '')

def publication_holds():
    """Temporarily stop new fixes where GitHub explicitly denied PR creation."""
    with db() as c:
        rows=c.execute("""SELECT repo, MAX(updated) AS denied_at FROM tasks
            WHERE kind='fix' AND status='error' AND updated>?
            AND result LIKE '%correct permissions to execute%CreatePullRequest%'
            GROUP BY repo""",(time.time()-86400,)).fetchall()
    return {row['repo']:row['denied_at']+86400 for row in rows}

def enqueue(kind, repo, number, note='', *, only_new=False):
    ok, _ = ready()
    if not ok:
        return False
    if kind=='fix' and publication_holds().get(repo,0)>time.time():
        return False
    # Fixes are unique forever; responses are unique per feedback event set.
    digest = hashlib.sha256(note.encode()).hexdigest()[:20] if kind == 'respond' else ''
    identity = f'{kind}:{repo}:{int(number)}:{digest}'
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT status FROM tasks WHERE identity=?', (identity,)).fetchone()
        if existing:
            if only_new:return False
            return existing[0] in ('queued', 'running', 'done', 'skipped', 'blocked')
        n = c.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]
        limit = config().get('max_pending', 8)
        if kind == 'fix':limit = max(1, limit-config().get('response_slots', 1))
        if n >= limit:
            return False
        now = time.time()
        c.execute('INSERT INTO tasks(identity,kind,repo,number,note,status,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                  (identity, kind, repo, int(number), note, 'queued', now, now))
    return True

def reserve_call(kind):
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
    return {'config': config(), 'ready': ready(), 'health': getmeta('health'),
            'pause': getmeta('pause'), 'tasks': counts, 'recent': recent, 'calls_last_hour': calls,
            'worker_heartbeat': getmeta('worker_heartbeat'), 'detector_heartbeat': getmeta('detector_heartbeat')}

if __name__ == '__main__':
    print(json.dumps(status(), indent=2))
