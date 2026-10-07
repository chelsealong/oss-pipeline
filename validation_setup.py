"""Disposable synthetic validation services; never expose controller credentials."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import urllib.request
import runtime as rt

class ValidationUnavailable(RuntimeError):pass

_sessions=[]

def terminate(process):
    if process.poll() is None:
        os.killpg(process.pid,signal.SIGTERM)
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()

@contextmanager
def session(task):
    resources=[];_sessions.append(resources)
    try:yield
    finally:
        _sessions.pop()
        for cleanup in reversed(resources):
            try:cleanup()
            except Exception as error:
                rt.setmeta(f"validation_cleanup:{task['id']}",{'type':type(error).__name__})

def safe_env(cache):
    # A strict allowlist also excludes future controller secrets.
    return {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(cache/'home'),
        'COREPACK_HOME':str(cache/'corepack'),'XDG_CACHE_HOME':str(cache/'xdg-cache'),
        'npm_config_cache':str(cache/'npm'),'npm_config_store_dir':str(cache/'pnpm-store'),
        'CI':'true','NEXT_TELEMETRY_DISABLED':'1',
        'LANG':'C.UTF-8','NODE_OPTIONS':'--max-old-space-size=4096'}

def sandbox(work,cache,args):
    cmd=['bwrap','--unshare-user','--uid','0','--gid','0','--unshare-pid',
         '--unshare-ipc','--die-with-parent','--new-session','--ro-bind','/','/',
         '--proc','/proc','--dev','/dev','--tmpfs','/tmp','--tmpfs','/run']
    # Ubuntu resolv.conf points into /run/systemd/resolve. Keep only its resolved
    # regular file readable after masking /run; no runtime socket is exposed.
    resolver=Path('/etc/resolv.conf').resolve()
    if resolver.is_relative_to(Path('/run')) and resolver.is_file():
        cmd+=['--dir',str(resolver.parent),'--ro-bind',str(resolver),str(resolver)]
    # /var/run is a symlink to /run on Ubuntu. Mask the directory rather than
    # attempting a regular-file bind over an existing Unix socket.
    hidden=[rt.DATA,Path.home()/'.codex',Path.home()/'.config',Path.home()/'.gitconfig']
    for name in ('CODEX_HOME','OSS_CLOUD_STATE_DIR'):
        if os.environ.get(name):hidden.append(Path(os.environ[name]))
    for path in hidden:
        if path.exists():
            cmd+=['--tmpfs',str(path)] if path.is_dir() else ['--ro-bind','/dev/null',str(path)]
    cmd+=['--bind',str(work),str(work),'--bind',str(cache),str(cache),
          '--chdir',str(work),'--',*args]
    return cmd

def compose_spec():
    # Controller-owned services only: no upstream Docker socket/host mounts,
    # external volumes, user databases, worker-tests profiles or production data.
    return {'services':{
      'postgres':{'image':'docker.io/postgres:17','environment':{
          'POSTGRES_PASSWORD':'postgres','POSTGRES_USER':'postgres','POSTGRES_DB':'postgres'},
          'ports':['127.0.0.1:5432:5432'],'volumes':['postgres:/var/lib/postgresql/data'],
          'healthcheck':{'test':['CMD-SHELL','pg_isready -U postgres'],'interval':'3s','retries':30}},
      'clickhouse':{'image':'docker.io/clickhouse/clickhouse-server:26.4',
          'environment':{'CLICKHOUSE_USER':'clickhouse','CLICKHOUSE_PASSWORD':'clickhouse'},
          'ports':['127.0.0.1:8123:8123','127.0.0.1:9000:9000'],
          'volumes':['clickhouse:/var/lib/clickhouse'],
          'healthcheck':{'test':['CMD-SHELL','wget --quiet -O /dev/null http://localhost:8123/ping'],'interval':'3s','retries':30}},
      'redis':{'image':'docker.io/redis:7.2.4','command':['--requirepass','myredissecret'],
          'ports':['127.0.0.1:6379:6379']},
      'minio':{'image':'cgr.dev/chainguard/minio','entrypoint':'sh',
          'command':['-c','mkdir -p /data/langfuse && minio server --address :9000 /data'],
          'environment':{'MINIO_ROOT_USER':'minio','MINIO_ROOT_PASSWORD':'miniosecret'},
          'ports':['127.0.0.1:9090:9000'],'volumes':['minio:/data']}},
      'volumes':{'postgres':{},'clickhouse':{},'minio':{}}}

def prepare_langfuse(work,folder):
    if not _sessions:raise ValidationUnavailable('Validation requires a cleanup session')
    if not shutil.which('docker') or not shutil.which('bwrap'):
        raise ValidationUnavailable('Docker and bubblewrap are required on the cloud runner')
    resources=_sessions[-1];cache=folder/'tool-cache';cache.mkdir(exist_ok=True)
    (cache/'home').mkdir(exist_ok=True)
    project='oss-validation-'+str(os.getpid())
    config=folder/'validation-compose.json';config.write_text(json.dumps(compose_spec()))
    docker=['docker','compose','--project-name',project,'--file',str(config)]
    env=safe_env(cache)
    log=(folder/'validation-setup.log').open('a');resources.append(log.close)
    def docker_run(args,timeout):
        subprocess.run(docker+args,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=timeout)
    resources.append(lambda:docker_run(['down','--volumes','--remove-orphans'],120))
    def execute(args,timeout=900,background=False):
        process=subprocess.Popen(sandbox(work,cache,args),env=env,stdout=log,
            stderr=subprocess.STDOUT,start_new_session=True)
        resources.append(lambda:terminate(process))
        if background:return process
        try:code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate(process);raise ValidationUnavailable('Setup command timed out: '+args[0])
        if code:raise ValidationUnavailable('Setup command failed: '+' '.join(args)+'; see validation-setup.log')
    try:
        for name,example in (('.env','.env.dev.example'),('.env.test','.env.test.example')):
            if not (work/name).exists():
                subprocess.run(['git','check-ignore','-q',name],cwd=work,check=True)
                raw=subprocess.run(['git','show','HEAD:'+example],cwd=work,capture_output=True,check=True).stdout
                (work/name).write_bytes(raw)
        docker_run(['up','--detach','--wait','--wait-timeout','180'],600)
        execute(['pnpm','install','--frozen-lockfile'],1800)
        execute(['pnpm','run','db:generate'])
        execute(['pnpm','--filter=shared','run','db:deploy'])
        execute(['pnpm','--filter=shared','run','ch:up'])
        # Test migrations use a separate synthetic database, never a user DB.
        docker_run(['exec','-T','postgres','psql','-U','postgres','-c','CREATE DATABASE langfuse_test;'],60)
        execute(['pnpm','--filter=shared','run','db:reset:test'])
        execute(['pnpm','--filter=shared','run','build'])
        execute(['pnpm','--filter=shared','run','db:seed'])
        execute(['pnpm','run','playwright:install'])
        web=execute(['pnpm','--filter=web','run','dev'],background=True)
        for _ in range(180):
            if web.poll() is not None:raise ValidationUnavailable('Synthetic web server exited; see setup log')
            try:
                with urllib.request.urlopen('http://localhost:3000',timeout=5) as response:
                    if response.status==200:
                        log.write('\nSynthetic DB/web services ready at http://localhost:3000. Browser checks still required.\n');log.flush()
                        return
            except Exception:pass
            time.sleep(2)
        raise ValidationUnavailable('Synthetic web server did not become ready')
    except (subprocess.SubprocessError,OSError) as error:
        raise ValidationUnavailable('Synthetic setup failed: '+type(error).__name__+'; see validation-setup.log') from error
