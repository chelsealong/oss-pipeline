"""Publish only immutable, independently reviewed, synthetic UI screenshots."""
import base64
import fcntl
import hashlib
import json
from pathlib import Path
import re
import struct
import tempfile
import time
import runtime as rt

REPOSITORY='chelsealong/oss-pipeline'
BRANCH='pr-assets'
START='<!-- OSS_SCREENSHOTS_START -->'
END='<!-- OSS_SCREENSHOTS_END -->'


def api(path, method='GET', data=None):
    import codex_worker as worker
    if method=='GET':return worker.api('repos/'+REPOSITORY+'/'+path)
    with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as payload:
        json.dump(data,payload);payload.flush()
        return json.loads(worker.run(['gh','api','--method',method,'repos/'+REPOSITORY+'/'+path,
                                      '--input',payload.name]))


def optional(path):
    try:return api(path)
    except RuntimeError as error:
        if re.search(r'HTTP 404\b',str(error)):return None
        raise


def describe(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Screenshot must be a regular task-owned PNG')
    raw=path.read_bytes()
    if (not 33<=len(raw)<=2*1024**2 or raw[:8]!=b'\x89PNG\r\n\x1a\n'
            or raw[12:16]!=b'IHDR' or not all(1<=v<=8000 for v in struct.unpack('>II',raw[16:24]))):
        raise RuntimeError('Screenshot must be a bounded valid PNG with usable dimensions')
    digest=hashlib.sha256(raw).hexdigest();name='screenshots/'+digest+'.png'
    return {'path':str(path),'sha256':digest,'size':len(raw),'asset':name,
        'blob':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),
        'url':f'https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}/{name}'}


def prepare(result, task, folder):
    root=folder/'tool-cache'/'pr-evidence'
    paths=[root/(name+'.png') for name in ('before','after')]
    if not any(path.exists() or path.is_symlink() for path in paths):return result,[]
    if not all(path.is_file() for path in paths):
        raise RuntimeError('Provide both before.png and after.png before screenshot review')
    manifest=[describe(path,root)|{'label':label} for path,label in zip(paths,('Before','After'))]
    field='pr_body' if task['kind']=='respond' else 'body'
    body=result.get(field) or ''
    if task['kind']=='respond' and not body:
        body=(json.loads((folder/'context.json').read_text()).get('pr') or {}).get('body') or ''
    body=re.sub(re.escape(START)+'.*?'+re.escape(END),'',body,flags=re.S).rstrip()
    images='\n\n'.join(f"**{item['label']}**\n\n![{item['label']}]({item['url']})" for item in manifest)
    result=dict(result);result[field]=body+'\n\n'+START+'\n### Screenshots\n\n'+images+'\n'+END+'\n'
    (folder/'screenshots-review.json').write_text(json.dumps(manifest,indent=2))
    return result,manifest


def verify(manifest):
    for item in manifest:
        path=Path(item['path'])
        if describe(path,path.parent)!= {key:value for key,value in item.items() if key!='label'}:
            raise RuntimeError('Screenshot changed after independent review')


def ensure_branch():
    if optional('git/ref/heads/'+BRANCH):return
    tree=api('git/trees','POST',{'tree':[{'path':'.gitkeep','mode':'100644','type':'blob','content':''}]})
    commit=api('git/commits','POST',{'message':'Initialize reviewed PR screenshot storage','tree':tree['sha'],'parents':[]})
    try:api('git/refs','POST',{'ref':'refs/heads/'+BRANCH,'sha':commit['sha']})
    except Exception:
        # A timed-out create may already exist. Read before treating it as failed.
        if not optional('git/ref/heads/'+BRANCH):raise


def read_uploaded(item):
    """Bounded read-after-write reconciliation; never repeat an upload."""
    path='contents/'+item['asset']+'?ref='+BRANCH
    for delay in (0,1,2,4):
        if delay:time.sleep(delay)
        existing=optional(path)
        if existing:
            if existing.get('sha')!=item['blob']:
                raise RuntimeError('Published screenshot content does not match independent review')
            return existing
    raise RuntimeError('Published screenshot is not yet readable after bounded reconciliation')


def publish(manifest):
    if not manifest:return
    verify(manifest)
    rt.DATA.mkdir(parents=True,exist_ok=True)
    with (rt.DATA/'screenshot-assets.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);ensure_branch()
        for item in manifest:
            path='contents/'+item['asset']+'?ref='+BRANCH
            existing=optional(path)
            if not existing:
                raw=Path(item['path']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=item['sha256']:
                    raise RuntimeError('Screenshot changed before upload')
                try:
                    api('contents/'+item['asset'],'PUT',{'branch':BRANCH,
                        'message':'Retain reviewed PR screenshot '+item['sha256'][:12],
                        'content':base64.b64encode(raw).decode()})
                except Exception:
                    # Never blindly replay a write. Matching immutable content
                    # positively reconciles an ambiguous response.
                    existing=read_uploaded(item)
                else:
                    existing=read_uploaded(item)
            if not existing or existing.get('sha')!=item['blob']:
                raise RuntimeError('Published screenshot content does not match independent review')
