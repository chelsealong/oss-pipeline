"""Keep ADK's complete tox suite inside the task's writable cache roots."""
XDG_NAMES = ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME')


def configure(env):
    # tox deliberately filters the caller's environment. Forward only these
    # controller-owned directories, never credentials or arbitrary variables.
    # += retains upstream pass_env and leaves commands/envlist/deps untouched.
    override = 'testenv.pass_env+=' + ','.join(XDG_NAMES)
    existing = env.get('TOX_OVERRIDE', '')
    env['TOX_OVERRIDE'] = ';'.join(x for x in (existing, override) if x)
    return ('\nADK validation: TOX_OVERRIDE forwards only the four task-owned XDG '
            'directories through tox. Keep this environment when running tox; '
            'run the complete upstream interpreter matrix. Do not edit tox.ini, '
            'skip tests/interpreters, or treat a baseline failure as a passing check.\n')
