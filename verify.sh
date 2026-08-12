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
    (r"gh api (?!.*-X [A-Z]+)(?!.*graphql)[^\n]*\s-f\s",
     "`gh api -f` with no explicit -X sends a POST"),
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
cfg = pathlib.Path("scan.py").read_text()
# Only repos we still scan. Dropping one from REPOS must not leave a check
# demanding it stay gated — but a repo that IS configured and auto-closes
# unassigned PRs must be in GATED, which is what cost pydantic-ai #7282.
# gemini-cli removed 2026-08-08: it has no require_issue_link workflow and
# nothing auto-closes there. 891 PRs carrying status/need-issue have merged and
# 18 of its 25 most recent merges are from non-members, so gating it behind an
# assignment blocked the only supply that was actually open (kind/bug, 99
# unassigned) while its help-wanted pool sat 29/30 self-assigned.
for repo in ("pydantic-ai", "langgraph", "langchain"):
    if f'"{repo}": {{' not in cfg:
        continue
    if repo not in w.split("GATED")[1].split("}")[0]:
        print(f"  FAIL  {repo} is configured but not in GATED — it auto-closes unassigned PRs"); bad += 1
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
# Same failure class, second instance: claim.sh wrote state/<key>.json and
# nothing read it. drain_queues skips GATED, so a granted assignment produced
# no PR — langgraph sat at eight claims and zero PRs for eight days. Any state
# the pipeline writes must have something that acts on it.
if not hasattr(w, "promote_claims"):
    print("  FAIL  watch.py has no promote_claims — claim.sh writes claims nobody reads"); bad += 1
else:
    pc = src.split("def promote_claims")[1].split("\ndef ")[0]
    if "dispatch_fix" not in pc:
        print("  FAIL  promote_claims never dispatches — assignments would go unused"); bad += 1
    if "already_dispatched" not in pc:
        print("  FAIL  promote_claims does not check already_dispatched — it would re-fire every cycle"); bad += 1
    if "budget_allows" not in pc:
        print("  FAIL  promote_claims does not consult budget_allows"); bad += 1
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
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import re, sys, yaml
bad = 0
# Both dispatchers, both workflows: respond-pr.yml takes `upstream`, not
# `repo_key`, and sending the wrong name 422s exactly as silently.
SCANNER = sys.argv[1]
PAIRS = [("fix-one.yml", "watch.py"), ("respond-pr.yml", "watch-prs.py")]
for wf, script in PAIRS:
    declared = set(yaml.safe_load(open(f".github/workflows/{wf}"))[True]["workflow_dispatch"]["inputs"])
    try:
        src = open(f"{SCANNER}/{script}").read()
    except OSError:
        print(f"  FAIL  cannot read {SCANNER}/{script}"); bad += 1; continue
    hits = re.findall(re.escape(wf) + r'(.{0,400}?)(?:\]|\n\n)', src, re.S)
    if not hits:
        print(f"  FAIL  {script} never dispatches {wf} — the trigger was lost"); bad += 1
        continue
    for blk in hits:
        sent = set(re.findall(r'''["']([a-z_]+)=''', blk))
        unknown = sent - declared
        if unknown:
            print(f"  FAIL  {script} dispatches {wf} with unknown input(s): {sorted(unknown)}"); bad += 1
print("  ok     dispatch inputs match both workflows" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 12. dispatch budget can actually reach the PR cap =="
# Two knobs, two files, opposite failure modes. A budget below the cap makes
# the cap unreachable — we would starve a repo while believing it was allowed
# more. comfyui sat at budget 2 / cap 1 for days with ten vetted candidates
# unread because the budget was being used as if it were the cap.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import re, sys
sys.path.insert(0, sys.argv[1])
import watch, scan
src = open(".github/workflows/fix-one.yml").read()
caps = {}
for names, n in re.findall(r'^\s+([a-z0-9|_*-]+)\) cap=(\d+)', src, re.M):
    for k in names.split("|"):
        caps[k] = int(n)
default_cap = caps.get("*", 2)
bad = 0
for k in scan.REPOS:
    b = watch.DISPATCH_BUDGET.get(k, watch.DEFAULT_BUDGET)
    c = caps.get(k, default_cap)
    if b < c:
        print(f"  FAIL  {k}: dispatch budget {b} < PR cap {c} — the cap is unreachable"); bad += 1
print("  ok     every budget can reach its cap" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 13. the stream formatter cannot fail a run =="
# It is an observability aid with the power to kill a 40-minute session. It has
# now done so twice: once via a shell-quoting NameError, and once because a
# WebSearch event carries `message` as a str and `(ev.get("message") or {})`
# raised AttributeError. Feed it the shapes that broke it and assert exit 0.
python3 - <<'PY3' || fail=$((fail+1))
import os, re, subprocess, sys, tempfile, textwrap
# BOTH workflows. respond-pr.yml kept the fragile version for a day after
# fix-one.yml was hardened — the fourth time a fix landed in one file and not
# the other — and it is the workflow that answers reviewers, so it runs more.
BODIES = []
for wf in ("fix-one.yml", "respond-pr.yml"):
    src = open(f".github/workflows/{wf}").read()
    i = src.index("cat > \"$RUNNER_TEMP/fmt.py\" <<'FMT'")
    j = src.index("\n          FMT\n", i)
    BODIES.append((wf, textwrap.dedent(src[src.index("\n", i) + 1:j])))
d = tempfile.mkdtemp()
EVENTS = [
    '{"message": "a plain string", "type": "assistant"}',
    '{"message": {"content": "not a list"}}',
    '{"message": {"content": [null, 3, "str"]}}',
    '{"message": {"content": [{"type": "tool_use", "name": "B", "input": "notadict"}]}}',
    '{"message": {"content": [{"type": "text", "text": null}]}}',
    '{"message": null, "type": "result", "subtype": "success", "num_turns": 7}',
    '"a bare json string"', '[]', 'not json at all',
]
bad = 0
for wf, body in BODIES:
    f = os.path.join(d, f"fmt_{wf}.py")
    open(f, "w").write(body)
    r = subprocess.run([sys.executable, "-u", f], input="\n".join(EVENTS) + "\n",
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"  FAIL  {wf} formatter exited {r.returncode} on malformed events"); bad += 1
    if "Traceback" in r.stderr:
        print(f"  FAIL  {wf} formatter raised on a malformed event"); bad += 1
print("  ok     both formatters survive malformed events" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 14. the per-repo patch size ceiling is wired up =="
# On openclaw size is the whole story: the only PR of ours that merged is +38
# and the seven still open run +47 to +1234, against a +61 median for external
# PRs that land there. The ceiling only works if REPO_KEY actually reaches the
# step — NUM was missing from this same step's env once, and the guard that
# depended on it could only ever fail.
python3 - <<'PY3' || fail=$((fail+1))
import sys, yaml
bad = 0
wf = yaml.safe_load(open(".github/workflows/fix-one.yml"))
step = None
for j in wf["jobs"].values():
    for st in j.get("steps", []):
        if str(st.get("name", "")).startswith("Open PR"):
            step = st
if step is None:
    print("  FAIL  no Open PR step found"); sys.exit(1)
run = step.get("run") or ""
env = step.get("env") or {}
if "max_added" not in run:
    print("  FAIL  Open PR step has no size ceiling"); bad += 1
if "REPO_KEY" not in env:
    print("  FAIL  REPO_KEY missing from the Open PR step env — the ceiling can never match"); bad += 1
if "openclaw)" not in run:
    print("  FAIL  openclaw has no ceiling, and size is the only thing gating it there"); bad += 1
# The responder can inflate a PR past the ceiling the opener refused to cross,
# and did: openclaw#120398 +140 -> +387, #118377 +47 -> +96.
resp = open(".github/workflows/respond-pr.yml").read()
if "MAX_ADDED" not in resp:
    print("  FAIL  respond-pr.yml has no size ceiling — it can grow a PR past the opener's limit"); bad += 1
if "HARD SIZE LIMIT" not in resp:
    print("  FAIL  the responder prompt never states the ceiling, so the agent cannot respect it"); bad += 1
gen = ""
for j in wf["jobs"].values():
    for st in j.get("steps", []):
        if st.get("id") == "gen":
            gen = st.get("run") or ""
if "120" not in gen:
    print("  FAIL  the generator is not told openclaw's ceiling — it would burn a session then be refused"); bad += 1
print("  ok     size ceiling wired into both the gate and the prompt" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 15. claim detection catches real claims and not lookalikes =="
# Missing a claim is the rudest failure this pipeline has: on langchain#38814
# two people had already claimed — one saying they had a patch ready — and we
# posted a third claim on top of them, because "I'd like to attempt a fix" was
# not in the verb list and "have a minimal patch ready" was not in the noun
# list. False positives are costly too: bare `pr` matched inside "problem".
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import importlib.util, re, sys
spec = importlib.util.spec_from_file_location("scan", sys.argv[1] + "/scan.py")
scan = importlib.util.module_from_spec(spec); spec.loader.exec_module(scan)

def detect(b):   # mirrors claimants(): quoted text is stripped first
    return bool(scan.CLAIM_PHRASES.search(re.sub(r"^\s*>.*$", "", b, flags=re.M).lower()))

POS = ["I'd like to attempt a fix — applying the same check",
       "I reproduced this against the current source and have a minimal patch ready.",
       "I'll take this one", "I'd like to work on this", "working on it", "/assign",
       "I have a fix for this", "I've got a patch locally", "I will open a PR shortly",
       "let me handle this", "I'd like to submit a PR for this"]
NEG = ["Does anyone have a fix for this?", "Would be great if someone could take a look.",
       "This is blocking me — any workaround?", "Thanks, that worked!",
       "I have a question about the config.", "Is there a PR for this already?",
       "> I'd like to take this one", "> I have a minimal patch ready\n\nThanks, go ahead.",
       "I have a similar problem on Windows.", "I have a prod deployment affected by this."]
bad = 0
for t in POS:
    if not detect(t):
        print(f"  FAIL  missed a claim: {t[:58]}"); bad += 1
for t in NEG:
    if detect(t):
        print(f"  FAIL  false claim on: {t[:58]}"); bad += 1
print(f"  ok     {len(POS)} claims caught, {len(NEG)} lookalikes ignored" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 16. context sections reference variables their own run block defines =="
# Three times now a step has referenced a name it never had: NUM absent from
# the Open PR env, repo_key sent to a workflow that declares upstream, and
# $PR_NUM/$UPSTREAM written into a collect block whose variables are $N and
# $UP. Each expands to empty and fails silently — "(unavailable)" instead of
# the PR's file list. Assert every $VAR a run block uses is either assigned in
# that block, listed in its env, or a GitHub/runner builtin.
python3 - <<'PY3' || fail=$((fail+1))
import re, sys, yaml
BUILTIN = {"GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_WORKSPACE", "GITHUB_TOKEN",
           "GITHUB_STEP_SUMMARY", "RUNNER_TEMP", "HOME", "PATH", "GITHUB_PATH",
           "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "PIPESTATUS",
           "GITHUB_RUN_ID", "GITHUB_RUN_NUMBER", "GITHUB_ACTOR", "GITHUB_EVENT_NAME",
           "GITHUB_SERVER_URL", "GITHUB_API_URL", "RUNNER_OS", "RUNNER_ARCH",
           # awk field/record builtins — these appear inside embedded awk scripts
           "NF", "NR", "FS", "OFS", "RS", "ORS"}
bad = 0
for wf in ("fix-one.yml", "respond-pr.yml"):
    doc = yaml.safe_load(open(f".github/workflows/{wf}"))
    for job in doc["jobs"].values():
        for st in job.get("steps", []):
            run = st.get("run")
            if not run:
                continue
            env = set((st.get("env") or {}) ) | set(job.get("env") or {}) | set(doc.get("env") or {})
            # Assignments are not always at line start: this file writes
            # `UP="..."; N="..."`, and requiring ^ reported N undefined.
            assigned = set(re.findall(r'(?:^|[;&|(]|\bthen\b|\bdo\b)\s*([A-Z_][A-Z0-9_]*)=', run, re.M))
            assigned |= set(re.findall(r'\b([A-Z_][A-Z0-9_]*)=\$\(', run))
            assigned |= set(re.findall(r'^\s*(?:export|local|declare)\s+([A-Z_][A-Z0-9_]*)', run, re.M))
            used = set(re.findall(r'\$\{?([A-Z_][A-Z0-9_]*)\}?', run))
            unknown = used - assigned - env - BUILTIN
            if unknown:
                print(f"  FAIL  {wf} step {st.get('name','?')!r} uses undefined {sorted(unknown)}"); bad += 1
print("  ok     every run block defines the variables it reads" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 17. the cloud re-vet honours the same config the scanner does =="
# scan.py's search skips the assignee filter for repos that set
# ignore_assignees, because langfuse assigns a triager to nearly every bug.
# fix-one.yml's re-vet applied the filter anyway and killed all 11 langfuse
# dispatches before a session started — 11 dispatches, 0 PRs, for a check the
# config explicitly disables. Any repo carrying the flag must survive the
# re-vet with an assignee present.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, yaml
sys.path.insert(0, sys.argv[1])
import scan
bad = 0
wf = yaml.safe_load(open(".github/workflows/fix-one.yml"))
pre = None
for j in wf["jobs"].values():
    for st in j.get("steps", []):
        if st.get("id") == "pre":
            pre = st
if pre is None:
    print("  FAIL  no re-vet step"); sys.exit(1)
run, env = pre.get("run") or "", pre.get("env") or {}
if "ignore_assignees" not in run:
    print("  FAIL  the re-vet ignores ignore_assignees — it will kill every dispatch to a triaged repo"); bad += 1
if "REPO_KEY" not in env:
    print("  FAIL  REPO_KEY missing from the re-vet env — the config lookup cannot resolve"); bad += 1
flagged = [k for k, c in scan.REPOS.items() if c.get("ignore_assignees")]
if not flagged:
    print("  note   no repo currently sets ignore_assignees")
print("  ok     re-vet honours ignore_assignees" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 18. an announcement is always withdrawn when no PR follows =="
# adk's CONTRIBUTING asks to be told before work starts, so we comment and
# start immediately. A comment we do not honour is worse than none: it tells
# everyone else the issue is taken while nothing is coming, which is how
# langgraph#8408 sat "claimed" with no work for weeks. Announce and withdraw
# must exist together, and the withdraw must run even when the job fails.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, yaml
sys.path.insert(0, sys.argv[1])
import scan
bad = 0
wf = yaml.safe_load(open(".github/workflows/fix-one.yml"))
steps = [st for j in wf["jobs"].values() for st in j.get("steps", [])]
ann = next((s for s in steps if s.get("id") == "announce"), None)
wd = next((s for s in steps if "Withdraw" in str(s.get("name", ""))), None)
opener = next((s for s in steps if s.get("id") == "openpr"), None)
flagged = [k for k, c in scan.REPOS.items() if c.get("announce_before_work")]
if flagged and ann is None:
    print(f"  FAIL  {flagged} ask to be announced to, but no announce step exists"); bad += 1
if ann is not None:
    if wd is None:
        print("  FAIL  announce step with no withdrawal — an unhonoured claim would stand"); bad += 1
    elif "always()" not in str(wd.get("if", "")):
        print("  FAIL  withdrawal is not always() — a failed run would leave the claim"); bad += 1
    if opener is None or "opened=true" not in (opener.get("run") or ""):
        print("  FAIL  Open PR does not report success, so withdrawal cannot tell"); bad += 1
    if "grep -qx 'chelsealong'" not in (ann.get("run") or ""):
        print("  FAIL  announce is not idempotent — it would comment twice on a retry"); bad += 1
print("  ok     announce and withdraw are wired together" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 19. repos that told us to stop are actually excluded =="
# spec-kit's maintainer asked three times, across seven closed PRs, that we not
# open PRs for catalog submissions — those flow through the project's own
# agentic workflow, which validates and opens the PR itself. A request like that
# has to live in the config, not in a person's memory.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import re, sys
sys.path.insert(0, sys.argv[1])
import scan
bad = 0
cfg = scan.REPOS.get("spec-kit")
if cfg is None:
    print("  note   spec-kit is no longer configured"); sys.exit(0)
need = {"extension-submission", "preset-submission", "bundle-submission"}
missing = need - set(cfg.get("exclude_labels") or ())
if missing:
    print(f"  FAIL  spec-kit does not exclude {sorted(missing)} — mnriem asked three times"); bad += 1
pat = cfg.get("exclude_title") or ""
# The label lands after the issue is filed; #4068 was "[Extension]: Add specjudge"
# with only `enhancement` on it, so the title prefix has to carry it too.
for t in ("[Extension]: Add specjudge", "[Preset]: Add x", "[Bundle]: Add y"):
    if not re.search(pat, t, re.I):
        print(f"  FAIL  spec-kit title filter misses {t!r}"); bad += 1
for t in ("argument-hint injection is not fold-aware",
          "reject duplicate provides.templates entries"):
    if re.search(pat, t, re.I):
        print(f"  FAIL  spec-kit title filter would drop a code fix: {t!r}"); bad += 1
print("  ok     spec-kit catalog submissions are excluded" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo
if [ "$fail" -eq 0 ]; then echo "  PASS — safe to commit"; exit 0; fi
echo "  $fail FAILURE(S) — do not commit"; exit 1
