"""Budget pauses and transient vet failures cannot lose discovered issues."""
import json
import contextlib
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
                      patch.object(watch,'log'),patch.object(watch,'NO_TRIGGER',True)]
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
             patch.object(watch,'save_seen'),patch.object(watch,'sweep',return_value=(1,0)) as discover:
            local_service.main()
            discover.assert_called_once_with(['adk'],{'adk':[]},100,False,vetting=False)

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


if __name__=='__main__':unittest.main()
