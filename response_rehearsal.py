"""Read-only reproduction of the real OpenClaw response-history regression."""
import json
from pathlib import Path
import execution_support
import codex_worker
import runtime as rt


def rehearse(number):
    folder=(rt.DATA/'response-rehearsal'/str(number)).resolve();folder.mkdir(parents=True,exist_ok=True)
    work=folder/'work'
    def run(args,cwd=None):
        return codex_worker.run(args,cwd=cwd,timeout=600)
    repo='openclaw/openclaw'
    pr=json.loads(run(['gh','api',f'repos/{repo}/pulls/{number}']))
    head=pr['head']['sha']
    run(['git','clone','--depth=1','--no-checkout',pr['head']['repo']['clone_url'],str(work)])
    def git(*args):return run(['git',*args],work)
    git('remote','add','upstream','https://github.com/'+repo+'.git')
    git('fetch','--depth=1','upstream','main:refs/remotes/upstream/main')
    git('fetch','--depth=1','origin',head);git('checkout','--detach','FETCH_HEAD')
    try:result=execution_support.prepare_response_base(work,'main',pr,number)
    except RuntimeError as error:
        # An independently confirmed real source conflict is an expected held
        # outcome, not a shallow-graph failure or a reason to disable all repos.
        if (pr.get('mergeable') is not False or
                not str(error).startswith('Current upstream conflicts with PR branch; manual conflict resolution required: ')):
            raise
        if git('rev-parse','HEAD')!=head:raise RuntimeError('Conflict recovery changed the PR head') from error
        result={'original_head':head,'base':head,'outcome':'confirmed_conflict_retained','reason':str(error)}
    git('merge-base','--is-ancestor',head,'HEAD')
    if git('status','--porcelain'):raise RuntimeError('Response rehearsal left an unresolved working tree')
    result.update(repository=repo,pr=number,model_calls=0,publications=0)
    (folder/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result));return result


def main():
    results=[rehearse(number) for number in (158915,159036)]
    (rt.DATA/'response-rehearsal/result.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':main()
