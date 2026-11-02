"""Reproduce fork-only blobs and safe Hermes restoration without network/models."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import codex_worker as worker
import execution_support as execution
import repo_limits
import runtime as rt
import task_recovery
import work_evidence


class NovemberRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name,value in [('ROOT',self.root),('DATA',self.root/'data'),
                           ('CONFIG',self.root/'runtime.json')]:
            self.enterContext(patch.object(rt,name,value))
        self.enterContext(patch.object(rt,'checkpoint'))
        rt.CONFIG.write_text(json.dumps({'backend':'codex-local','enabled':True,'max_pending':8}))
        rt.setmeta('health',{'ok':True,'at':time.time()})
        rt.setmeta('worker_heartbeat',time.time())

    def git(self,work,*args):
        return subprocess.run(['git',*args],cwd=work,check=True,capture_output=True,text=True).stdout.strip()

    def fork_fixture(self):
        upstream=self.root/'upstream';upstream.mkdir()
        self.git(upstream,'init','-b','main')
        self.git(upstream,'config','user.name','Test')
        self.git(upstream,'config','user.email','test@example.invalid')
        self.git(upstream,'config','uploadpack.allowFilter','true')
        (upstream/'base').write_text('base\n')
        self.git(upstream,'add','.');self.git(upstream,'commit','-m','base')
        fork=self.root/'fork'
        self.git(self.root,'clone',upstream.as_uri(),str(fork))
        self.git(fork,'config','user.name','Test')
        self.git(fork,'config','user.email','test@example.invalid')
        self.git(fork,'config','uploadpack.allowFilter','true')
        self.git(fork,'checkout','-b','fix-owned')
        (fork/'fork-only.txt').write_text('A fork-only blob absent from upstream upload-pack\n')
        self.git(fork,'add','.');self.git(fork,'commit','-m','fork change')
        head=self.git(fork,'rev-parse','HEAD')
        # Upstream serves the PR graph, but cannot hydrate its fork-only blob.
        # Copy just commit/tree objects, leaving blob ownership with the fork.
        for oid in self.git(fork,'rev-list','--objects','--no-object-names','HEAD','^main').splitlines():
            kind=self.git(fork,'cat-file','-t',oid)
            if kind=='blob':continue
            raw=subprocess.run(['git','cat-file',kind,oid],cwd=fork,check=True,capture_output=True).stdout
            subprocess.run(['git','hash-object','-t',kind,'-w','--stdin'],cwd=upstream,
                           input=raw,check=True,capture_output=True)
        self.git(upstream,'update-ref','refs/pull/1/head',head)
        return upstream,fork,{'head':{'sha':head,'ref':'fix-owned'}}

    def test_fork_only_blob_reproduces_failure_and_checkout_hydrates_from_owner(self):
        upstream,fork,pr=self.fork_fixture()
        real_git=worker.git
        def old_config(work,*args):
            # Emulate the former code: origin was a publishing remote only.
            if args[:2]==('config','remote.origin.promisor'):return ''
            if args[:2]==('config','remote.origin.partialclonefilter'):return ''
            return real_git(work,*args)
        broken=self.root/'broken'
        with patch.object(worker,'git',side_effect=old_config):
            with self.assertRaisesRegex(RuntimeError,'promisor remote'):
                execution.checkout_response(broken,upstream.as_uri(),fork.as_uri(),'main',pr,1)
        work=self.root/'fixed'
        execution.checkout_response(work,upstream.as_uri(),fork.as_uri(),'main',pr,1)
        self.assertEqual((work/'fork-only.txt').read_bytes(),(fork/'fork-only.txt').read_bytes())
        self.assertEqual(self.git(work,'rev-parse','HEAD'),pr['head']['sha'])
        self.assertEqual(self.git(work,'rev-parse','--is-shallow-repository'),'false')
        self.assertEqual(self.git(work,'status','--porcelain'),'')
        self.assertEqual(self.git(upstream,'rev-parse','main'),self.git(fork,'rev-parse','main'))

    def test_hermes_restoration_preserves_holds_attempts_and_spend(self):
        now=time.time()
        with rt.db() as db:
            for ident,kind,repo in [(1,'fix','hermes'),(2,'respond','NousResearch/hermes-agent'),
                                    (3,'fix','firecrawl'),(4,'fix','hermes')]:
                db.execute('INSERT INTO tasks(id,kind,repo,number,status,created,updated,attempts) '
                    'VALUES (?,?,?,?,?,?,?,?)',(ident,kind,repo,ident,'quota_wait',now,now,2))
            db.execute('INSERT INTO calls VALUES (?,?)',(now,'codex'))
        for ident in (1,2,3,4):rt.task_state(ident,quota_prior_status='queued',folder='retained')
        rt.task_state(2,quota_prior_result='Existing actionable feedback')
        rt.task_state(4,publication_started=True)
        with rt.db() as db:
            db.execute('BEGIN IMMEDIATE')
            repo_limits.restore_enabled_pending(db,{'hermes':now+86400})
            rows={r['id']:dict(r) for r in db.execute('SELECT * FROM tasks')}
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
        self.assertEqual([rows[i]['status'] for i in (1,2,3,4)],
                         ['publication_wait','queued','quota_wait','quota_wait'])
        self.assertTrue(all(r['attempts']==2 for r in rows.values()))
        self.assertEqual(rt.task_state(2)['folder'],'retained')
        self.assertEqual(rows[2]['result'],'Existing actionable feedback')
        self.assertEqual(repo_limits.PR_CAPS['hermes'],8)
        self.assertEqual(repo_limits.TASK_BUDGETS['hermes'],16)

    def test_promisor_recovery_checks_remote_and_spends_no_model_turn(self):
        self.assertTrue(rt.enqueue('respond','openclaw/openclaw',154033))
        reason=('git failed (128): fatal: remote error: upload-pack: not our ref '+'a'*40+
                '\nfatal: could not fetch '+'b'*40+' from promisor remote\n')
        with rt.db() as db:
            db.execute("UPDATE tasks SET status='error',result=?,attempts=1",(reason,))
            task=dict(db.execute('SELECT * FROM tasks').fetchone())
        rt.task_state(task['id'],phase='error',last_execution_phase='checkout',
                      remote_base='c'*40,base=None)
        self.assertTrue(task_recovery.repaired_infrastructure(task,rt.task_state(task['id'])))
        with patch.object(worker,'response_context',return_value={'head':{'sha':'d'*40}}):
            self.assertEqual(task_recovery.reconcile(),[])
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(worker,'response_context',return_value={'head':{'sha':'c'*40}}):
            self.assertEqual(task_recovery.reconcile(),[task['id']])
        state=rt.task_state(task['id'])
        self.assertIn('response_promisor_repaired',state)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT attempts FROM tasks').fetchone()[0],1)
        self.assertFalse(task_recovery.repaired_infrastructure(task,state))

    def test_obsolete_universal_proof_wait_resumes_once_without_approving_patch(self):
        self.assertTrue(rt.enqueue('fix','openclaw',190123))
        with rt.db() as db:
            db.execute("UPDATE tasks SET attempts=1,status='running'")
            task=dict(db.execute('SELECT * FROM tasks').fetchone())
        folder=rt.DATA/'jobs'/str(task['id'])/'attempt-1'
        work=folder/'work';work.mkdir(parents=True)
        self.git(work,'init','-b','main');self.git(work,'config','user.name','Test')
        self.git(work,'config','user.email','test@example.invalid')
        (work/'value.py').write_text('value = 1\n');self.git(work,'add','.')
        self.git(work,'commit','-m','baseline');base=self.git(work,'rev-parse','HEAD')
        (work/'value.py').write_text('value = 2\n')
        reason=('VALIDATION_ENVIRONMENT: The isolated runner has no available model credentials or endpoint, '
                'so the repository-required live Gateway and model proof cannot be completed. '
                'The tested source and regression patch is preserved in the checkout.')
        rt.task_state(task['id'],attempt=1,folder=str(folder.relative_to(rt.DATA)),
                      base=base,phase='validation_wait',last_execution_phase='generation')
        with rt.db() as db:db.execute("UPDATE tasks SET status='validation_wait',result=?",(reason,))
        work_evidence.capture(task['id'])
        with patch.object(task_recovery,'audited_unpublished',return_value=(False,'remote branch exists')):
            self.assertEqual(task_recovery.recover_submission_regressions(),[])
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(task_recovery,'audited_unpublished',return_value=(True,'no remote branch')), \
             patch.object(worker,'cap_ok',return_value=(True,'')):
            self.assertEqual(task_recovery.recover_submission_regressions(),[task['id']])
            self.assertEqual(task_recovery.recover_submission_regressions(),[])
        state=rt.task_state(task['id'])
        self.assertIn('resume_progress',state)
        self.assertIn('submission_proof_rechecked',state)
        self.assertNotIn('approval_resume',state)
        self.assertNotIn('review',state)
        with rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],0)
        task.update(status='validation_wait',result='VALIDATION_ENVIRONMENT: Required Android screenshots unavailable.')
        self.assertIsNone(task_recovery.submission_regression(task,state))


if __name__=='__main__':unittest.main()
