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

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patches=[patch.object(rt,'DATA',self.root/'data'),patch.object(rt,'CONFIG',self.root/'runtime.json')]
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
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],8)
            self.assertEqual(c.execute('SELECT count(*) FROM tasks WHERE number=1').fetchone()[0],1)
    def test_response_event_dedup_and_new_feedback(self):
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',1,'event1'))
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',1,'event1'))
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',1,'event2'))
        with rt.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],2)
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
    def test_failure_is_not_replayed(self):
        rt.enqueue('fix','hermes',1)
        with rt.db() as c:c.execute("UPDATE tasks SET status='error'")
        self.assertFalse(rt.enqueue('fix','hermes',1))
    def test_bad_config_fails_closed(self):
        rt.CONFIG.write_text('{')
        self.assertFalse(rt.ready()[0])
        with patch.object(intent,'_load_key',side_effect=AssertionError('credential touched')):
            with self.assertRaises(rt.Paused):intent._ask('x','y')
    def test_coding_budget_also_stops_judge(self):
        for _ in range(4):self.assertTrue(rt.reserve_call('codex')[0])
        self.assertFalse(rt.ready()[0])
        self.assertFalse(rt.reserve_call('judge')[0])
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
        valid={'verdict':'APPROVE','reason':'tests ran','tests_verified':True}
        self.assertEqual(worker.validate_result(valid,worker.REVIEW_SCHEMA),valid)
    def test_assistant_approval_without_turn_completion_is_not_success(self):
        output=self.root/'review.json'
        fake=Mock(pid=123456789,returncode=-15)
        fake.communicate.side_effect=subprocess.TimeoutExpired('fake-codex',0)
        def spawn(*args,**kwargs):
            kwargs['stdout'].write(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'verdict':'APPROVE','reason':'ok','tests_verified':True})}})+'\n')
            kwargs['stdout'].flush()
            return fake
        with patch.object(worker,'binary',return_value='/bin/true'),patch.object(worker.subprocess,'Popen',side_effect=spawn),patch.object(worker.os,'killpg'):
            with self.assertRaises(subprocess.TimeoutExpired):
                worker.agent('test',self.root,output,worker.REVIEW_SCHEMA,timeout=0)
        self.assertFalse(output.exists())

if __name__=='__main__':unittest.main()
