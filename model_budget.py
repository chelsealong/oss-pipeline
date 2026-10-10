"""Atomic admission budgets; no model calls, credentials or upstream writes."""
import math
import time
import uuid
import runtime as rt


def rate_snapshot(payload):
    """Persist only quota numbers from the existing authenticated host."""
    if not isinstance(payload, dict):
        return
    windows = []
    limits = payload.get('rateLimits') or {}
    if not isinstance(limits, dict):
        return
    for name in ('primary', 'secondary'):
        w = limits.get(name)
        if not isinstance(w, dict):
            continue
        used, reset = w.get('usedPercent'), w.get('resetsAt')
        if isinstance(used, (int, float)) and isinstance(reset, (int, float)):
            if math.isfinite(used) and math.isfinite(reset) and 0 <= used <= 100:
                windows.append({'name': name, 'used_percent': used, 'resets_at': reset})
    if windows:
        rt.setmeta('codex_quota', {'at': time.time(), 'windows': windows})


def quota_gate(db, *, starting=False):
    snapshot = rt._meta(db, 'codex_quota', {})
    if time.time() - snapshot.get('at', 0) > 300:
        return True, ''  # The durable call ceiling remains the fallback.
    threshold = rt.config().get('codex_start_used_percent' if starting else 'codex_stop_used_percent',
                                85 if starting else 95)
    for window in snapshot.get('windows', []):
        if window['resets_at'] > time.time() and window['used_percent'] >= threshold:
            return False, 'Codex reported quota reserve reached; wait until ' + str(window['resets_at'])
    return True, ''


def codex_admit(db, task, phase, spent, limit):
    """Reserve the next independent review before admitting generation/repair."""
    cfg = rt.config()
    if not cfg.get('codex_review_reservations'):
        return True, ''
    now = time.time()
    active = {str(r[0]) for r in db.execute("SELECT id FROM tasks WHERE status='running'")}
    leases = {k: v for k, v in rt._meta(db, 'codex_review_leases', {}).items()
              if k in active and v.get('until', 0) > now}
    ident = str(task['id']) if task else ''
    remaining = sum(v['remaining'] for k, v in leases.items() if k != ident)
    own = leases.get(ident, {}).get('remaining', 0)
    start = phase in ('generation', 'remediation')
    need = 2 if task and start else 1
    allowed, why = quota_gate(db, starting=phase == 'generation' and not own)
    if not allowed:
        return False, why
    if spent + remaining + max(own, need) > limit:
        return False, 'Codex budget reserved for independent reviews'
    group = ('validation' if not task or task['kind'] == 'canary' else
             'repair' if phase in ('remediation', 'rereview', 'review-policy', 'rereview-policy') else
             'maintenance' if task['kind'] == 'respond' else 'new_fix')
    usage = [x for x in rt._meta(db, 'codex_allocations', []) if x['at'] > now - 18000]
    # Soft shares can borrow idle capacity, but may not starve the other queue.
    shares = cfg.get('codex_phase_shares', {})
    if phase == 'generation' and group in ('new_fix', 'maintenance'):
        other = 'respond' if group == 'new_fix' else 'fix'
        waiting = db.execute("SELECT 1 FROM tasks WHERE kind=? AND status IN ('queued','retry_wait') LIMIT 1", (other,)).fetchone()
        if (waiting and sum(x['group'] == group for x in usage) >= shares.get(group, limit)
                and spent + remaining + max(own, need) + 2 > limit):
            return False, 'Codex budget reserved for other task queue'
    if task:
        leases[ident] = {'remaining': max(own, need) - 1, 'until': now + 7200}
    rt._putmeta(db, 'codex_review_leases', leases)
    usage.append({'at': now, 'group': group, 'task': task['id'] if task else None, 'phase': phase})
    rt._putmeta(db, 'codex_allocations', usage)
    return True, ''


def queue_ready(db, state):
    """Avoid a checkout/judge pass when its generation cannot reserve review."""
    cfg=rt.config()
    if not cfg.get('codex_review_reservations'):
        return True
    resumed=bool(state.get('resume_progress'))
    if not quota_gate(db,starting=not resumed)[0]:
        return False
    now=time.time()
    active={str(r[0]) for r in db.execute("SELECT id FROM tasks WHERE status='running'")}
    reserved=sum(v['remaining'] for k,v in rt._meta(db,'codex_review_leases',{}).items()
                 if k in active and v.get('until',0)>now)
    spent=db.execute("SELECT count(*) FROM calls WHERE kind='codex' AND at>?",(now-18000,)).fetchone()[0]
    return spent+reserved+(1 if resumed else 2)<=cfg.get('codex_sessions_per_5h',45)


def release_review(task_id):
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        leases = rt._meta(db, 'codex_review_leases', {})
        leases.pop(str(task_id), None)
        rt._putmeta(db, 'codex_review_leases', leases)


def free_models():
    cfg = rt.config()
    return set(cfg.get('judge_free_only_models', []))


def canary_delay():
    """Seconds until four ordinary turns fit; never refund prior spending."""
    now=time.time()
    limit=rt.config().get('codex_sessions_per_5h',45)
    if limit<4:
        raise RuntimeError('Two canaries require a configured ceiling of at least four turns')
    with rt.db() as db:
        times=[row[0] for row in db.execute(
            "SELECT at FROM calls WHERE kind='codex' AND at>? ORDER BY at",(now-18000,))]
    release=len(times)+4-limit
    return max(0,times[release-1]+18000-now+1) if release>0 else 0


def reserve_judge(model, system, user, output_limit=200):
    ok, why = rt.ready()
    if not ok:
        raise rt.JudgeDeferred(why)
    cfg = rt.config()
    if rt.getmeta('judge_auth_until',0)>time.time():
        raise rt.JudgeDeferred('judge authentication cooldown; retain pending work')
    if cfg.get('judge_free_only_required') and model not in free_models():
        raise rt.JudgeDeferred('judge free-only protection has not been verified for this model')
    # Conservative UTF-8 byte reservation, never a claim of actual token usage.
    # Do not truncate long reports: defer rather than silently omit an objection.
    input_bytes = len(system.encode('utf-8')) + len(user.encode('utf-8'))
    if input_bytes > cfg.get('judge_max_input_bytes', 12000):
        raise rt.JudgeDeferred('judge input exceeds bounded context; retain for inspection')
    reserved = input_bytes + output_limit + 128
    now = time.time()
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        hour = db.execute("SELECT count(*) FROM calls WHERE kind='judge' AND at>?", (now - 3600,)).fetchone()[0]
        day = db.execute("SELECT count(*) FROM calls WHERE kind='judge' AND at>?", (now - 86400,)).fetchone()[0]
        if hour >= cfg.get('judge_requests_per_hour', 30) or day >= cfg.get('judge_requests_per_day', 150):
            raise rt.JudgeDeferred('judge request budget reached; retain pending work')
        rows = [x for x in rt._meta(db, 'judge_usage', []) if x['at'] > now - 86400 * 7]
        spent = sum(x['charged_tokens'] for x in rows if x['at'] > now - 86400)
        if spent + reserved > cfg.get('judge_tokens_per_day', 100000):
            raise rt.JudgeDeferred('judge token budget reached; retain pending work')
        ident = uuid.uuid4().hex
        rows.append({'id': ident, 'at': now, 'model': model, 'reserved_tokens': reserved,
                     'charged_tokens': reserved, 'actual_tokens': None, 'status': 'reserved'})
        rt._putmeta(db, 'judge_usage', rows)
        if not rt._meta(db, 'judge_meter_started_at'):
            rt._putmeta(db, 'judge_meter_started_at', now)
        db.execute('INSERT INTO calls(at,kind) VALUES (?,?)', (now, 'judge'))
    rt.checkpoint()  # Reserve durably before HTTP, including fallback requests.
    return ident


def settle_judge(ident, response=None):
    usage = (response.get('usage') or {}) if isinstance(response,dict) else {}
    if not isinstance(usage,dict):usage={}
    total = usage.get('total_tokens')
    if total is None and all(isinstance(usage.get(k), int) for k in ('prompt_tokens', 'completion_tokens')):
        total = usage['prompt_tokens'] + usage['completion_tokens']
    known = isinstance(total, int) and not isinstance(total, bool) and total >= 0
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = rt._meta(db, 'judge_usage', [])
        for row in rows:
            if row['id'] == ident:
                row.update(status='completed' if response is not None else 'unknown',
                           actual_tokens=total if known else None)
                if known:
                    row['charged_tokens'] = total
                break
        rt._putmeta(db, 'judge_usage', rows)
    rt.checkpoint()  # Unknown/failed requests retain their full reservation.


def status():
    now = time.time()
    with rt.db() as db:
        rows = [x for x in rt._meta(db, 'judge_usage', []) if x['at'] > now - 86400]
        count = db.execute("SELECT count(*) FROM calls WHERE kind='judge' AND at>?", (now - 86400,)).fetchone()[0]
        return {'judge_requests_24h': count, 'judge_charged_tokens_24h': sum(x['charged_tokens'] for x in rows),
                'judge_measured_tokens_24h': sum(x['actual_tokens'] or 0 for x in rows),
                'judge_historical_unmetered_requests': max(0, count - len(rows)),
                'judge_meter_started_at': rt._meta(db, 'judge_meter_started_at'),
                'codex_review_leases': rt._meta(db, 'codex_review_leases', {}),
                'codex_quota': rt._meta(db, 'codex_quota', {}),
                'codex_allocations': rt._meta(db, 'codex_allocations', [])}
