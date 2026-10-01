#!/usr/bin/env python3
"""GitHub-hosted supervisor; serial execution, durable state, bounded handover."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import cloud_store
import runtime as rt

REPOSITORY='chelsealong/oss-pipeline'

def gh(args, **kwargs):
    p=subprocess.run(['gh',*args],capture_output=True,text=True,timeout=90,**kwargs)
    if p.returncode:raise RuntimeError('GitHub control operation failed (exit '+str(p.returncode)+')')
    return p.stdout.strip()

def auth_file():
    return Path(os.environ['CODEX_HOME'])/'auth.json'

def validate_auth(raw):
    value=json.loads(raw)
    if value.get('auth_mode')!='chatgpt' or value.get('OPENAI_API_KEY'):
        raise RuntimeError('Cloud runtime requires ChatGPT subscription authentication')
    if not (value.get('tokens') or {}).get('refresh_token'):
        raise RuntimeError('Cloud login is incomplete')
    return value

def install_auth():
    raw=os.environ.pop('CODEX_AUTH_JSON','')
    validate_auth(raw)
    path=auth_file();path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(raw)
    (path.parent/'config.toml').write_text('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')

def rotate_auth(previous):
    raw=auth_file().read_text();validate_auth(raw)
    digest=hashlib.sha256(raw.encode()).hexdigest()
    if digest!=previous:
        gh(['secret','set','CODEX_AUTH_JSON','--repo',REPOSITORY],input=raw)
    return digest

def cloud_enabled():
    return gh(['api',f'repos/{REPOSITORY}/actions/variables/CODEX_CLOUD_ENABLED','--jq','.value'])=='true'

def configure(mode):
    rt.CONFIG.parent.mkdir(exist_ok=True)
    rt.CONFIG.write_text(json.dumps({'backend':'codex-cloud' if mode=='live' else 'codex-local',
        'enabled':True,'model':'gpt-6-sol','max_pending':8,'response_slots':1,
        'codex_sessions_per_5h':45,'judge_requests_per_hour':120})+'\n')
    rt.DATA.mkdir(exist_ok=True)

def stop(process, timeout=30):
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()

def run(mode,seconds):
    configure(mode)
    previous=hashlib.sha256(auth_file().read_bytes()).hexdigest()
    children=[];logs=[];restored=False
    def start(name,args):
        log=(rt.DATA/(name+'.log')).open('a');logs.append(log)
        child=subprocess.Popen([sys.executable,'-u',*args],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        children.append(child);return child
    try:
        if mode=='live':
            if not cloud_enabled():raise RuntimeError('Cloud production switch is off')
            cloud_store.restore()
            restored=True
        else:
            # No production state or upstream tasks are imported into canary.
            import codex_worker
            codex_worker.healthcheck()
            if mode=='canary':
                rt.setmeta('worker_heartbeat',time.time())
                if not rt.enqueue('canary','runtime',1):raise RuntimeError('Canary enqueue failed')
                subprocess.run([sys.executable,'codex_worker.py','--once'],check=True,timeout=600)
                with rt.db() as db:
                    row=db.execute('SELECT status FROM tasks WHERE kind="canary"').fetchone()
                if not row or row[0]!='done':raise RuntimeError('Cloud canary failed')
            print('Cloud '+mode+' passed; no upstream publication.',flush=True)
            return
        worker=start('worker',['codex_worker.py'])
        detectors=[start('watch',['local_service.py','watch']),start('prwatch',['local_service.py','prwatch'])]
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline and cloud_enabled():
            if any(p.poll() is not None for p in children):raise RuntimeError('A cloud service exited unexpectedly')
            cloud_store.save()
            previous=rotate_auth(previous)
            s=rt.status()
            print(json.dumps({'at':time.time(),'ready':s['ready'],'tasks':s['tasks'],'calls':s['calls_last_hour']}),flush=True)
            time.sleep(60)
        # Stop admitting work, allow the active task to complete, then hand over.
        (rt.DATA/'drain').touch()
        for child in detectors:stop(child)
        drain_deadline=time.monotonic()+5400
        while worker.poll() is None and time.monotonic()<drain_deadline:
            cloud_store.save();previous=rotate_auth(previous);time.sleep(30)
        if worker.poll() is None:raise RuntimeError('Handover deadline reached; inspect interrupted task')
        if worker.returncode:raise RuntimeError('Worker failed during handover')
        cloud_store.save()
        if cloud_enabled():
            with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('continue=true\n')
    finally:
        for child in reversed(children):stop(child)
        for log in logs:log.close()
        try:
            if mode=='live' and restored:
                # Record task status and retained budgets even on a service failure.
                cloud_store.save()
        finally:
            # A checkpoint failure must not lose a refreshed subscription token.
            rotate_auth(previous)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['install-auth','probe','canary','live'])
    ap.add_argument('--seconds',type=int,default=14400);args=ap.parse_args()
    if args.mode=='install-auth':install_auth()
    else:
        if not 60<=args.seconds<=14400:raise SystemExit('seconds must be between 60 and 14400')
        signal.signal(signal.SIGTERM,lambda s,f:sys.exit(1))
        run(args.mode,args.seconds)
