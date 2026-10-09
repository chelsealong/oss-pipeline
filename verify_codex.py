#!/usr/bin/env python3
"""Offline commit gate for the active Codex runtime; never invokes model APIs."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import yaml

root=Path(__file__).resolve().parent
for path in root.glob('*.py'):
    ast.parse(path.read_text(),filename=str(path))
for path in root.glob('*.sh'):
    subprocess.run(['bash','-n',str(path)],check=True)
for path in (root/'.github/workflows').glob('*.yml'):
    yaml.safe_load(path.read_text())
# Git hooks export the parent index/worktree into every test subprocess. Strip
# these so temporary fixture repositories cannot accidentally stage the parent.
test_env={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
subprocess.run([sys.executable,'-m','unittest','-v','test_runtime','test_recovery','test_codex_host','test_resumption','test_pr_followup','test_claim_admission','test_discovery_recovery','test_publication_holds'],cwd=root,env=test_env,check=True)
print('Codex gate passed: Python/shell syntax, archived workflow YAML, runtime regression tests; no API calls.')
