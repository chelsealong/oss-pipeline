"""Production admission regressions; all model and network I/O is simulated."""
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import admission_health
import codex_worker
import intent
import local_service
import model_budget
import runtime as rt
import scan
import screening
import watch


class ScreeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg={'backend':'codex-local','enabled':True,'codex_review_reservations':True,
            'codex_sessions_per_5h':45,'codex_screening_fallback':True,
            'judge_issue_triage':True,'judge_tokens_per_day':100000,
            'judge_free_only_required':True,'judge_free_only_models':['fixture'],
            'judge_requests_per_hour':30,'judge_requests_per_day':150}
        self.stack=contextlib.ExitStack()
        for target,name,value in [(rt,'DATA',self.root/'data'),(rt,'ROOT',self.root),
                (intent,'CACHE',self.root/'intent.json'),(intent,'RETIRED',self.root/'retired.json'),
                (intent,'LOG',self.root/'intent.log'),(intent,'MODELS',['fixture']),
                (intent,'_down',{}),(watch,'PENDING_VET',self.root/'pending.json')]:
            self.stack.enter_context(patch.object(target,name,value))
        self.stack.enter_context(patch.object(rt,'config',return_value=self.cfg))
        self.stack.enter_context(patch.object(rt,'checkpoint'))
        self.stack.enter_context(patch.object(intent,'_load_key',return_value='fixture'))
        self.http=self.stack.enter_context(patch.object(intent.urllib.request,'urlopen',side_effect=AssertionError('No provider request allowed')))
        self.stack.enter_context(patch.object(watch,'log'))
        rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
        rt.setmeta('judge_usage',[{'at':time.time(),'charged_tokens':98964,'actual_tokens':98964,'status':'completed'}])
        now=time.time()
        with rt.db() as db:
            self.ident=db.execute("INSERT INTO tasks(identity,kind,repo,number,status,created,updated,attempts) VALUES ('fix:fixture:1:','fix','comfyui',1,'running',?,?,1)",(now,now)).lastrowid
        self.task={'id':self.ident,'kind':'fix','repo':'comfyui','number':1,'attempts':1,'note':''}
        self.issue={'number':1,'state':'open','title':'Reproducible failure','body':'I observed a reproducible bug. '*30,
            'user':{'login':'reporter'},'comments':0,'labels':[],'assignees':[],
            'created_at':'2026-10-01T00:00:00Z','html_url':'https://github.com/Comfy-Org/ComfyUI/issues/1'}
        self.stack.enter_context(patch.object(scan,'linked_prs',return_value=[]))
        self.stack.enter_context(patch.object(scan,'claimants',return_value=[]))
        self.stack.enter_context(patch.object(codex_worker,'api',side_effect=lambda _:self.issue))
        self.agent=self.stack.enter_context(patch.object(codex_worker,'agent',side_effect=self.fake_agent))

    def tearDown(self):
        self.stack.close();self.tmp.cleanup()

    def fake_agent(self,prompt,work,output,schema,**kwargs):
        self.assertTrue(kwargs['read_only']);self.assertEqual(kwargs['timeout'],180)
        task=kwargs['task']
        ok,why=rt.reserve_call('codex',task=task,phase=output.stem)
        if not ok:raise rt.Paused(why)
        rt.task_state(task['id'],phase=output.stem)
        if 'needs' in schema['properties']:
            result={'needs':'NOTHING','why':'Status only'}
        else:
            claimed='I have implemented the fix' in prompt
            result={'claim':claimed,'kind':'BUG','evidence':'I have implemented the fix' if claimed else '', 'why':'fixture judgement'}
            if 'kind' not in schema['properties']:result={k:result[k] for k in ('claim','why')}
        output.write_text(json.dumps(result));return result

    def eligible(self):
        return codex_worker.fix_eligible('comfyui',{'upstream':'Comfy-Org/ComfyUI'},1,self.task)

    def test_exhausted_judge_offers_candidate_but_worker_must_screen_before_coding(self):
        ok,_,extra=scan.vet_candidate({},'Comfy-Org/ComfyUI',self.issue)
        self.assertTrue(ok);self.assertTrue(extra['screening_required']);self.agent.assert_not_called()
        self.assertTrue(self.eligible()[0]);self.assertEqual(self.agent.call_count,1)
        self.assertTrue(self.eligible()[0]);self.assertEqual(self.agent.call_count,1)
        self.http.assert_not_called()
        with rt.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM calls WHERE kind='codex'").fetchone()[0],1)
        self.assertFalse(rt.task_state(self.ident).get('generation_started'))

    def test_claim_at_end_of_long_report_stops_before_checkout(self):
        self.issue['body']='I observe a ValueError in execution.py.\n'+'Reproduction details.\n'*900+'I have implemented the fix and will open a PR.'
        self.assertTrue(scan.vet_candidate({},'Comfy-Org/ComfyUI',self.issue)[0])
        with patch.object(codex_worker,'cap_ok',return_value=(True,'')),\
             patch.object(codex_worker,'finish') as finish,patch.object(codex_worker,'run') as run:
            codex_worker.process_one(self.task)
        self.assertEqual(finish.call_args.args[1],'skipped');run.assert_not_called()
        self.assertIn('I have implemented the fix',self.agent.call_args.args[0])
        self.http.assert_not_called()

    def test_competing_pr_never_enters_semantic_fallback(self):
        with patch.object(scan,'linked_prs',return_value=['closing-ref PR#2(OPEN)']):
            self.assertFalse(self.eligible()[0])
        self.agent.assert_not_called()

    def test_auth_unavailability_does_not_become_an_admission_approval(self):
        with patch.object(intent,'_ask_remote',side_effect=rt.JudgeDeferred('judge authentication unavailable')):
            with self.assertRaises(rt.JudgeDeferred):self.eligible()
        self.agent.assert_not_called()

    def test_screening_obeys_shared_and_own_budgets_without_charging_generation(self):
        self.cfg['codex_screening_per_5h']=1
        self.assertTrue(self.eligible()[0])
        self.issue['body']+=' Newly reported detail.'
        with self.assertRaisesRegex(rt.JudgeDeferred,'screening budget'):self.eligible()
        self.assertEqual(len(rt.getmeta('codex_screening_calls')),1)
        self.assertFalse(rt.task_state(self.ident).get('generation_started'))
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)

    def test_screening_reserves_room_for_code_and_independent_review(self):
        self.cfg['codex_sessions_per_5h']=3
        with rt.db() as db:db.execute("INSERT INTO calls VALUES (?,'codex')",(time.time(),))
        with self.assertRaisesRegex(rt.JudgeDeferred,'reserved for coding'):self.eligible()
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)

    def test_disabled_repo_cannot_spend_fallback_calls(self):
        self.task['repo']='hermes'
        with screening.worker(self.task,'hermes'):
            with self.assertRaisesRegex(rt.JudgeDeferred,'quota disabled'):
                screening.ask(intent.SYSTEM,'I am working on the fix',rt.JudgeDeferred('judge token budget reached'))
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)

    def test_duplicate_discovery_preserves_backoff_and_concurrent_new_record(self):
        watch.retain_for_vetting('comfyui',{'number':1,'createdAt':'2026-10-01T00:00:00Z'})
        old=watch.pending_vet();old['comfyui']['1']['retry_after']=9999999999;watch.save_pending_vet(old)
        before=watch.pending_vet();changed=json.loads(json.dumps(before));del changed['comfyui']['1']
        watch.retain_for_vetting('comfyui',{'number':1,'createdAt':'2026-10-01T00:00:00Z'})
        self.assertEqual(watch.pending_vet()['comfyui']['1']['retry_after'],9999999999)
        watch.retain_for_vetting('comfyui',{'number':2,'createdAt':'2026-10-02T00:00:00Z'})
        watch.save_pending_vet(changed,before)
        self.assertEqual(set(watch.pending_vet()['comfyui']),{'2'})

    def test_discovery_runs_while_coding_is_unavailable_and_never_judges(self):
        with patch.object(rt,'lock',return_value=contextlib.nullcontext()),\
             patch.object(rt,'ready',side_effect=AssertionError('Discovery must not depend on coding health')),\
             patch.object(watch,'load_seen',return_value={'comfyui':[1]}),patch.object(watch,'save_seen'),\
             patch.object(watch,'sweep') as sweep:
            local_service.discover(once=True)
        self.assertFalse(sweep.call_args.kwargs['vetting']);self.agent.assert_not_called()

    def test_stalled_health_distinguishes_live_process_from_admission(self):
        with rt.db() as db:
            db.execute("UPDATE tasks SET status='skipped'")
            db.execute("INSERT INTO calls VALUES (?,'codex')",(time.time()-7200,))
        watch.retain_for_vetting('comfyui',{'number':2,'createdAt':'2026-10-02T00:00:00Z'})
        self.assertTrue(rt.ready()[0]);self.assertEqual(admission_health.status()['state'],'stalled')

    def test_adk_duplicate_does_not_generate_coordination_request(self):
        with patch.object(scan,'linked_prs',return_value=['PR#2']),patch.object(scan,'coordination_allowed') as coordinate:
            self.assertFalse(scan.vet({'announce_before_work':True},'google/adk-python',self.issue)[0])
        coordinate.assert_not_called()
        self.assertEqual(rt.getmeta('coordination:google/adk-python#1')['status'],'not_needed')

    def test_adk_valid_coordination_is_visible_and_never_sent(self):
        self.assertFalse(scan.vet({'announce_before_work':True},'google/adk-python',self.issue)[0])
        requests=admission_health.status()['coordination_requests']
        self.assertEqual(len(requests),1);self.assertEqual(requests[0]['delivery'],'not_sent')
        self.assertIn('May I work',requests[0]['draft']);self.agent.assert_not_called()

    def test_deferred_feedback_refreshes_source_and_skips_status_before_checkout(self):
        self.task.update(kind='respond',repo='openclaw/openclaw',note=json.dumps({'events':['IC_1@date'],'screening_events':['IC_1@date']}))
        response={'data':{'node':{'body':'I have started the automated review.','issue':{'number':1,'repository':{'nameWithOwner':'openclaw/openclaw'}}}}}
        with patch.object(codex_worker,'run',return_value=json.dumps(response)):
            self.assertFalse(screening.feedback(self.task,'openclaw','openclaw/openclaw'))
        self.assertTrue(self.agent.call_args.kwargs['read_only'])

    def test_failed_screening_response_is_not_repeated_forever(self):
        def fail(*args,**kwargs):
            self.fake_agent(*args,**kwargs)
            raise RuntimeError('Lost model response')
        self.agent.side_effect=fail
        with self.assertRaisesRegex(RuntimeError,'Lost model response'):self.eligible()
        self.agent.side_effect=self.fake_agent
        with self.assertRaisesRegex(rt.JudgeDeferred,'manual assessment'):self.eligible()
        self.assertEqual(self.agent.call_count,1)


if __name__=='__main__':unittest.main()
