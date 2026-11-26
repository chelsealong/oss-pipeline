import contextlib
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import codex_worker as worker
import coordination
import intent
import pr_followup
import runtime as rt
import scan


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(rt,'DATA',Path(self.tmp.name)))
        self.config={'enabled':True,'backend':'codex-local','adk_coordination_enabled':True}
        self.stack.enter_context(patch.object(rt,'config',return_value=self.config))
        self.checkpoint=self.stack.enter_context(patch.object(rt,'checkpoint'))
        self.stack.enter_context(patch.object(rt,'ready',return_value=(True,'')))
        self.stack.enter_context(patch.object(rt,'dispatch_headroom',return_value=(True,'')))
        self.cap=self.stack.enter_context(patch.object(worker,'cap_ok',return_value=(True,'')))
        self.stack.enter_context(patch('candidate_budget.requires_replacement',return_value=False))
        self.links=self.stack.enter_context(patch.object(scan,'linked_prs',return_value=[]))
        self.claim=self.stack.enter_context(patch.object(intent,'is_claim',return_value=(False,'')))
        self.claimants=self.stack.enter_context(patch.object(scan,'claimants',return_value=[]))
        self.comments=[]
        self.stack.enter_context(patch.object(pr_followup,'comments',side_effect=lambda *a:self.comments))
        self.stack.enter_context(patch.object(scan,'gh',side_effect=lambda *a:json.dumps([self.comments])))
        self.issue={'number':8376,'state':'open','title':'Ranged read loses data','body':'A reproducible failure with detailed steps. '*10,
                    'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'comments':0,
                    'labels':[],'assignees':[],'user':{'login':'reporter'}}
        def read(*a):
            result=copy.deepcopy(self.issue);result['comments']=len(self.comments);return result
        self.stack.enter_context(patch.object(worker,'api',side_effect=read))
        self.post=self.stack.enter_context(patch.object(coordination.subprocess,'run',side_effect=self.post_comment))
        with rt.db() as db:
            ident=db.execute("INSERT INTO tasks(identity,kind,repo,number,status,created,updated) VALUES ('fix:adk:8376:','fix','adk',8376,'running',?,?)",(time.time(),time.time())).lastrowid
        self.task={'id':ident,'kind':'fix','repo':'adk','number':8376}
        self.cfg={'upstream':coordination.REPO,'announce_before_work':True}

    def tearDown(self):
        self.stack.close();self.tmp.cleanup()

    def post_comment(self,args,**kw):
        self.assertEqual(args[0:4],['gh','api','-X','POST'])
        self.assertEqual(rt.getmeta('adk_coordination_post:8376')['status'],'posting')
        self.assertTrue(self.checkpoint.called)
        comment={'id':8,'html_url':'https://github.com/google/adk-python/issues/8376#issuecomment-8',
                 'user':{'login':'chelsealong'},'author_association':'NONE','body':json.loads(kw['input'])['body']}
        self.comments.append(comment)
        return types.SimpleNamespace(returncode=0,stdout=json.dumps(comment))

    def test_discovery_offers_but_only_worker_posts_and_rechecks(self):
        ok,_,extra=scan.vet_candidate(self.cfg,coordination.REPO,self.issue)
        self.assertTrue(ok);self.assertTrue(extra['coordination_required']);self.post.assert_not_called()
        self.assertTrue(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.assertTrue(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.assertEqual(self.post.call_count,1)
        self.assertIn('coordination_url',rt.task_state(self.task['id']))
        self.assertFalse(rt.task_state(self.task['id']).get('generation_started'))

    def test_stale_backlog_and_disabled_policy_never_publish(self):
        self.issue['created_at']='2026-01-01T00:00:00Z'
        self.assertFalse(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.config['adk_coordination_enabled']=False
        self.assertFalse(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.post.assert_not_called()

    def test_author_claim_competing_pr_and_refusal_block_requests(self):
        self.claim.return_value=(True,'author is implementing')
        self.assertFalse(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.claim.return_value=(False,'');self.links.return_value=['PR#2']
        self.assertFalse(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.links.return_value=[]
        self.comments.append({'body':'@chelsealong please wait','user':{'login':'maintainer'},'author_association':'MEMBER','id':1})
        self.assertFalse(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.post.assert_not_called()

    def test_ambiguous_post_never_repeats_and_can_reconcile_remote_marker(self):
        def ambiguous(*a,**kw):
            self.post_comment(*a,**kw)
            raise TimeoutError('connection lost after server accepted comment')
        self.post.side_effect=ambiguous
        with self.assertRaises(TimeoutError):coordination.ensure_request(self.task,self.cfg,self.issue)
        url=coordination.ensure_request(self.task,self.cfg,self.issue)
        self.assertIn('issuecomment-8',url);self.assertEqual(self.post.call_count,1)
        self.assertEqual(rt.getmeta('adk_coordination_post:8376')['status'],'sent')

    def test_unresolved_write_and_failed_checkpoint_never_repost(self):
        self.checkpoint.side_effect=rt.PersistenceError('checkpoint down')
        with self.assertRaises(rt.PersistenceError):coordination.ensure_request(self.task,self.cfg,self.issue)
        self.checkpoint.side_effect=None
        with self.assertRaisesRegex(rt.Paused,'reconciliation'):coordination.ensure_request(self.task,self.cfg,self.issue)
        self.post.assert_not_called()

    def test_daily_and_hourly_reservations_are_bounded(self):
        for times in [[time.time()],[time.time()-3700]*5]:
            rt.setmeta('adk_coordination_ledger',[{'at':x} for x in times])
            self.assertFalse(scan.vet_candidate(self.cfg,coordination.REPO,self.issue)[0])
        self.post.assert_not_called()

    def test_queued_candidate_waits_without_losing_attempt_when_slot_is_consumed(self):
        rt.setmeta('adk_coordination_ledger',[{'at':time.time()}])
        with self.assertRaises(rt.JudgeDeferred) as error:
            worker.fix_eligible('adk',self.cfg,8376,self.task)
        worker.handle_task_error(self.task,error.exception)
        with rt.db() as db:
            row=db.execute('SELECT status,attempts FROM tasks WHERE id=?',(self.task['id'],)).fetchone()
        self.assertEqual(row['status'],'triage_wait');self.assertEqual(row['attempts'],0)
        self.post.assert_not_called()

    def test_fresh_capacity_and_task_identity_are_required(self):
        self.cap.return_value=(False,'daily PR cap')
        with self.assertRaisesRegex(rt.Paused,'daily PR cap'):coordination.ensure_request(self.task,self.cfg,self.issue)
        self.cap.return_value=(True,'')
        with rt.db() as db:db.execute("UPDATE tasks SET status='done'")
        with self.assertRaisesRegex(rt.Paused,'not actively running'):coordination.ensure_request(self.task,self.cfg,self.issue)
        self.post.assert_not_called()

    def test_refusal_arriving_after_post_stops_coding(self):
        def refused(*args,**kw):
            result=self.post_comment(*args,**kw)
            self.comments.append({'body':'@chelsealong please wait','user':{'login':'maintainer'},'author_association':'MEMBER','id':9})
            return result
        self.post.side_effect=refused
        self.assertFalse(worker.fix_eligible('adk',self.cfg,8376,self.task)[0])
        self.assertEqual(self.post.call_count,1)


if __name__=='__main__':unittest.main()
