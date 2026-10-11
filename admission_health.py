"""Operational admission status; no models, GitHub requests or external alerts."""
import collections
import json
import time
import runtime as rt
import repo_limits


def status():
    import scan
    import watch
    now=time.time()
    try:pending=watch.pending_vet()
    except (OSError,ValueError):pending={}
    reasons=collections.Counter();by_repo={}
    for key,items in pending.items():
        if repo_limits.disabled(key) or scan.REPOS.get(key,{}).get('paused'):continue
        by_repo[key]=len(items)
        for item in items.values():
            reason=item.get('reason','awaiting admission')
            category=('judge_budget' if 'budget' in reason else 'long_context' if 'bounded context' in reason
                      else 'provisional_triage' if 'triage pending' in reason else 'awaiting_admission')
            reasons[category]+=1
    with rt.db() as db:
        active=db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running','retry_wait')").fetchone()[0]
        latest=db.execute("SELECT max(at) FROM calls WHERE kind='codex'").fetchone()[0]
        waits=db.execute("SELECT count(*) FROM tasks WHERE status='triage_wait'").fetchone()[0]
        coordination=[]
        for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'coordination:%'"):
            value=json.loads(row['value'])
            if value.get('status')=='needs_human' and value.get('last_checked',0)>now-86400:
                coordination.append({'key':row['key'],**value})
        usage=rt._meta(db,'judge_usage',[])
        spent=sum(x['charged_tokens'] for x in usage if x['at']>now-86400)
        screening=rt._meta(db,'codex_screening_calls',[])
        screenings_5h=sum(x['at']>now-18000 for x in screening)
        screenings_day=sum(x['at']>now-86400 for x in screening)
    idle_seconds=max(0,now-latest) if latest else None
    waiting=sum(by_repo.values())+waits
    stalled=bool(waiting and active==0 and (idle_seconds is None or idle_seconds>1800))
    return {'state':'stalled' if stalled else 'active' if active else 'idle',
            'pending_candidates':sum(by_repo.values()),'pending_by_repo':by_repo,
            'pending_reasons':dict(reasons),'screening_wait_tasks':waits,'active_tasks':active,
            'seconds_since_codex_call':idle_seconds,'judge_tokens_spent_24h':spent,
            'screening_calls_5h':screenings_5h,'screening_calls_24h':screenings_day,
            'coordination_requests':coordination,
            'warning':'Candidates are waiting but no work is active; inspect admission budgets and handoffs.' if stalled else ''}


def report(value):
    rt.setmeta('admission_health',{'at':time.time(),**value})
    if value['warning'] and time.time()-rt.getmeta('admission_warning_at',0)>1800:
        print('::warning title=Pipeline admission stalled::'+value['warning'],flush=True)
        rt.setmeta('admission_warning_at',time.time())
