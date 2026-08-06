#!/usr/bin/env bash
# Pre-commit evaluator for the OSS pipeline.
#
# Every check here exists because a change shipped broken and was found in
# production instead. Add a check for each new failure class; fixing only the
# instance leaves the next one free to happen.
set -uo pipefail
# Target dir is overridable so the harness can be pointed at a fixture and
# proven to actually catch the failures it claims to. An evaluator nobody can
# test is just another untested change.
cd "${VERIFY_ROOT:-$(dirname "$0")}" || exit 1
SCANNER="${SCANNER:-/Users/jialong/.local/share/oss-scanner}"
fail=0
note() { printf '  %-6s %s\n' "$1" "$2"; }
bad()  { fail=$((fail+1)); note "FAIL" "$1"; }
ok()   { note "ok" "$1"; }

echo "== 1. workflow YAML parses =="
for f in .github/workflows/*.yml; do
  if python3 -c "import yaml,sys;yaml.safe_load(open(sys.argv[1]))" "$f" 2>/dev/null; then
    ok "$(basename "$f")"
  else
    bad "$(basename "$f") is not valid YAML"
  fi
done

echo "== 2. embedded python parses, and carries no shell-quote hazard =="
python3 - <<'PY' || fail=$((fail+1))
import yaml, pathlib, ast, re, sys
bad = 0
for f in sorted(pathlib.Path(".github/workflows").glob("*.yml")):
    d = yaml.safe_load(f.read_text()) or {}
    for jn, job in (d.get("jobs") or {}).items():
        for st in job.get("steps", []):
            run = st.get("run") or ""
            # python embedded in a SINGLE-QUOTED shell string cannot contain '
            for m in re.finditer(r"python3?\s+(?:-u\s+)?-c\s+'(.*?)'\n", run, re.S):
                if "'" in m.group(1):
                    print(f"  FAIL  {f.name}/{st.get('name')}: python3 -c '...' contains an apostrophe "
                          "— the shell string ends there"); bad += 1
            # heredoc-delivered python must parse
            for m in re.finditer(r"<<'(\w+)'\n(.*?)\n\s*\1\n", run, re.S):
                # dedent, not a fixed strip: the YAML block scalar has already
                # removed the step's own indentation, so how much is left is
                # whatever the heredoc was written with. A fixed l[10:] reported
                # a valid formatter as broken.
                import textwrap
                body = textwrap.dedent(m.group(2))
                if "import " not in body and "print(" not in body:
                    continue
                try:
                    ast.parse(body)
                except SyntaxError as e:
                    print(f"  FAIL  {f.name}/{st.get('name')}: embedded python: {e}"); bad += 1
print("  ok     embedded python" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY

echo "== 3. shell hazards that killed a step in production =="
python3 - <<'PY' || fail=$((fail+1))
import yaml, pathlib, re, sys
bad = 0
HAZ = [
    (r"--paginate[^\n|]*\|\s*head\b",
     "`gh --paginate | head` SIGPIPEs the writer; exit 141 kills the step (use sed -n)"),
    (r"gh api (?!.*-X GET)(?!.*graphql)[^\n]*\s-f\s",
     "`gh api -f` without `-X GET` sends a POST"),
]
for f in sorted(pathlib.Path(".github/workflows").glob("*.yml")):
    d = yaml.safe_load(f.read_text()) or {}
    for jn, job in (d.get("jobs") or {}).items():
        for st in job.get("steps", []):
            run = st.get("run") or ""
            for pat, why in HAZ:
                if re.search(pat, run):
                    print(f"  FAIL  {f.name}/{st.get('name')}: {why}"); bad += 1
            # a pipeline whose status is read must not lose it
            if re.search(r"\|\s*tee\b", run) and "PIPESTATUS" not in run and "pipefail" not in run:
                print(f"  FAIL  {f.name}/{st.get('name')}: `| tee` without PIPESTATUS/pipefail "
                      "reports tee's status, which is always 0"); bad += 1
print("  ok     no known shell hazard" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY

echo "== 4. local scripts parse =="
for f in "$SCANNER"/*.py; do
  python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null \
    && ok "$(basename "$f")" || bad "$(basename "$f") does not parse"
done
for f in "$SCANNER"/*.sh; do
  bash -n "$f" 2>/dev/null && ok "$(basename "$f")" || bad "$(basename "$f") shell syntax"
done

echo "== 5. classifier unit tests =="
python3 - <<PY || fail=$((fail+1))
import sys, importlib.util
spec = importlib.util.spec_from_file_location("wp", "$SCANNER/watch-prs.py")
wp = importlib.util.module_from_spec(spec); spec.loader.exec_module(wp)
cases = [("openclaw/ci-gate", False), ("Check PR Status", False), ("merge-gate", False),
         ("Vercel", False), ("codecov/patch/x", False), ("security/snyk (org)", False),
         ("checks-node-compact-small-4", True), ("gateway-tests", True),
         ("check API types", True), ("test (3.13)", True), ("CodeQL", True), ("lint", True)]
bad = 0
for n, want in cases:
    got = not wp.NOT_OUR_CHECKS.search(n)
    if got != want:
        print(f"  FAIL  ownership: {n!r} -> ours={got}, expected {want}"); bad += 1
print("  ok     check-ownership classifier" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY

echo "== 6. queued work has a consumer =="
python3 - <<PY2 || fail=$((fail+1))
import sys, importlib.util
spec = importlib.util.spec_from_file_location("w", "$SCANNER/watch.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
bad = 0
# The queue only ever filled up: its sole reader was run-fix.sh, which runs
# claude locally and cannot authenticate from this machine. 8 of 13 repos
# dispatched nothing for days while 24 vetted candidates sat unread.
if not hasattr(w, "drain_queues"):
    print("  FAIL  watch.py has no drain_queues — queued candidates have no consumer"); bad += 1
else:
    src = open("$SCANNER/watch.py").read()
    if "budget_allows" not in src.split("def drain_queues")[1].split("def ")[0]:
        print("  FAIL  drain_queues does not consult budget_allows — it could run away"); bad += 1
    if "GATED" not in src.split("def drain_queues")[1].split("def ")[0]:
        print("  FAIL  drain_queues does not skip GATED repos — those need claim.sh first"); bad += 1
print("  ok     queue drainer present and gated" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY2

echo "== 7. tracked copies match the scanner =="
for f in scan.py watch-prs.py health.py; do
  if [ -f "$f" ] && [ -f "$SCANNER/$f" ]; then
    diff -q "$f" "$SCANNER/$f" >/dev/null 2>&1 && ok "$f in sync" || bad "$f differs from $SCANNER/$f"
  fi
done

echo
if [ "$fail" -eq 0 ]; then echo "  PASS — safe to commit"; exit 0; fi
echo "  $fail FAILURE(S) — do not commit"; exit 1
