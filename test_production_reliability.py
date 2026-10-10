"""Regressions from real October 10 production failures; entirely offline."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import codex_host
import codex_worker as worker
import execution_support as execution
import intent
import runtime as rt
import scan
import task_recovery
import validation_setup
import watch
import work_evidence


class ProductionReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patches=[patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
            patch.object(rt,'CONFIG',self.root/'runtime.json'),patch.object(rt,'checkpoint'),
            patch.object(watch,'PENDING_VET',self.root/'pending.json'),patch.object(watch,'log')]
        for item in self.patches:item.start()
        rt.CONFIG.write_text(json.dumps({'enabled':True,'backend':'codex-local','codex_sessions_per_5h':45}))
        rt.setmeta('health',{'ok':True,'at':time.time()});rt.setmeta('worker_heartbeat',time.time())
    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def task(self,number=1,repo='openclaw',kind='fix'):
        self.assertTrue(rt.enqueue(kind,repo,number,only_new=True))
        with rt.db() as db:
            db.execute('UPDATE tasks SET attempts=1 WHERE number=?',(number,))
            return dict(db.execute('SELECT * FROM tasks WHERE number=?',(number,)).fetchone())
    def status(self,task):
        with rt.db() as db:return db.execute('SELECT status FROM tasks WHERE id=?',(task['id'],)).fetchone()[0]
    def git(self,work,*args):
        return subprocess.run(['git',*args],cwd=work,capture_output=True,text=True,check=True).stdout.strip()
    def checkout(self,task):
        folder=rt.DATA/'jobs'/str(task['id'])/'attempt-1';work=folder/'work';work.mkdir(parents=True)
        self.git(work,'init','-b','main');self.git(work,'config','user.name','Test')
        self.git(work,'config','user.email','test@example.invalid')
        (work/'value.py').write_text('value = 1\n');self.git(work,'add','.')
        self.git(work,'commit','-m','base');base=self.git(work,'rev-parse','HEAD')
        rt.task_state(task['id'],folder=str(folder.relative_to(rt.DATA)),attempt=1,phase='generation',base=base)
        return folder,work,base
    def test_retry_claim_cannot_erase_completed_logs_or_patch(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 2\n')
        (folder/'generation.events.jsonl').write_text('{"proof":"first attempt"}\n')
        worker.finish(task,'retry_wait','read failed')
        bundle=rt.DATA/'evidence'/f"{task['id']}-1.json";original=bundle.read_bytes()
        shutil.rmtree(work)
        # Cover both legacy checkpoints and the new claim transition.
        rt.task_state(task['id'],terminal=False);work_evidence.capture(task['id'])
        self.assertEqual(bundle.read_bytes(),original)
        state=rt.task_state(task['id']);task['attempts']=2
        execution.claim_state(state,task,0);rt.setmeta(f"task:{task['id']}",state)
        worker.finish(task,'skipped','issue closed before retry')
        self.assertEqual(bundle.read_bytes(),original)
        self.assertIsNone(rt.task_state(task['id'])['folder'])
    def test_model_deadline_without_recoverable_patch_does_not_restart(self):
        task=self.task();self.checkout(task)
        worker.handle_task_error(task,subprocess.TimeoutExpired('Codex generation',2400))
        self.assertEqual(self.status(task),'execution_wait');self.assertTrue(rt.ready()[0])
        with rt.db() as db:self.assertIsNone(worker.next_queued(db,{}))
    def test_deployment_migrates_old_model_retries_without_starting_new_work(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n')
        worker.finish(task,'retry_wait',"Command 'Codex generation' timed out after 2400 seconds")
        empty=self.task(2,'comfyui')
        worker.finish(empty,'retry_wait',"Command 'Codex generation' timed out after 2400 seconds")
        with patch.object(worker,'agent',side_effect=AssertionError('No model during migration')):
            self.assertEqual(task_recovery.migrate_model_retries(),[task['id'],empty['id']])
            self.assertEqual(task_recovery.migrate_model_retries(),[])
        self.assertEqual(rt.task_state(task['id'])['timeout_continuations'],1)
        self.assertIsNotNone(rt.task_state(task['id'])['resume_progress'])
        self.assertEqual(self.status(empty),'execution_wait')
    def test_deadline_continuation_cannot_exceed_existing_attempt_limit(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n');task['attempts']=3
        worker.handle_task_error(task,subprocess.TimeoutExpired('Codex generation',2400))
        self.assertEqual(self.status(task),'execution_wait')
    def test_model_deadline_continues_saved_patch_once_and_never_regenerates_twice(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n')
        (work/'test_value.py').write_text('from value import value\nassert value == 42\n')
        worker.handle_task_error(task,subprocess.TimeoutExpired('Codex generation',2400))
        self.assertEqual(self.status(task),'retry_wait')
        state=rt.task_state(task['id']);self.assertEqual(state['timeout_continuations'],1)
        clone=self.root/'continued';self.git(self.root,'clone',str(work),str(clone))
        self.assertTrue(execution.restore_progress(task,clone,base))
        subprocess.run([sys.executable,'test_value.py'],cwd=clone,check=True)
        worker.handle_task_error(task,subprocess.TimeoutExpired('Codex generation',1200))
        self.assertEqual(self.status(task),'execution_wait')
    def test_corrupt_or_incomplete_evidence_is_not_resumed(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n');worker.finish(task,'execution_wait','timeout')
        bundle=task_recovery.latest_bundle(task['id']);data=json.loads(bundle.read_text())
        data['omitted']=['diff exceeds 8 MiB'];bundle.write_text(json.dumps(data))
        self.assertIsNone(execution.retained_progress(task))
        data['omitted']=[];bundle.write_text(json.dumps(data));entry=execution.retained_progress(task)
        rt.task_state(task['id'],resume_progress=entry);bundle.write_text(bundle.read_text()+' ')
        with self.assertRaisesRegex(RuntimeError,'changed'):execution.restore_progress(task,work,base)
    def test_usage_keeps_last_cumulative_total_and_survives_attempt_cleanup(self):
        task=self.task();folder,work,base=self.checkout(task);output=folder/'generation.json'
        events=[{'method':'thread/tokenUsage/updated','params':{'tokenUsage':{'total':{'inputTokens':n,'cachedInputTokens':n-5,'outputTokens':2}}}} for n in (50,100)]
        output.with_suffix('.events.jsonl').write_text('\n'.join(map(json.dumps,events)))
        execution.record_usage(task,output,time.monotonic())
        shutil.rmtree(folder)
        usage=rt.getmeta(f"usage:{task['id']}:1:generation")
        self.assertEqual(usage['tokens']['inputTokens'],100);self.assertFalse(usage['complete'])
    def test_cleanup_failure_preserves_done_and_does_not_pause_other_tasks(self):
        task=self.task();folder,work,base=self.checkout(task)
        with patch.object(rt,'config',return_value={'backend':'codex-cloud','enabled':True}),\
             patch.object(execution.shutil,'rmtree',side_effect=FileNotFoundError(str(work/'.aws'))):
            worker.finish(task,'done','already checkpointed')
        self.assertEqual(self.status(task),'done');self.assertIsNone(rt.getmeta('pause'))
        self.assertIsNotNone(rt.getmeta(f"cleanup:{task['id']}"))
    def test_github_limit_defers_only_unpublished_task(self):
        task=self.task();other=self.task(2,'comfyui')
        error=RuntimeError('gh failed (1): GraphQL: API rate limit already exceeded for user ID 14966750.')
        with patch.object(execution,'github_retry_after',return_value=time.time()+3600):
            worker.handle_task_error(task,error)
        self.assertEqual(self.status(task),'retry_wait')
        self.assertGreater(rt.task_state(task['id'])['retry_after'],time.time()+3500)
        self.assertIsNone(rt.getmeta('pause'))
        with rt.db() as db:self.assertEqual(worker.next_queued(db,{})['id'],other['id'])
        rt.task_state(task['id'],publication_started=True);worker.handle_task_error(task,error)
        self.assertEqual(self.status(task),'error')
    def test_github_retry_uses_actual_reset_time(self):
        reset=int(time.time()+120)
        response=json.dumps({'resources':{'graphql':{'remaining':0,'reset':reset},'core':{'remaining':12}}})
        with patch.object(execution.subprocess,'run',return_value=subprocess.CompletedProcess([],0,response)):
            self.assertEqual(execution.github_retry_after(),reset+5)
    def test_missing_optional_attestation_gets_one_fresh_policy_review(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 2\n')
        missing={'verdict':'BLOCK','tests_verified':True,'repairable':False,
            'reason':'HUMAN_REVIEW_REQUIRED: context.json has no named human attestation. Technical checks passed.'}
        approved={'verdict':'APPROVE','tests_verified':True,'repairable':False,'reason':'No applicable human gate; patch verified.'}
        with patch.object(worker,'agent',side_effect=[missing,approved]) as agent:
            _,review=worker.review_patch(task,work,folder,'comfyui',base,{},'review')
        self.assertEqual(review['verdict'],'APPROVE')
        self.assertEqual([c.args[2].stem for c in agent.call_args_list],['review','review-policy'])
        missing['reason']='HUMAN_REVIEW_REQUIRED: CONTRIBUTING.md requires human oversight for every contribution.'
        with patch.object(worker,'agent',return_value=missing) as agent:
            _,review=worker.review_patch(task,work,folder,'comfyui',base,{},'review')
        self.assertEqual(review['verdict'],'BLOCK');agent.assert_called_once()
    def test_old_metadata_only_human_block_rechecks_policy_once_with_retained_patch(self):
        task=self.task(repo='comfyui');folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 2\n')
        rt.task_state(task['id'],phase='review')
        worker.finish(task,'human_wait','review: HUMAN_REVIEW_REQUIRED: context.json has no named human attestation. Technical checks passed.')
        with patch.object(worker,'run',return_value='[]'),patch.object(task_recovery,'optional_api',return_value=None):
            self.assertEqual(task_recovery.reconcile(),[task['id']])
        state=rt.task_state(task['id']);self.assertTrue(state['policy_gate_rechecked'])
        self.assertIn('resume_progress',state);self.assertFalse(state.get('approval_resume'))
        worker.finish(task,'human_wait','HUMAN_REVIEW_REQUIRED: context.json has no named human attestation.')
        rt.task_state(task['id'],recovery_checked_at=0)
        self.assertEqual(task_recovery.reconcile(),[])
    def test_subjectless_claim_reaches_judge_but_is_not_blindly_true(self):
        for body,claim in [('Yes, PR coming shortly.',True),('Proposed fix (PR to follow)',True),
                           ('Is a PR coming shortly?',False)]:
            with patch.object(intent,'_cache',return_value={}),patch.object(intent,'_save'),\
                 patch.object(intent,'_log'),patch.object(intent,'_ask',return_value={'claim':claim,'why':'fixture'}) as judge:
                self.assertEqual(intent.is_claim(body)[0],claim);judge.assert_called_once()
    def test_active_implementation_bot_reserved_but_completed_run_not_reserved(self):
        comment={'user':{'login':'clawsweeper[bot]'},'created_at':'2026-10-10T00:00:00Z',
            'body':'<!-- clawsweeper-command-status:168081:implement_issue:auto -->\n- State: Building\n- Run: https://github.com/openclaw/clawsweeper/actions/runs/123'}
        for status,expected in [('in_progress',['clawsweeper[bot]']),('completed',[])]:
            with patch.object(scan,'gh',side_effect=[json.dumps([[comment]]),json.dumps({'status':status})]),\
                 patch.object(intent,'is_claim',side_effect=AssertionError('No model call')):
                self.assertEqual(scan.claimants('openclaw/openclaw',168081),expected)
    def test_ordinary_review_bot_comment_is_not_an_implementation_claim(self):
        comment={'user':{'login':'clawsweeper[bot]'},'body':'Codex review: this needs work.'}
        with patch.object(scan,'gh',return_value=json.dumps([[comment]])) as api:
            self.assertEqual(scan.claimants('openclaw/openclaw',1),[]);api.assert_called_once()
    def test_adk_existing_request_satisfies_ask_rule_but_refusal_wins(self):
        issue={'number':1,'comments':2,'labels':[],'assignees':[]}
        ours={'id':1,'user':{'login':'chelsealong'},'body':'May I work on this issue?', 'author_association':'NONE','html_url':'https://example.invalid/1'}
        refusal={'id':2,'user':{'login':'maintainer'},'body':'@chelsealong please wait','author_association':'MEMBER'}
        with patch.object(scan,'gh',return_value=json.dumps([[ours]])):
            self.assertTrue(scan.coordination_allowed('google/adk-python',issue))
        with patch.object(scan,'gh',return_value=json.dumps([[ours,refusal]])):
            self.assertFalse(scan.coordination_allowed('google/adk-python',issue))
        ours['user']['login']='someone-else'
        with patch.object(scan,'gh',return_value=json.dumps([[ours]])):
            self.assertFalse(scan.coordination_allowed('google/adk-python',issue))
    def test_large_discovery_reduces_failed_query_and_retains_other_repos(self):
        calls=[]
        def api(args,**kwargs):
            query=args[-1];calls.append(query)
            self.assertNotIn('r1:',query)
            if 'first:100,' in query:raise RuntimeError('Resource limits for this query exceeded')
            if 'google' in query:raise RuntimeError('HTTP 403 temporarily unavailable')
            return json.dumps({'data':{'r0':{'issues':{'nodes':[]}}}})
        with patch.object(scan,'gh',side_effect=api):
            self.assertEqual(watch.sweep(['adk','openclaw'],{},100,False,vetting=False),(0,0))
        self.assertEqual(len(calls),4);self.assertIsNotNone(rt.getmeta('discovery_error:adk'))
    def response_fixture(self,conflict=False):
        source=self.root/'source';source.mkdir();self.git(source,'init','-b','main')
        self.git(source,'config','user.name','Test');self.git(source,'config','user.email','test@example.invalid')
        (source/'shared').write_text('base\n');self.git(source,'add','.');self.git(source,'commit','-m','base')
        self.git(source,'checkout','-b','old-pr');(source/'shared' if conflict else source/'fix').write_text('fix\n')
        self.git(source,'add','.');self.git(source,'commit','-m','fix');original=self.git(source,'rev-parse','HEAD')
        self.git(source,'checkout','main');(source/'shared' if conflict else source/'new-main').write_text('advance\n')
        self.git(source,'add','.');self.git(source,'commit','-m','advance')
        work=self.root/'checkout';self.git(self.root,'clone','--depth=1',source.as_uri(),str(work))
        self.git(work,'remote','add','upstream',source.as_uri())
        self.git(work,'fetch','--depth=1','upstream','main:refs/remotes/upstream/main')
        self.git(work,'fetch','--depth=1','origin','old-pr');self.git(work,'checkout','-b','old-pr','FETCH_HEAD')
        return source,work,original
    def test_controller_base_update_is_fast_forward_from_remote_pr_and_never_pushes(self):
        source,work,original=self.response_fixture()
        result=execution.update_response_base(work,'main',original)
        self.git(work,'merge-base','--is-ancestor',original,'HEAD')
        self.git(work,'merge-base','--is-ancestor','upstream/main','HEAD')
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
        self.assertNotEqual(result['base'],original)
    def test_base_conflict_leaves_original_pr_head_intact(self):
        source,work,original=self.response_fixture(conflict=True)
        with self.assertRaisesRegex(RuntimeError,'conflicts'):execution.update_response_base(work,'main',original)
        self.assertEqual(self.git(work,'rev-parse','HEAD'),original)
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
        self.assertEqual(self.git(work,'status','--porcelain'),'')
    def run_real_response_controller(self,outcome):
        source,unused,original=self.response_fixture()
        task=self.task(repo='openclaw/openclaw',kind='respond')
        pr={'head':{'sha':original,'ref':'old-pr'},'body':'Existing PR description',
            'html_url':'https://github.com/openclaw/openclaw/pull/1'}
        real_run=worker.run
        def run(args,**kwargs):
            if args[0]=='gh':
                self.assertEqual(args[:4],['gh','api','--paginate','--slurp'])
                return '[[]]'
            if args[:4]==['git','remote','add','upstream']:args=args[:4]+[source.as_uri()]
            return real_run(args,**kwargs)
        def api(path):
            if path=='repos/chelsealong/openclaw':return {'parent':{'full_name':'openclaw/openclaw'},'clone_url':source.as_uri()}
            if path=='repos/openclaw/openclaw':return {'default_branch':'main'}
            if path=='repos/openclaw/openclaw/issues/1':return {'body':'Please update against current main','state':'open'}
            raise AssertionError('Unexpected API '+path)
        def agent(prompt,work,output,*args,**kwargs):
            if output.stem=='generation':
                if outcome=='READY':(work/'fix').write_text('updated fix\n')
                value={'outcome':outcome,'reason':'addressed','title':'Update fix against main',
                       'body':'Verified the requested change. Codex assisted.','tests':'fixture verified','pr_body':''}
            else:value={'verdict':'APPROVE','tests_verified':True,'repairable':False,'reason':'verified'}
            output.write_text(json.dumps(value));return value
        with patch.object(worker,'run',side_effect=run),patch.object(worker,'api',side_effect=api),\
             patch.object(worker,'response_context',return_value=pr),patch.object(worker,'agent',side_effect=agent),\
             patch.object(worker.pr_followup,'publish',return_value='https://example.invalid/comment') as post:
            worker.process(task)
        self.assertEqual(self.status(task),'done')
        return source,original,post.call_args,rt.task_state(task['id'])
    def test_reply_only_controller_uses_remote_head_and_does_not_publish_local_merge(self):
        source,original,call,state=self.run_real_response_controller('REPLY')
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
        self.assertEqual(call.args[2],original)
        self.assertNotEqual(state['base'],original)
    def test_code_update_controller_publishes_reviewed_fast_forward_after_base_update(self):
        source,original,call,state=self.run_real_response_controller('READY')
        new=self.git(source,'rev-parse','old-pr')
        self.assertNotEqual(new,original)
        self.git(source,'merge-base','--is-ancestor',original,new)
        self.assertEqual(call.args[2],new)
        self.assertEqual(self.git(source,'show','old-pr:fix'),'updated fix')
        self.assertEqual(state['publication_outcome'],'updated')
    def test_agent_environment_cannot_write_runner_control_files(self):
        names=('GITHUB_STEP_SUMMARY','GITHUB_OUTPUT','GITHUB_ENV','GITHUB_PATH','GITHUB_STATE')
        with patch.dict(os.environ,dict.fromkeys(names,'/runner/protected')):
            env=codex_host.clean_environment()
        self.assertTrue(all(name not in env for name in names))
    def test_required_gate_runs_without_model_and_cache_invalidates_on_patch_change(self):
        task=self.task();folder,work,base=self.checkout(task)
        command=[sys.executable,'-c','import os; assert "GH_PAT" not in os.environ; print("required gate passed")']
        with patch.object(rt,'config',return_value={'cloud_environment':True}),\
             patch.object(validation_setup,'sandbox',return_value=command) as sandbox,\
             patch.object(worker,'agent',side_effect=AssertionError('No model required')),\
             patch.dict(os.environ,{'GH_PAT':'not-for-test-process'}):
            execution.managed_check(task,work,folder,'openclaw')
            execution.managed_check(task,work,folder,'openclaw');self.assertEqual(sandbox.call_count,1)
            (work/'value.py').write_text('value = 7\n')
            execution.managed_check(task,work,folder,'openclaw');self.assertEqual(sandbox.call_count,2)
        self.assertEqual(json.loads((folder/'required-check.json').read_text())['exit_code'],0)
    def test_failed_required_gate_cannot_be_overridden_by_reviewer(self):
        task=self.task();folder,work,base=self.checkout(task)
        with patch.object(rt,'config',return_value={'cloud_environment':True}),\
             patch.object(validation_setup,'sandbox',return_value=[sys.executable,'-c','raise SystemExit(1)']),\
             patch.object(worker,'agent',return_value={'verdict':'APPROVE','tests_verified':True,'repairable':False,'reason':'incorrect approval'}):
            _,review=worker.review_patch(task,work,folder,'openclaw',base,{},'review')
            self.assertEqual(review['verdict'],'BLOCK');self.assertFalse(review['tests_verified'])
        self.assertEqual(json.loads((folder/'required-check.json').read_text())['exit_code'],1)
    def test_failed_required_gate_allows_one_repair_then_requires_passing_gate(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 2\n')
        result={'outcome':'READY','reason':'fixed','title':'fix','body':'Codex','tests':'focused tests pass'}
        calls=[]
        def agent(*args,**kwargs):
            phase=args[2].stem;calls.append(phase)
            if phase=='review':return {'verdict':'BLOCK','tests_verified':False,'repairable':True,'reason':'test fixture needs correction'}
            if phase=='remediation':
                (work/'value.py').write_text('value = 3\n');return result
            self.assertNotIn('mandatory controller gate FAILED',args[0])
            return {'verdict':'APPROVE','tests_verified':True,'repairable':False,'reason':'verified'}
        command=[sys.executable,'-c',f'from pathlib import Path; assert "value = 3" in Path({str(work / "value.py")!r}).read_text()']
        with patch.object(rt,'config',return_value={'cloud_environment':True}),\
             patch.object(validation_setup,'sandbox',return_value=command),patch.object(worker,'agent',side_effect=agent):
            _,review=worker.review_patch(task,work,folder,'openclaw',base,result,'review')
        self.assertEqual(calls,['review','remediation','rereview'])
        self.assertEqual(review['verdict'],'APPROVE')
        self.assertEqual(json.loads((folder/'required-check.json').read_text())['exit_code'],0)


if __name__=='__main__':unittest.main()
