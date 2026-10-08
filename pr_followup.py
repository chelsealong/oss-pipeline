"""Reviewed PR replies and bounded recovery of the pre-reply backlog.

Only the controller publishes. Ambiguous writes are reconciled, never retried.
"""
import hashlib
import json
import re
import time
import runtime as rt

POLICY_VERSION = '2026-10-08-replies-v1'


def response_paused(cfg):
    # This old switch described denied *creation*, not an upstream ban on
    # maintaining existing PRs. Other repository pauses remain in force.
    return bool(cfg.get('paused') and not cfg.get('respond_when_paused'))


def comments(repo, number):
    import codex_worker as worker
    pages = worker.run(['gh', 'api', '--paginate', '--slurp',
                        f'repos/{repo}/issues/{number}/comments'])
    return [c for page in json.loads(pages) for c in page]


def update_body(task, current, proposed, original):
    """Update a reviewed PR description once, preserving concurrent edits."""
    if not proposed or proposed == (current.get('body') or ''):
        return
    if len(proposed)>60000 or original is None or (current.get('body') or '') != original:
        raise rt.Paused('PR description changed; revalidate before updating')
    key='pr_body:'+hashlib.sha256(task['identity'].encode()).hexdigest()[:24]
    if rt.getmeta(key,{}).get('status') in ('posting','sent'):
        raise rt.Paused('Prior PR description update needs reconciliation; no repeated PATCH')
    rt.setmeta(key,{'status':'posting','task':task['id'],'at':time.time(),
        'body_sha256':hashlib.sha256(proposed.encode()).hexdigest()})
    rt.checkpoint()
    import subprocess
    result=subprocess.run(['gh','api','-X','PATCH',f"repos/{task['repo']}/pulls/{task['number']}",'--input','-'],
        input=json.dumps({'body':proposed}),capture_output=True,text=True,timeout=90)
    if result.returncode:raise RuntimeError('PR description update failed or ambiguous; inspect before retry')
    rt.setmeta(key,{'status':'sent','task':task['id'],'at':time.time()})
    rt.checkpoint()


def publish(task, body, expected_head, *, pr_body='', original_body=None):
    """Publish at most one reviewed reply for this durable feedback identity."""
    import codex_worker as worker
    if not rt.config().get('public_pr_replies'):
        raise rt.Paused('Public PR replies disabled')
    if task['kind'] != 'respond' or json.loads(task['note'] or '{}').get('is_issue'):
        raise ValueError('PR replies only; issue claims/coordination stay separate')
    body = body.strip()
    if not body or len(body) > 12000:
        raise ValueError('Reply must be nonempty and bounded')
    repo, number = task['repo'], task['number']
    digest = hashlib.sha256(task['identity'].encode()).hexdigest()[:24]
    key = f'pr_reply:{digest}'
    marker = f'<!-- oss-followup:{digest} -->'
    state = rt.getmeta(key, {})
    if state.get('status') == 'sent':
        return state['url']
    for c in comments(repo, number):
        if c['user']['login'] == worker.ME and marker in c['body']:
            rt.setmeta(key, {'status': 'sent', 'task': task['id'], 'url': c['html_url'],
                            'at': time.time(), 'reconciled': True})
            rt.checkpoint()
            return c['html_url']
    if state.get('status') == 'posting':
        raise rt.Paused('Ambiguous PR reply publication; no duplicate POST')
    current = worker.response_context(repo, number)
    if current['head']['sha'] != expected_head:
        raise rt.Paused('PR head moved; revalidate the reply before publishing')
    if not rt.ready(check_budget=False)[0]:
        raise rt.Paused('Runtime is not ready for public replies')
    update_body(task,current,pr_body,original_body)
    # Independent of model-call budgets and retained across runs. A changed
    # head or new reviewer message must not create an unlimited comment loop.
    now = time.time()
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        state = rt._meta(db, key, {})
        if state.get('status') in ('posting', 'sent'):
            raise rt.Paused('PR reply already reserved; reconcile before retry')
        ledger = rt._meta(db, 'pr_reply_ledger', [])
        ledger = [r for r in ledger if r['at'] > now - 86400]
        if (sum(r['repo'] == repo and r['number'] == number for r in ledger) >= 3
                or sum(r['at'] > now - 3600 for r in ledger) >= 12):
            raise rt.Paused('Public PR reply budget reached')
        ledger.append({'repo': repo, 'number': number, 'at': now, 'key': key})
        rt._putmeta(db, 'pr_reply_ledger', ledger)
        rt._putmeta(db, key, {'status': 'posting', 'task': task['id'], 'at': now,
                             'head': expected_head, 'body_sha256': hashlib.sha256(body.encode()).hexdigest()})
    rt.checkpoint()  # If persistence fails, POST must not happen.
    payload = json.dumps({'body': body + '\n\n' + marker})
    # Do not use worker.run's read retry path for a mutation.
    import subprocess
    result = subprocess.run(['gh', 'api', '-X', 'POST',
        f'repos/{repo}/issues/{number}/comments', '--input', '-'],
        input=payload, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('PR reply POST failed or ambiguous; reconcile marker before any retry')
    posted = json.loads(result.stdout)
    rt.setmeta(key, {'status': 'sent', 'task': task['id'], 'at': time.time(), 'url': posted['html_url']})
    rt.task_state(task['id'], replied_at=time.time(), reply_url=posted['html_url'])
    rt.checkpoint()
    return posted['html_url']


def recover_backlog(limit=3):
    """Offer each old PR thread once under the new policy, with fresh context.

    This creates a new review task, never replays a push or restores old approval.
    The original event set and current head are preserved in its identity.
    """
    import codex_worker as worker
    if not rt.config().get('public_pr_replies') or not rt.ready()[0]:
        return []
    with rt.db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM tasks WHERE kind='respond' ORDER BY updated DESC")]
    latest = {}
    for row in rows:
        latest.setdefault((row['repo'], row['number']), row)
    admitted = []
    checked = 0
    for (repo, number), task in latest.items():
        if checked >= limit or not rt.room('respond')[0]:
            break
        key = f'pr_followup_upgrade:{repo}#{number}'
        if rt.getmeta(key):
            continue
        try:
            note = json.loads(task['note'] or '{}')
            if note.get('is_issue') or note.get('followup_policy') == POLICY_VERSION:
                continue
            actionable_skip = task['status']=='skipped' and re.search(
                r'maintainer decision|draft reply|draft response', task.get('result',''), re.I)
            if not actionable_skip and task['status'] not in ('blocked', 'error', 'human_wait', 'validation_wait', 'done'):
                continue
            _, cfg = worker.configs(task)
            if response_paused(cfg) or not rt.room('respond', repo)[0]:
                continue
            # Failed reads do not permanently suppress the backlog; cap reads
            # each pass and back off per thread without spending model calls.
            attempt_key = key + ':checked'
            if rt.getmeta(attempt_key, 0) > time.time() - 900:
                continue
            checked += 1
            rt.setmeta(attempt_key, time.time())
            pr = worker.response_context(repo, number)
            note.update(followup_policy=POLICY_VERSION, previous_task=task['id'],
                        head=pr['head']['sha'])
            if rt.enqueue('respond', repo, number, json.dumps(note, sort_keys=True), only_new=True):
                rt.setmeta(key, {'at': time.time(), 'previous_task': task['id'], 'head': pr['head']['sha']})
                admitted.append(f'{repo}#{number}')
        except Exception as error:
            rt.setmeta(key + ':error', {'at': time.time(), 'reason': str(error)[:350]})
    if admitted:
        rt.checkpoint()
    return admitted
