"""Explicit real-PR retries preserve denial protection, spending and review outcomes."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import creation_recovery as recovery
import codex_worker as worker
import cloud_runtime
import runtime as rt


class CreationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        for key,value in [('ROOT',root),('DATA',root/'data'),('CONFIG',root/'config.json')]:
            self.enterContext(patch.object(rt,key,value))
        self.enterContext(patch.object(rt,'checkpoint'))
        self.enterContext(patch.object(worker,'checkpoint_claim'))
        self.enterContext(patch.dict('os.environ',{'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ID':'retry-test'}))
        rt.CONFIG.write_text(json.dumps({'backend':'codex-cloud','enabled':True,'max_pending':8,
                                        'codex_sessions_per_5h':45}))
        self.enterContext(patch.object(worker,'healthcheck',side_effect=lambda:
            rt.setmeta('health',{'ok':True,'at':time.time()})))
        now=time.time()
        with rt.db() as db:
            db.execute('INSERT INTO tasks(identity,kind,repo,number,status,created,updated,result,attempts) '
                'VALUES (?,?,?,?,?,?,?,?,?)',('old-denial','fix','hermes',1,'error',now-86400,now-86400,
                'does not have the correct permissions to execute `CreatePullRequest`',1))
            db.execute("INSERT INTO calls VALUES (?,'codex')",(now,))
        rt.setmeta('dispatch_budget',{'date':time.strftime('%Y-%m-%d',time.gmtime()),'used':{'hermes':2}})

    def processing(self,status,result):
        def process(task):
            self.assertIn('hermes',rt.publication_holds())
            self.assertTrue(rt.task_state(task['id']).get('creation_retry_request'))
            self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0])
            self.assertTrue(rt.reserve_call('codex',task=task,phase='review')[0])
            worker.finish(task,status,result)
        return process

    def test_new_denial_does_not_clear_hold_or_reset_spending_or_replay(self):
        denied='does not have the correct permissions to execute `CreatePullRequest`'
        with patch.object(worker,'process',side_effect=self.processing('error',denied)), \
             patch.object(recovery.task_recovery,'refresh_publication_holds') as refresh:
            with self.assertRaisesRegex(RuntimeError,'did not publish'):recovery.run(148449)
        refresh.assert_not_called()
        self.assertIn('hermes',rt.publication_holds())
        self.assertEqual(rt.getmeta('dispatch_budget')['used']['hermes'],3)
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],3)
        with patch.object(worker,'healthcheck') as health:
            with self.assertRaisesRegex(RuntimeError,'already attempted'):recovery.run(148449)
            health.assert_not_called()
        self.assertFalse(rt.enqueue('fix','hermes',148450))

    def test_real_ordinary_pr_is_required_for_remote_clearance(self):
        url='https://github.com/NousResearch/hermes-agent/pull/999999'
        ordinary={'number':999999,'isDraft':False,'url':url,
                  'createdAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        with patch.object(worker,'process',side_effect=self.processing('done',url)), \
             patch.object(worker,'run',side_effect=[json.dumps([ordinary]),'[[]]']):
            result=recovery.run(148449)
        self.assertEqual(result['result'],url)
        self.assertNotIn('hermes',rt.publication_holds())
        self.assertEqual(rt.getmeta('publication_clearance:hermes')['proof'],ordinary)
        self.assertEqual(rt.getmeta('dispatch_budget')['used']['hermes'],3)

    def test_blocked_review_cannot_publish_or_clear_creation_hold(self):
        with patch.object(worker,'process',side_effect=self.processing('blocked','review: regression fails')), \
             patch.object(recovery.task_recovery,'refresh_publication_holds') as refresh:
            with self.assertRaisesRegex(RuntimeError,'did not publish'):recovery.run(148449)
        refresh.assert_not_called()
        self.assertIn('hermes',rt.publication_holds())

    def test_schedule_cannot_request_an_exception(self):
        with patch.dict('os.environ',{'GITHUB_EVENT_NAME':'schedule'}),patch.object(worker,'process') as process:
            with self.assertRaisesRegex(RuntimeError,'explicit workflow dispatch'):recovery.run(148449)
            process.assert_not_called()

    def test_publication_requires_successful_preflight_and_actual_canary_on_same_main(self):
        good={'head_sha':'candidate','status':'completed','conclusion':'success'}
        with patch.dict('os.environ',{'GITHUB_SHA':'candidate'}), \
             patch.object(cloud_runtime,'gh',side_effect=[
                 '{"object":{"sha":"candidate"}}',json.dumps({'workflow_runs':[good]}),
                 json.dumps({'workflow_runs':[good|{'display_title':'Codex cloud live'}]})]):
            with self.assertRaisesRegex(RuntimeError,'codex-cloud.yml'):cloud_runtime.verified_creation_recovery()
        with patch.dict('os.environ',{'GITHUB_SHA':'candidate'}), \
             patch.object(cloud_runtime,'gh',side_effect=[
                 '{"object":{"sha":"candidate"}}',json.dumps({'workflow_runs':[good]}),
                 json.dumps({'workflow_runs':[good|{'display_title':'Codex cloud canary'}]})]):
            cloud_runtime.verified_creation_recovery()

    def test_changed_main_cannot_publish_from_stale_validation(self):
        with patch.dict('os.environ',{'GITHUB_SHA':'candidate'}), \
             patch.object(cloud_runtime,'gh',return_value='{"object":{"sha":"new-main"}}'):
            with self.assertRaisesRegex(RuntimeError,'not current main'):cloud_runtime.verified_creation_recovery()


if __name__=='__main__':unittest.main()
