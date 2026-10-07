"""Resumption must preserve limits, exact human review and publication safety."""
import base64
import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import cloud_runtime
import cloud_store
import human_review
import codex_worker as worker
import runtime as rt
import task_recovery as recovery
import validation_setup as validation
import watch
import work_evidence

class ResumptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
            patch.object(rt,'CONFIG',self.root/'runtime.json'),
            patch.dict(os.environ,{'OSS_ARTIFACT_KEY':base64.b64encode(b'k'*32).decode()})]
        for p in self.patches:p.start()
        rt.CONFIG.write_text(json.dumps({'backend':'codex-local','enabled':True,'max_pending':8,
            'max_pending_per_repo':3,'codex_sessions_per_5h':100}))
        rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def task(self,status='queued',reason='',number=1,attempts=1):
        self.assertTrue(rt.enqueue('fix','hermes',number,only_new=True))
        with rt.db() as db:
            db.execute('UPDATE tasks SET status=?,result=?,attempts=? WHERE number=?',(status,reason,attempts,number))
            return dict(db.execute('SELECT * FROM tasks WHERE number=?',(number,)).fetchone())
    def git(self,work,*args):
        return subprocess.run(['git',*args],cwd=work,capture_output=True,check=True,text=True).stdout.strip()
    def retained(self,task):
        folder=rt.DATA/'jobs'/str(task['id'])/'attempt-1';work=folder/'work';work.mkdir(parents=True)
        self.git(work,'init');self.git(work,'config','user.name','Test');self.git(work,'config','user.email','test@example.invalid')
        (work/'value.py').write_text('value = 1\n');self.git(work,'add','.');self.git(work,'commit','-m','baseline')
        base=self.git(work,'rev-parse','HEAD');(work/'value.py').write_text('value = 2\n')
        (work/'new_test.py').write_text('assert value == 2\n')
        rt.task_state(task['id'],folder=str(folder.relative_to(rt.DATA)),attempt=1,phase='generation',base=base,
            patch_digest=worker.fingerprint(work),terminal=True)
        work_evidence.capture(task['id'])
        return work,base,worker.fingerprint(work)
    def test_cap_wait_costs_no_model_and_wakes_only_with_capacity(self):
        task=self.task()
        with patch.object(worker,'cap_ok',return_value=(False,'open PR cap reached')),patch.object(worker,'agent') as agent:
            worker.process(task);agent.assert_not_called()
        with rt.db() as db:
            row=db.execute('SELECT * FROM tasks').fetchone()
            self.assertEqual(row['status'],'capacity_wait');self.assertEqual(row['attempts'],0)
            self.assertTrue(rt._room(db,'fix','hermes')[0])
        with patch.object(worker,'cap_ok',return_value=(False,'open PR cap')):self.assertEqual(recovery.reconcile(),[])
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(worker,'cap_ok',return_value=(True,'')):self.assertEqual(recovery.reconcile(),[task['id']])
        self.assertEqual(rt.status()['dispatch_budget']['used'],{})
    def test_historical_cap_skip_is_wait_not_a_permanent_tombstone(self):
        task=self.task('skipped','open PR cap 20/20',attempts=2)
        with patch.object(worker,'cap_ok',return_value=(False,'')):recovery.reconcile()
        with rt.db() as db:self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'capacity_wait')
        self.assertFalse(rt.enqueue('fix','hermes',1,only_new=True))
    def test_remote_branch_blocks_interrupted_replay(self):
        task=self.task('interrupted');rt.task_state(task['id'],phase='generation')
        with patch.object(recovery,'optional_api',return_value={'sha':'already-pushed'}),patch.object(worker,'run') as run:
            self.assertFalse(recovery.audited_unpublished(task)[0]);run.assert_not_called()
        with patch.object(recovery,'optional_api',return_value=None),patch.object(worker,'run',return_value='[]'):
            self.assertTrue(recovery.audited_unpublished(task)[0])
    def test_unknown_or_published_interruption_is_never_replayed(self):
        task=self.task('interrupted');rt.task_state(task['id'],phase='unknown')
        with patch.object(recovery,'optional_api') as api:
            self.assertFalse(recovery.audited_unpublished(task)[0]);api.assert_not_called()
        rt.task_state(task['id'],phase='generation',publication_started=True)
        with patch.object(recovery,'optional_api') as api:
            self.assertFalse(recovery.audited_unpublished(task)[0]);api.assert_not_called()
    def test_prior_attempt_publication_marker_is_retained(self):
        task=self.task('interrupted');work,base,digest=self.retained(task)
        rt.task_state(task['id'],publication_started=True);work_evidence.capture(task['id'])
        rt.task_state(task['id'],publication_started=False)
        self.assertFalse(recovery.audited_unpublished(task)[0])
    def test_read_failure_at_max_attempts_cannot_retry_forever(self):
        task=self.task('error','Cloning into work: connection reset',attempts=3)
        with patch.object(recovery,'audited_unpublished') as audit:
            self.assertEqual(recovery.reconcile(),[]);audit.assert_not_called()
    def test_unknown_network_reset_is_not_proof_of_a_failed_read(self):
        task=self.task('error','git failed: Recv failure: Connection reset')
        with patch.object(recovery,'optional_api') as api:
            self.assertFalse(recovery.audited_unpublished(task)[0]);api.assert_not_called()
        self.assertTrue(recovery.failed_read("Command '['git', 'fetch', 'origin']' timed out"))
    def test_404_is_absence_but_permission_errors_fail_closed(self):
        with patch.object(worker,'api',side_effect=RuntimeError('HTTP 404 Not Found')):
            self.assertIsNone(recovery.optional_api('ref'))
        with patch.object(worker,'api',side_effect=RuntimeError('HTTP 403 Forbidden')):
            with self.assertRaises(RuntimeError):recovery.optional_api('ref')
    def test_human_gate_requires_exact_named_review_and_restores_patch(self):
        task=self.task('human_wait','HUMAN_REVIEW_REQUIRED: oversight');work,base,digest=self.retained(task)
        self.assertEqual(recovery.reconcile(),[])
        with self.assertRaises(ValueError):recovery.record_approval(task['id'],base,digest,'review-bot[bot]')
        with self.assertRaises(ValueError):recovery.record_approval(task['id'],base,'a'*64,'human')
        recovery.record_approval(task['id'],base,digest,'human')
        self.git(work,'restore','value.py');(work/'new_test.py').unlink()
        self.assertIsNone(recovery.restore_approved(task,work,'f'*40))
        review=recovery.restore_approved(task,work,base)
        self.assertEqual(review['reviewer'],'human');self.assertEqual(worker.fingerprint(work),digest)
        self.assertEqual(recovery.reconcile(),[task['id']])
    def test_changed_evidence_invalidates_human_approval(self):
        task=self.task('human_wait');work,base,digest=self.retained(task)
        recovery.record_approval(task['id'],base,digest,'human')
        path=recovery.latest_bundle(task['id']);path.write_text(path.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'changed'):recovery.restore_approved(task,work,base)
    def test_review_digest_distinguishes_file_boundaries_and_executable_modes(self):
        self.assertNotEqual(work_evidence.patch_digest(b'',{'ab':b'c'}),
            work_evidence.patch_digest(b'',{'a':b'bc'}))
        self.assertNotEqual(work_evidence.patch_digest(b'+value  \n',{}),
            work_evidence.patch_digest(b'+value\n',{}))
        task=self.task('human_wait');work,base,digest=self.retained(task)
        (work/'new_test.py').chmod(0o755)
        changed=worker.fingerprint(work);self.assertNotEqual(digest,changed)
        rt.task_state(task['id'],patch_digest=changed);work_evidence.capture(task['id'])
        recovery.record_approval(task['id'],base,changed,'human')
        self.git(work,'restore','value.py');(work/'new_test.py').unlink()
        recovery.restore_approved(task,work,base)
        self.assertTrue((work/'new_test.py').stat().st_mode & 0o100)
        self.assertEqual(worker.fingerprint(work),changed)
    def test_approval_cannot_waive_publication_or_missing_evidence(self):
        task=self.task('error');work,base,digest=self.retained(task)
        with self.assertRaisesRegex(ValueError,'awaiting'):recovery.record_approval(task['id'],base,digest,'human')
    def test_historical_dispatch_tombstone_cannot_block_expired_safe_skip(self):
        task=self.task('skipped','competing work not confirmed',attempts=1)
        with rt.db() as db:db.execute('UPDATE tasks SET updated=?',(time.time()-4*86400,))
        with patch.object(watch,'_dispatched',return_value={'hermes':{'1':{'attempts':2}}}):
            self.assertFalse(watch.already_dispatched('hermes',1))
        self.assertTrue(rt.enqueue('fix','hermes',1,only_new=True))
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)
    def test_ambiguous_skip_never_reenters_queue(self):
        task=self.task('skipped');rt.task_state(task['id'],publication_started=True)
        with rt.db() as db:db.execute('UPDATE tasks SET updated=?',(time.time()-4*86400,))
        self.assertFalse(rt.enqueue('fix','hermes',1,only_new=True))
    def test_failed_successors_are_bounded_preserving_budget(self):
        rt.setmeta('dispatch_budget',{'date':'today','used':{'hermes':10}})
        with patch.object(cloud_runtime,'cloud_enabled',return_value=True):
            self.assertTrue(cloud_runtime.successor(True));self.assertTrue(cloud_runtime.successor(True))
            self.assertTrue(cloud_runtime.successor(True));self.assertFalse(cloud_runtime.successor(True))
        self.assertGreater(rt.getmeta('runner_restart_after'),time.time())
        self.assertEqual(rt.getmeta('dispatch_budget')['used'],{'hermes':10})
    def test_disabled_production_does_not_chain(self):
        with patch.object(cloud_runtime,'cloud_enabled',return_value=False):self.assertFalse(cloud_runtime.successor(True))
        self.assertIsNone(rt.getmeta('runner_restarts'))
    def test_failure_successor_requires_stopped_owner_persisted_state_and_auth(self):
        auth=self.root/'auth.json';auth.write_text('{}');output=self.root/'output'
        order=[]
        dead=SimpleNamespace(poll=lambda:1)
        with patch.object(cloud_runtime,'auth_file',return_value=auth),patch.object(cloud_runtime,'cloud_enabled',return_value=True),\
                patch.object(cloud_store,'restore'),patch.object(cloud_runtime,'requeue_validation_repair'),\
                patch.object(recovery,'reconcile'),patch.object(cloud_runtime.subprocess,'Popen',return_value=dead),\
                patch.object(cloud_runtime,'stop',side_effect=lambda child:order.append('stopped')),\
                patch.object(cloud_store,'save',side_effect=lambda:order.append('state')),\
                patch.object(cloud_runtime,'rotate_auth',side_effect=lambda previous:order.append('auth')),\
                patch.dict(os.environ,{'GITHUB_OUTPUT':str(output)}):
            with self.assertRaisesRegex(RuntimeError,'startup failed'):cloud_runtime.run('live',60)
        self.assertEqual(order,['stopped','state','auth','state'])
        self.assertEqual(output.read_text(),'continue=true\n')
        self.assertEqual(rt.getmeta('runner_failure')['type'],'RuntimeError')
    def test_failed_final_checkpoint_rotates_auth_but_cannot_chain(self):
        auth=self.root/'auth.json';auth.write_text('{}');output=self.root/'output'
        with patch.object(cloud_runtime,'auth_file',return_value=auth),patch.object(cloud_runtime,'cloud_enabled',return_value=True),\
                patch.object(cloud_store,'restore'),patch.object(cloud_runtime,'requeue_validation_repair'),\
                patch.object(recovery,'reconcile'),patch.object(cloud_runtime.subprocess,'Popen',return_value=SimpleNamespace(poll=lambda:1)),\
                patch.object(cloud_store,'save',side_effect=RuntimeError('checkpoint unavailable')),\
                patch.object(cloud_runtime,'rotate_auth') as rotate,\
                patch.dict(os.environ,{'GITHUB_OUTPUT':str(output)}):
            with self.assertRaisesRegex(RuntimeError,'checkpoint unavailable'):cloud_runtime.run('live',60)
            rotate.assert_called_once()
        self.assertFalse(output.exists());self.assertIsNone(rt.getmeta('runner_restarts'))
    def test_human_control_cannot_record_unattested_approval(self):
        with self.assertRaisesRegex(ValueError,'explicitly attest'):
            human_review.approve(1,'a'*40,'b'*64,'human','false')
    def test_canary_imports_only_spending_and_accounts_once_without_overwriting_tasks(self):
        store=self.root/'store';store.mkdir()
        snapshot={'version':1,'tables':{'tasks':[{'id':999,'status':'queued'}],
            'calls':[{'at':time.time()-30,'kind':'codex'}],
            'meta':[{'key':'dispatch_budget','value':'{"used":{"hermes":15}}'}]}}
        path=store/'checkpoint.json';path.write_text(json.dumps(snapshot))
        with patch.dict(os.environ,{'OSS_CLOUD_STATE_DIR':str(store),'GITHUB_RUN_ID':'123'}):
            cloud_store.seed_canary_budget()
            with rt.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
            started=time.time();self.assertTrue(rt.reserve_call('codex')[0])
            with patch.object(cloud_store,'command') as command:
                cloud_store.account_canary(started);raw=path.read_bytes()
                cloud_store.account_canary(started)
                self.assertEqual(command.call_count,3);self.assertEqual(path.read_bytes(),raw)
        saved=json.loads(raw)
        self.assertEqual(saved['tables']['tasks'],snapshot['tables']['tasks'])
        self.assertEqual(saved['tables']['meta'][0],snapshot['tables']['meta'][0])
        self.assertEqual(len(saved['tables']['calls']),2)
        with patch.dict(os.environ,{'OSS_CLOUD_STATE_DIR':str(store),'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2'}),\
                patch.object(cloud_store,'command') as command:
            second=time.time();self.assertTrue(rt.reserve_call('codex')[0])
            cloud_store.account_canary(second);cloud_store.account_canary(second)
            self.assertEqual(command.call_count,3)
        self.assertEqual(len(json.loads(path.read_text())['tables']['calls']),3)
    def test_service_cleanup_runs_after_failures_in_reverse_order(self):
        cleaned=[]
        with self.assertRaisesRegex(RuntimeError,'failed'):
            with validation.session({'id':1}):
                validation._sessions[-1].append(lambda:cleaned.append('docker'))
                validation._sessions[-1].append(lambda:cleaned.append('web'))
                raise RuntimeError('failed')
        self.assertEqual(cleaned,['web','docker']);self.assertEqual(validation._sessions,[])
    def test_browser_failure_is_not_swallowed_or_retried_as_web_startup(self):
        response=SimpleNamespace(status=200)
        class Ready:
            def __enter__(self):return response
            def __exit__(self,*args):return False
        web=SimpleNamespace(poll=lambda:None)
        with patch.object(validation.urllib.request,'urlopen',return_value=Ready()),\
                patch.object(validation,'check_browser',side_effect=RuntimeError('browser failed')) as browser,\
                patch.object(validation.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'browser failed'):
                validation.wait_for_web(web,self.root,self.root,io.StringIO())
            browser.assert_called_once();sleep.assert_not_called()
    def test_synthetic_setup_cannot_inherit_any_controller_secrets(self):
        with patch.dict(os.environ,{'GH_TOKEN':'secret','CODEX_AUTH_JSON':'secret','CUSTOM_SECRET':'secret'}):
            env=validation.safe_env(self.root/'cache')
        self.assertNotIn('secret',env.values())
        spec=validation.compose_spec()
        self.assertEqual(set(spec['services']),{'postgres','clickhouse','redis','minio'})
        for service in spec['services'].values():
            self.assertTrue(all(port.startswith('127.0.0.1:') for port in service['ports']))
            self.assertTrue(all(not mount.startswith('/') for mount in service.get('volumes',[])))
        self.assertTrue(all(not volume for volume in spec['volumes'].values()))
        cmd=validation.sandbox(self.root/'work',self.root/'cache',['node','--version'])
        self.assertIn(['--tmpfs','/run'],[cmd[i:i+2] for i in range(len(cmd)-1)])
        self.assertNotIn('/var/run/docker.sock',cmd)

if __name__=='__main__':unittest.main()
