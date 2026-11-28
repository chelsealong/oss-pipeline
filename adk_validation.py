"""Keep ADK's complete tox suite inside the task's writable cache roots."""
import os
from pathlib import Path
import sys

XDG_NAMES = ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME')


def _link_credentials(cache, home, jobs):
    """Bind CrewAI's hardcoded path to this job, never reuse an account store."""
    cache,home,jobs=Path(cache).resolve(),Path(home),Path(jobs).resolve()
    if not cache.is_relative_to(jobs):raise RuntimeError('CrewAI cache is outside owned jobs')
    link=home/'.local/share/crewai/credentials'
    if link.parent.resolve()!=link.parent:
        raise RuntimeError('CrewAI parent path is redirected')
    target=cache/'crewai-credentials';target.mkdir(parents=True,exist_ok=True,mode=0o700)
    if target.is_symlink():raise RuntimeError('CrewAI task cache is redirected')
    link.parent.mkdir(parents=True,exist_ok=True)
    if link.is_symlink():
        prior=link.resolve()
        if prior==target:return target
        if not prior.is_relative_to(jobs) or prior.name!='crewai-credentials':
            raise RuntimeError('Refusing to replace unrelated CrewAI storage')
        link.unlink()
    elif link.exists():
        raise RuntimeError('Refusing to reuse existing CrewAI credential storage')
    link.symlink_to(target,target_is_directory=True)
    return target


def prepare_cloud_cache(cache):
    import runtime as rt
    if rt.config().get('backend')!='codex-cloud':return
    if os.environ.get('GITHUB_ACTIONS')!='true' or sys.platform!='linux' or Path.home()!=Path('/home/runner'):
        raise RuntimeError('CrewAI storage mapping requires the ephemeral GitHub runner')
    _link_credentials(cache,Path.home(),rt.DATA/'jobs')


def configure(env):
    # tox deliberately filters the caller's environment. Forward only these
    # controller-owned directories, never credentials or arbitrary variables.
    # += retains upstream pass_env and leaves commands/envlist/deps untouched.
    override = 'testenv.pass_env+=' + ','.join(XDG_NAMES)
    existing = env.get('TOX_OVERRIDE', '')
    env['TOX_OVERRIDE'] = ';'.join(x for x in (existing, override) if x)
    return ('\nADK validation: the cloud controller maps CrewAI\'s hardcoded credential '
            'directory into this task cache; it contains no account login. TOX_OVERRIDE forwards only the four task-owned XDG '
            'directories through tox. Keep this environment when running tox; '
            'run the complete upstream interpreter matrix. Do not edit tox.ini, '
            'skip tests/interpreters, or treat a baseline failure as a passing check.\n')
