"""Exercise the real stdio multiplexer and socket client without model calls."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import codex_host
import runtime as rt

FAKE = r'''#!/usr/bin/env python3
import json, sys, threading, time
lock=threading.Lock(); threads={}; serial=0
def send(value):
    with lock:print(json.dumps(value),flush=True)
def event(thread, method, **extra):send({'method':method,'params':{'threadId':thread,**extra}})
def finish(p):
    t=p['threadId'];time.sleep(.08)
    if p['input'][0]['text']=='hang':return
    if p['input'][0]['text']=='fail':
        event(t,'turn/completed',turn={'id':t,'status':'failed','error':{'message':'intentional failure'}});return
    answer={'thread':t,'cwd':p['cwd'],'model':p['model'],'sandbox':p['sandboxPolicy'],
            'config':threads[t]['config'],'schema':p['outputSchema']}
    event(t,'item/completed',item={'type':'agentMessage','text':json.dumps(answer)})
    event(t,'turn/completed',turn={'id':t,'status':'completed'})
for line in sys.stdin:
    m=json.loads(line); method=m.get('method');p=m.get('params',{});result={}
    if 'id' not in m:continue
    if method=='thread/start':
        serial+=1;t='thread-'+str(serial);threads[t]=p;result={'thread':{'id':t}}
    if method=='turn/start':result={'turn':{'id':p['threadId']}}
    if method=='account/rateLimits/read':result={'rateLimits':{'primary':{'usedPercent':23,'resetsAt':time.time()+500}}}
    if method=='test/quota':send({'method':'account/rateLimits/updated','params':{'rateLimits':{'primary':{'usedPercent':91,'resetsAt':time.time()+500}},'ignored_secret':'do-not-store'}})
    send({'id':m['id'],'result':result})
    if method=='turn/start':threading.Thread(target=finish,args=(p,),daemon=True).start()
    if method=='turn/interrupt':event(p['threadId'],'turn/completed',turn={'id':p['turnId'],'status':'interrupted'})
'''

class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.socket=self.root/'host.sock'
        self.patches=[patch.object(rt,'DATA',self.root),patch.object(rt,'config',return_value={'codex_socket':str(self.socket),'codex_review_reservations':True})]
        for p in self.patches:p.start()
        binary=self.root/'fake-codex';binary.write_text(FAKE);binary.chmod(0o700)
        self.host=codex_host.Host(str(binary))
        self.server=codex_host.Server(str(self.socket),codex_host.Handler);self.server.host=self.host
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.host.stop()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def request(self,index,prompt='work',timeout=5):
        return {'prompt':prompt,'work':str(self.root/f'work-{index}'),'roots':[str(self.root/f'cache-{index}')],
            'model':'gpt-6-sol','schema':{'type':'object'},'cache_env':{'CARGO_HOME':f'cargo-{index}'},
            'phase':'generation','timeout':timeout}
    def test_concurrent_sessions_keep_outputs_sandboxes_and_caches_separate(self):
        def run(index):return json.loads(codex_host.execute(self.request(index),self.root/f'{index}.json'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(len({r['thread']for r in results}),2)
        for index,result in enumerate(results):
            self.assertEqual(result['cwd'],str(self.root/f'work-{index}'))
            self.assertEqual(result['config']['shell_environment_policy.set']['CARGO_HOME'],f'cargo-{index}')
            self.assertEqual(result['sandbox']['writableRoots'],[str(self.root/f'cache-{index}')])
            self.assertEqual(result['model'],'gpt-6-sol')
            self.assertEqual(result['schema'],{'type':'object'})
        self.assertIsNone(self.host.proc.poll())
    def test_failed_turn_never_produces_accepted_output(self):
        output=self.root/'failed.json'
        with self.assertRaisesRegex(RuntimeError,'intentional failure'):
            codex_host.execute(self.request(0,'fail'),output)
        self.assertFalse(output.exists())
    def test_screening_uses_read_only_sandbox_without_health_probe_flag(self):
        request={**self.request(0),'read_only':True,'probe':False}
        result=json.loads(codex_host.execute(request,self.root/'screening.json'))
        self.assertEqual(result['sandbox'],{'type':'readOnly'})
    def test_timeout_waits_for_confirmed_interruption_before_retry(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            codex_host.execute(self.request(0,'hang',timeout=.15),self.root/'timeout.json')
        self.assertEqual(self.host.streams,{})
        self.assertIsNone(self.host.proc.poll())
        self.assertTrue(codex_host.execute(self.request(1),self.root/'next.json'))
    def test_auth_owner_environment_omits_paid_and_publishing_credentials(self):
        names=('OPENAI_API_KEY','GH_PAT','CODEX_AUTH_JSON','OSS_ARTIFACT_KEY','QWEN_API_KEY')
        with patch.dict(os.environ,{**dict.fromkeys(names,'secret'),'OSS_READONLY_GH_TOKEN':'read-only'}):
            env=codex_host.clean_environment()
        for name in names:self.assertNotIn(name,env)
        self.assertEqual(env['GH_TOKEN'],'read-only')
    def test_existing_host_reads_and_routes_global_quota_without_new_turn(self):
        self.assertEqual(rt.getmeta('codex_quota')['windows'][0]['used_percent'],23)
        self.host.call('test/quota',{})
        self.assertEqual(rt.getmeta('codex_quota')['windows'][0]['used_percent'],91)
        self.assertNotIn('do-not-store',json.dumps(rt.getmeta('codex_quota')))
        self.assertFalse(self.host.streams)

if __name__=='__main__':unittest.main()
