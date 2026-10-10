"""One authenticated Codex app-server (stdio), multiple isolated ephemeral threads.

Workers connect over a private local Unix socket. Only this process owns Codex's
refreshable login; publication credentials and the evidence key are excluded.
"""
import argparse
import itertools
import json
import os
from pathlib import Path
import queue
import signal
import socket
import socketserver
import subprocess
import threading
import time
import runtime as rt

def clean_environment():
    env=dict(os.environ)
    for name in ('OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN',
                 'GH_TOKEN','GITHUB_TOKEN','GH_PAT','QWEN_API_KEY','DASHSCOPE_API_KEY','CODEX_AUTH_JSON','OSS_ARTIFACT_KEY'):
        env.pop(name,None)
    token=env.pop('OSS_READONLY_GH_TOKEN','')
    if token:env['GH_TOKEN']=token
    for name in ('GITHUB_STEP_SUMMARY','GITHUB_OUTPUT','GITHUB_ENV','GITHUB_PATH','GITHUB_STATE'):
        env.pop(name,None)
    return env

class Host:
    def __init__(self, binary):
        flags=[]
        for feature in ('plugins','remote_plugin','apps','multi_agent','hooks'):
            flags+=['--disable',feature]
        with (rt.DATA/'codex-host.stderr.log').open('a') as errors:
            self.proc=subprocess.Popen([binary,'app-server',*flags,'--listen','stdio://'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,
                env=clean_environment(),text=True,bufsize=1,start_new_session=True)
        self.ids=itertools.count(1);self.pending={};self.streams={};self.lock=threading.Lock()
        self.reader=threading.Thread(target=self.read,daemon=True);self.reader.start()
        try:
            self.call('initialize',{'clientInfo':{'name':'oss_pipeline','title':'OSS pipeline','version':'2'},
                'capabilities':{'experimentalApi':True}})
            self.send({'method':'initialized','params':{}})
        except BaseException:
            self.stop();raise
    def send(self,message):
        with self.lock:
            self.proc.stdin.write(json.dumps(message)+'\n');self.proc.stdin.flush()
    def call(self,method,params,timeout=60):
        ident=next(self.ids);reply=queue.Queue();self.pending[ident]=reply
        try:
            self.send({'id':ident,'method':method,'params':params})
            result=reply.get(timeout=timeout)
            if 'error'in result:raise RuntimeError('Codex: '+str(result['error'].get('message','request failed'))[:500])
            return result['result']
        finally:self.pending.pop(ident,None)
    def read(self):
        for line in self.proc.stdout:
            try:message=json.loads(line)
            except ValueError:continue
            if 'id'in message and 'method'not in message:
                target=self.pending.get(message['id'])
                if target is not None:target.put(message)
            elif 'id'in message:
                # approvalPolicy=never: no implicit approval of an unexpected request.
                self.send({'id':message['id'],'error':{'code':-32601,'message':'Interactive requests are disabled'}})
            else:
                target=self.streams.get(message.get('params',{}).get('threadId'))
                if target is not None:target.put(message)
        for target in list(self.pending.values()):target.put({'error':{'message':'app-server exited'}})
        for target in list(self.streams.values()):target.put({'method':'host/exited','params':{}})
    def stop(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid,signal.SIGTERM)
            try:self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:os.killpg(self.proc.pid,signal.SIGKILL);self.proc.wait()
        self.reader.join(timeout=5)
        self.proc.stdin.close();self.proc.stdout.close()

def run_turn(host,request,emit):
    probe=request.get('probe',False)
    work=request['work'];roots=request.get('roots',[work])
    thread=host.call('thread/start',{'model':request['model'],'cwd':work,'approvalPolicy':'never',
        'sandbox':'read-only' if probe else 'workspace-write','ephemeral':True,
        'config':{'shell_environment_policy.set':request.get('cache_env',{})}})['thread']['id']
    stream=queue.Queue();host.streams[thread]=stream;turn_id=None;completed=False
    try:
        params={'threadId':thread,'model':request['model'],'cwd':work,'approvalPolicy':'never',
            'input':[{'type':'text','text':request['prompt']}],
            'sandboxPolicy':{'type':'readOnly'} if probe else {'type':'workspaceWrite','writableRoots':roots,'networkAccess':True},
            'outputSchema':request.get('schema')}
        if request.get('effort'):params['effort']=request['effort']
        turn_id=host.call('turn/start',params)['turn']['id']
        deadline=time.monotonic()+request['timeout'];answer=''
        while time.monotonic()<deadline:
            try:event=stream.get(timeout=min(5,max(.1,deadline-time.monotonic())))
            except queue.Empty:continue
            emit({'event':event})
            method=event.get('method');data=event.get('params',{})
            if method=='host/exited':raise RuntimeError('Codex: app-server exited')
            if method=='item/completed' and data.get('item',{}).get('type')=='agentMessage':
                answer=data['item']['text']
            if method=='turn/completed':
                completed=True
                if data['turn']['status']!='completed':
                    raise RuntimeError('Codex: '+str(data['turn'].get('error') or data['turn']['status'])[:500])
                if not answer:raise RuntimeError('Codex: completed without structured answer')
                emit({'complete':True,'text':answer});return
        raise subprocess.TimeoutExpired('Codex '+request.get('phase','turn'),request['timeout'])
    finally:
        if turn_id and not completed:
            try:
                host.call('turn/interrupt',{'threadId':thread,'turnId':turn_id},timeout=15)
                end=time.monotonic()+30
                while time.monotonic()<end:
                    event=stream.get(timeout=max(.1,end-time.monotonic()))
                    if event.get('method')=='turn/completed':break
                else:raise RuntimeError('Turn did not stop')
            except Exception:
                # An unconfirmed interruption cannot leave an agent writing
                # while its controller retries. Fail the whole host closed.
                host.stop();os._exit(2)
        host.streams.pop(thread,None)
        try:host.call('thread/unsubscribe',{'threadId':thread},timeout=15)
        except Exception:pass

class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads=True

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        def emit(value):self.wfile.write(json.dumps(value).encode()+b'\n');self.wfile.flush()
        try:
            request=json.loads(self.rfile.readline(2*1024**2))
            run_turn(self.server.host,request,emit)
        except subprocess.TimeoutExpired as exc:
            emit({'error':str(exc),'timed_out':True})
        except Exception as exc:
            try:emit({'error':str(exc)[:1000]})
            except (OSError,ValueError):pass

def execute(request,output):
    """Return only a verified completed answer; preserve every received event."""
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    sock.settimeout(request['timeout']+180)
    try:
        sock.connect(rt.config()['codex_socket'])
        sock.sendall(json.dumps(request).encode()+b'\n')
        with sock.makefile('rb') as stream,output.with_suffix('.events.jsonl').open('w') as events:
            for line in stream:
                message=json.loads(line)
                if 'event'in message:
                    events.write(json.dumps(message['event'])+'\n');events.flush()
                if message.get('timed_out'):raise subprocess.TimeoutExpired('Codex '+request['phase'],request['timeout'])
                if 'error'in message:raise RuntimeError(message['error'])
                if message.get('complete'):
                    output.write_text(message['text']);return message['text'].strip()
        raise RuntimeError('Codex: host connection ended without verified completion')
    finally:sock.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True);args=parser.parse_args()
    path=Path(rt.config()['codex_socket']);path.parent.mkdir(parents=True,exist_ok=True)
    with rt.lock('codex-host'):
        path.unlink(missing_ok=True)
        host=Host(args.binary)
        try:
            with Server(str(path),Handler) as server:
                os.chmod(path,0o600);server.host=host
                def monitor():
                    host.proc.wait();server.shutdown()
                threading.Thread(target=monitor,daemon=True).start()
                server.serve_forever(poll_interval=.5)
            if host.proc.returncode:raise RuntimeError('Codex app-server exited')
        finally:host.stop();path.unlink(missing_ok=True)

if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:exit(1))
    main()
