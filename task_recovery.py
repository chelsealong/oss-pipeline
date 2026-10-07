"""Bounded resumption, remote reconciliation and exact-patch human handoff.

No model calls or public comments. A wait never waives upstream requirements.
"""
import base64
import hashlib
import json
from pathlib import Path
import re
import time
import runtime as rt

WAIT_STATES=('capacity_wait','human_wait','validation_wait')

def failed_read(reason):
    # A connection reset alone could have happened during an old push whose
    # publication marker was lost. Require positive clone/fetch evidence.
    return bool(re.search(r'Cloning into|\[.*git.*(?:fetch|clone).*timed out',reason,re.I))

def wait_kind(reason):
    if re.search(r'HUMAN_REVIEW_REQUIRED:|human (?:oversight|verification|review|signoff|sign-off)|maintainer (?:approval|permission)|coordination required',reason,re.I):
        return 'human_wait'
    if re.search(r'VALIDATION_ENVIRONMENT:|browser signoff|browser verification|web app.*(?:not running|down)|Postgres.*unreachable|ClickHouse.*unreachable',reason,re.I):
        return 'validation_wait'
    return 'blocked'

def optional_api(path):
    import codex_worker as worker
    try:return worker.api(path)
    except RuntimeError as error:
        if re.search(r'HTTP 404\b',str(error)):return None
        raise

def latest_bundle(task_id):
    paths=list((rt.DATA/'evidence').glob(f'{task_id}-*.json'))
    return max(paths,key=lambda p:int(p.stem.split('-')[-1])) if paths else None

def approval_for(task_id):
    return rt.getmeta(f'human_approval:{task_id}',{})

def handoff(task):
    """Make an older retained patch reviewable without regenerating or publishing."""
    path=latest_bundle(task['id']);state=rt.task_state(task['id'])
    if path and not state.get('patch_digest'):
        payload=json.loads(path.read_text())
        if payload.get('diff') or payload.get('untracked'):
            import work_evidence
            files={name:base64.b64decode(raw) for name,raw in payload.get('untracked',{}).items()}
            if files and not payload.get('untracked_modes'):
                # Older evidence never captured mode. The reviewable reconstruction
                # explicitly uses non-executable files and requires fresh checks.
                payload['untracked_modes']={name:0o644 for name in files}
                payload['legacy_mode_note']='Original modes unavailable; review reconstructed non-executable files and re-run checks.'
            digest=work_evidence.patch_digest(base64.b64decode(payload.get('diff','')),files,payload.get('untracked_modes'))
            state=rt.task_state(task['id'],patch_digest=digest)
            payload['execution']['patch_digest']=digest
            import work_evidence
            work_evidence.atomic(path,json.dumps(payload,sort_keys=True).encode())
    rt.setmeta(f"followup:task:{task['id']}",{'status':'needs_human','task':task['id'],
        'repo':task['repo'],'number':task['number'],'reason':task['result'],
        'base':state.get('base'),'patch_digest':state.get('patch_digest'),
        'evidence_bundle':path.name+'.enc' if path else None})

def record_approval(task_id,base,digest,reviewer):
    """Called by an explicitly human-triggered control action after patch review."""
    if not reviewer or '[bot]' in reviewer:raise ValueError('A named human reviewer is required')
    with rt.db() as db:
        task=db.execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task or task['status']!='human_wait' or rt.task_state(task_id).get('publication_started'):
        raise ValueError('Only an unpublished task awaiting human review can be approved')
    path=latest_bundle(task_id)
    if path is None:raise ValueError('Retained patch evidence is unavailable')
    payload=json.loads(path.read_text());execution=payload['execution']
    if execution.get('base')!=base or execution.get('patch_digest')!=digest:
        raise ValueError('Approval does not match the retained exact patch')
    if not re.fullmatch(r'[0-9a-f]{40}',base) or not re.fullmatch(r'[0-9a-f]{64}',digest):
        raise ValueError('Invalid base or patch digest')
    if not payload.get('diff') and not payload.get('untracked'):raise ValueError('No patch to review')
    if payload.get('omitted'):raise ValueError('Incomplete patch evidence cannot be approved')
    rt.setmeta(f'human_approval:{task_id}',{'base':base,'patch_digest':digest,
        'bundle':path.name,'bundle_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'reviewer':reviewer,'reviewed_at':time.time()})

def restore_approved(task,work,base):
    """Reapply only the exact human-reviewed patch onto its unchanged base."""
    import codex_worker as worker
    approval=approval_for(task['id'])
    if not approval:return None
    if approval['base']!=base:return None
    path=rt.DATA/'evidence'/approval['bundle']
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=approval['bundle_sha256']:
        raise ValueError('Human-reviewed evidence changed')
    payload=json.loads(path.read_text())
    for name in payload.get('untracked',{}):
        target=work/name
        if '.git' in Path(name).parts or not target.resolve().is_relative_to(work.resolve()) or target.exists():
            raise ValueError('Unsafe or conflicting retained file')
    diff=base64.b64decode(payload.get('diff',''))
    if diff:
        patch=work.parent/'human-reviewed.patch';patch.write_bytes(diff)
        worker.git(work,'apply','--check',str(patch));worker.git(work,'apply',str(patch))
    for name,raw in payload.get('untracked',{}).items():
        mode=payload.get('untracked_modes',{}).get(name,0o644)
        if mode not in (0o644,0o755):raise ValueError('Unsafe retained file mode')
        target=work/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(base64.b64decode(raw));target.chmod(mode)
    if worker.fingerprint(work)!=approval['patch_digest']:
        raise ValueError('Restored patch differs from human-reviewed patch')
    return approval

def audited_unpublished(task):
    """A failed read/known pre-publication phase and absent remote branch are required."""
    import codex_worker as worker
    state=rt.task_state(task['id'])
    if state.get('publication_started'):return False,'Publication may already have happened'
    paths=list((rt.DATA/'evidence').glob(f"{task['id']}-*.json"))
    if any(json.loads(p.read_text())['execution'].get('publication_started') for p in paths):
        return False,'A prior attempt may already have published'
    read_failure=failed_read(task.get('result',''))
    phase=state.get('last_execution_phase',state.get('phase'))
    known_phase=phase in ('checkout','generation','review','remediation','rereview','validation')
    if not read_failure and not (task['status']=='interrupted' and known_phase):
        return False,'No positive evidence of a pre-publication interruption'
    key,cfg=worker.configs(task);impl=cfg.get('implements_in') or cfg['upstream']
    if task['kind']=='respond':
        pr=worker.response_context(impl,task['number'])
        return bool(state.get('base') and pr['head']['sha']==state['base']),'Current PR head must match the retained base'
    branch=f"fix/codex-{key}-{task['number']}"
    remote=optional_api(f'repos/{worker.ME}/{impl.split("/")[1]}/git/ref/heads/{branch}')
    if remote:return False,'Remote branch exists; inspect publication before resuming'
    prs=json.loads(worker.run(['gh','pr','list','--repo',impl,'--head',worker.ME+':'+branch,
                              '--state','all','--json','number']))
    if prs:return False,'A remote PR already uses this task branch'
    return True,'Verified pre-publication failure and no remote branch/PR'

def requeue(task):
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
        if not current or current['status']!=task['status'] or current['updated']!=task['updated']:return False
        if not rt._room(db,current['kind'],current['repo'])[0]:return False
        state=rt._meta(db,f"task:{task['id']}",{})
        if state.get('publication_started'):return False
        if current['status']=='human_wait':
            state['approval_resume']=True
        state.update(phase='queued',terminal=False,retry_after=0)
        rt._putmeta(db,f"task:{task['id']}",state)
        db.execute("UPDATE tasks SET status='queued',updated=? WHERE id=?",(time.time(),task['id']))
        rt._putmeta(db,f"followup:task:{task['id']}",{'status':'resumed','task':task['id']})
    return True

def reconcile(limit=8):
    """Reconcile outside SQLite transactions; waits consume no queue/model budget."""
    import codex_worker as worker
    resumed=[]
    with rt.db() as db:
        tasks=[dict(r) for r in db.execute("SELECT * FROM tasks WHERE status IN ('capacity_wait','human_wait','validation_wait','error','interrupted','skipped','blocked') ORDER BY updated")]
    checked=0
    for task in tasks:
        state=rt.task_state(task['id']);status=task['status'];reason=task.get('result','')
        # Upgrade historical transient cap skips without generating again.
        if task['kind']=='fix' and status in ('skipped','blocked') and re.search(r'open PR cap|daily PR cap',reason):
            if not state.get('publication_started'):
                with rt.db() as db:db.execute("UPDATE tasks SET status='capacity_wait' WHERE id=? AND status=?",(task['id'],status))
                task['status']=status='capacity_wait'
        if status=='blocked' and wait_kind(reason) in ('human_wait','validation_wait'):
            new=wait_kind(reason)
            with rt.db() as db:db.execute('UPDATE tasks SET status=? WHERE id=? AND status=?',(new,task['id'],status))
            task['status']=status=new
            handoff(task)
        if state.get('recovery_checked_at',0)>time.time()-900:continue
        if status=='human_wait':
            approval=approval_for(task['id'])
            if not approval or approval.get('base')!=state.get('base') or approval.get('patch_digest')!=state.get('patch_digest'):continue
        if status=='validation_wait':
            # A new validation bootstrap can rescue an old environment block once.
            if task['repo']!='langfuse' or state.get('validation_bootstrap_version')=='1':continue
        if status in ('error','interrupted'):
            if task['attempts']>=3 or state.get('publication_started'):continue
            phase=state.get('last_execution_phase',state.get('phase'))
            if status=='error' and not failed_read(reason):continue
            if status=='interrupted' and phase not in ('checkout','generation','review','remediation','rereview','validation'):continue
        elif status not in WAIT_STATES:continue
        if checked>=limit:break
        checked+=1;rt.task_state(task['id'],recovery_checked_at=time.time())
        try:
            if task['kind']=='fix' and rt.publication_holds().get(task['repo'],0)>time.time():continue
            if status=='capacity_wait':
                key,cfg=worker.configs(task)
                if not worker.cap_ok(key,cfg.get('implements_in') or cfg['upstream'])[0]:continue
            elif status in ('error','interrupted'):
                ok,why=audited_unpublished(task)
                rt.setmeta(f"recovery_audit:{task['id']}",{'at':time.time(),'safe':ok,'reason':why})
                if not ok:continue
            if requeue(task):resumed.append(task['id'])
        except Exception as error:
            rt.setmeta(f"followup:task:{task['id']}",{'status':'needs_human','task':task['id'],
                'reason':'Recovery check failed: '+str(error)[:350]})
    if resumed:rt.checkpoint()
    return resumed
