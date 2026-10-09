import concurrent.futures
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
from unittest.mock import patch, Mock
import runtime as rt
import codex_worker as worker
import intent
import watch
import scan
import cloud_state
import cloud_runtime
import cloud_store
import cloud_login
import base64
import work_evidence

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),patch.object(rt,'CONFIG',self.root/'runtime.json'),
                      patch.dict('os.environ',{'OSS_ARTIFACT_KEY':base64.b64encode(b'x'*32).decode()})]
        for p in self.patches:p.start()
        self.cfg={'backend':'codex-local','enabled':True,'max_pending':8,'judge_requests_per_hour':3,'codex_sessions_per_5h':4}
        self.save()
        rt.setmeta('health',{'ok':True,'at':time.time()})
        rt.setmeta('worker_heartbeat',time.time())
    def save(self):rt.CONFIG.write_text(json.dumps(self.cfg))
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_disabled_or_dead_worker_blocks_judge_before_network(self):
        with patch.object(intent,'_load_key',side_effect=AssertionError('credential touched')):
            self.cfg['enabled']=False;self.save()
            with self.assertRaises(rt.Paused):intent._ask('x','y')
            self.cfg['enabled']=True;self.save();rt.setmeta('worker_heartbeat',0)
            with self.assertRaises(rt.Paused):intent._ask('x','y')
    def test_each_http_fallback_consumes_budget(self):
        for _ in range(3):self.assertTrue(rt.reserve_call('judge')[0])
        with patch.object(intent.urllib.request,'urlopen',side_effect=AssertionError('network called')):
            with self.assertRaises(rt.Paused):intent._ask_one('model','system','user','fake',author='test')
    def test_concurrent_enqueue_unique_and_bounded(self):
        def put(n):return rt.enqueue('fix','hermes',n)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(put,[1]*12+list(range(2,22))))
        with rt.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],7)
            self.assertEqual(c.execute('SELECT count(*) FROM tasks WHERE number=1').fetchone()[0],1)
    def test_response_event_dedup_and_new_feedback(self):
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',1,'event1'))
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',1,'event1'))
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',1,'event2'))
        with rt.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],2)
    def test_full_fix_queue_still_admits_feedback(self):
        for n in range(7):self.assertTrue(rt.enqueue('fix','hermes',n))
        self.assertFalse(rt.room()[0])
        self.assertFalse(rt.enqueue('fix','adk',99))
        self.assertTrue(rt.room('respond')[0])
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',77,'review-event'))
        self.assertFalse(rt.room('respond')[0])
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',77,'review-event'))
    def test_issue_author_claim_and_coordination_rejected_before_coding(self):
        issue={'number':9,'title':'A real bug','body':'A detailed reproducible report. '*6,
               'labels':[],'assignees':[],'user':{'login':'reporter'}}
        with patch.object(scan,'linked_prs',return_value=[]),patch.object(intent,'is_claim',return_value=(True,'offered fix')),patch.object(scan,'claimants',side_effect=AssertionError('unnecessary API')):
            self.assertIn('issue author',scan.vet({},'o/r',issue)[1])
        with patch.object(scan,'linked_prs',side_effect=AssertionError('unnecessary API')):
            self.assertIn('coordination',scan.vet({'announce_before_work':True},'o/r',issue)[1])
    def test_langfuse_triage_assignment_is_not_a_claim(self):
        issue={'number':9,'title':'A real bug','body':'A detailed reproducible report. '*6,
               'labels':[],'assignees':[{'login':'maintainer'}]}
        with patch.object(scan,'linked_prs',return_value=[]),patch.object(scan,'claimants',return_value=[]):
            self.assertTrue(scan.vet({'ignore_assignees':True},'o/r',issue)[0])
            self.assertFalse(scan.vet({},'o/r',issue)[0])
    def test_circuit_stops_dispatch_without_cloud_fallback(self):
        rt.pause('auth revoked')
        with patch.object(watch.subprocess,'Popen',side_effect=AssertionError('fallback started')):
            self.assertFalse(watch.trigger_fix('hermes',42))
        self.assertFalse(rt.enqueue('fix','hermes',42))
        self.assertFalse(rt.reserve_call('judge')[0])
    def test_dispatch_uses_exact_candidate_and_no_workflow(self):
        with patch.object(watch,'record_dispatch') as record,patch.object(watch.subprocess,'run',side_effect=AssertionError('cloud invoked')):
            self.assertTrue(watch.dispatch_fix('hermes',123))
            record.assert_called_once_with('hermes',123)
        with rt.db() as c:self.assertEqual(c.execute('SELECT number FROM tasks').fetchone()[0],123)
    def test_repeated_fix_detection_does_not_charge_dispatch_twice(self):
        with patch.object(watch,'record_dispatch'),patch.object(watch,'budget_allows',return_value=True),patch.object(watch,'budget_charge') as charge:
            self.assertTrue(watch.trigger_fix('hermes',123))
            self.assertFalse(watch.trigger_fix('hermes',123))
            charge.assert_called_once_with('hermes')
    def test_failure_is_not_replayed(self):
        rt.enqueue('fix','hermes',1)
        with rt.db() as c:c.execute("UPDATE tasks SET status='error'")
        self.assertFalse(rt.enqueue('fix','hermes',1))
    def test_pr_permission_denial_holds_only_that_repo_without_losing_queue(self):
        denial='GraphQL: chelsealong does not have the correct permissions to execute `CreatePullRequest`'
        self.assertTrue(worker.repo_pr_permission_denied({'kind':'fix'},denial))
        self.assertFalse(worker.repo_pr_permission_denied({'kind':'respond'},denial))
        self.assertFalse(worker.repo_pr_permission_denied({'kind':'fix'},'HTTP 403 from another endpoint'))
        self.assertTrue(rt.enqueue('fix','hermes',1))
        with rt.db() as c:
            c.execute("UPDATE tasks SET status='error', result=?, updated=? WHERE repo='hermes'",
                      (denial,time.time()))
        self.assertFalse(rt.enqueue('fix','hermes',2))
        self.assertTrue(rt.enqueue('fix','openclaw',3))
        with rt.db() as c:
            task=worker.next_queued(c,rt.publication_holds())
        self.assertEqual((task['repo'],task['number']),('openclaw',3))
        with rt.db() as c:c.execute("UPDATE tasks SET updated=? WHERE repo='hermes'",(time.time()-86401,))
        self.assertFalse(rt.enqueue('fix','hermes',2))
    def test_bad_config_fails_closed(self):
        rt.CONFIG.write_text('{')
        self.assertFalse(rt.ready()[0])
        with patch.object(intent,'_load_key',side_effect=AssertionError('credential touched')):
            with self.assertRaises(rt.Paused):intent._ask('x','y')
    def test_coding_budget_also_stops_judge(self):
        for _ in range(4):self.assertTrue(rt.reserve_call('codex')[0])
        self.assertFalse(rt.ready()[0])
        self.assertFalse(rt.reserve_call('judge')[0])
    def test_cloud_auth_rejects_api_billing(self):
        for value in [{'auth_mode':'apikey','OPENAI_API_KEY':'fake'},
                      {'auth_mode':'chatgpt','tokens':{}},
                      {'auth_mode':'chatgpt','OPENAI_API_KEY':'fake','tokens':{'refresh_token':'fake'}}]:
            with self.assertRaises(RuntimeError):cloud_runtime.validate_auth(json.dumps(value))
        cloud_runtime.validate_auth(json.dumps({'auth_mode':'chatgpt','tokens':{'refresh_token':'fake'}}))
    def test_failed_restore_never_overwrites_cloud_checkpoint(self):
        auth=self.root/'auth.json';auth.write_text('{}')
        with patch.object(cloud_runtime,'auth_file',return_value=auth),patch.object(cloud_runtime,'cloud_enabled',return_value=True),patch.object(cloud_store,'restore',side_effect=RuntimeError('restore failed')),patch.object(cloud_store,'save') as save,patch.object(cloud_runtime,'rotate_auth') as rotate:
            with self.assertRaisesRegex(RuntimeError,'restore failed'):
                cloud_runtime.run('live',60)
            save.assert_not_called()
            rotate.assert_called_once()
    def test_cloud_control_retries_reads_without_replaying_secret_writes(self):
        failed=Mock(returncode=1,stderr='TLS handshake timeout',stdout='')
        succeeded=Mock(returncode=0,stderr='',stdout='true')
        with patch.object(cloud_runtime.subprocess,'run',side_effect=[failed,succeeded]) as call,patch.object(cloud_runtime.time,'sleep'):
            self.assertTrue(cloud_runtime.cloud_enabled())
            self.assertEqual(call.call_count,2)
        with patch.object(cloud_runtime.subprocess,'run',return_value=failed) as call:
            with self.assertRaises(RuntimeError):cloud_runtime.gh(['secret','set','CODEX_AUTH_JSON','--repo',cloud_runtime.REPOSITORY],input='not-a-secret')
            call.assert_called_once()
    def test_recovery_requeues_only_audited_blocks_once_and_defers_held_repo(self):
        self.cfg['backend']='codex-cloud';self.save()
        cases=[(316,'respond','langfuse/langfuse','ERR_PNPM_STORE_DIR_OPEN_OPERATION_LOCK','blocked'),
               (244,'respond','NousResearch/hermes-agent','/var/tmp/hermes-pytest-1001','blocked'),
               (219,'respond','openclaw/openclaw',"'git', 'fetch', 'origin' timed out after 300 seconds",'error'),
               (267,'respond','openclaw/openclaw',"'git', 'fetch', 'origin' timed out after 300 seconds",'error'),
               (295,'fix','langfuse','Rust cache is read-only','blocked'),
               (206,'fix','hermes','/var/tmp/hermes-pytest-1001','blocked'),
               (237,'fix','hermes','correct permissions to execute `CreatePullRequest`','error'),
               (256,'fix','comfyui','aiohttp is missing','done')]
        with rt.db() as c:
            for task_id,kind,repo,result,status in cases:
                c.execute('INSERT INTO tasks(id,identity,kind,repo,number,status,created,updated,result,attempts) VALUES (?,?,?,?,?,?,?,?,?,?)',
                          (task_id,f'test:{task_id}',kind,repo,task_id,status,time.time(),time.time(),result,1))
        with patch.object(rt,'checkpoint'):
            self.assertEqual(cloud_runtime.requeue_validation_repair(),[316,244,267,295])
            with rt.db() as c:c.execute("UPDATE tasks SET status='blocked' WHERE id=316")
            self.assertEqual(cloud_runtime.requeue_validation_repair(),[])
            with rt.db() as c:c.execute('UPDATE tasks SET updated=? WHERE id=237',(time.time()-86401,))
            self.assertEqual(cloud_runtime.requeue_validation_repair(),[])
            with rt.db() as c:
                denied=c.execute('SELECT updated FROM tasks WHERE id=237').fetchone()[0]
            rt.setmeta('publication_clearance:hermes',{'denied_at':denied,'proof':{'number':999}})
            self.assertEqual(cloud_runtime.requeue_validation_repair(),[206])
        with rt.db() as c:
            self.assertEqual(c.execute('SELECT status FROM tasks WHERE id=256').fetchone()[0],'done')
            self.assertEqual(c.execute('SELECT status FROM tasks WHERE id=219').fetchone()[0],'error')
        self.assertEqual(rt.getmeta(cloud_runtime.RETRY_MARKER),[206,244,267,295,316])
    def test_audited_disconnect_recovery_respects_attempts_and_publication_markers(self):
        self.cfg['backend']='codex-cloud';self.save()
        reason='Codex: host connection ended without verified completion'
        with rt.db() as c:
            for task_id,attempts in [(605,2),(609,3)]:
                c.execute('INSERT INTO tasks(id,identity,kind,repo,number,status,created,updated,result,attempts) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (task_id,f'test:{task_id}','fix','langfuse' if task_id==605 else 'openclaw',task_id,
                     'error',time.time(),time.time(),reason,attempts))
        rt.task_state(605,publication_started=True)
        with patch.object(rt,'checkpoint'):
            self.assertEqual(cloud_runtime.requeue_validation_repair(),[])
            rt.task_state(605,publication_started=False)
            self.assertEqual(cloud_runtime.requeue_validation_repair(),[605])
            self.assertEqual(cloud_runtime.requeue_validation_repair(),[])

    def test_cloud_login_only_publishes_encrypted_device_code(self):
        private=self.root/'private.pem';public=self.root/'public.pem'
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(private)],capture_output=True,check=True)
        key=subprocess.run(['openssl','pkey','-in',str(private),'-pubout'],capture_output=True,check=True).stdout.decode()
        with patch.dict('os.environ',{'CODEX_LOGIN_PUBLIC_KEY':key}),patch.object(cloud_runtime,'gh') as call:
            self.assertFalse(cloud_login.publish_challenge('Starting...',public,'123'))
            call.assert_not_called()
            self.assertTrue(cloud_login.publish_challenge('\x1b[94m8ABC-DE123\x1b[0m',public,'123'))
            sent=call.call_args.args[0][-1]
            self.assertNotIn('8ABC-DE123',sent)
            encrypted=base64.b64decode(json.loads(sent)['encrypted_code'])
            value=subprocess.run(['openssl','pkeyutl','-decrypt','-inkey',str(private),'-pkeyopt','rsa_padding_mode:oaep','-pkeyopt','rsa_oaep_md:sha256'],input=encrypted,capture_output=True,check=True).stdout
            self.assertEqual(value,b'8ABC-DE123')
    def test_cloud_reservation_must_persist_before_model_request(self):
        self.cfg['backend']='codex-cloud';self.save()
        with patch('cloud_store.save',side_effect=RuntimeError('checkpoint unavailable')):
            with self.assertRaises(RuntimeError):rt.reserve_call('judge')
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
    def test_network_error_does_not_pause_all_repos_for_half_hour(self):
        self.assertEqual(worker.failure_cooldown('Recv failure: Connection reset by peer'),0)
        self.assertEqual(worker.failure_cooldown(subprocess.TimeoutExpired(['git','fetch'],300)),0)
        self.assertEqual(worker.failure_cooldown('403 auth unavailable'),1800)
        self.assertEqual(worker.failure_cooldown('quota exceeded'),1800)
    def test_failed_claim_query_does_not_mean_unclaimed(self):
        with patch.object(scan,'gh',side_effect=RuntimeError('API unavailable')):
            with self.assertRaises(RuntimeError):scan.claimants('owner/repo',1)
    def test_claimants_reads_all_pages_without_incompatible_gh_flags(self):
        pages=[[{'user':{'login':'reader'},'body':'I see the issue','created_at':'2026-10-01T00:00:00Z'}],
               [{'user':{'login':'fixer'},'body':'I am fixing this','created_at':'2026-10-01T00:00:00Z'}]]
        def fake_gh(args):
            self.assertIn('--paginate',args);self.assertIn('--slurp',args);self.assertNotIn('--jq',args)
            return json.dumps(pages)
        with patch.object(scan,'gh',side_effect=fake_gh),patch.object(intent,'is_claim',side_effect=[(False,'reader'),(True,'claimed')]),patch.object(scan,'_claim_went_cold',return_value=False):
            self.assertEqual(scan.claimants('owner/repo',1),['fixer'])
    def test_scope_blocks_auth_and_manifests(self):
        for filename in ['src/auth/token.py','package.json','.github/workflows/test.yml']:
            with patch.object(worker,'git',side_effect=[filename,'']):
                with self.assertRaises(RuntimeError):worker.scope_check(self.root,'hermes')
    def test_review_requires_typed_explicit_verdict(self):
        for review in [{'verdict':'APPROVE','reason':'ok','tests_verified':'false'},
                       {'verdict':'APPROVE','reason':'ok'},
                       {'verdict':'maybe','reason':'ok','tests_verified':True}]:
            with self.assertRaises(RuntimeError):worker.validate_result(review,worker.REVIEW_SCHEMA)
        valid={'verdict':'APPROVE','reason':'tests ran','tests_verified':True,'repairable':False}
        self.assertEqual(worker.validate_result(valid,worker.REVIEW_SCHEMA),valid)
    def test_health_probe_tolerates_sol_punctuation_but_not_other_text(self):
        for value in ['OSS_CODEX_READY','OSS_CODEX_READY.']:
            with patch.object(worker,'agent',return_value=value):worker.healthcheck()
        with patch.object(worker,'agent',return_value='NOT OSS_CODEX_READY'):
            with self.assertRaises(RuntimeError):worker.healthcheck()
    def test_assistant_approval_without_turn_completion_is_not_success(self):
        output=self.root/'review.json'
        fake=Mock(pid=123456789,returncode=-15)
        fake.communicate.side_effect=subprocess.TimeoutExpired('fake-codex',0)
        def spawn(*args,**kwargs):
            self.assertEqual(kwargs['env'].get('GH_TOKEN'),'fake-read-only-token')
            self.assertNotIn('QWEN_API_KEY',kwargs['env'])
            self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
            kwargs['stdout'].write(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'verdict':'APPROVE','reason':'ok','tests_verified':True})}})+'\n')
            kwargs['stdout'].flush()
            return fake
        with patch.dict('os.environ',{'GH_TOKEN':'fake-controller-token','OSS_READONLY_GH_TOKEN':'fake-read-only-token','QWEN_API_KEY':'fake','OPENAI_API_KEY':'fake'}),patch.object(worker,'binary',return_value='/bin/true'),patch.object(worker.subprocess,'Popen',side_effect=spawn),patch.object(worker.os,'killpg'):
            with self.assertRaises(subprocess.TimeoutExpired):
                worker.agent('test',self.root,output,worker.REVIEW_SCHEMA,timeout=0)
        self.assertFalse(output.exists())
    def test_cloud_agent_keeps_tool_caches_outside_checkout(self):
        self.cfg['backend']='codex-cloud';self.save()
        output=self.root/'job'/'generation.json'
        with patch.object(rt,'reserve_call',return_value=(True,'')),patch.object(worker,'binary',return_value='/bin/true'),patch.object(worker.subprocess,'Popen',side_effect=RuntimeError('captured')) as spawn:
            with self.assertRaisesRegex(RuntimeError,'captured'):
                worker.agent('test',self.root/'job'/'work',output,repo_key='langfuse')
        args=spawn.call_args.args[0]
        cache=output.parent/'tool-cache'
        self.assertIn(str(cache),args)
        self.assertNotIn('danger-full-access',args)
        env=spawn.call_args.kwargs['env']
        self.assertEqual(env['OSS_TASK_CACHE'],str(cache))
        self.assertEqual(env['COREPACK_HOME'],str(cache/'corepack'))
        self.assertEqual(env['CARGO_HOME'],str(cache/'cargo'))
        self.assertEqual(env['RUSTUP_HOME'],str(cache/'rustup'))
        self.assertNotIn('CODEX_AUTH_JSON',env)
        self.assertNotIn('OSS_ARTIFACT_KEY',env)
    def test_transient_read_retries_but_publish_does_not(self):
        fail=Mock(returncode=1,stderr='TLS handshake timeout',stdout='')
        ok=Mock(returncode=0,stderr='',stdout='{}')
        with patch.object(worker.subprocess,'run',side_effect=[fail,ok]) as call,patch.object(worker.time,'sleep'):
            self.assertEqual(worker.run(['gh','api','repos/o/r']),'{}')
            self.assertEqual(call.call_count,2)
        with patch.object(worker.subprocess,'run',return_value=fail) as call:
            with self.assertRaises(RuntimeError):worker.run(['gh','pr','create','--repo','o/r'])
            self.assertEqual(call.call_count,1)
    def test_graphql_timeout_retries_query_not_mutation(self):
        ok=Mock(returncode=0,stdout='{}')
        with patch.object(scan,'_pace'),patch.object(scan.subprocess,'run',side_effect=[subprocess.TimeoutExpired('gh',60),ok]) as call,patch('time.sleep'):
            self.assertEqual(scan.gh(['api','graphql','-f','query={viewer{login}}']),'{}')
            self.assertEqual(call.call_count,2)
        with patch.object(scan,'_pace'),patch.object(scan.subprocess,'run',side_effect=subprocess.TimeoutExpired('gh',60)) as call:
            with self.assertRaises(subprocess.TimeoutExpired):scan.gh(['api','graphql','-f','query=mutation { write }'])
            self.assertEqual(call.call_count,1)
    def test_cloud_checkpoint_preserves_limits_and_never_replays_running_job(self):
        self.assertTrue(rt.enqueue('fix','hermes',42))
        self.assertTrue(rt.reserve_call('judge')[0])
        with rt.db() as db:db.execute("UPDATE tasks SET status='running'")
        target=self.root/'cloud.json'
        cloud_state.export_state(target)
        with self.assertRaises(RuntimeError):cloud_state.import_state(target)
        (rt.DATA/'queue.sqlite3').unlink()
        cloud_state.import_state(target)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'interrupted')
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
        self.assertFalse(rt.ready()[0])
        self.assertIsNone(rt.getmeta('worker_heartbeat'))
    def test_cloud_store_pushes_state_without_deployment_credentials(self):
        store=self.root/'store';remote=self.root/'remote.git';store.mkdir()
        def git(*args,cwd=None):
            return subprocess.run(['git',*args],cwd=cwd,capture_output=True,text=True,check=True).stdout
        git('init','--bare',str(remote));git('init','-b','codex-state',cwd=store)
        git('config','user.name','Test',cwd=store);git('config','user.email','test@example.invalid',cwd=store)
        git('remote','add','origin',str(remote),cwd=store)
        (self.root/'state').mkdir();(self.root/'queue').mkdir()
        (self.root/'state/runtime.json').write_text('{"credential":"DO_NOT_EXPORT"}')
        (self.root/'state/seen.json').write_text('{"hermes":[1]}')
        self.cfg['backend']='codex-cloud';self.save()
        with patch.object(rt,'ROOT',self.root),patch.dict('os.environ',{'OSS_CLOUD_STATE_DIR':str(store)}):
            cloud_store.save()
        files=git('--git-dir',str(remote),'ls-tree','-r','--name-only','codex-state')
        self.assertIn('checkpoint.json',files);self.assertIn('state/seen.json',files)
        self.assertNotIn('runtime.json',files)
        self.assertNotIn('DO_NOT_EXPORT',(store/'checkpoint.json').read_text())

if __name__=='__main__':unittest.main()
