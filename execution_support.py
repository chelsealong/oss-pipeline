"""Bounded task execution, immutable usage records and controller-owned checks."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import runtime as rt

class RequiredCheckFailed(RuntimeError):pass


def github_throttle(error):
    return bool(re.search(r'(?:GraphQL|gh failed|HTTP 429).*?(?:rate.limit|too many)|secondary rate limit|API rate limit already exceeded',str(error),re.I|re.S))

def github_retry_after():
    # Read the actual reset time; do not blindly discard an hour of capacity.
    until=time.time()+900
    try:
        result=subprocess.run(['gh','api','rate_limit'],capture_output=True,text=True,check=True,timeout=15)
        resources=json.loads(result.stdout)['resources']
        resets=[resources[k]['reset']+5 for k in ('core','graphql') if resources.get(k,{}).get('remaining',1)==0]
        if resets:until=max(time.time()+60,min(time.time()+3600,max(resets)))
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):pass
    return until


def record_usage(task, output, started):
    if not task:return
    total=None;first=None;last=None;model=None
    path=output.with_suffix('.events.jsonl')
    if path.exists():
        with path.open(errors='replace') as stream:
            for line in stream:
                try:event=json.loads(line)
                except ValueError:continue
                event=event.get('event',event)
                at=event.get('emittedAtMs')
                if at:
                    first=first or at;last=at
                if event.get('method')=='thread/tokenUsage/updated':
                    total=event.get('params',{}).get('tokenUsage',{}).get('total')
                if event.get('method')=='thread/settings/updated':
                    model=event.get('params',{}).get('threadSettings',{}).get('model')
    rt.setmeta(f"usage:{task['id']}:{task.get('attempts',1)}:{output.stem}",
        {'task':task['id'],'repo':task['repo'],'attempt':task.get('attempts',1),'phase':output.stem,
         'at':time.time(),'seconds':round(time.monotonic()-started,3),'tokens':total,
         'first_event_ms':first,'last_event_ms':last,'model':model,
         'complete':output.exists(),'usage_available':total is not None})


def clean_owned_task(task, folder):
    """Cleanup is best effort after durable capture, never an account failure."""
    def onerror(function,path,error):
        if isinstance(error[1],FileNotFoundError):return
        raise error[1]
    for name in ('work','tool-cache'):
        try:
            if (folder/name).is_dir():shutil.rmtree(folder/name,onerror=onerror)
        except OSError as error:
            rt.setmeta(f"cleanup:{task['id']}",{'at':time.time(),'path':str(folder/name),
                'error':type(error).__name__,'reason':str(error)[:250]})


def claim_state(state, task, slot):
    # Do not point the new claim checkpoint at an old attempt. Preserve spending,
    # publication markers and explicit resumption metadata across this reset.
    state.update(started=time.time(),worker_slot=slot,terminal=False,retry_after=0,
                 folder=None,attempt=task['attempts'],phase='claimed',base=None)
    return state


def retained_progress(task):
    import task_recovery
    path=task_recovery.latest_bundle(task['id'])
    if not path:return None
    payload=json.loads(path.read_text())
    state=payload['execution']
    if state.get('publication_started') or not state.get('base'):return None
    if not payload.get('diff') and not payload.get('untracked'):return None
    if any('diff exceeds' in x or ': evidence size limit' in x or 'removed during snapshot' in x
           for x in payload.get('omitted',[])):return None
    return {'bundle':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'base':state['base'],'phase':state.get('last_execution_phase',state.get('phase'))}


def restore_progress(task, work, base):
    """One continuation can reuse a complete patch; no old approval is reused."""
    import codex_worker as worker
    entry=rt.task_state(task['id']).get('resume_progress')
    if not entry:return False
    path=rt.DATA/'evidence'/entry['bundle']
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
        raise RuntimeError('Retained progress changed; refusing regeneration')
    payload=json.loads(path.read_text())
    if payload['execution'].get('publication_started'):
        raise RuntimeError('Retained attempt may have published')
    # An advisory patch may be applied to advanced main only if Git can apply it
    # cleanly. The generator must recheck eligibility/tests and a fresh reviewer
    # must approve; a human-reviewed patch uses the separate exact-base path.
    paths=payload.get('untracked',{})
    for name in paths:
        target=work/name
        if '.git' in Path(name).parts or not target.resolve().is_relative_to(work.resolve()) or target.exists():
            raise RuntimeError('Unsafe or conflicting retained source file')
    diff=base64.b64decode(payload.get('diff',''))
    if diff:
        patch=work.parent/'retained-progress.patch';patch.write_bytes(diff)
        worker.git(work,'apply','--check',str(patch));worker.git(work,'apply',str(patch))
    for name,raw in paths.items():
        mode=payload.get('untracked_modes',{}).get(name,0o644)
        if mode not in (0o644,0o755):raise RuntimeError('Unsafe retained file mode')
        target=work/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(base64.b64decode(raw));target.chmod(mode)
    rt.task_state(task['id'],progress_restored=True,restored_from=entry['bundle'])
    return True


def response_history_complete(work, upstream):
    """A shallow graph can report an older, incorrect common ancestor."""
    import codex_worker as worker
    result=subprocess.run(['git','merge-base','--all','HEAD',upstream],cwd=work,capture_output=True,text=True)
    if result.returncode==1:return False
    if result.returncode:raise RuntimeError('Cannot determine PR merge base')
    shallow=Path(worker.git(work,'rev-parse','--git-path','shallow'))
    if not shallow.is_absolute():shallow=work/shallow
    if not shallow.exists():return True
    # Every path above the candidate bases must be complete. A cutoff on any
    # of those paths can hide a nearer common ancestor, even when another
    # (shorter) merge-parent path already reaches an old shared commit.
    boundary=set(shallow.read_text().splitlines())
    above=set(worker.git(work,'rev-list','HEAD',upstream,'--not',*result.stdout.split()).splitlines())
    return not boundary.intersection(above)


def update_response_base(work, default, original_head):
    """Merge current main locally, without rewriting or publishing PR history."""
    import codex_worker as worker
    upstream='refs/remotes/upstream/'+default
    for depth in (128,512,2048,8192):
        if response_history_complete(work,upstream):break
        worker.git(work,'fetch','--depth='+str(depth),'upstream',f'{default}:{upstream}')
        worker.git(work,'fetch','--depth='+str(depth),'origin',original_head)
    else:
        if not response_history_complete(work,upstream):
            raise RuntimeError('PR history exceeds bounded merge-base fetch; retained for follow-up')
    before=worker.git(work,'rev-parse','HEAD')
    try:
        worker.git(work,'-c','user.name='+worker.ME,'-c','user.email='+worker.EMAIL,
            'merge','--no-edit','--no-ff',upstream,'-m','Merge current upstream '+default+' for PR validation')
    except Exception as error:
        conflicts=subprocess.run(['git','diff','--name-only','--diff-filter=U'],cwd=work,capture_output=True,text=True)
        subprocess.run(['git','merge','--abort'],cwd=work,capture_output=True)
        if conflicts.returncode==0 and conflicts.stdout.strip():
            raise RuntimeError('Current upstream conflicts with PR branch; manual conflict resolution required: '+
                ', '.join(conflicts.stdout.splitlines())[:1200]) from error
        raise
    return {'original_head':original_head,'before':before,'base':worker.git(work,'rev-parse','HEAD'),
            'upstream':worker.git(work,'rev-parse',upstream)}


def managed_check(task,work,folder,key):
    """Run OpenClaw's slow required gate without model polling or model tokens."""
    if key!='openclaw' or not rt.config().get('cloud_environment'):return
    import codex_worker as worker
    import validation_setup as validation
    report_path=folder/'required-check.json'
    if report_path.exists():
        prior=json.loads(report_path.read_text())
        if (prior.get('exit_code')==0 and prior.get('head')==worker.git(work,'rev-parse','HEAD')
                and prior.get('patch_digest')==worker.fingerprint(work)):
            return
    cache=folder/'tool-cache';cache.mkdir(exist_ok=True)
    (cache/'home').mkdir(exist_ok=True)
    command=['pnpm','check:changed','--base','HEAD']
    rt.task_state(task['id'],phase='validation',phase_started=time.time());rt.checkpoint()
    started=time.time();status=None
    try:
        with (folder/'required-check.log').open('w') as out:
            process=subprocess.Popen(validation.sandbox(work,cache,command),env=validation.safe_env(cache),
                stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
            try:status=process.wait(timeout=2700)
            except subprocess.TimeoutExpired as error:
                raise validation.ValidationUnavailable('Required check:changed exceeded its controller deadline; patch and logs retained.') from error
            finally:validation.terminate(process)
    finally:
        report={'command':command,'exit_code':status,'seconds':round(time.time()-started,3),
                'head':worker.git(work,'rev-parse','HEAD'),'patch_digest':worker.fingerprint(work)}
        report_path.write_text(json.dumps(report,indent=2))
    if status!=0:
        raise RequiredCheckFailed('Required check:changed failed; inspect required-check.log and repair concrete code/test failures before rerunning the gate.')
