#!/usr/bin/env python3
"""Persist portable runtime state on a dedicated branch, with one writer lock."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import cloud_state
import runtime as rt

def command(args, cwd):
    p=subprocess.run(args,cwd=cwd,capture_output=True,text=True,timeout=90)
    if p.returncode:
        # Do not log credential-helper output or signed network redirect URLs.
        raise RuntimeError('Cloud state command failed: '+args[0]+' (exit '+str(p.returncode)+')')
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
        for directory in ('state','queue'):
            (store/directory).mkdir(exist_ok=True)
            for path in (rt.ROOT/directory).glob('*.json'):
                if path.name=='runtime.json':continue
                try:
                    raw=path.read_text();json.loads(raw)
                except (OSError,ValueError):continue  # another process is writing
                (store/directory/path.name).write_text(raw)
        command(['git','add','checkpoint.json','state','queue'],store)
        if command(['git','diff','--cached','--name-only'],store):
            command(['git','commit','-m','Checkpoint Codex runtime'],store)
        # Retry an ambiguous push by sending the SAME commit, never by
        # rewriting history or rerunning a task. Fast-forward only.
        command(['git','push','origin','HEAD:refs/heads/codex-state'],store)

def restore():
    store=Path(os.environ['OSS_CLOUD_STATE_DIR'])
    cloud_state.import_state(store/'checkpoint.json')
    for directory in ('state','queue'):
        (rt.ROOT/directory).mkdir(exist_ok=True)
        for path in (store/directory).glob('*.json'):
            if path.name!='runtime.json':shutil.copy2(path,rt.ROOT/directory/path.name)

if __name__=='__main__':
    import sys
    if sys.argv[1]=='save':save()
    elif sys.argv[1]=='restore':restore()
    else:raise SystemExit('Expected save or restore')
