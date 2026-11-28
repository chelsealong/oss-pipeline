"""Real tox subprocess regression, without downloads, models or live state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import adk_validation
import task_recovery


class AdkValidationTests(unittest.TestCase):
    def test_tox_forwards_cache_roots_without_secrets_or_changed_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            config='''[tox]
envlist = py
skipsdist = true
[testenv]
skip_install = true
pass_env = FIXTURE_KEEP
commands = python probe.py
'''
            (root/'tox.ini').write_text(config)
            (root/'probe.py').write_text('''import json, os
from pathlib import Path
names = ('XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_STATE_HOME')
assert os.environ['FIXTURE_KEEP']=='retained'
assert 'FAKE_PROVIDER_SECRET' not in os.environ
assert all(os.environ.get(n) for n in names), 'tox dropped writable XDG roots'
for n in names:
    target=Path(os.environ[n])/'crewai'/'credentials'
    target.mkdir(parents=True,exist_ok=True)
    (target/'probe').write_text('ok')
print('TASK_CACHE_WRITES_OK')
''')
            env={k:v for k,v in os.environ.items() if not k.startswith('TOX_')}
            env.update(FIXTURE_KEEP='retained',FAKE_PROVIDER_SECRET='must-not-pass')
            for n in adk_validation.XDG_NAMES:env[n]=str(root/n.lower())
            command=[sys.executable,'-m','tox','-q','-e','py']
            before=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=90)
            self.assertNotEqual(before.returncode,0)
            self.assertIn('tox dropped writable XDG roots',before.stdout+before.stderr)
            adk_validation.configure(env)
            after=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(after.returncode,0,after.stdout+after.stderr)
            self.assertIn('TASK_CACHE_WRITES_OK',after.stdout)
            self.assertEqual((root/'tox.ini').read_text(),config)
            for n in adk_validation.XDG_NAMES:
                self.assertEqual((Path(env[n])/'crewai/credentials/probe').read_text(),'ok')

    def test_recovery_requires_exact_known_unpublished_attempt_and_runs_once(self):
        task=dict(id=2116,number=8376,attempts=2,kind='fix',repo='adk',
                  status='validation_wait',result='VALIDATION_ENVIRONMENT: CrewAI /home/runner/.local/share')
        state=dict(base='03725e8c67ca27b2c07569797a0c18e867e0b217',
                   patch_digest='30809ecf1858634f60504bb356d0ddd9703c6606238c6cc8821faefc5b726a14',
                   last_execution_phase='generation')
        self.assertTrue(task_recovery.adk_tox_regression(task,state))
        for key,value in [('publication_started',True),('adk_tox_repaired',True),
                          ('base','0'*40),('patch_digest','0'*64),('last_execution_phase','publication')]:
            self.assertFalse(task_recovery.adk_tox_regression(task,{**state,key:value}),key)
        for key,value in [('id',9999),('number',1),('attempts',3),('status','human_wait'),
                          ('repo','openclaw'),('kind','respond'),('result','unrelated failure')]:
            self.assertFalse(task_recovery.adk_tox_regression({**task,key:value},state),key)


if __name__=='__main__':unittest.main()
