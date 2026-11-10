"""Real unchanged-checkout evidence and bounded task replacement regressions."""
import json
import concurrent.futures
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import candidate_budget as cb
import codex_worker as worker
import runtime as rt
import scan
import watch


class CandidateBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
            patch.object(rt,'CONFIG',self.root/'config.json'),patch.object(rt,'checkpoint'),
            patch.object(worker,'log'),patch.dict(watch.DISPATCH_BUDGET,{'openclaw':3})]
        for p in self.patches:p.start()
        rt.CONFIG.write_text(json.dumps({'enabled':True,'backend':'codex-local',
            'no_work_replacement_limits':{'openclaw':3},'candidate_capacity_precheck':True,
            'codex_sessions_per_5h':45}))
        rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def task(self,number):
        with rt.db() as db:
            now=time.time()
            db.execute("INSERT INTO tasks(identity,kind,repo,number,status,attempts,created,updated) VALUES (?,'fix','openclaw',?,'running',1,?,?)",(f'test:{number}',number,now,now))
            return dict(db.execute('SELECT * FROM tasks WHERE number=?',(number,)).fetchone())
    def skip(self,task,changed=False):
        folder=rt.DATA/'jobs'/str(task['id'])/'attempt-1';work=folder/'work';work.mkdir(parents=True)
        def git(*args):return subprocess.check_output(['git',*args],cwd=work,stderr=subprocess.DEVNULL,text=True).strip()
        git('init','-b','main');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
        (work/'source.py').write_text('value = 1\n');git('add','.');git('commit','-m','base');base=git('rev-parse','HEAD')
        rt.task_state(task['id'],folder=str(folder.relative_to(rt.DATA)),attempt=1,phase='generation',base=base)
        (folder/'generation.json').write_text(json.dumps({'outcome':'SKIP','reason':'Existing competing implementation'}))
        if changed:(work/'source.py').write_text('value = 2\n')
        worker.finish(task,'skipped','Existing competing implementation')
    def calls(self):
        with rt.db() as db:return db.execute('SELECT count(*) FROM calls').fetchone()[0]
    def test_three_proven_no_patch_investigations_allow_only_three_replacements(self):
        for n in range(3):
            task=self.task(n);self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0]);self.skip(task)
        self.assertEqual(len(rt.getmeta('no_work_assessments')),3)
        for n in range(3,6):
            task=self.task(n);rt.task_state(task['id'],replacement_candidate_verified=time.time())
            self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0]);self.skip(task)
        self.assertEqual(self.calls(),6)  # Every actual call remains charged.
        budget=rt.getmeta('dispatch_budget')
        self.assertEqual(budget['used']['openclaw'],3)
        self.assertEqual(budget['replacement_used']['openclaw'],3)
        self.assertEqual(len(rt.getmeta('no_work_assessments')),3)  # No recursive credits.
        task=self.task(6);rt.task_state(task['id'],replacement_candidate_verified=time.time())
        self.assertFalse(rt.reserve_call('codex',task=task,phase='generation')[0]);self.assertEqual(self.calls(),6)
        self.assertTrue(rt.reserve_call('codex',task=task,phase='review')[0])
    def test_changed_skip_cannot_supply_credit(self):
        task=self.task(1);self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0]);self.skip(task,changed=True)
        self.assertEqual(rt.getmeta('no_work_assessments',{}),{});self.assertEqual(self.calls(),1)
    def test_two_workers_cannot_spend_the_same_replacement_and_old_days_do_not_roll_over(self):
        with patch.dict(watch.DISPATCH_BUDGET,{'openclaw':1}):
            task=self.task(1);self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0]);self.skip(task)
            tasks=[self.task(n) for n in (2,3)]
            for task in tasks:rt.task_state(task['id'],replacement_candidate_verified=time.time())
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda t:rt.reserve_call('codex',task=t,phase='generation')[0],tasks))
            self.assertEqual(sorted(results),[False,True]);self.assertEqual(self.calls(),2)
            proofs=rt.getmeta('no_work_assessments')
            for proof in proofs.values():proof['date']='2000-01-01'
            rt.setmeta('no_work_assessments',proofs)
            with rt.db() as db:self.assertEqual(cb.remaining(db,'openclaw',rt._dispatch_budget(db)),0)
    def test_replacement_needs_invited_target_and_does_not_erase_reservations(self):
        for n in range(3):
            task=self.task(n);self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0]);self.skip(task)
        task=self.task(3)
        self.assertFalse(rt.reserve_call('codex',task=task,phase='generation')[0]);self.assertEqual(self.calls(),3)
        rt.task_state(task['id'],replacement_candidate_verified=time.time())
        with patch.object(rt,'config',return_value=rt.config()|{'codex_sessions_per_5h':3}):
            self.assertFalse(rt.reserve_call('codex',task=task,phase='generation')[0])
        self.assertNotIn('replacement_used',rt.getmeta('dispatch_budget'))
        self.assertEqual(self.calls(),3)
    def test_full_publication_capacity_stops_before_claim_screening_or_checkout(self):
        task=self.task(1)
        with patch.object(worker,'cap_ok',return_value=(False,'openclaw open PR cap (20)')),\
             patch.object(worker,'fix_eligible',side_effect=AssertionError('screening forbidden')),\
             patch.object(worker,'agent',side_effect=AssertionError('coding forbidden')):
            worker.process_one(task)
        self.assertEqual(self.calls(),0)
        self.assertFalse((rt.DATA/'jobs').exists())
    def test_source_triage_is_prioritized_over_newer_untriaged_candidates(self):
        cfg=scan.REPOS['openclaw']
        confirmed={'labels':[{'name':x} for x in cfg['triage_ready_labels']]}
        self.assertLess(scan.candidate_priority(cfg,confirmed),scan.candidate_priority(cfg,{'labels':[]}))
        self.assertTrue(cb.invited(cfg,confirmed));self.assertFalse(cb.invited(cfg,{'labels':[]}))
    def test_small_scan_window_spends_its_only_vet_on_confirmed_source_bug(self):
        cfg=scan.REPOS['openclaw']|{'max_vet':1}
        confirmed={'number':10,'title':'Reproduced source bug','created_at':'2026-11-08T00:00:00Z',
                   'labels':[{'name':x} for x in cfg['triage_ready_labels']]}
        newer={'number':11,'title':'Untriaged report','created_at':'2026-11-09T00:00:00Z','labels':[]}
        with patch.dict(scan.REPOS,{'openclaw':cfg}),\
             patch.object(scan,'search_issues',return_value=[newer,confirmed]),\
             patch.object(scan,'vet_candidate',return_value=(True,'clear',{})) as vet:
            result=scan.scan_repo('openclaw',30,15)
        self.assertEqual([r['number'] for r in result['candidates']],[10])
        self.assertEqual(vet.call_count,1)
    def test_upstream_rejected_label_is_checked_without_model_calls(self):
        issue={'number':5342,'title':'A code change','labels':[{'name':'triage-out-of-scope'}]}
        with patch.object(scan,'linked_prs',side_effect=AssertionError('unnecessary lookup')):
            ok,why,_=scan.vet(scan.REPOS['spec-kit'],'github/spec-kit',issue)
        self.assertFalse(ok);self.assertIn('excluded label',why);self.assertEqual(self.calls(),0)
    def test_last_ordinary_start_can_still_pass_final_eligibility_without_replacement_label(self):
        task=self.task(1);rt.task_state(task['id'],generation_started=time.time())
        rt.setmeta('dispatch_budget',{'date':time.strftime('%Y-%m-%d',time.gmtime()),'used':{'openclaw':3}})
        issue={'number':1,'state':'open','title':'A concrete source regression','labels':[],
               'body':'A reproducible failure with precise steps and expected output. '*4}
        with patch.object(worker,'api',return_value=issue),patch.object(scan,'linked_prs',return_value=[]),\
             patch.object(scan,'claimants',return_value=[]):
            self.assertTrue(worker.fix_eligible('openclaw',scan.REPOS['openclaw'],1,task)[0])
    def test_admission_stops_before_vetting_at_open_cap_and_caches_only_briefly(self):
        with patch.object(worker,'cap_ok',return_value=(False,'openclaw open PR cap (20)')) as cap:
            self.assertFalse(cb.admission('openclaw')[0]);self.assertFalse(cb.admission('openclaw')[0])
            self.assertEqual(cap.call_count,1)
        with patch.object(worker,'cap_ok',return_value=(True,'')),patch.object(cb.time,'time',return_value=time.time()+61):
            self.assertTrue(cb.admission('openclaw')[0])


if __name__=='__main__':unittest.main()
