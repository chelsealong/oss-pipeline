"""Offline tests for spending, concurrent cache writers and review admission."""
import concurrent.futures
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch
import codex_worker
import intent
import model_budget as budget
import runtime as rt
import scan


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg={'backend':'codex-local','enabled':True,'max_pending':8,
                  'codex_sessions_per_5h':45,'codex_review_reservations':True,
                  'codex_phase_shares':{'new_fix':24,'maintenance':10,'repair':6,'validation':5},
                  'judge_requests_per_hour':30,'judge_requests_per_day':150,
                  'judge_tokens_per_day':100000,'judge_max_attempts':2,
                  'judge_free_only_required':True,'judge_free_only_models':['a','b']}
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
                      patch.object(rt,'CONFIG',self.root/'config.json'),
                      patch.object(intent,'CACHE',self.root/'intent.json'),patch.object(intent,'LOG',self.root/'intent.log'),
                      patch.object(intent,'RETIRED',self.root/'retired.json'),patch.object(intent,'_down',{}),
                      patch.object(intent,'MODELS',['a','b','c']),patch.object(intent,'_load_key',return_value='fixture-only')]
        for p in self.patches:p.start()
        self.save();rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
    def save(self):rt.CONFIG.write_text(json.dumps(self.cfg))
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def task(self,kind='fix',repo='openclaw'):
        now=time.time()
        with rt.db() as db:
            cur=db.execute('INSERT INTO tasks(kind,repo,number,status,created,updated,attempts) VALUES (?,?,1,?,?,?,1)',
                           (kind,repo,'running',now,now))
            return {'id':cur.lastrowid,'kind':kind,'repo':repo,'number':1,'attempts':1}
    def response(self,decision=None,usage=None):
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(decision or {'claim':False,'why':'report'})}}],
                                    'usage':usage or {'prompt_tokens':40,'completion_tokens':10,'total_tokens':50}}).encode())
    def test_unknown_free_only_models_never_send_http(self):
        self.cfg['judge_free_only_models']=[];self.save()
        with patch.object(intent.urllib.request,'urlopen',side_effect=AssertionError('network')):
            with self.assertRaises(rt.JudgeDeferred):intent._ask('system','body')
        self.assertEqual(budget.status()['judge_requests_24h'],0)
    def test_missing_key_preserves_work_without_claim_or_coding_default(self):
        with patch.object(intent,'_load_key',return_value=None):
            with self.assertRaises(rt.JudgeDeferred):intent.is_claim('I see this failure')
            with self.assertRaises(rt.JudgeDeferred):intent.feedback_needs('Please explain this')
        self.assertFalse(intent._cache())
    def test_null_response_content_has_bounded_fallback_and_usage(self):
        def response(*a,**kw):return io.BytesIO(json.dumps({'choices':[{'message':{'content':None}}],
            'usage':{'total_tokens':12}}).encode())
        with patch.object(intent.urllib.request,'urlopen',side_effect=response) as http:
            with self.assertRaises(rt.JudgeDeferred):intent._ask('s','u')
        self.assertEqual(http.call_count,2)
        self.assertEqual(budget.status()['judge_measured_tokens_24h'],24)
    def test_record_actual_usage_and_cached_verdict_no_second_request(self):
        with patch.object(intent.urllib.request,'urlopen',side_effect=lambda *a,**k:self.response()) as http:
            self.assertFalse(intent.is_claim('I have a question about this bug')[0])
            self.assertFalse(intent.is_claim('I have a question about this bug')[0])
        self.assertEqual(http.call_count,1)
        self.assertEqual(budget.status()['judge_measured_tokens_24h'],50)
        self.assertEqual(budget.status()['judge_charged_tokens_24h'],50)
    def test_at_most_two_attempts_and_unknown_spending_not_refunded(self):
        with patch.object(intent.urllib.request,'urlopen',side_effect=TimeoutError()) as http:
            with self.assertRaises(rt.JudgeDeferred):intent._ask('system','body')
        self.assertEqual(http.call_count,2)
        status=budget.status();self.assertEqual(status['judge_requests_24h'],2)
        self.assertGreater(status['judge_charged_tokens_24h'],400)
        self.assertEqual(status['judge_measured_tokens_24h'],0)
        with patch.object(intent.urllib.request,'urlopen',side_effect=AssertionError('cooldown ignored')):
            with self.assertRaises(rt.JudgeDeferred):intent._ask('system','body')
    def test_global_auth_error_does_not_fall_through(self):
        error=urllib.error.HTTPError('https://fixture.invalid',401,'unauthorized',{},io.BytesIO(b'InvalidApiKey'))
        with patch.object(intent.urllib.request,'urlopen',side_effect=error) as http:
            with self.assertRaises(rt.JudgeDeferred):intent._ask('system','body')
        self.assertEqual(http.call_count,1)
    def test_long_feedback_is_deferred_without_truncating_objection(self):
        with patch.object(intent.urllib.request,'urlopen',side_effect=AssertionError('network')):
            with self.assertRaises(rt.JudgeDeferred):intent.feedback_needs('x'*20000+' Fix this defect in the middle. '+'x'*20000)
        self.assertEqual(budget.status()['judge_requests_24h'],0)
    def test_daily_count_includes_legacy_calls(self):
        self.cfg['judge_requests_per_day']=2;self.save()
        with rt.db() as db:db.executemany('INSERT INTO calls VALUES (?,?)',[(time.time()-7200,'judge')]*2)
        with self.assertRaisesRegex(rt.JudgeDeferred,'request budget'):budget.reserve_judge('a','s','u')
        self.assertEqual(budget.status()['judge_historical_unmetered_requests'],2)
    def test_daily_tokens_atomic_for_two_workers(self):
        self.cfg['judge_tokens_per_day']=400;self.save()
        def reserve(_):
            try:return budget.reserve_judge('a','s','u')
            except rt.JudgeDeferred:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:answers=list(pool.map(reserve,range(2)))
        self.assertEqual(sum(x is not None for x in answers),1)
    def test_missing_usage_keeps_reservation_and_export_preserves_it(self):
        ident=budget.reserve_judge('a','system','body')
        before=budget.status()['judge_charged_tokens_24h']
        budget.settle_judge(ident,{})
        self.assertEqual(budget.status()['judge_charged_tokens_24h'],before)
        import cloud_state
        target=self.root/'checkpoint.json';cloud_state.export_state(target)
        data=json.loads(target.read_text())
        rows={x['key']:json.loads(x['value']) for x in data['tables']['meta']}
        self.assertEqual(rows['judge_usage'][0]['charged_tokens'],before)
    def test_concurrent_cached_judgement_only_one_http_request(self):
        def http(*a,**k):time.sleep(.04);return self.response()
        with patch.object(intent.urllib.request,'urlopen',side_effect=http) as request:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda _:intent.is_claim('I only report this bug'),range(6)))
        self.assertEqual(request.call_count,1)
    def test_concurrent_cache_writes_preserve_other_keys(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda n:intent._save({str(n):{'claim':False}}),range(50)))
        self.assertEqual(len(intent._cache()),50)
    def test_issue_classification_requires_verbatim_evidence(self):
        with patch.object(intent,'_ask',return_value={'claim':False,'kind':'QUESTION','evidence':'invented'}):
            with self.assertRaises(rt.JudgeDeferred):intent.issue_intent('A real failure')
        self.assertFalse(intent._cache())
    def test_issue_triage_replaces_body_claim_call_and_preserves_unknown(self):
        self.cfg['judge_issue_triage']=True;self.save()
        issue={'number':1,'title':'Failure','body':'I observe an exception when loading. '*8,
               'labels':[],'assignees':[],'user':{'login':'reporter'}}
        verdict={'claim':False,'kind':'UNKNOWN','evidence':'','why':'needs code inspection'}
        with patch.object(scan,'linked_prs',return_value=[]),patch.object(scan,'claimants',return_value=[]),\
             patch.object(intent,'is_claim',side_effect=AssertionError('duplicate judgement')),patch.object(intent,'_ask',return_value=verdict) as ask:
            self.assertTrue(scan.vet({},'o/r',issue)[0]);self.assertTrue(scan.vet({},'o/r',issue)[0])
        self.assertEqual(ask.call_count,1)
    def test_question_is_pending_not_permanent_rejection(self):
        self.cfg['judge_issue_triage']=True;self.save()
        issue={'number':1,'title':'How to configure?','body':'How do I configure this? '*8,
               'labels':[],'assignees':[],'user':{'login':'reporter'}}
        with patch.object(scan,'linked_prs',return_value=[]),patch.object(intent,'_ask',return_value={
                'claim':False,'kind':'QUESTION','evidence':'How do I configure this?','why':'usage question'}):
            with self.assertRaises(rt.JudgeDeferred):scan.vet({},'o/r',issue)
    def test_updated_discussion_invalidates_stale_question_verdict(self):
        self.cfg['judge_issue_triage']=True;self.save()
        issue={'number':1,'title':'How to configure?','body':'How do I configure this? '*8,
               'labels':[],'assignees':[],'user':{'login':'reporter'},'comments':0}
        question={'claim':False,'kind':'QUESTION','evidence':'How do I configure this?'}
        bug={'claim':False,'kind':'BUG','evidence':''}
        comment=[[{'user':{'login':'maintainer'},'author_association':'MEMBER','body':'Confirmed defect. Please fix.'}]]
        with patch.object(scan,'linked_prs',return_value=[]),patch.object(scan,'claimants',return_value=[]),\
             patch.object(scan,'gh',return_value=json.dumps(comment)),patch.object(intent,'_ask',side_effect=[question,bug]) as ask:
            with self.assertRaises(rt.JudgeDeferred):scan.vet({},'o/r',issue)
            issue['comments']=1
            self.assertTrue(scan.vet({},'o/r',issue)[0])
            self.assertIn('Confirmed defect',ask.call_args.args[1])
    def test_generation_cannot_take_last_review_slot(self):
        self.cfg['codex_sessions_per_5h']=3;self.save()
        first=self.task();second=self.task(repo='comfyui')
        self.assertTrue(rt.reserve_call('codex',task=first,phase='generation')[0])
        self.assertFalse(rt.reserve_call('codex',task=second,phase='generation')[0])
        self.assertTrue(rt.reserve_call('codex',task=first,phase='review')[0])
        with rt.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM calls WHERE kind='codex'").fetchone()[0],2)
    def test_two_workers_each_finish_review_inside_ceiling(self):
        self.cfg['codex_sessions_per_5h']=4;self.save()
        tasks=[self.task(),self.task(repo='comfyui')]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(lambda t:rt.reserve_call('codex',task=t,phase='generation')[0],tasks))
        self.assertEqual(result,[True,True])
        for t in tasks:self.assertTrue(rt.reserve_call('codex',task=t,phase='review')[0])
        self.assertFalse(rt.reserve_call('codex')[0])
    def test_last_turn_can_resume_patch_but_cannot_start_checkout(self):
        with rt.db() as db:
            db.executemany('INSERT INTO calls VALUES (?,?)',[(time.time(),'codex')]*44)
            self.assertFalse(budget.queue_ready(db,{}))
            self.assertTrue(budget.queue_ready(db,{'resume_progress':{'phase':'review'}}))
    def test_finishing_skip_releases_review_reservation(self):
        self.cfg['codex_sessions_per_5h']=3;self.save()
        first=self.task();second=self.task(repo='comfyui')
        rt.reserve_call('codex',task=first,phase='generation')
        with patch.object(codex_worker.work_evidence,'capture'),patch.object(codex_worker,'log'):
            codex_worker.finish(first,'skipped','already fixed')
        self.assertTrue(rt.reserve_call('codex',task=second,phase='generation')[0])
    def test_provider_reserve_admits_review_but_not_new_work(self):
        budget.rate_snapshot({'rateLimits':{'primary':{'usedPercent':90,'resetsAt':time.time()+100}}})
        task=self.task()
        self.assertFalse(rt.reserve_call('codex',task=task,phase='generation')[0])
        self.assertTrue(rt.reserve_call('codex',task=task,phase='review')[0])
        budget.rate_snapshot({'rateLimits':{'primary':{'usedPercent':96,'resetsAt':time.time()+100}}})
        self.assertFalse(rt.ready()[0])
        self.assertTrue(rt.ready(check_budget=False)[0])
    def test_stale_or_expired_quota_falls_back_to_durable_ceiling(self):
        budget.rate_snapshot({'rateLimits':{'primary':{'usedPercent':100,'resetsAt':time.time()-1}}})
        self.assertTrue(rt.ready()[0])
        with rt.db() as db:db.executemany('INSERT INTO calls VALUES (?,?)',[(time.time(),'codex')]*45)
        self.assertFalse(rt.ready()[0])

    def test_precheck_judge_pause_releases_queue_without_spending_attempt(self):
        task=self.task();rt.task_state(task['id'],folder=None,phase='claimed')
        with patch.object(codex_worker.work_evidence,'capture'),patch.object(codex_worker,'log'):
            codex_worker.handle_task_error(task,rt.JudgeDeferred('judge token budget reached'))
        with rt.db() as db:
            row=dict(db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone())
        self.assertEqual((row['status'],row['attempts']),('triage_wait',0))
        self.assertEqual(budget.status()['judge_requests_24h'],0)
        rt.task_state(task['id'],retry_after=0)
        with rt.db() as db:self.assertEqual(codex_worker.next_queued(db,{})['id'],task['id'])

    def test_deferred_candidates_do_not_starve_other_repositories(self):
        import watch
        pending={'openclaw':{'1':{'number':1,'created_at':'2026-10-01T00:00:00Z','retry_after':time.time()+900}},
                 'comfyui':{'2':{'number':2,'created_at':'2026-10-01T00:00:00Z'}}}
        issue={'number':2,'state':'open','title':'Bug','body':'A bug','created_at':'2026-10-01T00:00:00Z','html_url':'https://fixture.invalid/2'}
        with patch.object(watch,'pending_vet',return_value=pending),patch.object(watch,'save_pending_vet'),\
             patch.object(scan,'gh',return_value=json.dumps(issue)) as gh,patch.object(scan,'vet',return_value=(True,'',{})),\
             patch.object(watch,'append_candidate'),patch.object(watch,'NO_TRIGGER',True):
            self.assertEqual(watch.recheck_pending_vet(['openclaw','comfyui'],limit=1),1)
        self.assertIn('Comfy-Org/ComfyUI',str(gh.call_args))

    def test_invalid_feedback_is_deferred_not_an_expensive_code_task(self):
        with patch.object(intent,'_ask',return_value={'needs':'perhaps'}):
            with self.assertRaises(rt.JudgeDeferred):intent.feedback_needs('Please investigate this')
    def test_deferred_feedback_remains_unseen_while_other_pr_dispatches(self):
        spec=importlib.util.spec_from_file_location('budget_prwatch',Path(__file__).with_name('watch-prs.py'))
        watcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(watcher)
        prs=[{'_repo':'openclaw/openclaw','number':1},{'_repo':'Comfy-Org/ComfyUI','number':2}]
        def items(pr):return [{'id':str(pr['number']),'kind':'comment','author':'maintainer'}]
        def judge(item,pr):
            if pr['number']==1:raise rt.JudgeDeferred('judge temporarily unavailable')
            return True,'fix requested'
        seen={}
        with patch.object(watcher,'open_prs',return_value=prs),patch.object(watcher,'claimed_issues',return_value=[]),\
             patch.object(watcher,'last_commit_author',return_value=watcher.ME),\
             patch.object(watcher,'someone_claimed_the_issue',return_value=(None,None)),\
             patch.object(watcher,'past_merge_window',return_value=(False,'')),\
             patch.object(watcher,'feedback_items',side_effect=items),patch.object(watcher,'actionable',side_effect=judge),\
             patch.object(watcher,'dispatch',return_value=True) as dispatch,patch.object(watcher,'log'),\
             patch.object(watcher,'DRY_RUN',False),patch.object(watcher,'RESEED',False):
            self.assertEqual(watcher.one_pass(seen),1)
            self.assertEqual(dispatch.call_args.args[:2],('Comfy-Org/ComfyUI',2))
            self.assertEqual(seen['openclaw/openclaw#1']['ids'],[])
            self.assertIn('1',seen['openclaw/openclaw#1']['judge_wait'])
            self.assertEqual(seen['Comfy-Org/ComfyUI#2']['ids'],['2'])


if __name__=='__main__':unittest.main()
