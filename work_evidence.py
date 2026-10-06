"""Recoverable per-attempt evidence; encrypted before entering the public state branch."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zlib
import runtime as rt

AAD=b'oss-pipeline-evidence-v1'

def key():
    try:value=base64.b64decode(os.environ['OSS_ARTIFACT_KEY'],validate=True)
    except (KeyError,ValueError) as exc:raise RuntimeError('Artifact encryption key unavailable') from exc
    if len(value)!=32:raise RuntimeError('Artifact encryption key must be 256 bits')
    return value

def encrypt(raw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce=os.urandom(12)
    return b'OSSE1'+nonce+AESGCM(key()).encrypt(nonce,zlib.compress(raw),AAD)

def decrypt(raw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not raw.startswith(b'OSSE1'):raise ValueError('Unknown evidence format')
    return zlib.decompress(AESGCM(key()).decrypt(raw[5:17],raw[17:],AAD))

def atomic(path, raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    tmp.write_bytes(raw);os.replace(tmp,path)

@contextlib.contextmanager
def task_lock(task_id):
    rt.DATA.mkdir(parents=True,exist_ok=True)
    with (rt.DATA/f'evidence-{task_id}.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield

def capture(task_id):
    with task_lock(task_id):_capture(task_id)

def _capture(task_id):
    state=rt.task_state(task_id)
    relative=state.get('folder')
    if not relative:return
    folder=rt.DATA/relative
    if not folder.resolve().is_relative_to(rt.DATA.resolve()) or not folder.is_dir():return
    dest=rt.DATA/'evidence'/f"{task_id}-{state.get('attempt',1)}.json"
    if state.get('terminal') and not (folder/'work/.git').exists() and dest.exists():return
    work=folder/'work';payload={'task':task_id,'execution':state,'files':{},'omitted':[]}
    # Explicitly omit tool-cache, Git metadata, credentials, and complete clones.
    total=0
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix not in ('.json','.jsonl','.log','.txt','.md'):continue
        if path.suffix in ('.jsonl','.log') and not state.get('terminal'):continue
        try:raw=path.read_bytes()
        except FileNotFoundError:
            payload['omitted'].append(path.name+': removed during snapshot');continue
        if len(raw)>4*1024**2:
            raw=raw[-4*1024**2:];payload['omitted'].append(path.name+': earlier log content exceeds 4 MiB')
        total+=len(raw)
        if total>16*1024**2:
            payload['omitted'].append(path.name+': evidence size limit');continue
        payload['files'][path.name]=base64.b64encode(raw).decode()
    # A retry can retain the previous attempt's base while clone/fetch is
    # still assembling a new shallow checkout. This snapshot must not run
    # Git against that incomplete checkout and crash the shared state owner.
    if state.get('phase') != 'checkout' and state.get('base') and (work/'.git').exists():
        def git(*args):
            return subprocess.run(['git',*args],cwd=work,capture_output=True,check=True,timeout=30).stdout
        payload['head']=git('rev-parse','HEAD').decode().strip()
        diff=git('diff',state.get('base','HEAD'),'--binary')
        if len(diff)<=8*1024**2:payload['diff']=base64.b64encode(diff).decode()
        else:payload['omitted'].append('diff exceeds 8 MiB')
        payload['untracked']={}
        for name in git('ls-files','--others','--exclude-standard','-z').decode().split('\0'):
            if not name:continue
            path=work/name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(work.resolve()):continue
            if any(part in ('node_modules','.venv','venv','tool-cache','target') for part in path.parts):continue
            try:
                if path.stat().st_size>2*1024**2 or total>24*1024**2:
                    payload['omitted'].append(name+': evidence size limit');continue
                raw=path.read_bytes()
            except FileNotFoundError:
                payload['omitted'].append(name+': removed during snapshot');continue
            total+=len(raw)
            payload['untracked'][name]=base64.b64encode(raw).decode()
    raw=json.dumps(payload,sort_keys=True).encode()
    if not dest.exists() or dest.read_bytes()!=raw:atomic(dest,raw)

def save_to(store):
    source=rt.DATA/'evidence';dest=store/'evidence';dest.mkdir(exist_ok=True)
    # Capture active work too, so a runner failure doesn't erase the entire turn.
    with rt.db() as db:
        ids=[r[0] for r in db.execute("SELECT id FROM tasks WHERE status='running'")]
    for task_id in ids:capture(task_id)
    for path in source.glob('*.json'):
        raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        target=dest/(path.name+'.enc');stamp=rt.DATA/'evidence-synced'/path.name
        if target.exists() and stamp.exists() and stamp.read_text()==digest:continue
        atomic(target,encrypt(raw));atomic(stamp,digest.encode())

def restore_from(store):
    for path in (store/'evidence').glob('*.json.enc'):
        # Verify authentication before writing any local file.
        raw=decrypt(path.read_bytes());json.loads(raw)
        atomic(rt.DATA/'evidence'/path.name.removesuffix('.enc'),raw)
        atomic(rt.DATA/'evidence-synced'/path.name.removesuffix('.enc'),hashlib.sha256(raw).hexdigest().encode())

def previous(task_id, attempt):
    paths=list((rt.DATA/'evidence').glob(f'{task_id}-*.json'))
    paths=[p for p in paths if int(p.stem.split('-')[-1])<attempt]
    return max(paths,key=lambda p:int(p.stem.split('-')[-1])) if paths else None
