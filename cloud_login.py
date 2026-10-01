#!/usr/bin/env python3
"""Independent subscription login on a runner; never print codes or tokens."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
import cloud_runtime

def publish_challenge(log, public_key, request_id):
    # Codex's official device flow prints a 4-5 character grouped user code.
    clean=re.sub(r'\x1b\[[0-9;]*m','',log)
    match=re.search(r'\b[A-Z0-9]{4}-[A-Z0-9]{5}\b',clean)
    if not match:return False
    public_key.write_text(os.environ['CODEX_LOGIN_PUBLIC_KEY'])
    encrypted=subprocess.run(['openssl','pkeyutl','-encrypt','-pubin','-inkey',str(public_key),
        '-pkeyopt','rsa_padding_mode:oaep','-pkeyopt','rsa_oaep_md:sha256'],
        input=match.group().encode(),capture_output=True,check=True).stdout
    value=json.dumps({'request':request_id,'encrypted_code':base64.b64encode(encrypted).decode()})
    cloud_runtime.gh(['variable','set','CODEX_LOGIN_CHALLENGE','--repo',cloud_runtime.REPOSITORY,'--body',value])
    return True

def main():
    home=Path(os.environ['CODEX_HOME']);home.mkdir(mode=0o700,parents=True,exist_ok=True)
    (home/'config.toml').write_text('cli_auth_credentials_store = "file"\n')
    log=home/'device-login.log'
    env=dict(os.environ)
    for key in ['GH_TOKEN','GITHUB_TOKEN','OPENAI_API_KEY','CODEX_API_KEY','QWEN_API_KEY']:
        env.pop(key,None)
    request_id=os.environ['GITHUB_RUN_ID']
    child=None;published=False
    try:
        with log.open('w') as output:
            child=subprocess.Popen(['codex','-c','cli_auth_credentials_store="file"','login','--device-auth'],
                env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+900
            while child.poll() is None and time.monotonic()<deadline:
                if not published:
                    published=publish_challenge(log.read_text(errors='replace'),home/'public.pem',request_id)
                    if published:print('Encrypted login challenge ready for the owner.',flush=True)
                time.sleep(3)
            if child.poll() is None:
                child.terminate();child.wait(timeout=15)
                raise RuntimeError('Device login expired; no credentials saved')
        if child.returncode or not cloud_runtime.auth_file().exists():
            raise RuntimeError('Official Codex device login failed; private login output withheld')
        raw=cloud_runtime.auth_file().read_text()
        cloud_runtime.validate_auth(raw)
        cloud_runtime.gh(['secret','set','CODEX_AUTH_JSON','--repo',cloud_runtime.REPOSITORY],input=raw)
        print('Independent ChatGPT subscription login saved to encrypted repository Secret.',flush=True)
    finally:
        if child and child.poll() is None:child.terminate();child.wait(timeout=15)
        if published:
            cloud_runtime.gh(['variable','delete','CODEX_LOGIN_CHALLENGE','--repo',cloud_runtime.REPOSITORY])

if __name__=='__main__':main()
