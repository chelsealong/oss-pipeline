"""Real tox subprocess regression, without downloads, models or live state."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import adk_validation
import task_recovery


class AdkValidationTests(unittest.TestCase):
    def test_real_crewai_ignores_xdg_but_writes_through_owned_cache_link(self):
        # Load the actual pinned upstream module without importing unrelated
        # CrewAI integrations, telemetry or provider clients.
        spec=importlib.util.find_spec('crewai_core')
        source=Path(spec.origin).with_name('token_manager.py')
        spec=importlib.util.spec_from_file_location('crewai_token_probe',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();home=root/'home';share=home/'.local/share'
            jobs=root/'jobs';cache=jobs/'task/tool-cache';cache.mkdir(parents=True)
            share.mkdir(parents=True);share.chmod(0o555)
            with patch.object(module.sys,'platform','linux'),\
                 patch.object(module.os.path,'expanduser',return_value=str(share)),\
                 patch.dict(os.environ,{'XDG_DATA_HOME':str(cache/'xdg-data')}):
                try:
                    if os.geteuid()!=0:
                        with self.assertRaises(PermissionError):module.TokenManager()
                    share.chmod(0o700)
                    target=adk_validation._link_credentials(cache,home,jobs)
                    share.chmod(0o555)
                    manager=module.TokenManager()
                    self.assertIsNone(manager.get_token())
                    self.assertTrue((target/'secret.key').is_file())
                    self.assertEqual((share/'crewai/credentials').resolve(),target)
                finally:share.chmod(0o700)
    def test_crewai_mapping_never_reuses_existing_account_store_or_external_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();home=root/'home';jobs=root/'jobs';cache=jobs/'task/cache'
            store=home/'.local/share/crewai/credentials';store.mkdir(parents=True)
            (store/'existing').write_text('preserve')
            with self.assertRaisesRegex(RuntimeError,'existing CrewAI'):
                adk_validation._link_credentials(cache,home,jobs)
            self.assertEqual((store/'existing').read_text(),'preserve')
            with self.assertRaisesRegex(RuntimeError,'outside owned jobs'):
                adk_validation._link_credentials(root/'outside',home,jobs)

    @unittest.skipUnless(os.environ.get('OSS_TEST_CREWAI_SANDBOX')=='1',
                         'real Linux sandbox exercised by cloud preflight/runtime gate')
    def test_real_crewai_in_readonly_linux_sandbox(self):
        self.assertIsNotNone(shutil.which('bwrap'))
        source=Path(importlib.util.find_spec('crewai_core').origin).with_name('token_manager.py')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();home=root/'home';share=home/'.local/share'
            jobs=root/'jobs';cache=jobs/'task/cache';cache.mkdir(parents=True)
            share.mkdir(parents=True)
            code=('import importlib.util, os; '
                  f's=importlib.util.spec_from_file_location("probe",{str(source)!r}); '
                  'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                  f'os.path.expanduser=lambda _: {str(share)!r}; '
                  'assert m.TokenManager().get_token() is None')
            cmd=['bwrap','--unshare-user','--uid','0','--gid','0','--ro-bind','/','/',
                 '--bind',str(cache),str(cache),'--',sys.executable,'-c',code]
            before=subprocess.run(cmd,capture_output=True,text=True)
            self.assertNotEqual(before.returncode,0)
            self.assertIn('Read-only file system',before.stderr)
            target=adk_validation._link_credentials(cache,home,jobs)
            after=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(after.returncode,0,after.stderr)
            self.assertTrue((target/'secret.key').exists())

    def test_reply_wording_recovery_is_exact_and_once_only(self):
        task=dict(id=1988,kind='respond',repo='openclaw/openclaw',number=202424,status='blocked',attempts=1)
        state=dict(last_execution_phase='reply-review',base='b87a161b96050626fac5eb920b08871fcf7b7d7b',
                   remote_base='b87a161b96050626fac5eb920b08871fcf7b7d7b',
                   patch_digest='af5570f5a1810b7af78caf4bc70a660f0df51e42baf91d4de5b2328de0e83dfc')
        self.assertTrue(task_recovery.reply_wording_regression(task,state))
        for change in ({'reply_wording_repaired':True},{'publication_started':True},
                       {'remote_base':'changed'},{'patch_digest':'changed'}):
            self.assertFalse(task_recovery.reply_wording_regression(task,{**state,**change}))
        for change in ({'id':2068},{'attempts':2},{'status':'human_wait'}):
            self.assertFalse(task_recovery.reply_wording_regression({**task,**change},state))
        self.assertTrue(task_recovery.reply_evidence_regression({**task,'attempts':2},
            {**state,'reply_wording_repaired':True}))
        self.assertFalse(task_recovery.reply_evidence_regression({**task,'attempts':3},state))
        self.assertFalse(task_recovery.reply_evidence_regression({**task,'attempts':2},
            {**state,'reply_command_evidence_repaired':True}))

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
        self.assertTrue(task_recovery.adk_crewai_regression({**task,'attempts':3},
            {**state,'adk_tox_repaired':True}))
        self.assertFalse(task_recovery.adk_crewai_regression({**task,'attempts':4},state))
        self.assertFalse(task_recovery.adk_crewai_regression(task,{**state,'adk_crewai_repaired':True}))
        for key,value in [('publication_started',True),('adk_tox_repaired',True),
                          ('base','0'*40),('patch_digest','0'*64),('last_execution_phase','publication')]:
            self.assertFalse(task_recovery.adk_tox_regression(task,{**state,key:value}),key)
        for key,value in [('id',9999),('number',1),('attempts',3),('status','human_wait'),
                          ('repo','openclaw'),('kind','respond'),('result','unrelated failure')]:
            self.assertFalse(task_recovery.adk_tox_regression({**task,key:value},state),key)


if __name__=='__main__':unittest.main()
