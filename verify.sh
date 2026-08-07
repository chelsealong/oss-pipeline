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

echo "== 6. guards apply to every agent call, not just one file =="
python3 - <<PY2 || fail=$((fail+1))
import yaml, pathlib, re, sys
# Three times in one day a guard was added to one workflow and not the other:
# the one-shot session rule, the dispatch reason, and streaming output. fix-one
# generates every new PR and was the one left blind — a run that ended without
# a conclusion could not be diagnosed at all. Structural rules belong in a
# check, not in whoever remembers.
bad = 0
for f in sorted(pathlib.Path(".github/workflows").glob("*.yml")):
    d = yaml.safe_load(f.read_text()) or {}
    for jn, job in (d.get("jobs") or {}).items():
        for st in job.get("steps", []):
            run = st.get("run") or ""
            if "claude -p" not in run:
                continue
            # Probes send a fixed one-line prompt and assert on the reply; they
            # do no work, so streaming and a one-shot rule would be noise. The
            # rules below are for calls that change a repository.
            if f.name.startswith("probe-"):
                continue
            # pipeline.yml's fixer is retired — armed only by explicit dispatch
            # after it was found running on every scheduled tick with no review
            # gate. Left in place, not maintained.
            if f.name == "pipeline.yml":
                continue
            name = f"{f.name}/{st.get('name')}"
            if "output-format stream-json" not in run:
                print(f"  FAIL  {name}: claude -p without streaming output — a failed run leaves no trace"); bad += 1
            if not re.search(r"ONE-SHOT|one-shot|never background", run, re.I):
                print(f"  FAIL  {name}: prompt lacks the one-shot session rule"); bad += 1
            if "pipefail" not in run and "PIPESTATUS" not in run:
                print(f"  FAIL  {name}: piped agent call without pipefail/PIPESTATUS — the agent's status is masked"); bad += 1
print("  ok     every agent call is streamed, bounded and status-preserving" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY2

echo "== 7. the generator prompt forbids out-of-scope edits =="
python3 - <<PY2 || fail=$((fail+1))
import yaml, pathlib, re, sys
# openclaw#115138 got a dependency-graph commit on a two-file SQLite fix and
# self-reverted. The prompt has to name lockfiles explicitly; "minimal fix" was
# not enough.
d = yaml.safe_load(pathlib.Path(".github/workflows/fix-one.yml").read_text()) or {}
run = ""
for jn, job in (d.get("jobs") or {}).items():
    for st in job.get("steps", []):
        if st.get("id") == "gen":
            run = st.get("run") or ""
bad = 0
for term in ("lockfile", "pnpm-lock", "uv.lock", "manifest"):
    if term.lower() not in run.lower():
        print(f"  FAIL  generator prompt does not mention {term!r}"); bad += 1
print("  ok     dependency edits are named and forbidden" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY2

echo "== 8. per-repo contribution gates are encoded =="
python3 - <<PY2 || fail=$((fail+1))
import yaml, pathlib, sys
# Every gate below cost a PR to discover. pydantic-ai closed ours after 18
# seconds because its pr-guard requires assignment; vllm enforces DCO per
# commit. Neither showed up in the repo's merge statistics, which is what was
# measured before adding them.
gen = ""
d = yaml.safe_load(pathlib.Path(".github/workflows/fix-one.yml").read_text()) or {}
for jn, job in (d.get("jobs") or {}).items():
    for st in job.get("steps", []):
        if st.get("id") == "gen":
            gen = st.get("run") or ""
bad = 0
if "Signed-off-by" not in gen or "vllm" not in gen:
    print("  FAIL  generator prompt does not require Signed-off-by for vllm (DCO)"); bad += 1
w = pathlib.Path("watch.py").read_text()
for repo in ("pydantic-ai", "langgraph", "langchain", "gemini-cli"):
    if repo not in w.split("GATED")[1].split("}")[0]:
        print(f"  FAIL  {repo} is not in GATED but auto-closes unassigned PRs"); bad += 1
print("  ok     assignment gates and DCO are encoded" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY2

echo "== 9. queued work has a consumer =="
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

echo "== 10. tracked copies match the scanner =="
for f in scan.py watch-prs.py health.py; do
  if [ -f "$f" ] && [ -f "$SCANNER/$f" ]; then
    diff -q "$f" "$SCANNER/$f" >/dev/null 2>&1 && ok "$f in sync" || bad "$f differs from $SCANNER/$f"
  fi
done

echo "== 11. dispatch inputs match the workflow's declared inputs =="
# A renamed workflow_dispatch input does not error at the workflow; it makes
# every dispatch 422 "Unexpected inputs provided" and the queue drains into
# nothing, silently. Assert the names watch.py sends are the names fix-one
# declares.
python3 - "$SCANNER/watch.py" <<'PY3' || fail=$((fail+1))
import re, sys, yaml
bad = 0
declared = set(yaml.safe_load(open(".github/workflows/fix-one.yml"))[True]["workflow_dispatch"]["inputs"])
src = open(sys.argv[1]).read()
for blk in re.findall(r'fix-one\.yml(.{0,400}?)(?:\]|\n\n)', src, re.S):
    sent = set(re.findall(r'''["']([a-z_]+)=''', blk))
    unknown = sent - declared
    if unknown:
        print(f"  FAIL  watch.py dispatches fix-one.yml with unknown input(s): {sorted(unknown)}"); bad += 1
print("  ok     dispatch inputs match fix-one.yml" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo
if [ "$fail" -eq 0 ]; then echo "  PASS — safe to commit"; exit 0; fi
echo "  $fail FAILURE(S) — do not commit"; exit 1
