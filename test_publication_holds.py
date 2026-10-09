"""An expired permission timer cannot spend coding calls without clearance."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import runtime as rt
import codex_worker as worker
import task_recovery as recovery


class PublicationHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
            patch.object(rt,'CONFIG',self.root/'runtime.json')]
        for p in self.patches:p.start()
        rt.CONFIG.write_text(json.dumps({'backend':'codex-local','enabled':True,
            'codex_sessions_per_5h':100}))
        rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
        self.assertTrue(rt.enqueue('fix','hermes',1))
        self.denied=time.time()-86400*2
        with rt.db() as db:
            db.execute("UPDATE tasks SET status='error',result=?,updated=?",
                ('GraphQL: chelsealong does not have the correct permissions to execute `CreatePullRequest`',self.denied))

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def pr(self,at,draft=False):
        return {'number':2,'createdAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(at)),
            'isDraft':draft,'url':'https://github.com/NousResearch/hermes-agent/pull/2'}

    def test_expired_denial_still_holds_fixes_but_not_other_repos_or_responses(self):
        self.assertFalse(rt.enqueue('fix','hermes',2))
        self.assertTrue(rt.enqueue('fix','openclaw',3))
        self.assertTrue(rt.enqueue('respond','NousResearch/hermes-agent',4,'feedback'))
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)

    def test_old_ordinary_pr_or_new_draft_is_not_clearance(self):
        prs=[self.pr(self.denied-60),self.pr(self.denied+60,True)]
        with patch.object(worker,'run',return_value=json.dumps(prs)):
            self.assertEqual(recovery.refresh_publication_holds(),[])
        self.assertFalse(rt.enqueue('fix','hermes',2))

    def test_new_ordinary_pr_clears_only_its_denial_and_later_denial_reholds(self):
        with patch.object(worker,'run',side_effect=[json.dumps([self.pr(self.denied+60)]),'[[]]']):
            self.assertEqual(recovery.refresh_publication_holds(),['hermes'])
        self.assertTrue(rt.enqueue('fix','hermes',2))
        with rt.db() as db:
            db.execute("UPDATE tasks SET status='error',result=?,updated=? WHERE number=2",
                ('does not have the correct permissions to execute `CreatePullRequest`',time.time()))
        self.assertIn('hermes',rt.publication_holds())

    def test_formerly_draft_pr_is_not_proof_of_ordinary_creation(self):
        with patch.object(worker,'run',side_effect=[json.dumps([self.pr(self.denied+60)]),
                '[[{"event":"ready_for_review"}]]']):
            self.assertEqual(recovery.refresh_publication_holds(),[])
        self.assertIn('hermes',rt.publication_holds())

    def test_incomplete_timeline_keeps_hold(self):
        with patch.object(worker,'run',side_effect=[json.dumps([self.pr(self.denied+60)]),
                RuntimeError('timeline unavailable')]):
            self.assertEqual(recovery.refresh_publication_holds(),[])
        self.assertIn('hermes',rt.publication_holds())

    def test_read_failure_retains_hold_and_is_throttled_without_model_or_write(self):
        with patch.object(worker,'run',side_effect=RuntimeError('HTTP 403 Forbidden')) as read:
            self.assertEqual(recovery.refresh_publication_holds(),[])
            self.assertEqual(recovery.refresh_publication_holds(),[])
            read.assert_called_once()
            self.assertEqual(read.call_args.args[0][:3],['gh','pr','list'])
        self.assertIn('hermes',rt.publication_holds())


if __name__=='__main__':unittest.main()
