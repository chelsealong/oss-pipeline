"""Reviewed UI evidence publication and bounded recovery, with no network calls."""
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import runtime as rt
import codex_worker as worker
import screenshot_evidence as images
import task_recovery as recovery
import work_evidence
import test_production_reliability as fixtures

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1cAAAAASUVORK5CYII=')


class ScreenshotEvidenceTests(unittest.TestCase):
    setUp=fixtures.ProductionReliabilityTests.setUp
    tearDown=fixtures.ProductionReliabilityTests.tearDown
    task=fixtures.ProductionReliabilityTests.task
    checkout=fixtures.ProductionReliabilityTests.checkout
    git=fixtures.ProductionReliabilityTests.git
    status=fixtures.ProductionReliabilityTests.status

    def evidence(self):
        task=self.task();folder,work,base=self.checkout(task)
        root=folder/'tool-cache'/'pr-evidence';root.mkdir(parents=True)
        for name in ('before','after'):(root/(name+'.png')).write_bytes(PNG)
        result,manifest=images.prepare({'body':'Scope and actual checks'},task,folder)
        return task,folder,work,base,result,manifest

    def test_description_is_idempotent_and_response_preserves_original(self):
        task,folder,work,base,result,manifest=self.evidence()
        again,unused=images.prepare(result,task,folder)
        self.assertEqual(result,again);self.assertEqual(len(manifest),2)
        self.assertIn(manifest[0]['url'],result['body'])
        (folder/'context.json').write_text(json.dumps({'pr':{'body':'Existing disclosure'}}))
        result,unused=images.prepare({'pr_body':''},task|{'kind':'respond'},folder)
        self.assertTrue(result['pr_body'].startswith('Existing disclosure'))

    def test_invalid_partial_or_symlink_images_never_prepare_for_publication(self):
        task,folder,work,base,result,manifest=self.evidence()
        path=Path(manifest[0]['path']);path.unlink()
        with self.assertRaisesRegex(RuntimeError,'both'):images.prepare(result,task,folder)
        path.write_bytes(b'not a PNG')
        with self.assertRaisesRegex(RuntimeError,'PNG'):images.prepare(result,task,folder)
        path.unlink();outside=self.root/'external.png';outside.write_bytes(PNG);path.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError,'task-owned'):images.prepare(result,task,folder)

    def test_review_binds_images_and_cannot_upload_before_approval(self):
        task,folder,work,base,result,manifest=self.evidence()
        verdict={'verdict':'APPROVE','reason':'verified','tests_verified':True,'repairable':False}
        def review(prompt,*args,**kwargs):
            self.assertIn(str(folder/'screenshots-review.json'),prompt)
            self.assertIn(manifest[0]['url'],json.loads((folder/'generation.json').read_text())['body'])
            return verdict
        with patch.object(worker,'agent',side_effect=review),patch.object(images,'publish') as publish:
            body,unused=worker.review_patch(task,work,folder,'openclaw',base,result,'Review')
            publish.assert_not_called()
        self.assertEqual(rt.task_state(task['id'])['reviewed_screenshots'],manifest)
        Path(manifest[0]['path']).write_bytes(PNG+b'changed')
        with self.assertRaisesRegex(RuntimeError,'changed'):images.publish(manifest)

    def test_policy_rereview_image_mutation_invalidates_approval(self):
        task,folder,work,base,result,manifest=self.evidence();calls=[]
        def review(*args,**kwargs):
            calls.append(True)
            if len(calls)==1:return {'verdict':'BLOCK','reason':'HUMAN_REVIEW_REQUIRED: context.json has no attestation','tests_verified':True,'repairable':False}
            Path(manifest[0]['path']).write_bytes(PNG+b'changed')
            return {'verdict':'APPROVE','reason':'verified','tests_verified':True,'repairable':False}
        with patch.object(worker,'agent',side_effect=review):
            with self.assertRaisesRegex(RuntimeError,'changed'):
                worker.review_patch(task,work,folder,'openclaw',base,result,'Review')
        self.assertFalse(rt.task_state(task['id']).get('reviewed_screenshots'))

    def test_ambiguous_upload_is_read_back_without_replaying_write(self):
        task,folder,work,base,result,manifest=self.evidence();objects={};writes=[]
        def api(path,method='GET',data=None):
            key=path.split('?')[0]
            if method=='GET':
                if key not in objects:raise RuntimeError('HTTP 404 missing')
                return objects[key]
            self.assertEqual(method,'PUT');self.assertNotIn('sha',data)
            writes.append(key);objects[key]={'sha':manifest[0]['blob']}
            raise RuntimeError('connection lost after write')
        with patch.object(images,'api',side_effect=api),patch.object(images,'ensure_branch'):
            images.publish(manifest);images.publish(manifest)
        self.assertEqual(len(writes),1)  # Identical content is stored once.

    def test_mismatching_remote_content_is_never_overwritten(self):
        task,folder,work,base,result,manifest=self.evidence()
        with patch.object(images,'api',return_value={'sha':'wrong'}) as api,patch.object(images,'ensure_branch'):
            with self.assertRaisesRegex(RuntimeError,'does not match'):images.publish(manifest)
        self.assertEqual(api.call_count,1)

    def test_successful_upload_with_temporarily_missing_readback_writes_only_once(self):
        task,folder,work,base,result,manifest=self.evidence();writes=[];reads=[]
        def api(path,method='GET',data=None):
            if method=='PUT':writes.append(path);return {'content':{'sha':manifest[0]['blob']}}
            reads.append(path)
            if len(reads)<=3:raise RuntimeError('HTTP 404 not visible yet')
            return {'sha':manifest[0]['blob']}
        with patch.object(images,'api',side_effect=api),patch.object(images,'ensure_branch'),patch.object(images.time,'sleep') as sleep:
            images.publish(manifest)
        self.assertEqual(len(writes),1);self.assertEqual(sleep.call_count,2)

    def test_readback_timeout_is_bounded_and_distinct_from_content_mismatch(self):
        task,folder,work,base,result,manifest=self.evidence()
        with patch.object(images,'optional',return_value=None) as read,patch.object(images.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'not yet readable'):images.read_uploaded(manifest[0])
        self.assertEqual(read.call_count,4)

    def test_readback_recovery_requires_matching_remote_images_and_new_review(self):
        task,folder,work,base,result,manifest=self.evidence()
        (work/'value.py').write_text('value = 2\n')
        reason='Published screenshot content does not match independent review'
        rt.task_state(task['id'],phase='error',last_execution_phase='review',reviewed_screenshots=manifest)
        with rt.db() as db:db.execute("UPDATE tasks SET status='error',result=? WHERE id=?",(reason,task['id']))
        work_evidence.capture(task['id'])
        with patch.object(images,'read_uploaded',side_effect=RuntimeError('mismatch')),patch.object(recovery,'audited_unpublished') as audit:
            self.assertEqual(recovery.recover_submission_regressions(),[]);audit.assert_not_called()
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(images,'read_uploaded',return_value={'sha':manifest[0]['blob']}),patch.object(recovery,'audited_unpublished',return_value=(True,'no remote branch or PR')):
            self.assertEqual(recovery.recover_submission_regressions(),[task['id']])
        state=rt.task_state(task['id'])
        self.assertIn('resume_progress',state);self.assertIn('screenshot_readback_repaired',state)
        self.assertEqual(state['phase'],'queued');self.assertFalse(state.get('human_review'))
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT attempts FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],1)
            db.execute("UPDATE tasks SET status='error' WHERE id=?",(task['id'],))
        rt.task_state(task['id'],recovery_checked_at=0)
        self.assertEqual(recovery.recover_submission_regressions(),[])

    def test_encrypted_bundle_retains_only_named_images_and_text_resume_omits_them(self):
        task,folder,work,base,result,manifest=self.evidence()
        (folder/'tool-cache'/'private-cache').write_text('must not retain')
        work_evidence.capture(task['id'])
        bundle=json.loads(recovery.latest_bundle(task['id']).read_text())
        self.assertEqual(set(bundle['screenshots']),{'before.png','after.png'})
        self.assertEqual(base64.b64decode(bundle['screenshots']['before.png']),PNG)
        self.assertNotIn('must not retain',json.dumps(bundle))
        context=work_evidence.recovery_context(task['id'],2,folder).read_text()
        self.assertNotIn(base64.b64encode(PNG).decode(),context)

    def test_screenshot_gate_recovery_is_once_only_unpublished_and_retains_attempts(self):
        task,folder,work,base,result,manifest=self.evidence()
        reason='review: The code review and focused checks found no defect; no before/after screenshots. This is a missing submission gate.'
        (work/'value.py').write_text('value = 2\n')
        rt.task_state(task['id'],attempt=3,phase='blocked',last_execution_phase='review',submission_repaired={'repair':'optional_broad_check'})
        with rt.db() as db:db.execute("UPDATE tasks SET status='blocked',attempts=3,result=? WHERE id=?",(reason,task['id']))
        work_evidence.capture(task['id'])
        with patch.object(recovery,'audited_unpublished',return_value=(False,'remote branch exists')):
            self.assertEqual(recovery.recover_submission_regressions(),[])
        rt.task_state(task['id'],recovery_checked_at=0)
        with patch.object(recovery,'audited_unpublished',return_value=(True,'no publication')):
            self.assertEqual(recovery.recover_submission_regressions(),[task['id']])
        state=rt.task_state(task['id']);self.assertIn('ui_evidence_repaired',state)
        with rt.db() as db:
            self.assertEqual(db.execute('SELECT attempts FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],3)
            db.execute("UPDATE tasks SET status='blocked' WHERE id=?",(task['id'],))
        rt.task_state(task['id'],recovery_checked_at=0)
        self.assertEqual(recovery.recover_submission_regressions(),[])


if __name__=='__main__':unittest.main()
