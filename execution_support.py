"""Bounded task execution, immutable usage records and controller-owned checks."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import runtime as rt

class RequiredCheckFailed(RuntimeError):pass


def git_process(args, cwd=None, timeout=300):
    """Own fetch helpers too: a timed-out Git parent must not leave writers."""
    process=subprocess.Popen(args,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             text=True,start_new_session=True)
    try:
        stdout,stderr=process.communicate(timeout=timeout)
    except BaseException:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(args,process.returncode,stdout,stderr)


def validation_environment(work, env):
    """Carry ignored task-local Python dependencies into a fresh review thread."""
    for name in ('.venv','venv'):
        root=work/name
        if (not root.resolve().is_relative_to(work.resolve()) or not (root/'pyvenv.cfg').is_file()
                or not (root/'bin/python').is_file()):continue
        ignored=subprocess.run(['git','check-ignore','-q',name+'/pyvenv.cfg'],cwd=work,capture_output=True)
        if ignored.returncode:continue
        env['VIRTUAL_ENV']=str(root)
        env['PATH']=str(root/'bin')+os.pathsep+env.get('PATH',os.defpath)
        return ('\nController validation environment: the existing ignored virtual environment is '+str(root)+'. '
            'Use its explicit interpreter '+str(root/'bin/python')+' for Python tests and its bin directory for Ruff/tools. '
            'Dependencies installed during generation persist in this same checkout; a fresh review thread must independently rerun '
            'checks in that environment. A login shell may reset PATH, so use absolute executable paths when necessary. '
            'Missing packages in system Python do not establish a validation blocker. Inspect the existing environment and the '
            'recorded commands before returning VALIDATION_ENVIRONMENT; never accept prior test claims without checking them.\n')
    return ''


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
    rt.task_state(task['id'],progress_rebase_needed=None,progress_restored=False)
    # An advisory patch may be applied to advanced main only if Git can apply it
    # cleanly. The generator must recheck eligibility/tests and a fresh reviewer
    # must approve; a human-reviewed patch uses the separate exact-base path.
    paths=payload.get('untracked',{})
    collisions=[]
    for name in paths:
        target=work/name
        if (Path(name).is_absolute() or any(part in ('.git','..') for part in Path(name).parts)
                or not target.resolve().is_relative_to(work.resolve())):
            raise RuntimeError('Unsafe retained source file')
        if target.exists():collisions.append(name)
    diff=base64.b64decode(payload.get('diff',''))
    patch=work.parent/'retained-progress.patch';patch.write_bytes(diff)
    def adaptation(reason):
        source=work.parent/'retained-source'
        for name,raw in paths.items():
            target=source/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(base64.b64decode(raw))
        rt.task_state(task['id'],progress_rebase_needed={'patch':str(patch),
            'source':str(source) if paths else None,'reason':reason,'original_base':entry['base']})
        return False
    if collisions:return adaptation('Upstream now contains retained paths: '+', '.join(collisions))
    if diff:
        try:worker.git(work,'apply','--check',str(patch))
        except RuntimeError as error:
            # --check changes nothing. Preserve source for semantic adaptation,
            # rather than discarding work after an upstream rename/context edit.
            return adaptation(str(error)[:1200])
        worker.git(work,'apply',str(patch))
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


def checkout_response(work, upstream_url, fork_url, default, pr, pr_number):
    """Complete commit graph from its authoritative promisor; no shallow upgrades."""
    import codex_worker as worker
    worker.run(['git','clone','--filter=blob:none','--single-branch','--branch',default,
                '--no-checkout',upstream_url,str(work)],timeout=900)
    worker.git(work,'remote','rename','origin','upstream')
    worker.git(work,'remote','add','origin',fork_url)
    target='refs/remotes/upstream/response-head'
    worker.run(['git','fetch','--no-tags','--filter=blob:none','upstream',
                f'refs/pull/{pr_number}/head:{target}'],cwd=work,timeout=900)
    if worker.git(work,'rev-parse',target)!=pr['head']['sha']:
        raise rt.Paused('PR head moved before checkout')
    worker.git(work,'checkout','-b',pr['head']['ref'],target)
    if worker.git(work,'rev-parse','--is-shallow-repository')!='false':
        raise RuntimeError('Response checkout must have complete commit history')


def prepare_response_base(work, default, pr, pr_number):
    """Ordinary follow-ups operate on the PR head, not an unsolicited main merge."""
    import codex_worker as worker
    original=pr['head']['sha']
    if worker.git(work,'rev-parse','HEAD')!=original:
        raise rt.Paused('PR head moved before response preparation')
    if pr.get('mergeable') is False:
        return update_response_base(work,default,original,pr_number)
    # Unknown mergeability is not a claim of being mergeable. CI/review still
    # decides landing; a reply or scoped code update needs no main integration.
    return {'original_head':original,'base':original,'merged':False,
            'mergeable':pr.get('mergeable'),'reason':'No confirmed merge conflict'}


def update_response_base(work, default, original_head, pr_number=None):
    """Merge current main locally, without rewriting or publishing PR history."""
    import codex_worker as worker
    upstream='refs/remotes/upstream/'+default
    head_ref='refs/remotes/upstream/response-head'
    source=f'refs/pull/{pr_number}/head' if pr_number is not None else original_head
    for extent in ('--depth=128','--depth=512','--unshallow'):
        if response_history_complete(work,upstream):break
        if worker.git(work,'rev-parse','--is-shallow-repository')=='false':break
        # Fetch both graph tips together without creating a partial/promisor
        # repository. Lazy blob fetches must not race an outer shallow update.
        # A fixed depth ceiling cannot handle old PRs in fast-moving repositories.
        worker.run(['git','fetch','--no-tags',extent,'upstream',
                    f'+{default}:{upstream}',f'{source}:{head_ref}'],cwd=work,timeout=900)
        if worker.git(work,'rev-parse',head_ref)!=original_head:
            raise rt.Paused('PR head moved during response history fetch')
    if not response_history_complete(work,upstream):
        raise RuntimeError('Cannot prove complete PR merge history; retained for follow-up')
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
    """Explicit rehearsal diagnostic; not an automatic PR-submission gate."""
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
