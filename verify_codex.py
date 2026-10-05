#!/usr/bin/env python3
"""Offline commit gate for the active Codex runtime; never invokes model APIs."""
import ast
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
subprocess.run([sys.executable,'-m','unittest','-v','test_runtime','test_recovery','test_codex_host'],cwd=root,check=True)
print('Codex gate passed: Python/shell syntax, archived workflow YAML, runtime regression tests; no API calls.')
