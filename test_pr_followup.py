"""Reply writes, review gates, ambiguity and backlog must survive restarts."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch
import codex_worker as worker
import pr_followup as replies
import runtime as rt


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
                      patch.object(rt,'CONFIG',self.root/'runtime.json')]
        for p in self.patches:p.start()
        self.cfg={'backend':'codex-local','enabled':True,'public_pr_replies':True,
                  'max_pending':8,'codex_sessions_per_5h':100}
        self.save();rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
        self.task={'id':1,'kind':'respond','repo':'langfuse/langfuse','number':17876,
                   'identity':'respond:langfuse/langfuse:17876:events','note':'{}'}
        self.context={'head':{'sha':'a'*40}}
    def save(self):rt.CONFIG.write_text(json.dumps(self.cfg))
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def publish(self):return replies.publish(self.task,'Verified answer.','a'*40)
    def test_comment_once_and_restored_state_does_not_post_twice(self):
        with patch.object(replies,'comments',return_value=[]),patch.object(worker,'response_context',return_value=self.context),patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"html_url":"https://github.com/example/comment"}')) as post:
            self.assertEqual(self.publish(),self.publish());self.assertEqual(post.call_count,1)
            payload=json.loads(post.call_args.kwargs['input'])
            self.assertIn('<!-- oss-followup:',payload['body'])
    def test_timeout_write_never_blindly_reposts(self):
        with patch.object(replies,'comments',return_value=[]),patch.object(worker,'response_context',return_value=self.context),patch('subprocess.run',side_effect=TimeoutError('ambiguous')) as post:
            with self.assertRaises(TimeoutError):self.publish()
            with self.assertRaisesRegex(rt.Paused,'Ambiguous'):self.publish()
            self.assertEqual(post.call_count,1)
    def test_ambiguous_write_reconciles_remote_marker(self):
        with patch.object(replies,'comments',return_value=[]),patch.object(worker,'response_context',return_value=self.context),patch('subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='')):
            with self.assertRaises(RuntimeError):self.publish()
        import hashlib
        marker='<!-- oss-followup:'+hashlib.sha256(self.task['identity'].encode()).hexdigest()[:24]+' -->'
        with patch.object(replies,'comments',return_value=[{'user':{'login':'chelsealong'},'body':marker,'html_url':'remote'}]),patch('subprocess.run') as post:
            self.assertEqual(self.publish(),'remote');post.assert_not_called()
    def test_checkpoint_failure_prevents_post(self):
        with patch.object(replies,'comments',return_value=[]),patch.object(worker,'response_context',return_value=self.context),patch.object(rt,'checkpoint',side_effect=rt.PersistenceError()),patch('subprocess.run') as post:
            with self.assertRaises(rt.PersistenceError):self.publish()
            post.assert_not_called()
    def test_moved_head_and_disabled_replies_prevent_post(self):
        with patch.object(replies,'comments',return_value=[]),patch.object(worker,'response_context',return_value={'head':{'sha':'b'*40}}),patch('subprocess.run') as post:
            with self.assertRaisesRegex(rt.Paused,'head moved'):self.publish()
            post.assert_not_called()
        self.cfg['public_pr_replies']=False;self.save()
        with patch.object(replies,'comments') as read:
            with self.assertRaises(rt.Paused):self.publish()
            read.assert_not_called()
    def test_reply_budget_survives_new_event_ids(self):
        with patch.object(replies,'comments',return_value=[]),patch.object(worker,'response_context',return_value=self.context),patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"html_url":"remote"}')) as post:
            for i in range(3):self.task['identity']=f'event{i}';self.publish()
            self.task['identity']='event3'
            with self.assertRaisesRegex(rt.Paused,'budget'):self.publish()
            self.assertEqual(post.call_count,3)
    def test_reply_reviewer_rejection_or_mutation_prevents_publication(self):
        work=self.root/'work';work.mkdir();folder=self.root/'job';folder.mkdir()
        verdict={'verdict':'BLOCK','tests_verified':False,'reason':'unsupported test claim','repairable':False}
        with patch.object(worker,'fingerprint',return_value='digest'),patch.object(worker,'git',return_value='base'),patch.object(worker,'agent',return_value=verdict),patch.object(replies,'publish') as post:
            self.assertIsNone(worker.reviewed_reply(self.task,work,folder,'langfuse','base','answer'));post.assert_not_called()
        verdict.update(verdict='APPROVE',tests_verified=True)
        with patch.object(worker,'fingerprint',side_effect=['before','after']),patch.object(worker,'agent',return_value=verdict),patch.object(replies,'publish') as post:
            with self.assertRaisesRegex(RuntimeError,'changed the patch'):worker.reviewed_reply(self.task,work,folder,'langfuse','base','answer')
            post.assert_not_called()
    def test_new_policy_reoffers_old_block_once_without_replaying_push(self):
        self.assertTrue(rt.enqueue('respond','langfuse/langfuse',17876,'{"events":["old"]}',only_new=True))
        with rt.db() as db:db.execute("UPDATE tasks SET status='blocked',result='Draft reply',attempts=3")
        with patch.object(worker,'configs',return_value=('langfuse',{})),patch.object(worker,'response_context',return_value=self.context):
            self.assertEqual(replies.recover_backlog(),['langfuse/langfuse#17876'])
            self.assertEqual(replies.recover_backlog(),[])
        with rt.db() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY id')]
        self.assertEqual(rows[0]['status'],'blocked');self.assertEqual(rows[1]['status'],'queued')
        note=json.loads(rows[1]['note']);self.assertEqual(note['events'],['old']);self.assertEqual(note['head'],'a'*40)
        self.assertNotIn('publication_started',rt.task_state(rows[1]['id']))
    def test_creation_only_pause_does_not_block_existing_pr_maintenance(self):
        self.assertFalse(replies.response_paused({'paused':'cannot create PRs','respond_when_paused':True}))
        self.assertTrue(replies.response_paused({'paused':'upstream disallows autonomous work'}))
    def test_reply_only_result_is_not_valid_for_new_fixes(self):
        result={'outcome':'REPLY','title':'','reason':'answer','body':'text','tests':'','pr_body':''}
        worker.validate_result(result,worker.RESPONSE_SCHEMA)
        with self.assertRaises(RuntimeError):worker.validate_result(result,worker.GEN_SCHEMA)

    def test_metadata_update_preserves_concurrent_maintainer_edit(self):
        with patch('subprocess.run') as post:
            with self.assertRaisesRegex(rt.Paused,'description changed'):
                replies.update_body(self.task,{'body':'maintainer edit'},'new body','old body')
            post.assert_not_called()
    def test_metadata_patch_not_repeated_after_success_or_ambiguity(self):
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as post:
            replies.update_body(self.task,{'body':'old'},'new','old')
            replies.update_body(self.task,{'body':'new'},'new','old')
            self.assertEqual(post.call_count,1)
        self.task['identity']='other event'
        with patch('subprocess.run',side_effect=TimeoutError()) as post:
            with self.assertRaises(TimeoutError):replies.update_body(self.task,{'body':'old'},'new','old')
            with self.assertRaises(rt.Paused):replies.update_body(self.task,{'body':'old'},'new','old')
            self.assertEqual(post.call_count,1)


if __name__=='__main__':unittest.main()
