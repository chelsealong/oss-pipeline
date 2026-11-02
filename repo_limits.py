"""User-selected repository budgets. No network or model calls."""
DISABLED_REPOS = {
    'mem0': 'mem0ai/mem0',
    'litellm': 'BerriAI/litellm',
    'firecrawl': 'firecrawl/firecrawl',
    'gemini-cli': 'google-gemini/gemini-cli',
    'pydantic-ai': 'pydantic/pydantic-ai',
    'vllm': 'vllm-project/vllm',
    'crawl4ai': 'unclecode/crawl4ai',
}
DISABLED_NAMES = {name.lower() for pair in DISABLED_REPOS.items() for name in pair}
DEFAULT_PR_CAP = 3
PR_CAPS = {'hermes': 8, 'openclaw': 5, 'adk': 5,
           **dict.fromkeys(DISABLED_REPOS, 0)}
DEFAULT_TASK_BUDGET = 6
TASK_BUDGETS = {'hermes': 16, 'openclaw': 10, 'adk': 10,
                **dict.fromkeys(DISABLED_REPOS, 0)}
OPEN_PR_CAPS = {'openclaw': 20}
DISABLED_REASON = 'repository quota disabled by user on 2026-10-10'


def disabled(repo):
    return bool(repo and repo.lower() in DISABLED_NAMES)


def executor_policy(repo):
    """Use the same limits in model instructions and controller admission."""
    maximum=OPEN_PR_CAPS.get(repo)
    concurrent=(f'{maximum} simultaneously open PRs, including drafts'
                if maximum is not None else 'no additional pipeline concurrency cap')
    return (f'Current pipeline PR limits for {repo}: {PR_CAPS.get(repo,DEFAULT_PR_CAP)} new PRs '
        f'per UTC day; {concurrent}. The controller checks these before generation and again '
        'before publication. These current settings supersede historical lessons and earlier '
        'PR_CAP outcomes. Do not stop at an older or invented safety threshold. Existing-PR '
        'maintenance does not use new-PR capacity. Follow any stricter CURRENT upstream '
        'requirement, citing its exact source when it prevents submission.')


def hold_pending(db):
    """Retain disabled work without queue occupancy, attempts or model spending."""
    import time
    import runtime as rt
    rows=db.execute("SELECT id,repo,status,result FROM tasks WHERE status IN "
        "('queued','retry_wait','triage_wait','capacity_wait','publication_wait','execution_wait',"
        "'human_wait','validation_wait')").fetchall()
    for row in rows:
        if not disabled(row['repo']):continue
        state=rt._meta(db,f"task:{row['id']}",{})
        state.update(quota_prior_status=row['status'],quota_prior_result=row['result'],phase='quota_wait')
        rt._putmeta(db,f"task:{row['id']}",state)
        db.execute("UPDATE tasks SET status='quota_wait',result=?,updated=? WHERE id=?",
                   (DISABLED_REASON,time.time(),row['id']))


def restore_enabled_pending(db, creation_holds):
    """Restore paused work only after user re-enables its repository."""
    import time
    import runtime as rt
    rows=db.execute("SELECT * FROM tasks WHERE status='quota_wait' ORDER BY updated").fetchall()
    for row in rows:
        if disabled(row['repo']):continue
        state=rt._meta(db,f"task:{row['id']}",{})
        prior=state.get('quota_prior_status')
        if (state.get('publication_started') or prior not in
                ('queued','retry_wait','triage_wait','capacity_wait','publication_wait',
                 'execution_wait','human_wait','validation_wait')):continue
        if prior in ('queued','retry_wait'):
            if row['kind']=='fix' and creation_holds.get(row['repo'],0)>time.time():
                prior='publication_wait'
            elif not rt._room(db,row['kind'],row['repo'])[0]:continue
        state.update(phase=prior,quota_resumed_at=time.time())
        state.pop('quota_prior_status',None)
        followup=rt._meta(db,f"followup:task:{row['id']}",{})
        reason=state.pop('quota_prior_result',None)
        if reason is None:reason=followup.get('reason') or row['result']
        rt._putmeta(db,f"task:{row['id']}",state)
        db.execute('UPDATE tasks SET status=?,result=?,updated=? WHERE id=?',
                   (prior,reason,time.time(),row['id']))
