"""Rehearse real OpenClaw PR validation without any model or publication calls."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch
import execution_support
import runtime as rt
import validation_setup as validation


def main():
    folder=rt.DATA/'production-rehearsal';folder.mkdir(parents=True,exist_ok=True)
    folder=folder.resolve();work=folder/'work';cache=folder/'tool-cache'
    cache.mkdir();(cache/'home').mkdir()
    def git(*args):
        return subprocess.run(['git',*args],cwd=work if work.exists() else folder,
                              capture_output=True,text=True,check=True,timeout=600).stdout.strip()
    git('clone','--depth=1','--no-checkout','https://github.com/openclaw/openclaw.git',str(work))
    # This is an existing real contribution, never a test PR or new submission.
    git('fetch','--depth=2','origin','refs/pull/167962/head')
    git('checkout','--detach','FETCH_HEAD');head=git('rev-parse','HEAD')
    git('reset','--mixed','HEAD^')
    base=git('rev-parse','HEAD')
    if not git('diff','--name-only'):raise RuntimeError('Real PR rehearsal has no source patch')
    with (folder/'install.log').open('w') as log:
        process=subprocess.Popen(validation.sandbox(work,cache,['pnpm','install','--frozen-lockfile']),
            env=validation.safe_env(cache),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            if process.wait(timeout=900):raise RuntimeError('Real repository dependency install failed; inspect install.log')
        finally:validation.terminate(process)
    cfg={'backend':'codex-local','enabled':False,'cloud_environment':True}
    with patch.object(rt,'config',return_value=cfg):
        execution_support.managed_check({'id':1,'repo':'openclaw','kind':'canary','attempts':1},work,folder,'openclaw')
    report=json.loads((folder/'required-check.json').read_text())
    report.update({'repository':'openclaw/openclaw','source_pr':167962,'source_head':head,
                   'base':base,'model_calls':0,'publications':0})
    (folder/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
