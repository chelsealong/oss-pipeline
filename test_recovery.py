"""Behavioral regressions for throughput, recovery, review and coordination."""
import base64
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import cloud_runtime
import cloud_store
import codex_worker as worker
import runtime as rt
import scan
import watch
import work_evidence as evidence

spec=importlib.util.spec_from_file_location('prwatch',Path(__file__).with_name('watch-prs.py'))
prwatch=importlib.util.module_from_spec(spec);spec.loader.exec_module(prwatch)

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
            patch.object(rt,'CONFIG',self.root/'runtime.json'),
            patch.dict(os.environ,{'OSS_ARTIFACT_KEY':base64.b64encode(b'k'*32).decode()})]
        for p in self.patches:p.start()
        self.cfg={'backend':'codex-local','enabled':True,'max_pending':8,'max_pending_per_repo':3,
            'codex_sessions_per_5h':100,'judge_requests_per_hour':100}
        self.save();rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def save(self):rt.CONFIG.write_text(json.dumps(self.cfg))
    def task(self,repo='hermes',number=1,kind='fix',note=''):
        self.assertTrue(rt.enqueue(kind,repo,number,note,only_new=True))
        with rt.db() as db:return dict(db.execute('SELECT * FROM tasks ORDER BY id DESC LIMIT 1').fetchone())
    def git(self,work,*args):
        return subprocess.run(['git',*args],cwd=work,capture_output=True,text=True,check=True).stdout.strip()
    def checkout(self,task):
        folder=rt.DATA/'jobs'/str(task['id'])/'attempt-1';work=folder/'work';work.mkdir(parents=True)
        self.git(work,'init');self.git(work,'config','user.name','Test');self.git(work,'config','user.email','test@example.invalid')
        (work/'value.py').write_text('value = 1\n');self.git(work,'add','.');self.git(work,'commit','-m','baseline')
        base=self.git(work,'rev-parse','HEAD')
        rt.task_state(task['id'],folder=str(folder.relative_to(rt.DATA)),attempt=1,base=base,phase='generation')
        task['attempts']=1
        return folder,work,base
    def test_admission_does_not_spend_and_first_generation_spends_once(self):
        task=self.task();watch.budget_charge('hermes')
        self.assertEqual(rt.status()['dispatch_budget']['used'],{})
        for phase in ['generation','review','remediation','rereview','generation']:
            self.assertTrue(rt.reserve_call('codex',task=task,phase=phase)[0])
        self.assertEqual(rt.status()['dispatch_budget']['used'],{'hermes':1})
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],5)
    def test_skipped_precheck_never_spends_viable_task_budget(self):
        task=self.task()
        with patch.object(worker,'cap_ok',return_value=(True,'')),patch.object(worker,'fix_eligible',return_value=(False,'claimed by another contributor')),patch.object(worker,'agent',side_effect=AssertionError('model invoked')):
            worker.process(task)
        self.assertEqual(rt.status()['dispatch_budget']['used'],{})
    def test_concurrent_reservations_cannot_exceed_daily_cap(self):
        tasks=[self.task(number=n) for n in range(3)]
        with patch.dict(watch.DISPATCH_BUDGET,{'hermes':1}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results=list(pool.map(lambda t:rt.reserve_call('codex',task=t,phase='generation')[0],tasks))
        self.assertEqual(results.count(True),1)
        self.assertEqual(rt.status()['dispatch_budget']['used'],{'hermes':1})
    def test_per_repo_share_restored_without_blocking_other_repos(self):
        rt.setmeta('fix_sessions',[{'repo':'hermes','task':n,'at':time.time()}for n in range(scan.session_share('hermes'))])
        self.assertFalse(scan.session_headroom('hermes')[0])
        self.assertFalse(rt.enqueue('fix','hermes',55))
        self.assertTrue(scan.session_headroom('adk')[0])
    def test_repository_cannot_fill_queue_and_feedback_leaves_fix_room(self):
        for n in range(3):self.task('hermes',n)
        self.assertFalse(rt.enqueue('respond','NousResearch/hermes-agent',77,'new'))
        self.task('adk',88)
        with rt.db() as db:db.execute('DELETE FROM tasks')
        for n in range(7):self.task('o/repo'+str(n),n,'respond','event')
        self.assertFalse(rt.enqueue('respond','o/eighth',8,'event'))
        self.assertTrue(rt.enqueue('fix','hermes',99))
    def test_two_workers_never_claim_same_repo_and_alternate_feedback(self):
        self.task('NousResearch/hermes-agent',1,'respond','feedback')
        self.task('hermes',2)
        self.task('adk',3)
        def claim():
            with rt.db() as db:
                db.execute('BEGIN IMMEDIATE');t=worker.next_queued(db,{})
                if t:db.execute("UPDATE tasks SET status='running' WHERE id=?",(t['id'],))
                return t
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:tasks=list(pool.map(lambda _:claim(),range(2)))
        self.assertEqual({rt.repo_key(t['repo'])for t in tasks},{'hermes','adk'})
        self.assertEqual({t['kind']for t in tasks},{'respond','fix'})
    def test_failed_claim_checkpoint_releases_running_repo_without_executing(self):
        task=self.task();task['attempts']=1
        with rt.db() as db:db.execute("UPDATE tasks SET status='running' WHERE id=?",(task['id'],))
        with patch.object(rt,'checkpoint',side_effect=rt.PersistenceError('unavailable')):
            with self.assertRaises(rt.PersistenceError):worker.checkpoint_claim(task)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'retry_wait')
            self.assertIsNone(worker.next_queued(db,{}))
        rt.task_state(task['id'],retry_after=0)
        with rt.db() as db:self.assertEqual(worker.next_queued(db,{})['id'],task['id'])
        # A historical ambiguous publication must never be made retryable.
        with rt.db() as db:db.execute("UPDATE tasks SET status='running' WHERE id=?",(task['id'],))
        rt.task_state(task['id'],publication_started=True)
        with patch.object(rt,'checkpoint',side_effect=rt.PersistenceError('unavailable')):
            with self.assertRaises(rt.PersistenceError):worker.checkpoint_claim(task)
        with rt.db() as db:self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'interrupted')

    def test_git_timeout_retries_only_task_and_preserves_model_service(self):
        task=self.task();task['attempts']=1
        worker.handle_task_error(task,subprocess.TimeoutExpired(['git','fetch'],300))
        self.assertTrue(rt.ready()[0]);self.assertIsNone(rt.getmeta('pause'))
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'retry_wait')
            self.assertIsNone(worker.next_queued(db,{}))
        rt.task_state(task['id'],retry_after=0)
        with rt.db() as db:self.assertEqual(worker.next_queued(db,{})['id'],task['id'])
    def test_ambiguous_publication_never_automatically_replayed(self):
        task=self.task();task['attempts']=1;rt.task_state(task['id'],publication_started=True)
        worker.handle_task_error(task,subprocess.TimeoutExpired(['git','push'],300))
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'error')
            self.assertIsNone(worker.next_queued(db,{}))
    def test_publication_hold_frees_other_repo_queue_and_keeps_feedback_eligible(self):
        fix=self.task('hermes',1)
        reply=self.task('NousResearch/hermes-agent',2,'respond','feedback')
        with rt.db() as db:
            db.execute('BEGIN IMMEDIATE')
            chosen=worker.next_queued(db,{'hermes':time.time()+3600})
            self.assertEqual(chosen['id'],reply['id'])
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?',(fix['id'],)).fetchone()[0],'publication_wait')
            self.assertTrue(rt._room(db,'fix','hermes')[0])
        # When the hold expires, restoration is capped by the same queue limits.
        self.cfg['max_pending_per_repo']=1;self.save()
        with rt.db() as db:
            db.execute('BEGIN IMMEDIATE');worker.next_queued(db,{})
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?',(fix['id'],)).fetchone()[0],'publication_wait')
            db.execute("UPDATE tasks SET status='done' WHERE id=?",(reply['id'],))
            self.assertEqual(worker.next_queued(db,{})['id'],fix['id'])
    def test_ordinary_retries_are_bounded_and_account_failure_pauses(self):
        task=self.task();task['attempts']=3
        worker.handle_task_error(task,subprocess.TimeoutExpired(['git','fetch'],300))
        with rt.db() as db:self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'error')
        worker.handle_task_error(task,RuntimeError('Codex: quota exceeded'))
        self.assertFalse(rt.ready()[0])
    def test_migration_keeps_old_spending_and_prepaid_queue(self):
        task=self.task();(self.root/'state').mkdir()
        (self.root/'state/dispatch-budget.json').write_text(json.dumps({'date':time.strftime('%Y-%m-%d',time.gmtime()),'used':{'hermes':19}}))
        cloud_runtime.migrate_accounting();cloud_runtime.migrate_accounting()
        self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0])
        self.assertEqual(rt.status()['dispatch_budget']['used']['hermes'],19)
    def test_repair_requires_new_review_and_is_bounded(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 2\n')
        generation={'outcome':'READY','reason':'fixed','title':'fix','body':'Codex','tests':'passed'}
        block={'verdict':'BLOCK','reason':'missing edge case','tests_verified':False,'repairable':True}
        approve={'verdict':'APPROVE','reason':'verified','tests_verified':True,'repairable':False}
        with patch.object(worker,'agent',side_effect=[block,generation,approve]) as agent:
            result,review=worker.review_patch(task,work,folder,'hermes',base,generation,'review')
        self.assertEqual(review['verdict'],'APPROVE')
        self.assertEqual([c.args[2].stem for c in agent.call_args_list],['review','remediation','rereview'])
        with patch.object(worker,'agent',side_effect=[block,generation,block]) as agent:
            _,review=worker.review_patch(task,work,folder,'hermes',base,generation,'review')
        self.assertEqual(review['verdict'],'BLOCK');self.assertEqual(agent.call_count,3)
    def test_duplicate_block_does_not_trigger_repair(self):
        task=self.task();folder,work,base=self.checkout(task)
        block={'verdict':'BLOCK','reason':'duplicate','tests_verified':True,'repairable':False}
        with patch.object(worker,'agent',return_value=block) as agent:
            _,review=worker.review_patch(task,work,folder,'hermes',base,{},'review')
        self.assertEqual(review,block);agent.assert_called_once()
    def test_reviewer_mutation_invalidates_even_an_approval(self):
        task=self.task();folder,work,base=self.checkout(task)
        def mutate(*args,**kwargs):
            (work/'value.py').write_text('value = 999\n')
            return {'verdict':'APPROVE','reason':'ok','tests_verified':True,'repairable':False}
        with patch.object(worker,'agent',side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError,'review changed'):worker.review_patch(task,work,folder,'hermes',base,{},'review')
    def test_evidence_survives_cleanup_and_encryption_roundtrip(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n');(work/'new_test.py').write_text('assert 42 == 42\n')
        (folder/'generation.events.jsonl').write_text('sensitive-log-fixture\n')
        (folder/'tool-cache').mkdir();(folder/'tool-cache'/'auth.json').write_text('DO-NOT-COLLECT')
        rt.task_state(task['id'],terminal=True);evidence.capture(task['id'])
        raw=(rt.DATA/'evidence'/'1-1.json').read_bytes();payload=json.loads(raw)
        self.assertIn('value = 42',base64.b64decode(payload['diff']).decode())
        self.assertIn('new_test.py',payload['untracked']);self.assertNotIn(b'DO-NOT-COLLECT',raw)
        store=self.root/'store';store.mkdir();evidence.save_to(store)
        sealed=(store/'evidence'/'1-1.json.enc').read_bytes()
        self.assertNotIn(b'sensitive-log-fixture',sealed);self.assertEqual(evidence.decrypt(sealed),raw)
        shutil.rmtree(work);evidence.capture(task['id'])
        self.assertEqual((rt.DATA/'evidence'/'1-1.json').read_bytes(),raw)
        (rt.DATA/'evidence'/'1-1.json').unlink();evidence.restore_from(store)
        self.assertEqual((rt.DATA/'evidence'/'1-1.json').read_bytes(),raw)
        evidence.save_to(store);self.assertEqual((store/'evidence'/'1-1.json.enc').read_bytes(),sealed)
        with self.assertRaises(Exception):evidence.decrypt(sealed[:-1]+bytes([sealed[-1]^1]))
    def test_failed_retry_clone_does_not_reuse_old_base_during_finish(self):
        task=self.task();folder,work,base=self.checkout(task)
        task['attempts']=2
        with patch.object(worker,'cap_ok',return_value=(True,'')),patch.object(worker,'fix_eligible',return_value=(True,'')),patch.object(worker,'api',return_value={'parent':{'full_name':'NousResearch/hermes-agent'},'clone_url':'https://example.invalid/fork'}):
            def incomplete_clone(*args,**kwargs):
                dest=Path(args[0][-1]);dest.mkdir(parents=True)
                self.git(dest,'init')
                raise subprocess.TimeoutExpired(['git','clone'],600)
            with patch.object(worker,'run',side_effect=incomplete_clone):
                with self.assertRaises(subprocess.TimeoutExpired):worker.process(task)
        self.assertIsNone(rt.task_state(task['id'])['base'])
        worker.handle_task_error(task,subprocess.TimeoutExpired(['git','clone'],600))
        with rt.db() as db:self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'retry_wait')

    def test_checkpoint_during_retry_checkout_does_not_diff_previous_base(self):
        task=self.task();folder,work,base=self.checkout(task)
        # A retry moves to a new shallow checkout; the previous base is absent.
        retry=folder.parent/'attempt-2';new_work=retry/'work';new_work.mkdir(parents=True)
        self.git(new_work,'init')
        rt.task_state(task['id'],folder=str(retry.relative_to(rt.DATA)),attempt=2,
                      phase='checkout',terminal=False)
        with rt.db() as db:db.execute("UPDATE tasks SET status='running' WHERE id=?",(task['id'],))
        store=self.root/'store';store.mkdir()
        evidence.save_to(store)
        payload=json.loads(evidence.decrypt((store/'evidence'/'1-2.json.enc').read_bytes()))
        self.assertNotIn('diff',payload)
        self.assertNotIn('head',payload)
        # Once checkout completes, a bad base is a real error and must surface.
        rt.task_state(task['id'],phase='generation')
        with self.assertRaises(subprocess.CalledProcessError):evidence.capture(task['id'])

    def test_committed_but_unpublished_changes_are_in_evidence(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 9\n');self.git(work,'commit','-am','not yet pushed')
        evidence.capture(task['id']);payload=json.loads((rt.DATA/'evidence'/'1-1.json').read_text())
        self.assertIn('value = 9',base64.b64decode(payload['diff']).decode())
    def test_maintainer_invitation_unlocks_coordination_not_random_comments(self):
        issue={'number':123,'labels':[],'assignees':[],'comments':1}
        comment={'id':7,'body':'@chelsealong go ahead','author_association':'NONE','html_url':'https://example.invalid/comment'}
        with patch.object(scan,'gh',return_value=json.dumps([[comment]])):
            self.assertFalse(scan.coordination_allowed('google/adk-python',issue))
        self.assertEqual(rt.getmeta('coordination:google/adk-python#123')['status'],'needs_human')
        comment['author_association']='MEMBER'
        with patch.object(scan,'gh',return_value=json.dumps([[comment]])):
            self.assertTrue(scan.coordination_allowed('google/adk-python',issue))
        revoked=dict(comment,id=8,body='@chelsealong please wait')
        with patch.object(scan,'gh',return_value=json.dumps([[comment,revoked]])):
            self.assertFalse(scan.coordination_allowed('google/adk-python',issue))
    def test_feedback_identity_is_stable_until_actual_change(self):
        pr={'labels':{'nodes':[{'name':'needs proof'}]},'commits':{'nodes':[{'commit':{'oid':'a'*40}}]}}
        check={'name':'test','sha':'a'*12,'ours':True}
        with patch.object(prwatch,'failing_checks',return_value=[check]),patch.object(prwatch,'_utc_day',return_value='2026-10-04'):
            first=prwatch.feedback_items(pr)
        with patch.object(prwatch,'failing_checks',return_value=[check]),patch.object(prwatch,'_utc_day',return_value='2026-10-05'):
            self.assertEqual(prwatch.feedback_items(pr),first)
        pr['commits']['nodes'][0]['commit']['oid']='b'*40
        self.assertNotEqual(prwatch.standing_item(pr)['id'],first[0]['id'])
    def test_approved_patch_can_publish_at_model_budget_limit(self):
        self.cfg['codex_sessions_per_5h']=1;self.save();rt.reserve_call('codex')
        self.assertFalse(rt.ready()[0]);self.assertTrue(rt.ready(check_budget=False)[0])

if __name__=='__main__':unittest.main()
