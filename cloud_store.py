#!/usr/bin/env python3
"""Persist portable runtime state on a dedicated branch, with one writer lock."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import cloud_state
import runtime as rt
import work_evidence

def command(args, cwd):
    p=subprocess.run(args,cwd=cwd,capture_output=True,text=True,timeout=90)
    if p.returncode:
        # Do not log credential-helper output or signed network redirect URLs.
        import re
        category=('non-fast-forward' if re.search(r'non-fast-forward|fetch first|rejected',p.stderr,re.I)
            else 'transport' if re.search(r'TLS|SSL|timeout|connection|HTTP 50[234]',p.stderr,re.I)
            else 'disk' if re.search(r'No space left|disk full',p.stderr,re.I) else 'unclassified')
        raise RuntimeError('Cloud state command failed: '+args[0]+' '+args[1]+' (exit '+str(p.returncode)+', '+category+')')
    return p.stdout.strip()

def save():
    if rt.config().get('backend')!='codex-cloud':return
    store=Path(os.environ['OSS_CLOUD_STATE_DIR'])
    if not (store/'.git').exists():raise RuntimeError('Cloud state checkout missing')
    # Unlike the service singleton, wait for an in-flight checkpoint.
    import fcntl
    rt.DATA.mkdir(parents=True,exist_ok=True)
    with (rt.DATA/'cloud-checkpoint.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        cloud_state.export_state(store/'checkpoint.json')
        work_evidence.save_to(store)
        for directory in ('state','queue'):
            (store/directory).mkdir(exist_ok=True)
            for path in (rt.ROOT/directory).glob('*.json'):
                if path.name=='runtime.json':continue
                try:
                    raw=path.read_text();json.loads(raw)
                except (OSError,ValueError):continue  # another process is writing
                (store/directory/path.name).write_text(raw)
        command(['git','add','checkpoint.json','state','queue','evidence'],store)
        if command(['git','diff','--cached','--name-only'],store):
            command(['git','commit','-m','Checkpoint Codex runtime'],store)
        # Retry an ambiguous push by sending the SAME commit, never by
        # rewriting history or rerunning a task. Fast-forward only.
        for attempt in range(3):
            try:
                command(['git','push','origin','HEAD:refs/heads/codex-state'],store)
                break
            except (RuntimeError,subprocess.TimeoutExpired):
                if attempt==2:raise
                time.sleep(2*(attempt+1))

def restore():
    store=Path(os.environ['OSS_CLOUD_STATE_DIR'])
    cloud_state.import_state(store/'checkpoint.json')
    work_evidence.restore_from(store)
    for directory in ('state','queue'):
        (rt.ROOT/directory).mkdir(exist_ok=True)
        for path in (store/directory).glob('*.json'):
            if path.name!='runtime.json':shutil.copy2(path,rt.ROOT/directory/path.name)

def seed_canary_budget():
    """Canaries share the call ceiling but never import production tasks."""
    store=Path(os.environ['OSS_CLOUD_STATE_DIR'])
    snapshot=json.loads((store/'checkpoint.json').read_text())
    with rt.db() as db:
        if db.execute('SELECT count(*) FROM calls').fetchone()[0]:
            raise RuntimeError('Canary spending ledger is not empty')
        db.executemany('INSERT INTO calls(at,kind) VALUES (?,?)',
            [(row['at'],row['kind']) for row in snapshot['tables']['calls']])

def account_canary(started):
    """Persist only new canary calls into the otherwise unchanged live state."""
    store=Path(os.environ['OSS_CLOUD_STATE_DIR']);path=store/'checkpoint.json'
    snapshot=json.loads(path.read_text())
    marker='canary_accounted:'+os.environ['GITHUB_RUN_ID']+':'+os.environ.get('GITHUB_RUN_ATTEMPT','1')
    if any(row['key']==marker for row in snapshot['tables']['meta']):return
    with rt.db() as db:
        calls=[dict(row) for row in db.execute('SELECT at,kind FROM calls WHERE at>=?',(started,))]
    snapshot['tables']['calls'].extend(calls)
    snapshot['tables']['meta'].append({'key':marker,'value':json.dumps({'at':time.time(),'calls':len(calls)})})
    work_evidence.atomic(path,json.dumps(snapshot,sort_keys=True).encode())
    command(['git','add','checkpoint.json'],store)
    command(['git','commit','-m','Retain bounded Codex canary call spending'],store)
    for attempt in range(3):
        try:
            command(['git','push','origin','HEAD:refs/heads/codex-state'],store);break
        except (RuntimeError,subprocess.TimeoutExpired):
            if attempt==2:raise
            time.sleep(2*(attempt+1))

if __name__=='__main__':
    import sys
    if sys.argv[1]=='save':save()
    elif sys.argv[1]=='restore':restore()
    else:raise SystemExit('Expected save or restore')
