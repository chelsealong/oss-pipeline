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
    def test_cloud_precheck_skip_has_no_checkout_to_clean(self):
        task=self.task();rt.task_state(task['id'],folder=None,phase='claimed')
        cfg=rt.config()|{'backend':'codex-cloud'}
        with patch.object(rt,'config',return_value=cfg),patch.object(worker,'cap_ok',return_value=(True,'')),patch.object(worker,'fix_eligible',return_value=(False,'issue closed')),patch.object(execution,'clean_owned_task') as cleanup:
            worker.process_one(task)
        self.assertEqual(self.status(task),'skipped');cleanup.assert_not_called()
        with rt.db() as db:self.assertEqual(db.execute('SELECT result FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],'issue closed')

    def test_stale_cap_recovers_once_with_remote_and_capacity_checks_preserving_spend(self):
        task=self.task();folder,work,base=self.checkout(task)
        self.assertTrue(rt.reserve_call('codex',task=task,phase='generation')[0])
        budget=rt.getmeta('dispatch_budget')
        reason='PR_CAP: chelsealong has 18 open OpenClaw PRs. The supplied pipeline policy requires stopping new fixes at 18.'
        rt.task_state(task['id'],phase='blocked',last_execution_phase='generation')
        with rt.db() as db:db.execute("UPDATE tasks SET status='blocked',result=? WHERE id=?",(reason,task['id']))
        work_evidence.capture(task['id'])
        with patch.object(worker,'cap_ok',return_value=(False,'open PR cap')),patch.object(task_recovery,'audited_unpublished') as audit:
            self.assertEqual(task_recovery.recover_submission_regressions(),[]);audit.assert_not_called()
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(worker,'cap_ok',return_value=(True,'')),patch.object(task_recovery,'audited_unpublished',return_value=(False,'remote branch exists')):
            self.assertEqual(task_recovery.recover_submission_regressions(),[])
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(worker,'cap_ok',return_value=(True,'')),patch.object(task_recovery,'audited_unpublished',return_value=(True,'no remote publication')):
            self.assertEqual(task_recovery.recover_submission_regressions(),[task['id']])
        self.assertEqual(rt.getmeta('dispatch_budget'),budget)
        state=rt.task_state(task['id']);self.assertIn('stale_cap_repaired',state)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT attempts FROM tasks').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
            db.execute("UPDATE tasks SET status='blocked'")
            task=dict(db.execute('SELECT * FROM tasks').fetchone())
        self.assertIsNone(task_recovery.submission_regression(task,state))
        state.pop('stale_cap_repaired')
        self.assertIsNone(task_recovery.submission_regression(task|{'result':reason+' Also lacks a reproduction.'},state))
        self.assertIsNone(task_recovery.submission_regression(task,state|{'publication_started':True}))
    def test_checkpoint_git_bounds_pack_without_changing_push_target(self):
        import cloud_store
        result=subprocess.CompletedProcess([],0,'saved\n','')
        with patch.object(execution,'git_process',return_value=result) as run:
            self.assertEqual(cloud_store.command(['git','push','origin','HEAD:refs/heads/codex-state'],self.root),'saved')
        command=run.call_args.args[0]
        self.assertIn('pack.window=0',command);self.assertIn('pack.threads=1',command)
        self.assertIn('gc.auto=0',command)
        self.assertEqual(command[-3:],['push','origin','HEAD:refs/heads/codex-state'])
    def test_checkpoint_timeout_terminates_owned_helpers(self):
        import cloud_store
        pidfile=self.root/'helper.pid'
        script='import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)'
        with self.assertRaises(subprocess.TimeoutExpired):
            cloud_store.command([sys.executable,'-c',script,str(pidfile)],self.root,timeout=1)
        pid=int(pidfile.read_text())
        status=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip()
        self.assertTrue(not status or status.startswith('Z'),status)
    def test_process_memory_diagnostics_never_read_arguments(self):
        import runner_resources
        for pid,rss in ((1,100),(2,500)):
            root=self.root/str(pid);root.mkdir()
            (root/'status').write_text(f'Name:\tgit\nVmRSS:\t{rss} kB\n')
            (root/'cmdline').write_text('a sensitive command argument')
        rows=runner_resources.process_memory(self.root)
        self.assertEqual(rows[0],{'pid':2,'name':'git','rss_bytes':500*1024})
        self.assertNotIn('sensitive',json.dumps(rows))
    def test_fresh_review_thread_uses_generation_virtual_environment(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'.gitignore').write_text('.venv/\n')
        subprocess.run([sys.executable,'-m','venv','--without-pip',str(work/'.venv')],check=True)
        python=work/'.venv/bin/python'
        site=Path(subprocess.check_output([str(python),'-c','import sysconfig; print(sysconfig.get_path("purelib"))'],text=True).strip())
        (site/'oss_test_only_dependency.py').write_text('VALUE = 42\n')
        self.assertNotEqual(subprocess.run([sys.executable,'-c','import oss_test_only_dependency'],cwd=work,capture_output=True).returncode,0)
        cfg=rt.config()|{'codex_socket':str(self.root/'fake.sock'),'cloud_environment':True}
        def execute(request,output):
            environment=dict(os.environ)|request['cache_env']
            result=subprocess.run(['python','-c','import oss_test_only_dependency as d; print(d.VALUE)'],cwd=work,env=environment,capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),'42')
            self.assertEqual(environment['VIRTUAL_ENV'],str(work/'.venv'))
            self.assertEqual(environment['NODE_OPTIONS'],'--max-old-space-size=4096')
            state_dir=Path(environment['XDG_STATE_HOME']);state_dir.mkdir(parents=True,exist_ok=True)
            self.assertTrue(state_dir.is_relative_to(folder/'tool-cache'))
            (state_dir/'pnpm-lock-fixture').write_text('writable')
            self.assertEqual(validation_setup.safe_env(folder/'tool-cache')['XDG_STATE_HOME'],str(state_dir))
            self.assertEqual(environment['XDG_CONFIG_HOME'],str(folder/'tool-cache/xdg-config'))
            self.assertIn(str(python),request['prompt'])
            return json.dumps({'verdict':'APPROVE','reason':'verified local dependency','tests_verified':True,'repairable':False})
        with patch.object(rt,'config',return_value=cfg),patch.object(worker,'binary',return_value='/fake/codex'),patch.object(codex_host,'execute',side_effect=execute):
            worker._agent('Independently review.',work,folder/'review.json',worker.REVIEW_SCHEMA,task=task)
    def test_tracked_virtual_environment_is_not_implicitly_selected(self):
        task=self.task();folder,work,base=self.checkout(task)
        root=work/'.venv';(root/'bin').mkdir(parents=True)
        (root/'pyvenv.cfg').write_text('test');(root/'bin/python').write_text('test')
        (work/'.gitignore').write_text('.venv/\n')
        self.git(work,'add','-f','.venv/pyvenv.cfg')
        env={'PATH':'/usr/bin'}
        self.assertEqual(execution.validation_environment(work,env),'')
        self.assertEqual(env,{'PATH':'/usr/bin'})
    def test_langfuse_runs_exclusively_without_stopping_existing_work(self):
        ordinary=self.task(number=11);heavy=self.task(number=12,repo='langfuse')
        with rt.db() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=?",(ordinary['id'],))
            self.assertIsNone(worker.next_queued(db,{}))
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?',(ordinary['id'],)).fetchone()[0],'running')
            db.execute("UPDATE tasks SET status='done' WHERE id=?",(ordinary['id'],))
            self.assertEqual(worker.next_queued(db,{})['id'],heavy['id'])
            db.execute("UPDATE tasks SET status='running' WHERE id=?",(heavy['id'],))
        self.task(number=13,repo='comfyui')
        with rt.db() as db:self.assertIsNone(worker.next_queued(db,{}))
    def test_langfuse_review_inherits_measured_memory_profile(self):
        task=self.task(repo='langfuse');folder,work,base=self.checkout(task)
        cfg=rt.config()|{'codex_socket':str(self.root/'fake.sock'),'cloud_environment':True}
        def execute(request,output):
            self.assertEqual(request['cache_env']['NODE_OPTIONS'],'--max-old-space-size=8192')
            self.assertEqual(request['cache_env']['XDG_CONFIG_HOME'],str(folder/'tool-cache/xdg-config'))
            return json.dumps({'verdict':'APPROVE','reason':'fixture','tests_verified':True,'repairable':False})
        with patch.object(rt,'config',return_value=cfg),patch.object(worker,'binary',return_value='/fake/codex'),patch.object(codex_host,'execute',side_effect=execute):
            worker._agent('Review',work,folder/'review.json',worker.REVIEW_SCHEMA,repo_key='langfuse',task=task)
    def test_validation_recovery_does_not_waive_hardware_or_repeat_memory_repair(self):
        for key,reason,marker in (
            ('langfuse','VALIDATION_ENVIRONMENT: Full web lint exhausted the runner’s fixed 4 GiB Node heap twice.','validation_memory_repaired'),
            ('openclaw','VALIDATION_ENVIRONMENT: Both system and Playwright Chromium aborted before rendering.','browser_config_repaired')):
            task={'kind':'fix','repo':key,'status':'validation_wait','attempts':3,'result':reason}
            state={'base':'a'*40}
            self.assertTrue(task_recovery.submission_regression(task,state))
            self.assertIsNone(task_recovery.submission_regression(task,state|{marker:{'at':1}}))
            self.assertIsNone(task_recovery.submission_regression(task|{'result':'Android device unavailable'},state))
    def test_checkpoint_failure_recovery_requires_matching_failure_and_unpublished_patch(self):
        task=self.task(repo='langfuse');folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 2\n')
        rt.task_state(task['id'],attempt=3,phase='validation',started=100,last_execution_phase='validation')
        with rt.db() as db:db.execute("UPDATE tasks SET status='interrupted',attempts=3 WHERE id=?",(task['id'],))
        work_evidence.capture(task['id'])
        self.assertEqual(task_recovery.recover_submission_regressions(),[])
        failure={'at':200,'type':'TimeoutExpired','reason':"Command '['git', 'push', 'origin', 'HEAD:refs/heads/codex-state']' timed out after 90 seconds"}
        rt.setmeta('runner_failure',failure)
        with patch.object(task_recovery,'audited_unpublished',return_value=(True,'verified absent')):
            self.assertEqual(task_recovery.recover_submission_regressions(),[task['id']])
        state=rt.task_state(task['id']);self.assertIn('checkpoint_repaired',state)
        self.assertIn('resume_progress',state)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT attempts FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],3)
            db.execute("UPDATE tasks SET status='interrupted' WHERE id=?",(task['id'],))
        rt.task_state(task['id'],recovery_checked_at=0)
        self.assertEqual(task_recovery.recover_submission_regressions(),[])
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
    def test_upstream_rename_preserves_source_for_adaptation_without_partial_apply(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n')
        (work/'test_value.py').write_text('assert value == 42\n')
        worker.finish(task,'blocked','review needs another check')
        entry=execution.retained_progress(task);rt.task_state(task['id'],resume_progress=entry)
        fresh=self.root/'advanced';self.git(self.root,'clone',str(work),str(fresh))
        self.git(fresh,'config','user.name','Test');self.git(fresh,'config','user.email','test@example.invalid')
        self.git(fresh,'mv','value.py','moved.py');self.git(fresh,'commit','-m','upstream rename')
        current=self.git(fresh,'rev-parse','HEAD')
        self.assertFalse(execution.restore_progress(task,fresh,current))
        self.assertEqual(self.git(fresh,'status','--porcelain'),'')
        state=rt.task_state(task['id']);self.assertFalse(state['progress_restored'])
        self.assertIn('value = 42',Path(state['progress_rebase_needed']['patch']).read_text())
        self.assertTrue((Path(state['progress_rebase_needed']['source'])/'test_value.py').exists())
        self.assertEqual(self.git(fresh,'rev-parse','HEAD'),current)
    def test_resume_context_keeps_findings_without_recursive_base64_transcripts(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n')
        findings={'outcome':'READY','reason':'fix retained','tests':'3 regressions passed','body':'Focused proof'}
        (folder/'generation.json').write_text(json.dumps(findings))
        (folder/'generation.events.jsonl').write_text('OLD_TRANSCRIPT_'*50000)
        worker.finish(task,'blocked','review needs correction')
        following=rt.DATA/'jobs'/str(task['id'])/'attempt-2';following.mkdir()
        path=work_evidence.recovery_context(task['id'],2,following)
        brief=json.loads(path.read_text())
        self.assertEqual(brief['results']['generation.json'],findings)
        self.assertNotIn('OLD_TRANSCRIPT',path.read_text());self.assertLess(path.stat().st_size,2048)
        self.assertTrue(task_recovery.latest_bundle(task['id']).exists())
    def test_resource_diagnostics_record_pressure_and_tolerate_unavailable_cgroups(self):
        import runner_resources
        memory=self.root/'meminfo';memory.write_text('MemTotal: 16000000 kB\nMemAvailable: 900000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        events=self.root/'events';events.write_text('oom 2\noom_kill 1\n')
        value=runner_resources.sample(memory,events,self.root)
        self.assertLess(value['memory_bytes']['MemAvailable'],1024**3)
        self.assertEqual(value['memory_events']['oom_kill'],1)
        events.unlink();memory.unlink()
        value=runner_resources.sample(memory,events,self.root)
        self.assertIsNone(value['memory_bytes']);self.assertIsNone(value['memory_events'])
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
        self.git(source,'update-ref','refs/pull/1/head',original)
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
    def test_old_pr_fetches_complete_history_without_publishing(self):
        source,work,original=self.response_fixture()
        base=self.git(source,'merge-base','main','old-pr')
        # Long main history exceeds both cheap fetch steps; build it in one process.
        rows=[];tip=self.git(source,'rev-parse','main')
        for i in range(600):
            rows.append(f'commit refs/heads/main\ncommitter Test <test@example.invalid> {1800000000+i} +0000\ndata 7\nadvance\n'+
                        ('from '+tip+'\n' if i==0 else '')+'\n')
        subprocess.run(['git','fast-import','--quiet'],cwd=source,input=''.join(rows),text=True,check=True,capture_output=True)
        self.git(work,'fetch','--depth=1','upstream','+main:refs/remotes/upstream/main')
        result=execution.update_response_base(work,'main',original,1)
        self.assertEqual(self.git(work,'rev-parse','--is-shallow-repository'),'false')
        self.assertEqual(self.git(work,'merge-base',original,'upstream/main'),base)
        self.git(work,'merge-base','--is-ancestor',original,result['base'])
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
        self.assertEqual((work/'fix').read_text(),'fix\n')
    def test_changed_pr_head_during_history_fetch_does_not_merge(self):
        source,work,original=self.response_fixture()
        self.git(source,'update-ref','refs/pull/1/head',self.git(source,'rev-parse','main'))
        with self.assertRaisesRegex(rt.Paused,'PR head moved'):
            execution.update_response_base(work,'main',original,1)
        self.assertEqual(self.git(work,'rev-parse','HEAD'),original)
    def test_full_graph_response_checkout_refuses_changed_pr_head(self):
        source,unused,original=self.response_fixture()
        self.git(source,'update-ref','refs/pull/1/head',self.git(source,'rev-parse','main'))
        work=self.root/'fresh-response'
        with self.assertRaisesRegex(rt.Paused,'PR head moved'):
            execution.checkout_response(work,source.as_uri(),source.as_uri(),'main',
                {'head':{'sha':original,'ref':'old-pr'}},1)
        self.assertEqual(self.git(work,'rev-parse','--is-shallow-repository'),'false')
        self.assertEqual(self.git(work,'branch','--list','old-pr'),'')
    def test_timed_out_git_process_reaps_helper_writers(self):
        marker=self.root/'orphan-write'
        child='import time;from pathlib import Path;time.sleep(1);Path('+repr(str(marker))+').touch()'
        parent='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);print("started",flush=True);time.sleep(10)'
        with self.assertRaises(subprocess.TimeoutExpired):
            execution.git_process([sys.executable,'-c',parent],timeout=.3)
        time.sleep(1.1)
        self.assertFalse(marker.exists(),'Timed-out helper still wrote to the checkout')
    def test_history_failure_recovery_requires_unchanged_unpublished_head(self):
        task=self.task(repo='openclaw/openclaw',kind='respond');task.update(status='error',
            result='PR history exceeds bounded merge-base fetch; retained for follow-up')
        state={'phase':'error','last_execution_phase':'checkout','remote_base':'a'*40,'base':None}
        rt.task_state(task['id'],**state)
        self.assertTrue(task_recovery.repaired_infrastructure(task,state))
        with patch.object(worker,'response_context',return_value={'head':{'sha':'a'*40}}):
            self.assertTrue(task_recovery.audited_unpublished(task)[0])
        with patch.object(worker,'response_context',return_value={'head':{'sha':'b'*40}}):
            self.assertFalse(task_recovery.audited_unpublished(task)[0])
        self.assertFalse(task_recovery.repaired_infrastructure(task,state|{'publication_started':True}))
        self.assertFalse(task_recovery.repaired_infrastructure(task|{'result':'Current upstream conflicts with PR branch'},state))
    def test_base_conflict_leaves_original_pr_head_intact(self):
        source,work,original=self.response_fixture(conflict=True)
        with self.assertRaisesRegex(RuntimeError,'conflicts'):execution.update_response_base(work,'main',original)
        self.assertEqual(self.git(work,'rev-parse','HEAD'),original)
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
        self.assertEqual(self.git(work,'status','--porcelain'),'')
    def test_shallow_old_common_ancestor_does_not_trigger_false_conflicts(self):
        source=self.root/'source';source.mkdir();self.git(source,'init','-b','main')
        self.git(source,'config','user.name','Test');self.git(source,'config','user.email','test@example.invalid')
        (source/'shared').write_text('old\n');self.git(source,'add','.');self.git(source,'commit','-m','root')
        root=self.git(source,'rev-parse','HEAD');self.git(source,'branch','side')
        (source/'shared').write_text('base\n');self.git(source,'commit','-am','actual PR base')
        base=self.git(source,'rev-parse','HEAD');self.git(source,'checkout','-b','old-pr')
        (source/'shared').write_text('patch\n');self.git(source,'commit','-am','fix')
        original=self.git(source,'rev-parse','HEAD');self.git(source,'checkout','main')
        for i in range(8):self.git(source,'commit','--allow-empty','-m','main '+str(i))
        self.git(source,'checkout','side');(source/'side').write_text('independent\n')
        self.git(source,'add','.');self.git(source,'commit','-m','short path to old ancestor')
        self.git(source,'checkout','main');self.git(source,'merge','--no-ff','side','-m','merge side')
        work=self.root/'checkout';self.git(self.root,'clone','--depth=3',source.as_uri(),str(work))
        self.git(work,'remote','add','upstream',source.as_uri())
        self.git(work,'fetch','--depth=3','upstream','main:refs/remotes/upstream/main')
        self.git(work,'fetch','--depth=3','origin','old-pr');self.git(work,'checkout','-b','old-pr','FETCH_HEAD')
        self.assertEqual(self.git(work,'merge-base','HEAD','upstream/main'),root)
        self.assertFalse(execution.response_history_complete(work,'upstream/main'))
        execution.update_response_base(work,'main',original)
        self.assertEqual(self.git(work,'merge-base',original,'upstream/main'),base)
        self.assertEqual((work/'shared').read_text(),'patch\n')
        self.assertTrue((work/'side').exists())
        self.git(work,'merge-base','--is-ancestor',original,'HEAD')
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
    def test_non_conflict_merge_errors_keep_the_actual_cause(self):
        source,work,original=self.response_fixture();real_git=worker.git
        def fail_merge(path,*args):
            if 'merge' in args:raise subprocess.TimeoutExpired('git merge',300)
            return real_git(path,*args)
        with patch.object(worker,'git',side_effect=fail_merge):
            with self.assertRaises(subprocess.TimeoutExpired):execution.update_response_base(work,'main',original)
        self.assertEqual(self.git(work,'rev-parse','HEAD'),original)
    def test_false_conflict_recovery_preserves_attempts_and_requires_unchanged_remote(self):
        task=self.task(repo='openclaw/openclaw',kind='respond');task.update(attempts=3,status='error')
        old_error='Current upstream conflicts with PR branch; manual conflict resolution required'
        with rt.db() as db:db.execute("UPDATE tasks SET status='error',attempts=3,result=? WHERE id=?",(old_error,task['id']))
        rt.task_state(task['id'],attempt=3,phase='error',last_execution_phase='checkout',base=None)
        evidence=rt.DATA/'evidence';evidence.mkdir()
        (evidence/f"{task['id']}-2.json").write_text(json.dumps({'execution':{'attempt':2,'base':'a'*40}}))
        (evidence/f"{task['id']}-3.json").write_text(json.dumps({'execution':{'attempt':3,'base':None},'files':{}}))
        with patch.object(worker,'response_context',return_value={'head':{'sha':'b'*40}}):
            self.assertEqual(task_recovery.recover_shallow_merge_errors(),[])
        with patch.object(worker,'response_context',return_value={'head':{'sha':'a'*40}}):
            self.assertEqual(task_recovery.recover_shallow_merge_errors(),[task['id']])
            self.assertEqual(task_recovery.recover_shallow_merge_errors(),[])
        with rt.db() as db:self.assertEqual(db.execute('SELECT attempts FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],3)
        self.assertEqual(self.status(task),'queued')
        self.assertEqual(rt.task_state(task['id'])['remote_base'],'a'*40)
        with rt.db() as db:db.execute("UPDATE tasks SET status='error' WHERE id=?",(task['id'],))
        self.assertEqual(task_recovery.recover_shallow_merge_errors(),[])
    def run_real_response_controller(self,outcome,fail_fetch=False):
        source,unused,original=self.response_fixture()
        task=self.task(repo='openclaw/openclaw',kind='respond')
        pr={'head':{'sha':original,'ref':'old-pr'},'body':'Existing PR description',
            'html_url':'https://github.com/openclaw/openclaw/pull/1'}
        real_run=worker.run;fetches=[]
        def run(args,**kwargs):
            if args[0]=='gh':
                self.assertEqual(args[:4],['gh','api','--paginate','--slurp'])
                return '[[]]'
            if args[0]=='git':args=[source.as_uri() if arg=='https://github.com/openclaw/openclaw.git' else arg for arg in args]
            if fail_fetch and args[:5]==['git','fetch','--no-tags','--filter=blob:none','upstream']:
                fetches.append(args)
                raise subprocess.TimeoutExpired(args,300)
            return real_run(args,**kwargs)
        def api(path):
            if path=='repos/chelsealong/openclaw':return {'parent':{'full_name':'openclaw/openclaw'},'clone_url':source.as_uri()}
            if path=='repos/openclaw/openclaw':return {'default_branch':'main'}
            if path=='repos/openclaw/openclaw/issues/1':return {'body':'Please update against current main','state':'open'}
            raise AssertionError('Unexpected API '+path)
        def agent(prompt,work,output,*args,**kwargs):
            self.assertFalse(fail_fetch,'A timed-out checkout must not reach a model')
            self.assertEqual(self.git(work,'rev-parse','--is-shallow-repository'),'false')
            self.assertEqual(self.git(work,'config','remote.upstream.promisor'),'true')
            self.assertEqual(self.git(work,'remote','get-url','origin'),source.as_uri())
            if output.stem=='generation':
                if outcome=='READY':(work/'fix').write_text('updated fix\n')
                value={'outcome':outcome,'reason':'addressed','title':'Update fix against main',
                       'body':'Verified the requested change. Codex assisted.','tests':'fixture verified','pr_body':''}
            else:value={'verdict':'APPROVE','tests_verified':True,'repairable':False,'reason':'verified'}
            output.write_text(json.dumps(value));return value
        with patch.object(worker,'run',side_effect=run),patch.object(worker,'api',side_effect=api),\
             patch.object(worker,'response_context',return_value=pr),patch.object(worker,'agent',side_effect=agent),\
             patch.object(worker.pr_followup,'publish',return_value='https://example.invalid/comment') as post:
            if fail_fetch:
                with self.assertRaises(subprocess.TimeoutExpired) as caught:worker.process(task)
                worker.handle_task_error(task,caught.exception)
            else:worker.process(task)
        if fail_fetch:
            self.assertEqual(len(fetches),1);self.assertEqual(self.status(task),'retry_wait')
            post.assert_not_called()
        else:self.assertEqual(self.status(task),'done')
        return source,original,post.call_args,rt.task_state(task['id'])
    def test_response_fetch_timeout_defers_without_reusing_possibly_locked_checkout(self):
        source,original,call,state=self.run_real_response_controller('READY',fail_fetch=True)
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
        self.assertIsNone(state.get('base'));self.assertFalse(state.get('publication_started'))
    def test_reply_only_controller_preserves_original_pr_base(self):
        source,original,call,state=self.run_real_response_controller('REPLY')
        self.assertEqual(self.git(source,'rev-parse','old-pr'),original)
        self.assertEqual(call.args[2],original)
        self.assertEqual(state['base'],original)
        self.assertFalse(state['base_update']['merged'])
    def test_code_update_controller_publishes_reviewed_fast_forward_without_main_merge(self):
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
    def test_submission_does_not_run_optional_broad_diagnostic(self):
        task=self.task();folder,work,base=self.checkout(task)
        (folder/'required-check.json').write_text(json.dumps({'exit_code':1,'head':base}))
        (folder/'required-check.log').write_text('Unchanged baseline dead export')
        verdict={'verdict':'APPROVE','tests_verified':True,'repairable':False,'reason':'focused checks verified; baseline disclosed'}
        with patch.object(execution,'managed_check',side_effect=AssertionError('Optional broad diagnostic must not block PR submission')),\
             patch.object(worker,'agent',return_value=verdict) as agent:
            _,review=worker.review_patch(task,work,folder,'openclaw',base,{},'review')
        self.assertEqual(review,verdict)
        self.assertIn('Inspect and disclose',agent.call_args.args[0])
    def test_required_focused_failure_still_blocks_submission(self):
        task=self.task();folder,work,base=self.checkout(task)
        verdict={'verdict':'BLOCK','tests_verified':False,'repairable':False,'reason':'Required regression failed'}
        with patch.object(worker,'agent',return_value=verdict):
            _,review=worker.review_patch(task,work,folder,'openclaw',base,{},'review')
        self.assertEqual(review,verdict)
    def test_repair_still_requires_fresh_independent_review(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 2\n')
        result={'outcome':'READY','reason':'fixed','title':'fix','body':'Codex','tests':'focused tests pass'}
        calls=[]
        def agent(*args,**kwargs):
            phase=args[2].stem;calls.append(phase)
            if phase=='review':return {'verdict':'BLOCK','tests_verified':False,'repairable':True,'reason':'test fixture needs correction'}
            if phase=='remediation':
                (work/'value.py').write_text('value = 3\n');return result
            self.assertEqual((work/'value.py').read_text(),'value = 3\n')
            return {'verdict':'APPROVE','tests_verified':True,'repairable':False,'reason':'verified'}
        with patch.object(worker,'agent',side_effect=agent):
            _,review=worker.review_patch(task,work,folder,'openclaw',base,result,'review')
        self.assertEqual(calls,['review','remediation','rereview'])
        self.assertEqual(review['verdict'],'APPROVE')
    def test_confirmed_conflict_is_not_bypassed_by_response_preparation(self):
        source,work,original=self.response_fixture(conflict=True)
        with self.assertRaisesRegex(RuntimeError,'conflicts'):
            execution.prepare_response_base(work,'main',{'head':{'sha':original},'mergeable':False},1)
        self.assertEqual(self.git(work,'rev-parse','HEAD'),original)
    def test_optional_gate_recovery_restores_patch_once_without_approval(self):
        task=self.task();folder,work,base=self.checkout(task)
        (work/'value.py').write_text('value = 42\n')
        rt.task_state(task['id'],phase='review')
        worker.finish(task,'blocked','review: The mandatory controller gate failed on a dead-export scan; unchanged baseline.')
        with patch.object(task_recovery,'optional_api',return_value=None),patch.object(worker,'run',return_value='[]'):
            self.assertEqual(task_recovery.recover_submission_regressions(),[task['id']])
            self.assertEqual(task_recovery.recover_submission_regressions(),[])
        state=rt.task_state(task['id']);self.assertIn('resume_progress',state)
        self.assertNotIn('approval_resume',state);self.assertEqual(self.status(task),'queued')
        with rt.db() as db:self.assertEqual(db.execute('SELECT attempts FROM tasks').fetchone()[0],1)
    def test_exhausted_checkout_regression_gets_one_audited_attempt_not_reset(self):
        task=self.task(repo='openclaw/openclaw',kind='respond')
        with rt.db() as db:
            db.execute("UPDATE tasks SET status='error',attempts=3,result=?",('git failed (128): fatal: shallow file has changed since we read it\n',))
        rt.task_state(task['id'],attempt=3,last_execution_phase='checkout',base=None,remote_base='a'*40)
        evidence=rt.DATA/'evidence';evidence.mkdir()
        (evidence/f"{task['id']}-3.json").write_text(json.dumps({'execution':{'attempt':3,'base':None},'files':{}}))
        with patch.object(worker,'response_context',return_value={'head':{'sha':'b'*40}}):
            self.assertEqual(task_recovery.recover_submission_regressions(),[])
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(worker,'response_context',return_value={'head':{'sha':'a'*40}}):
            self.assertEqual(task_recovery.recover_submission_regressions(),[task['id']])
            with rt.db() as db:db.execute("UPDATE tasks SET status='error'")
            self.assertEqual(task_recovery.recover_submission_regressions(),[])
        with rt.db() as db:self.assertEqual(db.execute('SELECT attempts FROM tasks').fetchone()[0],3)


if __name__=='__main__':unittest.main()
