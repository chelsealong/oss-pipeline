"""Offline boundary and zero-spending tests for repository allocation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import codex_worker as worker
import repo_limits as limits
import runtime as rt
import scan
import watch


class RepoLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
                      patch.object(rt,'CONFIG',self.root/'runtime.json')]
        for p in self.patches:p.start()
        rt.CONFIG.write_text(json.dumps({'backend':'codex-local','enabled':True,
            'codex_sessions_per_5h':45,'max_pending':8}))
        rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_disabled_aliases_reject_new_and_old_tasks_before_spending(self):
        for key,upstream in limits.DISABLED_REPOS.items():
            self.assertEqual(watch.DISPATCH_BUDGET[key],0)
            self.assertEqual(scan.session_share(key),0)
            if key in scan.REPOS:
                self.assertTrue(scan.REPOS[key]['paused'])
                self.assertFalse(scan.REPOS[key]['respond_when_paused'])
            for name in (key,upstream,upstream.upper()):
                for kind in ('fix','respond'):
                    self.assertFalse(rt.enqueue(kind,name,1))
                    task={'id':1,'repo':name,'kind':kind}
                    self.assertFalse(rt.reserve_call('codex',task=task,phase='generation')[0])
                    with self.assertRaises(rt.Paused):worker.configs(task)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)
    def test_disabled_publication_cap_makes_no_github_call(self):
        with patch.object(worker,'run',side_effect=AssertionError('unnecessary GitHub read')):
            for key,upstream in limits.DISABLED_REPOS.items():
                self.assertFalse(worker.cap_ok(key,upstream)[0])
    def test_historical_backlog_held_without_losing_evidence_or_call_history(self):
        now=time.time()
        with rt.db() as db:
            db.executemany('INSERT INTO tasks(id,kind,repo,number,status,created,updated,attempts) VALUES (?,?,?,?,?,?,?,?)',[
                (1,'fix','firecrawl',100,'queued',now,now,2),
                (2,'respond','BerriAI/litellm',200,'retry_wait',now,now,1)])
            db.execute('INSERT INTO calls VALUES (?,?)',(now,'codex'))
        rt.task_state(1,folder='retained',patch_digest='exact-patch')
        self.assertTrue(rt.enqueue('fix','spec-kit',300))
        with rt.db() as db:
            db.execute('BEGIN IMMEDIATE')
            chosen=worker.next_queued(db,{})
            self.assertEqual(chosen['repo'],'spec-kit')
            rows=[tuple(r) for r in db.execute('SELECT id,status,attempts FROM tasks WHERE id<=2 ORDER BY id')]
            self.assertEqual(rows,[(1,'quota_wait',2),(2,'quota_wait',1)])
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
        self.assertEqual(rt.task_state(1)['patch_digest'],'exact-patch')
        self.assertEqual(rt.task_state(1)['folder'],'retained')
    def test_daily_caps_at_requested_boundaries_for_every_active_repo(self):
        for key,cfg in scan.REPOS.items():
            if limits.disabled(key):continue
            maximum={'hermes':8,'openclaw':5,'adk':5}.get(key,3)
            impl=cfg.get('implements_in') or cfg['upstream']
            with patch.object(worker,'run',return_value=json.dumps([{}]*maximum)):
                self.assertEqual(worker.cap_ok(key,impl),(False,'daily PR cap'))
            replies=[json.dumps([{}]*(maximum-1))]+([json.dumps([{}]*19)] if key=='openclaw' else [])
            with patch.object(worker,'run',side_effect=replies):
                self.assertTrue(worker.cap_ok(key,impl)[0])
    def test_openclaw_twenty_open_blocks_even_below_daily_cap(self):
        with patch.object(worker,'run',side_effect=['[]',json.dumps([{}]*20)]) as read:
            self.assertEqual(worker.cap_ok('openclaw','openclaw/openclaw'),(False,'openclaw open PR cap (20)'))
            query=read.call_args.args[0]
            self.assertEqual(query[query.index('--state')+1],'open')
            self.assertNotIn('--draft',query)
    def test_last_daily_task_slot_keeps_review_and_retry_available(self):
        today=time.strftime('%Y-%m-%d',time.gmtime())
        for key in scan.REPOS:
            if limits.disabled(key):continue
            with self.subTest(repo=key):
                maximum={'hermes':16,'openclaw':10,'adk':10}.get(key,6)
                rt.setmeta('dispatch_budget',{'date':today,'used':{key:maximum-1}})
                self.assertTrue(rt.enqueue('fix',key,901))
                self.assertTrue(rt.enqueue('fix',key,902))
                with rt.db() as db:
                    tasks=[dict(row) for row in db.execute(
                        'SELECT * FROM tasks WHERE repo=? ORDER BY number',(key,))]
                    before=db.execute('SELECT count(*) FROM calls').fetchone()[0]
                self.assertTrue(rt.reserve_call('codex',task=tasks[0],phase='generation')[0])
                self.assertEqual(rt.dispatch_headroom(key),(False,'daily viable-task budget reached'))
                self.assertFalse(rt.enqueue('fix',key,903))
                self.assertEqual(rt.reserve_call('codex',task=tasks[1],phase='generation'),
                                 (False,'daily viable-task budget reached'))
                self.assertFalse(rt.task_state(tasks[1]['id']).get('generation_started'))
                self.assertTrue(rt.reserve_call('codex',task=tasks[0],phase='review')[0])
                self.assertTrue(rt.reserve_call('codex',task=tasks[0],phase='generation')[0])
                self.assertEqual(rt.getmeta('dispatch_budget')['used'][key],maximum)
                with rt.db() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],before+3)
                    db.execute("UPDATE tasks SET status='done' WHERE repo=?",(key,))
    def test_lowered_cap_preserves_already_spent_daily_budget(self):
        budget={'date':time.strftime('%Y-%m-%d',time.gmtime()),'used':{'hermes':25,'adk':12}}
        rt.setmeta('dispatch_budget',budget)
        self.assertFalse(rt.dispatch_headroom('hermes')[0])
        self.assertFalse(rt.enqueue('fix','hermes',903))
        self.assertFalse(rt.reserve_call('codex',
            task={'id':999,'kind':'fix','repo':'hermes'},phase='generation')[0])
        self.assertEqual(rt.getmeta('dispatch_budget'),budget)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)
    def test_disabled_pr_feedback_never_reaches_judge(self):
        spec=importlib.util.spec_from_file_location('quota_prwatch',Path(__file__).with_name('watch-prs.py'))
        watcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(watcher)
        prs=[{'_repo':repo,'number':1} for repo in limits.DISABLED_REPOS.values()]
        with patch.object(watcher,'open_prs',return_value=prs),patch.object(watcher,'claimed_issues',return_value=[]),\
             patch.object(watcher,'last_commit_author',side_effect=AssertionError('feedback examined')),\
             patch.object(watcher,'feedback_items',side_effect=AssertionError('judge context fetched')):
            self.assertEqual(watcher.one_pass({}),0)


if __name__=='__main__':unittest.main()
