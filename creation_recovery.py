"""One explicitly requested real Hermes PR attempt, retaining the global hold."""
import json
import os
import threading
import time

import codex_worker as worker
import execution_support as execution
import repo_limits
import runtime as rt
import task_recovery


def run(number):
    if os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch':
        raise RuntimeError('Creation recovery requires an explicit workflow dispatch')
    if number<=0 or repo_limits.disabled('hermes'):
        raise RuntimeError('Invalid or disabled recovery repository/issue')
    if rt.config().get('backend')!='codex-cloud':
        raise RuntimeError('Creation recovery must use the shared cloud owner')
    marker='creation_retry:'+os.environ['GITHUB_RUN_ID']
    identity=f'fix:hermes:{number}:'
    if rt.getmeta(marker):raise RuntimeError('This explicit recovery request was already attempted')
    stop=threading.Event()
    rt.setmeta('worker_heartbeat',time.time())
    thread=threading.Thread(target=worker.heartbeat,args=(stop,),daemon=True)
    thread.start()
    try:
        worker.healthcheck()
        ok,why=rt.ready()
        if not ok:raise rt.Paused(why)
        ok,why=rt.dispatch_headroom('hermes')
        if not ok:raise rt.Paused(why)
        with rt.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if rt._meta(db,marker):raise RuntimeError('This explicit recovery request was already attempted')
            if db.execute("SELECT 1 FROM tasks WHERE status='running'").fetchone():
                raise RuntimeError('Another task is running; refusing overlapping recovery')
            if db.execute('SELECT 1 FROM tasks WHERE identity=?',(identity,)).fetchone():
                raise RuntimeError('This issue already has a durable task; inspect it before retrying')
            ok,why=rt._room(db,'fix','hermes')
            if not ok:raise rt.Paused(why)
            now=time.time()
            note=('User explicitly requested one real, current-issue Hermes fix and ordinary PR attempt. '
                  'The historical creation hold is not permission clearance. All current upstream '
                  'policy, duplicate/claim checks, tests, independent review and budgets still apply.')
            cursor=db.execute('INSERT INTO tasks(identity,kind,repo,number,note,status,created,updated,attempts) '
                'VALUES (?,?,?,?,?,?,?,?,?)',(identity,'fix','hermes',number,note,'running',now,now,1))
            task=dict(db.execute('SELECT * FROM tasks WHERE id=?',(cursor.lastrowid,)).fetchone())
            rt._putmeta(db,marker,{'at':now,'task':task['id'],'repo':'hermes','issue':number,
                                 'source':'explicit user-requested real PR recovery'})
            state=execution.claim_state({},task,'creation-recovery')
            state['creation_retry_request']=marker
            rt._putmeta(db,f"task:{task['id']}",state)
        worker.checkpoint_claim(task)
        try:
            worker.process(task)
        except rt.PersistenceError:
            raise
        except Exception as error:
            worker.handle_task_error(task,error)
        with rt.db() as db:
            final=dict(db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone())
        print(json.dumps({key:final[key] for key in ('id','repo','number','status','result')},ensure_ascii=False),flush=True)
        if final['status']!='done' or not final['result'].startswith('https://github.com/NousResearch/hermes-agent/pull/'):
            raise RuntimeError('Real PR recovery did not publish; inspect the retained task result')
        # Only the real ordinary PR can clear the earlier denial. Re-run the
        # existing remote-proof check without waiting out its polling interval.
        rt.setmeta('publication_check:hermes',{})
        task_recovery.refresh_publication_holds()
        rt.checkpoint()
        return final
    finally:
        stop.set()
        thread.join(timeout=2)
