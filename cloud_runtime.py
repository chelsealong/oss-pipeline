#!/usr/bin/env python3
"""One cloud state/auth owner, bounded workers, durable evidence and handover."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import cloud_store
import runtime as rt
import work_evidence
import task_recovery
import pr_followup

REPOSITORY='chelsealong/oss-pipeline'
# Audited tasks whose previous attempt stopped before publication. Recreate
# the patch in a fresh checkout; never replay an interrupted/pushed task.
VALIDATION_RETRY = (
    (316,'blocked','ERR_PNPM_STORE_DIR_OPEN_OPERATION_LOCK'),
    (244,'blocked','/var/tmp/hermes-pytest'),
    (245,'blocked','/var/tmp/hermes-pytest'),
    (266,'blocked','/var/tmp/hermes-pytest'),
    # These fetches timed out before any generation, review, or push. For
    # openclaw#164200, only retry its latest task (267), not 219 and 222.
    (267,'error',"'git', 'fetch', 'origin'"),
    (268,'error',"'git', 'fetch', 'origin'"),
    (271,'error',"'git', 'fetch', 'origin'"),
    (273,'error',"'git', 'fetch', 'origin'"),
    (274,'error',"'git', 'fetch', 'origin'"),
    (275,'error',"'git', 'fetch', 'origin'"),
    (295,'blocked','Rust cache is read-only'),
    (256,'blocked','aiohttp is missing'),
    (206,'blocked','/var/tmp/hermes-pytest'),
    (227,'blocked','/var/tmp/hermes-pytest'),
    (241,'blocked','/var/tmp/hermes-pytest'),
    (300,'blocked','/var/tmp/hermes-pytest'),
)
REPAIR_RETRY = (
    (384,'blocked','check:changed rejects TypeScript outside the checkout'),
    (325,'blocked','requires compiler files inside the checkout'),
    (359,'blocked','requires dependencies physically inside the checkout'),
    (316,'blocked','rustup tried to write to a read-only home'),
    (212,'blocked','documentation correction'),
    (197,'blocked','review: The regression test is timing-dependent'),
    (235,'blocked','review: The fix addresses a reproduced guard refusal'),
    (266,'blocked','review: The lock wait can exceed the tool deadline'),
    (301,'blocked','review: The patch fixes the reported leading cases'),
    (349,'blocked','review: Recovery is wired only in createWindow()'),
)
VALIDATION_RETRY=REPAIR_RETRY+VALIDATION_RETRY
RETRY_MARKER='cloud_validation_retry_20261005'

def gh(args, **kwargs):
    # A failed variable read is safe to repeat; secret updates and workflow
    # dispatches are mutations and must never be replayed blindly.
    readonly=args[:2]==['api',f'repos/{REPOSITORY}/actions/variables/CODEX_CLOUD_ENABLED']
    for attempt in range(3 if readonly else 1):
        try:
            p=subprocess.run(['gh',*args],capture_output=True,text=True,timeout=90,**kwargs)
        except subprocess.TimeoutExpired:
            if not readonly or attempt==2:raise
        else:
            if not p.returncode:return p.stdout.strip()
            if not readonly or not re.search(r'TLS|timeout|connection|HTTP 50[234]|unexpected EOF',p.stderr,re.I) or attempt==2:
                raise RuntimeError('GitHub control operation failed (exit '+str(p.returncode)+')')
        time.sleep(2*(attempt+1))

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
        'enabled':True,'cloud_environment':True,'model':'gpt-6-sol','max_pending':8,
        'response_slots':2,'fix_slots':1,'max_pending_per_repo':3,'workers':2,
        'public_pr_replies':True,'maintain_existing_prs':True,
        'codex_socket':str(rt.DATA/'codex-host.sock'),
        'codex_sessions_per_5h':45,'judge_requests_per_hour':120})+'\n')
    rt.DATA.mkdir(exist_ok=True)

def migrate_accounting():
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if rt._meta(db,'accounting_v2'):return
        budget=rt._dispatch_budget(db)
        rt._putmeta(db,'dispatch_budget',budget)
        for task in db.execute("SELECT id,created FROM tasks WHERE kind='fix' AND status IN ('queued','running')"):
            day=time.strftime('%Y-%m-%d',time.gmtime(task['created']))
            state=rt._meta(db,f"task:{task['id']}",{})
            state['dispatch_paid_date']=day
            rt._putmeta(db,f"task:{task['id']}",state)
        rt._putmeta(db,'accounting_v2',time.time())

def requeue_validation_repair():
    """Retry only audited, unpublished infrastructure blocks, once per task."""
    if rt.config().get('backend')!='codex-cloud':return []
    held=rt.publication_holds()
    chosen=[]
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT value FROM meta WHERE key=?',(RETRY_MARKER,)).fetchone()
        attempted=set(json.loads(row['value'])) if row else set()
        pending=db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running','retry_wait')").fetchone()[0]
        slots=max(0,rt.config().get('max_pending',8)-rt.config().get('response_slots',1)-pending)
        for task_id,status,evidence in VALIDATION_RETRY:
            if not slots:break
            if task_id in attempted:continue
            task=db.execute('SELECT kind,repo,status,result FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or task['status']!=status or evidence not in (task['result'] or ''):
                continue
            if not rt._room(db,task['kind'],task['repo'])[0]:continue
            if task['kind']=='fix' and held.get(task['repo'],0)>time.time():
                continue
            db.execute("UPDATE tasks SET status='queued',updated=? WHERE id=?",(time.time(),task_id))
            attempted.add(task_id);chosen.append(task_id);slots-=1
        if chosen:
            db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                       (RETRY_MARKER,json.dumps(sorted(attempted))))
    if chosen:rt.checkpoint()
    return chosen

def stop(process, timeout=30):
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()

def successor(failed):
    """Only after every auth owner stopped and state/token persistence succeeded."""
    if not cloud_enabled():return False
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        now=time.time()
        events=[at for at in rt._meta(db,'runner_restarts',[]) if at>now-3600]
        if failed:
            if len(events)>=3:
                rt._putmeta(db,'runner_restart_after',events[0]+3600)
                rt._putmeta(db,'followup:runner',{'status':'needs_human',
                    'reason':'Three service failures in one hour; automatic restart is cooling down.'})
                return False
            events.append(now)
        rt._putmeta(db,'runner_restarts',events)
    return True

def run(mode,seconds):
    configure(mode)
    previous=hashlib.sha256(auth_file().read_bytes()).hexdigest()
    children=[];logs=[];restored=False;failed=False;canary_budget=False;started=time.time()
    def start(name,args):
        log=(rt.DATA/(name+'.log')).open('a');logs.append(log)
        child=subprocess.Popen([sys.executable,'-u',*args],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        children.append(child);return child
    try:
        if mode=='canary':
            cloud_store.seed_canary_budget();canary_budget=True
        if mode=='live':
            if not cloud_enabled():raise RuntimeError('Cloud production switch is off')
            work_evidence.key()  # Never run production without encrypted recovery.
            cloud_store.restore()
            restored=True
            migrate_accounting()
            requeue_validation_repair()
            if rt.getmeta('runner_restart_after',0)>time.time():
                print('Cloud restart circuit is cooling down; no work admitted.',flush=True)
                return
            task_recovery.reconcile()
            pr_followup.recover_backlog()
        import codex_worker
        host=start('codex-host',['codex_host.py','--binary',codex_worker.binary()])
        for _ in range(120):
            if Path(rt.config()['codex_socket']).exists():break
            if host.poll() is not None:raise RuntimeError('Codex host startup failed')
            time.sleep(.5)
        else:raise RuntimeError('Codex host startup timed out')
        if mode!='live':
            # No production state or upstream tasks are imported into canary.
            import codex_worker
            codex_worker.healthcheck()
            if mode=='canary':
                rt.setmeta('worker_heartbeat',time.time())
                for slot in range(2):
                    if not rt.enqueue('canary',f'runtime-{slot}',slot+1):raise RuntimeError('Canary enqueue failed')
                workers=[start(f'canary-{slot}',['codex_worker.py','--managed','--slot',str(slot),'--once']) for slot in range(2)]
                for worker in workers:
                    if worker.wait(timeout=660):raise RuntimeError('Canary worker failed')
                with rt.db() as db:
                    rows=db.execute('SELECT status FROM tasks WHERE kind="canary"').fetchall()
                if len(rows)!=2 or any(r[0]!='done' for r in rows):raise RuntimeError('Cloud canary failed; inspect encrypted evidence')
            print('Cloud '+mode+' passed; no upstream publication.',flush=True)
            return
        workers=[start(f'worker-{slot}',['codex_worker.py','--managed','--slot',str(slot)]) for slot in range(rt.config()['workers'])]
        detectors=[start('watch',['local_service.py','watch']),start('prwatch',['local_service.py','prwatch'])]
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline and cloud_enabled():
            if any(p.poll() is not None for p in children):raise RuntimeError('A cloud service exited unexpectedly')
            requeue_validation_repair()
            task_recovery.reconcile()
            pr_followup.recover_backlog()
            cloud_store.save()
            previous=rotate_auth(previous)
            s=rt.status()
            rt.setmeta('throughput_24h',{'at':time.time(),'tasks':s['throughput_24h']})
            print(json.dumps({'at':time.time(),'ready':s['ready'],'tasks':s['tasks'],'calls':s['calls_last_hour'],
                'active':s['active'],'dispatch_budget':s['dispatch_budget'],
                'throughput_24h':s['throughput_24h'],
                'publication_outcomes_24h':s['publication_outcomes_24h']}),flush=True)
            time.sleep(60)
        # Stop admitting work, allow the active task to complete, then hand over.
        (rt.DATA/'drain').touch()
        for child in detectors:stop(child)
        # Generation + review + one correction + re-review may take 100
        # minutes, before checkout/network overhead. Leave that work time to
        # finish inside GitHub's six-hour job limit.
        drain_deadline=time.monotonic()+9000
        while any(w.poll() is None for w in workers) and time.monotonic()<drain_deadline:
            cloud_store.save();previous=rotate_auth(previous);time.sleep(30)
        if any(w.poll() is None for w in workers):raise RuntimeError('Handover deadline reached; inspect interrupted task')
        if any(w.returncode for w in workers):raise RuntimeError('Worker failed during handover')
        cloud_store.save()
    except BaseException as error:
        failed=True
        if mode=='live' and restored:
            rt.setmeta('runner_failure',{'at':time.time(),'type':type(error).__name__,
                'reason':str(error)[:350]})
        raise
    finally:
        for child in reversed(children):stop(child)
        for log in logs:log.close()
        try:
            if mode=='live' and restored:
                # Record task status and retained budgets even on a service failure.
                cloud_store.save()
        finally:
            # A checkpoint failure must not lose a refreshed subscription token.
            try:rotate_auth(previous)
            finally:
                if canary_budget:cloud_store.account_canary(started)
        if mode=='live' and restored and rt.getmeta('runner_restart_after',0)<=time.time():
            if successor(failed):
                cloud_store.save()  # Restart spending must survive before dispatch.
                with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('continue=true\n')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['install-auth','probe','canary','live'])
    ap.add_argument('--seconds',type=int,default=10800);args=ap.parse_args()
    if args.mode=='install-auth':install_auth()
    else:
        if not 60<=args.seconds<=10800:raise SystemExit('seconds must be between 60 and 10800')
        signal.signal(signal.SIGTERM,lambda s,f:sys.exit(1))
        run(args.mode,args.seconds)
