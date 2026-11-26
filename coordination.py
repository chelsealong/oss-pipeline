"""User-authorized, bounded ADK coordination at worker admission, never discovery.

Reserve and checkpoint before POST; reconcile ambiguous writes without reposting.
Only recent, fully screened issues with an active repair task may send a request.
"""
import datetime as dt
import hashlib
import json
import re
import subprocess
import time
import runtime as rt

REPO = 'google/adk-python'


def enabled(upstream):
    return upstream == REPO and rt.config().get('adk_coordination_enabled') is True


def can_offer(upstream, issue):
    if not enabled(upstream):
        return False
    state = rt.getmeta(f"coordination:{upstream}#{issue['number']}", {})
    if state.get('status') == 'refused':
        return False
    try:
        created = dt.datetime.fromisoformat(issue['created_at'].replace('Z', '+00:00')).timestamp()
    except (KeyError, ValueError, TypeError):
        return False
    # Do not mass-replay historical drafts. Normal invitation handling remains.
    if not 0 <= time.time() - created <= 72 * 3600:
        return False
    ledger = rt.getmeta('adk_coordination_ledger', [])
    return (sum(x['at'] > time.time()-3600 for x in ledger) < 1
            and sum(x['at'] > time.time()-86400 for x in ledger) < 5)


def ensure_request(task, cfg, issue):
    """Called only after strict semantic/claim/duplicate checks in a worker."""
    import codex_worker as worker
    import pr_followup
    import scan
    repo = cfg['upstream']; number = issue['number']
    if not enabled(repo) or task['kind'] != 'fix' or task['repo'] != 'adk' or task['number'] != number:
        raise rt.Paused('ADK coordination requires an authorized repair task')
    key = f'adk_coordination_post:{number}'
    marker = '<!-- oss-adk-coordinate:' + str(number) + ' -->'
    for c in pr_followup.comments(repo, number):
        if c['user']['login'] == worker.ME and marker in c.get('body', ''):
            rt.setmeta(key, {'status':'sent', 'url':c['html_url'], 'task':task['id'], 'at':time.time()})
            rt.checkpoint()
            return c['html_url']
    prior = rt.getmeta(key, {})
    if prior.get('status') in ('posting', 'sent'):
        raise rt.Paused('ADK coordination publication needs reconciliation; no repeated POST')
    # Fresh issue/comments and all gates immediately before the external write.
    current = worker.api(f'repos/{repo}/issues/{number}')
    if current.get('state') != 'open' or 'pull_request' in current:
        raise rt.Paused('ADK coordination issue is no longer open')
    ok, why, extra = scan.vet(cfg, repo, current, allow_coordination=True)
    if not ok:
        raise rt.Paused('ADK coordination eligibility changed: ' + why)
    if not extra.get('coordination_required'):
        return None  # An existing request/invitation now satisfies the rule.
    for ok, why in (rt.ready(), rt.dispatch_headroom('adk'), worker.cap_ok('adk', repo)):
        if not ok:
            raise rt.Paused('ADK coordination admission: ' + why)
    title = re.sub(r'[\x00-\x1f\x7f`*_\[\]<>]', '', current.get('title',''))[:180].replace('@', '＠')
    body = (f'May I work on this issue (#{number}: {title}) and, if I can reproduce it, '
            'submit a focused fix with regression coverage? Please let me know if someone is '
            'already handling it or if you would prefer a different approach. '
            'I would use Codex assistance and disclose that in any PR.\n\n' + marker)
    now = time.time()
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT kind,repo,number,status FROM tasks WHERE id=?', (task['id'],)).fetchone()
        if not row or tuple(row) != ('fix','adk',number,'running'):
            raise rt.Paused('Coordination task is not actively running')
        if rt._meta(db,key,{}).get('status') in ('posting','sent'):
            raise rt.Paused('Coordination already reserved; reconcile before retry')
        ledger = [x for x in rt._meta(db,'adk_coordination_ledger',[]) if x['at'] > now-86400]
        if len(ledger) >= 5 or any(x['at'] > now-3600 for x in ledger):
            raise rt.Paused('ADK coordination request budget reached')
        ledger.append({'number':number,'task':task['id'],'at':now})
        rt._putmeta(db,'adk_coordination_ledger',ledger)
        rt._putmeta(db,key,{'status':'posting','task':task['id'],'at':now,
                          'body_sha256':hashlib.sha256(body.encode()).hexdigest()})
    rt.checkpoint()
    # Mutations never use the generic read-retry helper.
    result = subprocess.run(['gh','api','-X','POST',f'repos/{repo}/issues/{number}/comments','--input','-'],
                            input=json.dumps({'body':body}),capture_output=True,text=True,timeout=90)
    if result.returncode:
        raise rt.Paused('ADK coordination POST failed or ambiguous; reconcile before retry')
    posted = json.loads(result.stdout)
    rt.setmeta(key,{'status':'sent','task':task['id'],'at':now,'url':posted['html_url']})
    rt.task_state(task['id'],coordination_url=posted['html_url'])
    rt.checkpoint()
    return posted['html_url']
