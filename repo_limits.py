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
OPEN_PR_CAPS = {'openclaw': 20}
DISABLED_REASON = 'repository quota disabled by user on 2026-10-10'


def disabled(repo):
    return bool(repo and repo.lower() in DISABLED_NAMES)


def hold_pending(db):
    """Retain disabled work without queue occupancy, attempts or model spending."""
    import time
    import runtime as rt
    rows=db.execute("SELECT id,repo,status FROM tasks WHERE status IN "
        "('queued','retry_wait','triage_wait','capacity_wait','publication_wait','execution_wait')").fetchall()
    for row in rows:
        if not disabled(row['repo']):continue
        state=rt._meta(db,f"task:{row['id']}",{})
        state.update(quota_prior_status=row['status'],phase='quota_wait')
        rt._putmeta(db,f"task:{row['id']}",state)
        db.execute("UPDATE tasks SET status='quota_wait',result=?,updated=? WHERE id=?",
                   (DISABLED_REASON,time.time(),row['id']))
