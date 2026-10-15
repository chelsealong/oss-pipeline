"""User-selected repository budgets. No network or model calls."""
DISABLED_REPOS = {
    'hermes': 'NousResearch/hermes-agent',
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
PR_CAPS = {'openclaw': 5, 'adk': 5,
           **dict.fromkeys(DISABLED_REPOS, 0)}
DEFAULT_TASK_BUDGET = 6
TASK_BUDGETS = {'openclaw': 10, 'adk': 10,
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
    rows=db.execute("SELECT id,repo,status FROM tasks WHERE status IN "
        "('queued','retry_wait','triage_wait','capacity_wait','publication_wait','execution_wait',"
        "'human_wait','validation_wait')").fetchall()
    for row in rows:
        if not disabled(row['repo']):continue
        state=rt._meta(db,f"task:{row['id']}",{})
        state.update(quota_prior_status=row['status'],phase='quota_wait')
        rt._putmeta(db,f"task:{row['id']}",state)
        db.execute("UPDATE tasks SET status='quota_wait',result=?,updated=? WHERE id=?",
                   (DISABLED_REASON,time.time(),row['id']))
