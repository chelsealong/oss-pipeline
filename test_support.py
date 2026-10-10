"""Explicit historical policy fixtures for generic runtime regressions."""
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def active_hermes():
    """Exercise recovery with an enabled repo; test_repo_limits checks the stop."""
    import repo_limits
    import scan
    import watch
    names=repo_limits.DISABLED_NAMES-{'hermes','nousresearch/hermes-agent'}
    cfg={k:v for k,v in scan.REPOS['hermes'].items() if k not in ('paused','respond_when_paused')}
    with patch.object(repo_limits,'DISABLED_NAMES',names), \
         patch.dict(watch.DISPATCH_BUDGET,{'hermes':16}), \
         patch.dict(repo_limits.PR_CAPS,{'hermes':8}), \
         patch.dict(scan.REPOS,{'hermes':cfg}):
        yield
