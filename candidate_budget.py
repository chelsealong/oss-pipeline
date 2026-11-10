"""Bounded replacements for proven no-patch investigations; never refund calls."""
import base64
import json
import time
import runtime as rt


def remaining(db, repo, budget):
    import watch
    if budget['used'].get(repo, 0) > watch.DISPATCH_BUDGET.get(repo, watch.DEFAULT_BUDGET):
        return 0  # Lowering a cap must never erase already-spent starts.
    limit = rt.config().get('no_work_replacement_limits', {}).get(repo, 0)
    proofs = rt._meta(db, 'no_work_assessments', {})
    sources = sum(p['repo'] == repo and p['date'] == budget['date'] for p in proofs.values())
    used = budget.get('replacement_used', {}).get(repo, 0)
    return max(0, min(limit, sources) - used)


def requires_replacement(repo):
    import watch
    with rt.db() as db:
        budget = rt._dispatch_budget(db)
        return budget['used'].get(repo, 0) >= watch.DISPATCH_BUDGET.get(repo, watch.DEFAULT_BUDGET)


def invited(cfg, issue):
    labels = {x['name'] for x in issue.get('labels', [])}
    ready = cfg.get('triage_ready_labels', set())
    return bool(labels & {'help wanted', 'good first issue'} or (ready and ready <= labels))


def evidence_proof(task, state, payload):
    """Only a completed, unchanged first investigation can supply one credit."""
    if (task['kind'] != 'fix' or task['status'] != 'skipped'
            or not state.get('dispatch_paid_date') or state.get('dispatch_replacement')
            or state.get('publication_started') or state.get('resume_progress')
            or state.get('last_execution_phase') != 'generation'):
        return None
    execution = payload.get('execution', {})
    if (payload.get('task') != task['id'] or execution.get('attempt') != task['attempts']
            or execution.get('publication_started') or not execution.get('terminal')
            or not state.get('base') or execution.get('base') != state['base'] or payload.get('head') != state['base']
            or payload.get('omitted') or payload.get('untracked')
            or 'diff' not in payload or payload['diff'] != ''):
        return None
    try:
        result = json.loads(base64.b64decode(payload['files']['generation.json'], validate=True))
    except (KeyError, ValueError, TypeError):
        return None
    if result.get('outcome') != 'SKIP' or result.get('reason') != task['result']:
        return None
    return {'task': task['id'], 'repo': task['repo'], 'date': state['dispatch_paid_date'],
            'attempt': task['attempts'], 'base': state['base'], 'at': time.time()}


def reconcile():
    """Import current-day encrypted evidence after restore, without replaying work."""
    import task_recovery
    changed = 0
    with rt.db() as db:
        today = rt._dispatch_budget(db)['date']
        proofs = rt._meta(db, 'no_work_assessments', {})
        tasks = [dict(t) for t in db.execute("SELECT * FROM tasks WHERE kind='fix' AND status='skipped'")]
    for task in tasks:
        if str(task['id']) in proofs:
            continue
        state = rt.task_state(task['id'])
        if (state.get('dispatch_paid_date') != today
                or not rt.config().get('no_work_replacement_limits', {}).get(task['repo'])):
            continue
        path = task_recovery.latest_bundle(task['id'])
        if not path:
            continue
        try:
            proof = evidence_proof(task, state, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            continue
        if proof:
            with rt.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current_task=db.execute('SELECT status,attempts FROM tasks WHERE id=?',(task['id'],)).fetchone()
                if current_task['status']!='skipped' or current_task['attempts']!=task['attempts']:
                    continue
                current = rt._meta(db, 'no_work_assessments', {})
                current[str(task['id'])] = proof
                rt._putmeta(db, 'no_work_assessments', current)
            changed += 1
    return changed


def admission(repo):
    """Avoid semantic screening where no implementation/publication room exists."""
    if not rt.config().get('candidate_capacity_precheck'):
        return True, ''
    import scan
    import codex_worker
    if rt.publication_holds().get(repo, 0) > time.time():
        return False, 'ordinary PR creation remains held'
    ok, why = rt.dispatch_headroom(repo)
    if not ok:
        return ok, why
    name = 'candidate_capacity:' + repo
    cached = rt.getmeta(name, {})
    if time.time() - cached.get('at', 0) < 60:
        return cached['allowed'], cached['reason']
    cfg = scan.REPOS[repo]
    try:
        ok, why = codex_worker.cap_ok(repo, cfg.get('implements_in') or cfg['upstream'])
    except Exception:
        ok, why = False, 'publication capacity lookup unavailable; retain candidate'
    rt.setmeta(name, {'at': time.time(), 'allowed': ok, 'reason': why})
    return ok, why
