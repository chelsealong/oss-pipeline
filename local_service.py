#!/usr/bin/env python3
"""Local detection services. The worker's health gates every model call."""
import argparse
import importlib.util
import json
import signal
import time
import runtime as rt

def main():
    ap=argparse.ArgumentParser();ap.add_argument('role',choices=['watch','prwatch','health']);ap.add_argument('--once',action='store_true');args=ap.parse_args()
    if args.role=='health':
        print(json.dumps(rt.status(),indent=2));return
    with rt.lock(args.role):
        if args.role=='watch':
            import scan, watch
            keys=[k for k,v in scan.REPOS.items() if not v.get('paused')]
            seen=watch.load_seen();bootstrap=not seen
            last_drain=0;last_scan=0;scan_index=0;last_capture=0;last_pending_vet=0
        else:
            spec=importlib.util.spec_from_file_location('prwatch',rt.ROOT/'watch-prs.py')
            wp=importlib.util.module_from_spec(spec);spec.loader.exec_module(wp)
            seen=wp.load_seen()
        while True:
            rt.setmeta(args.role+'_heartbeat',time.time())
            if args.role=='watch':rt.setmeta('detector_heartbeat',time.time())
            try:
                # Discovery must continue while workers are busy. Admission
                # remains separately bounded by room()/enqueue().
                is_ready=rt.ready()[0]
                if args.role=='watch' and not is_ready and rt.config().get('enabled'):
                    # No model calls while paused. Capture a wider GitHub-only
                    # window so quota resets do not leave only five new issues.
                    if time.time()-last_capture>60:
                        watch.sweep(keys,seen,100,bootstrap,vetting=False)
                        watch.save_seen(seen);bootstrap=False;last_capture=time.time()
                elif is_ready:
                    if args.role=='watch':
                        new,accepted=watch.sweep(keys,seen,5,bootstrap)
                        watch.save_seen(seen);bootstrap=False
                        if new:watch.log(f'Codex detection: {new} new, {accepted} accepted')
                        if time.time()-last_pending_vet>60:
                            watch.recheck_pending_vet(keys)
                            last_pending_vet=time.time()
                        if time.time()-last_drain>600 and rt.room()[0]:
                            watch.recheck_deferred(keys)
                            watch.drain_queues(keys)
                            watch.promote_claims(keys)
                            last_drain=time.time()
                        # One repo at a time, every ~120s: reconciliation covers
                        # all active repos without a second queue-file writer.
                        if time.time()-last_scan>120:
                            key=keys[scan_index%len(keys)];scan_index+=1
                            res=scan.scan_repo(key,30,15)
                            if not res.get('partial'):
                                (scan.QUEUE/f'{key}.json').write_text(json.dumps(res,indent=2)+'\n')
                            else:watch.log(f'{key}: partial scan, preserving existing queue')
                            last_scan=time.time()
                    else:
                        n=wp.one_pass(seen)
                        wp.save_seen(seen)
                        wp.log(f'Codex PR check completed: {n} queued')
            except rt.Paused as e:
                print('Paused:',str(e),flush=True)
                # Persist accepted event IDs even if the next judgement hit its cap.
                if args.role=='prwatch':wp.save_seen(seen)
            except Exception as e:
                import traceback
                traceback.print_exc()
                rt.setmeta(args.role+'_error',{'at':time.time(),'error':str(e)[:500]})
            if args.once:return
            time.sleep(5 if args.role=='watch' else 300)

if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda s,f:exit(0))
    main()
