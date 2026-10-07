"""Human-dispatched approval of retained exact-patch evidence, never a model verdict."""
import argparse
import json
import os
import cloud_store
import runtime as rt
import task_recovery

def approve(task_id,base,digest,reviewer,attested):
    if attested!='true':raise ValueError('Human must explicitly attest to reviewing the exact patch')
    with rt.db() as db:
        row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not row:raise ValueError('Unknown task')
    task=dict(row)
    if task['status']=='blocked' and task_recovery.wait_kind(task['result'])=='human_wait':
        with rt.db() as db:db.execute("UPDATE tasks SET status='human_wait' WHERE id=?",(task_id,))
        task_recovery.handoff(task)
    task_recovery.record_approval(task_id,base,digest,reviewer)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--task',type=int,required=True)
    args=parser.parse_args()
    # This serialized workflow has no Codex process and cannot refresh its login.
    rt.CONFIG.parent.mkdir(exist_ok=True)
    rt.CONFIG.write_text(json.dumps({'backend':'codex-cloud','enabled':False}))
    cloud_store.restore()
    approve(args.task,os.environ['REVIEW_BASE'],os.environ['REVIEW_DIGEST'],
        os.environ['GITHUB_ACTOR'],os.environ['HUMAN_ATTESTATION'])
    cloud_store.save()
    print('Exact-patch human approval saved. Normal upstream checks and independent review still apply.')
