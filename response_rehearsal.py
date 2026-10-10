"""Read-only reproduction of the real Hermes shallow-history regression."""
import json
from pathlib import Path
import subprocess
import execution_support
import runtime as rt


def main():
    folder=(rt.DATA/'response-rehearsal').resolve();folder.mkdir(parents=True,exist_ok=True)
    work=folder/'work'
    def run(args,cwd=None):
        result=subprocess.run(args,cwd=cwd,capture_output=True,text=True,timeout=600)
        if result.returncode:raise RuntimeError(result.stderr[-2000:]+result.stdout[-2000:])
        return result.stdout.strip()
    pr=json.loads(run(['gh','api','repos/NousResearch/hermes-agent/pulls/127606']))
    head=pr['head']['sha']
    run(['git','clone','--depth=1','--no-checkout',pr['head']['repo']['clone_url'],str(work)])
    def git(*args):return run(['git',*args],work)
    git('remote','add','upstream','https://github.com/NousResearch/hermes-agent.git')
    git('fetch','--depth=1','upstream','main:refs/remotes/upstream/main')
    git('fetch','--depth=1','origin',head);git('checkout','--detach','FETCH_HEAD')
    result=execution_support.update_response_base(work,'main',head)
    git('merge-base','--is-ancestor',head,'HEAD')
    git('merge-base','--is-ancestor','upstream/main','HEAD')
    if git('status','--porcelain'):raise RuntimeError('Response rehearsal left an unresolved working tree')
    result.update(repository='NousResearch/hermes-agent',pr=127606,model_calls=0,publications=0,
                  merge_base=git('merge-base',head,'upstream/main'))
    (folder/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
