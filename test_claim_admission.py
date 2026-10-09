"""Claim text after long reproductions must disqualify work before coding."""
import hashlib
import unittest
from unittest.mock import patch
import intent


class ClaimAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.cache = {}
        self.patches = [patch.object(intent, '_cache', side_effect=lambda:self.cache),
                        patch.object(intent, '_save', side_effect=lambda value:None),
                        patch.object(intent, '_log')]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()

    def report(self, tail):
        return 'I observed this reproducible failure.\n' + ('Detailed reproduction evidence.\n'*100) + tail

    def judge(self, system, text, **kwargs):
        return {'claim':'I have implemented the fix' in text, 'why':'ready-made fix' if
                'I have implemented the fix' in text else 'report only'}

    def test_ready_made_fix_after_long_report_is_not_admitted(self):
        report=self.report('I have implemented the fix and will open a PR.')
        with patch.object(intent, '_ask', side_effect=self.judge):
            self.assertTrue(intent.is_claim(report,author='reporter')[0])

    def test_same_reproduction_with_different_contribution_intent_has_distinct_cache(self):
        with patch.object(intent, '_ask', side_effect=self.judge) as ask:
            self.assertFalse(intent.is_claim(self.report('I cannot implement a fix.'))[0])
            self.assertTrue(intent.is_claim(self.report('I have implemented the fix.'))[0])
            self.assertEqual(ask.call_count,2)

    def test_old_prefix_only_false_verdict_cannot_hide_ready_made_fix(self):
        report=self.report('I have implemented the fix.')
        old_key=hashlib.sha256(intent._strip_markup(report)[:1200].encode()).hexdigest()[:16]
        self.cache[old_key]={'claim':False,'why':'old incomplete report'}
        with patch.object(intent, '_ask', side_effect=self.judge) as ask:
            self.assertTrue(intent.is_claim(report)[0])
            ask.assert_called_once()

    def test_quoted_other_persons_claim_is_not_attributed_to_reporter(self):
        report='I observed the failure.\n> I have implemented the fix.\nI cannot implement a fix.'
        with patch.object(intent, '_ask', side_effect=self.judge):
            self.assertFalse(intent.is_claim(report)[0])


if __name__=='__main__':unittest.main()
