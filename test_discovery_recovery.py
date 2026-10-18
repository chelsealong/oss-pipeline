"""Budget pauses and transient vet failures cannot lose discovered issues."""
import json
import contextlib
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import watch
import runtime as rt
import local_service


class DiscoveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patches=[patch.object(watch,'PENDING_VET',self.root/'pending.json'),
                      patch.object(watch,'DEFERRED',self.root/'deferred.json'),
                      patch.object(watch,'log'),patch.object(watch,'NO_TRIGGER',True),
                      patch.object(rt,'ROOT',self.root),patch.object(rt,'DATA',self.root/'data'),
                      patch.object(rt,'CONFIG',self.root/'runtime.json'),patch.object(rt,'checkpoint')]
        for p in self.patches:p.start()
        self.node={'number':123,'title':'A reproducible bug','body':'Reproduction. '*30,
                   'url':'https://github.com/google/adk-python/issues/123',
                   'createdAt':'2026-10-09T00:00:00Z','author':{'login':'reporter'},
                   'assignees':{'nodes':[]},'labels':{'nodes':[]},'comments':{'totalCount':0}}

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def sweep(self, vetting=True):
        response=json.dumps({'data':{'r0':{'issues':{'nodes':[self.node]}}}})
        with patch.object(watch.scan,'gh',return_value=response):
            return watch.sweep(['adk'],{},100,False,vetting=vetting)

    def test_budget_paused_discovery_is_durable_and_never_calls_a_judge(self):
        with patch.object(watch.scan,'vet',side_effect=AssertionError('model forbidden')):
            self.assertEqual(self.sweep(vetting=False),(1,0))
        self.assertIn('123',watch.pending_vet()['adk'])
        with patch.object(rt,'ready',return_value=(False,'budget exhausted')),patch.object(watch.scan,'gh') as api:
            self.assertEqual(watch.recheck_pending_vet(['adk']),0)
            api.assert_not_called()
        self.assertIn('123',watch.pending_vet()['adk'])

    def test_transient_vet_failure_survives_seen_deduplication(self):
        with patch.object(watch.scan,'vet',side_effect=rt.Paused('judge budget exhausted')):
            self.assertEqual(self.sweep(),(1,0))
        self.assertIn('123',watch.pending_vet()['adk'])

    def test_service_keeps_github_discovery_running_when_coding_budget_is_empty(self):
        with patch('sys.argv',['local_service.py','watch','--once']),\
             patch.object(rt,'lock',return_value=contextlib.nullcontext()),\
             patch.object(rt,'setmeta'),patch.object(rt,'ready',return_value=(False,'budget exhausted')),\
             patch.object(rt,'config',return_value={'enabled':True}),\
             patch.dict(watch.scan.REPOS,{'adk':{'upstream':'google/adk-python'}},clear=True),\
             patch.object(watch,'load_seen',return_value={'adk':[]}),\
             patch('task_recovery.refresh_publication_holds'),\
             patch.object(watch,'save_seen'),patch.object(watch,'sweep',return_value=(1,0)) as discover:
            local_service.main()
            discover.assert_called_once_with(['adk'],{'adk':[]},100,False,vetting=False)

    def test_permission_held_repo_uses_discovery_only_while_other_repos_are_vetted(self):
        with patch('sys.argv',['local_service.py','watch','--once']),\
             patch.object(rt,'lock',return_value=contextlib.nullcontext()),\
             patch.object(rt,'setmeta'),patch.object(rt,'ready',return_value=(True,'')),\
             patch.object(rt,'room',return_value=(False,'')),\
             patch.object(rt,'publication_holds',return_value={'hermes':time.time()+86400}),\
             patch.dict(watch.scan.REPOS,{'hermes':{},'adk':{}},clear=True),\
             patch.object(watch,'load_seen',return_value={'hermes':[],'adk':[]}),\
             patch.object(watch,'save_seen'),patch('task_recovery.refresh_publication_holds'),\
             patch.object(watch,'sweep',return_value=(0,0)) as discover,\
             patch.object(watch,'recheck_pending_vet') as vet,\
             patch.object(watch.scan,'scan_repo',return_value={'partial':True}) as scan:
            local_service.main()
            self.assertEqual(discover.call_args_list[0].args[0],['hermes'])
            self.assertEqual(discover.call_args_list[0].kwargs,{'vetting':False})
            self.assertEqual(discover.call_args_list[1].args[0],['adk'])
            vet.assert_called_once_with(['adk']);self.assertEqual(scan.call_args.args[0],'adk')

    def test_recovery_refreshes_issue_and_never_dispatches_a_closed_one(self):
        self.sweep(vetting=False)
        with patch.object(rt,'ready',return_value=(True,'')),\
             patch.object(watch.scan,'gh',return_value=json.dumps({'state':'closed'})),\
             patch.object(watch.scan,'vet') as vet,patch.object(watch,'append_candidate') as append:
            self.assertEqual(watch.recheck_pending_vet(['adk']),0)
            vet.assert_not_called();append.assert_not_called()
        self.assertEqual(watch.pending_vet()['adk'],{})

    def test_recovery_admits_only_after_fresh_successful_vetting(self):
        self.sweep(vetting=False)
        issue=watch.to_rest_shape(self.node);issue.update(state='open',title='Updated live title')
        with patch.object(rt,'ready',return_value=(True,'')),\
             patch.object(watch.scan,'gh',return_value=json.dumps(issue)),\
             patch.object(watch.scan,'vet',return_value=(True,'clear',{})) as vet,\
             patch.object(watch,'append_candidate') as append:
            self.assertEqual(watch.recheck_pending_vet(['adk']),1)
            self.assertEqual(vet.call_args.args[2]['title'],'Updated live title')
            self.assertEqual(append.call_args.args[1]['number'],123)
        self.assertEqual(watch.pending_vet()['adk'],{})

    def test_known_issue_triage_transition_wakes_admission_once_without_model(self):
        seen={'openclaw':[123]}
        cfg=watch.scan.REPOS['openclaw']
        self.node['labels']['nodes']=[{'name':name} for name in cfg['triage_ready_labels']]
        response=json.dumps({'data':{'r0':{'issues':{'nodes':[self.node]}}}})
        watch.defer('openclaw',123,self.node['createdAt'])
        with patch.object(rt,'local',return_value=True),patch.object(watch.scan,'gh',return_value=response),\
             patch.object(watch.scan,'vet',side_effect=AssertionError('Discovery must not judge')):
            self.assertEqual(watch.sweep(['openclaw'],seen,5,False,vetting=False),(0,0))
            self.assertIn('123',watch.pending_vet()['openclaw'])
            # Even after admission removes it, unchanged labels cannot replay it.
            watch.save_pending_vet({})
            watch.sweep(['openclaw'],seen,5,False,vetting=False)
            self.assertEqual(watch.pending_vet(),{})
        issue=watch.to_rest_shape(self.node);issue['state']='open'
        watch.retain_for_vetting('openclaw',self.node)
        with patch.object(rt,'ready',return_value=(True,'')),patch.object(watch.scan,'gh',return_value=json.dumps(issue)),\
             patch.object(watch.scan,'vet_candidate',return_value=(False,'claimed by another contributor',{})):
            self.assertEqual(watch.recheck_pending_vet(['openclaw']),0)
        self.assertNotIn('openclaw',watch._load_deferred())


if __name__=='__main__':unittest.main()
