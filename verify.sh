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

# The gate has to measure what the maintainer sees. It did not: openclaw#126138
# passed at "+107" against a 120 ceiling and GitHub reports it as +190. Two
# faults compounded — `^+[^+]` needs a character after the +, so every added
# BLANK line was invisible (16 on that PR), and the diff on disk was written by
# the reviewer before the remediation step's commits.
if "grep -c '^+[^+]'" in run:
    print("  FAIL  size counted with ^+[^+] — added blank lines are not counted"); bad += 1
if "^+++" not in run:
    print("  FAIL  size count does not subtract the +++ header, so it over- or under-counts"); bad += 1
if "git merge-base" not in run or "change.diff" not in run.split("max_added")[1][:900]:
    print("  FAIL  the gate reuses a diff it did not compute — it can be stale"); bad += 1
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

# Offering to TEST is not claiming. "I will try this to run the build locally"
# on adk#6691 closed adk#6697 automatically — a PR a collaborator had just
# called "the right one to land", while its reporter was verifying it against
# AlloyDB. Bare `try` is out; a claim now needs "try to <verb>".
POS = ["I'll try to fix this today", "I will look into this and send a patch",
       "I'd like to attempt a fix — applying the same check",
       "I reproduced this against the current source and have a minimal patch ready.",
       "I'll take this one", "I'd like to work on this", "working on it", "/assign",
       "I have a fix for this", "I've got a patch locally", "I will open a PR shortly",
       "let me handle this", "I'd like to submit a PR for this"]
NEG = ["Does anyone have a fix for this?", "Would be great if someone could take a look.",
       "This is blocking me — any workaround?", "Thanks, that worked!",
       "I have a question about the config.", "Is there a PR for this already?",
       "> I'd like to take this one", "> I have a minimal patch ready\n\nThanks, go ahead.",
       "I have a similar problem on Windows.", "I have a prod deployment affected by this.",
       "sure I will try this to run the build locally instead of 2.6.1 and will update.",
       "I'll try your branch and report back",
       "I will try the build from #6697 and see if it works",
       "let me try this patch on my setup"]
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
    # Removed 2026-08-13 on Bruce's instruction. Assert it stays gone rather
    # than quietly passing, so a future re-add has to be deliberate.
    import re as _re
    src = open(sys.argv[1] + "/scan.py").read()
    if "spec-kit REMOVED" not in src:
        print("  FAIL  spec-kit is unconfigured but the removal note is gone — was it re-added and dropped?"); sys.exit(1)
    print("  ok     spec-kit removed on instruction, note intact"); sys.exit(0)
need = {"extension-submission", "preset-submission", "bundle-submission",
        # Their own workflows' failure reports: #4077 is the catalog submission
        # workflow reporting its own break and asking for an agent — theirs.
        "agentic-workflows"}
missing = need - set(cfg.get("exclude_labels") or ())
if missing:
    print(f"  FAIL  spec-kit does not exclude {sorted(missing)} — mnriem asked three times"); bad += 1
pat = cfg.get("exclude_title") or ""
# The label lands after the issue is filed; #4068 was "[Extension]: Add specjudge"
# with only `enhancement` on it, so the title prefix has to carry it too.
for t in ("[Extension]: Add specjudge", "[Preset]: Add x", "[Bundle]: Add y",
          "[aw] Add Community Extension from Issue Submission failed"):
    if not re.search(pat, t, re.I):
        print(f"  FAIL  spec-kit title filter misses {t!r}"); bad += 1
for t in ("argument-hint injection is not fold-aware",
          "reject duplicate provides.templates entries"):
    if re.search(pat, t, re.I):
        print(f"  FAIL  spec-kit title filter would drop a code fix: {t!r}"); bad += 1
print("  ok     spec-kit catalog submissions are excluded" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 20. claiming cannot outrun delivery =="
# pydantic blocked us on 2026-08-12. claim.sh had posted the identical line
# "I'd like to take this one..." on eight of their issues over five days and
# delivered zero PRs. langchain was receiving the same pattern — 5 comments, 4
# claims, 0 PRs — when this was found. A claim is a promise; more promises while
# the old ones are unkept is what reads as squatting.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys
bad = 0
src = open(sys.argv[1] + "/claim.sh").read()
if "MAX_UNFULFILLED" not in src:
    print("  FAIL  claim.sh has no unfulfilled-claim gate — it can promise without delivering"); bad += 1
if "pydantic-ai" in src.split("declare -a GATED=(")[1].split(")")[0]:
    print("  FAIL  pydantic-ai is still in claim.sh's GATED list — that org has blocked us"); bad += 1
scan_src = open(sys.argv[1] + "/scan.py").read()
if '"pydantic-ai": {' in scan_src:
    print("  FAIL  pydantic-ai is configured again — the org blocked us; do not re-add"); bad += 1
if "pydantic-ai REMOVED" not in scan_src:
    print("  FAIL  the pydantic-ai removal note is gone — the reason must stay recorded"); bad += 1
print("  ok     claiming is gated on delivery, pydantic-ai stays out" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 21. the responder respects each repo's real merge window =="
# Each repo's window comes from where its landings actually stop, and the two
# in the table stop in different places for different reasons. hermes lands by
# salvage — a maintainer cherry-picks our commits into their own PR — and those
# arrived at 16h, 151h, 3 days and 5 days, so its window is 10 days. adk lands
# by Copybara import: over 204 external PRs the median was 4.5 days and 97% of
# landings were inside 14 days, so past that a PR is finished, not slow.
# The window must survive a missing or malformed createdAt without skipping a
# live PR, and every repo in the table must be exercised at its own boundary —
# a case written for a repo that had no window keeps passing after one is added
# and stops testing anything.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import importlib.util, sys
from datetime import datetime, timezone, timedelta
spec = importlib.util.spec_from_file_location("wp", sys.argv[1] + "/watch-prs.py")
wp = importlib.util.module_from_spec(spec); spec.loader.exec_module(wp)
bad = 0
if not getattr(wp, "MERGE_WINDOW_HOURS", None):
    print("  FAIL  no merge-window table"); sys.exit(1)
src = open(sys.argv[1] + "/watch-prs.py").read()
if "createdAt" not in src.split("number title url")[1][:60]:
    print("  FAIL  the PR query does not fetch createdAt, so age cannot be computed"); bad += 1
now = datetime.now(timezone.utc)
iso = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")
# Boundary cases read the table rather than hard-coding it, so raising a
# window does not silently leave this check testing the old one. Every repo
# in the table is exercised, so adding one cannot leave it untested.
CASES = []
for repo, W in wp.MERGE_WINDOW_HOURS.items():
    CASES += [({"_repo": repo, "createdAt": iso(1)}, False),
              ({"_repo": repo, "createdAt": iso(W - 1)}, False),
              ({"_repo": repo, "createdAt": iso(W + 1)}, True),
              ({"_repo": repo, "createdAt": iso(W * 5)}, True),
              ({"_repo": repo}, False),
              ({"_repo": repo, "createdAt": "garbage"}, False)]
# A repo with no entry must never be aged out.
CASES += [({"_repo": "langgenius/dify", "createdAt": iso(9000)}, False)]
# The windows are measurements, not preferences. Guard the two that were
# derived above so a casual edit cannot quietly revert them.
if wp.MERGE_WINDOW_HOURS.get("NousResearch/hermes-agent") != 240:
    print("  FAIL  hermes window is not 10 days — salvage lands up to 5 days out"); bad += 1
if wp.MERGE_WINDOW_HOURS.get("google/adk-python") != 336:
    print("  FAIL  adk window is not 14 days — 97% of its landings are inside it"); bad += 1
# ComfyUI is the fastest repo we track: external PRs merge at a median of 0.5
# days, p90 7.2, and 37 of 37 sampled inside 14. A longer window here would
# spend sessions answering PRs the queue has already moved past.
# These two were set from a biased sample and had to be corrected. Sampling the
# newest-CREATED merged PRs selects for the ones that merged fast: the slow ones
# have not merged yet and cannot appear. That method put openclaw's slowest
# external merge at 4h and the window at 24h; openclaw#121306 merged the same
# day at 10 days old. Re-measured by MERGE time over 84 PRs: p90 140h.
if wp.MERGE_WINDOW_HOURS.get("Comfy-Org/ComfyUI") != 240:
    print("  FAIL  ComfyUI window is not 10 days — its p90 by merge time is 8.8"); bad += 1
if wp.MERGE_WINDOW_HOURS.get("openclaw/openclaw") != 168:
    print("  FAIL  openclaw window is not 7 days — its p90 by merge time is 5.8"); bad += 1
# The method note has to stay next to the numbers, or the next person measures
# it the same wrong way.
wsrc = src.split("MERGE_WINDOW_HOURS = {")[0][-1400:]
if "merge time" not in wsrc.lower() or "creation" not in wsrc.lower():
    print("  FAIL  the sampling-bias warning is gone from beside the window table"); bad += 1

# A failing check is only ours if our change could have caused it. Four
# unrelated PRs of ours — an arXiv link fix, two Desktop changes, a skills
# validator — all failed hermes's `Run tests slice 10/12` on a test in
# tests/gateway/test_goal_continuation_drain.py that none of them touches,
# while other authors were failing slices 9/12 and 2/12 in the same hour.
if not hasattr(wp, "broken_for_everyone"):
    print("  FAIL  nothing checks whether a failing check is failing for everyone"); bad += 1
if wp._norm_check("Run tests slice 10/12") != wp._norm_check("Run tests slice 9/12"):
    print("  FAIL  shard numbers are not normalised, so a broken suite reads as ours"); bad += 1
# Fork-only failures cannot be seen that way: they never appear on a PR opened
# from a branch inside the org. openclaw's check-sqlite-session-flip-proof was
# traced to fork isolation and written into lessons, and was still being treated
# as ours.
if not wp.FORK_ONLY_CHECKS.get("openclaw/openclaw"):
    print("  FAIL  established fork-only checks are not recorded"); bad += 1
if wp._is_ours({"_repo": "openclaw/openclaw"}, "check-sqlite-session-flip-proof"):
    print("  FAIL  a known fork-only check still counts as ours"); bad += 1
if not wp._is_ours({"_repo": "openclaw/openclaw"}, "build / tsc"):
    print("  FAIL  a genuine build failure is being dismissed as not ours"); bad += 1
for pr, want in CASES:
    got, _ = wp.past_merge_window(pr)
    if got != want:
        print(f"  FAIL  {pr.get('createdAt','(none)')[:24]} in {pr['_repo']}: got {got}, want {want}"); bad += 1
print("  ok     merge window applied, and safe on missing/bad timestamps" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 22. landed-commit counting sees every identity and every repo =="
# adk's Copybara preserves whatever author the PR's commits carried: #6498 went
# in as jialongli001@gmail.com and #6649 as chelsealong@126.com. health.py
# searched one address and reported adk as 1 landed commit instead of 2, from
# 2026-07-28 until 08-13. REPO_LIST had drifted the other way — spec-kit still
# listed after removal, ComfyUI never added despite having a landed commit — so
# it is derived from scan.REPOS now, plus an explicit RETIRED list.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import importlib.util, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("h", sys.argv[1] + "/health.py")
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
import scan
bad = 0
emails = getattr(h, "ME_EMAILS", None)
if not emails or len(emails) < 2:
    print("  FAIL  health.py searches a single author identity — Copybara rewrites it"); bad += 1
elif not any("jialongli001" in e for e in emails):
    print("  FAIL  the old gmail identity is missing; adk#6498 lands under it"); bad += 1
src = open(sys.argv[1] + "/health.py").read()
defn = src.split("REPO_LIST = sorted(")[1][:200] if "REPO_LIST = sorted(" in src else ""
if "scan.REPOS" not in defn:
    print("  FAIL  REPO_LIST is hand-maintained again — it drifts silently"); bad += 1
active = {c.get("implements_in") or c["upstream"] for c in scan.REPOS.values()}
missing = active - set(h.REPO_LIST)
if missing:
    print(f"  FAIL  REPO_LIST misses configured repos: {sorted(missing)}"); bad += 1
if "github/spec-kit" not in h.REPO_LIST:
    print("  FAIL  a retired repo with landed commits vanished from the report"); bad += 1
# Credit is not a commit. hermes#86244 landed our analysis under teknium1's
# authorship — real, but not a commit of ours — while AutoGPT#13761/#13764
# matched only because an overlap bot listed our open PRs in a comment. The
# metric is only useful if it excludes bot noise and does not double-count
# salvages, which already appear in the commit total.
if not hasattr(h, "check_credited"):
    print("  FAIL  no credited metric"); bad += 1
else:
    cs = open(sys.argv[1] + "/health.py").read().split("def check_credited")[1].split("\ndef ")[0]
    if "CREDIT" not in cs:
        print("  FAIL  check_credited counts any mention, including bot comments"); bad += 1
    if "pulls/{it['n']}/commits" not in cs:
        print("  FAIL  check_credited does not exclude salvages — they are already counted as landed"); bad += 1
    if '"credited": check_credited()' not in open(sys.argv[1] + "/health.py").read():
        print("  FAIL  the credited metric is not reported"); bad += 1
print("  ok     all identities searched, repo list derived, credit kept separate" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 23. a quota refusal stops dispatching instead of reporting success =="
# `claude -p` prints "You've hit your session limit" and exits 0, so the run
# reported success and the watcher kept dispatching into an empty subscription:
# on 2026-08-14, 14 of the 23 runs that reached Claude were turned away in under
# 70 seconds while per-repo allowances drained. fix-one.yml now fails a named
# step; the watcher finds it via the jobs endpoint (a log download takes minutes
# per run and cannot be done every cycle) and pauses.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import importlib.util, json, pathlib, sys, tempfile, yaml
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("w", sys.argv[1] + "/watch.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
bad = 0

wf = yaml.safe_load(open(".github/workflows/fix-one.yml"))
steps = [st for j in wf["jobs"].values() for st in j.get("steps", [])]
q = next((st for st in steps if "QUOTA EXHAUSTED" in str(st.get("name", ""))), None)
if q is None:
    print("  FAIL  fix-one.yml has no QUOTA EXHAUSTED step — a refusal still reports success"); bad += 1
else:
    if "QUOTA" not in str(q.get("if", "")):
        print("  FAIL  the QUOTA step does not key on the generator outcome"); bad += 1
    if "exit 1" not in (q.get("run") or ""):
        print("  FAIL  the QUOTA step does not fail, so the run stays green and invisible"); bad += 1

for name in ("quota_paused", "note_quota_exhausted", "check_quota_runs"):
    if not hasattr(w, name):
        print(f"  FAIL  watch.py has no {name}"); bad += 1
src = open(sys.argv[1] + "/watch.py").read()
if "quota_paused()" not in src.split("def budget_allows")[1].split("\ndef ")[0]:
    print("  FAIL  budget_allows ignores the pause — dispatching continues while empty"); bad += 1
if "--log" in src.split("def check_quota_runs")[1].split("\ndef ")[0]:
    print("  FAIL  check_quota_runs downloads logs; that takes minutes per run"); bad += 1

# Behaviour, including the failure modes that must NOT stop the pipeline.
tmp = pathlib.Path(tempfile.mkdtemp())
w.QUOTA_STATE = tmp / "q.json"; w.scan.STATE = tmp; w.log = lambda m: None
if w.quota_paused():
    print("  FAIL  paused with no state file"); bad += 1
w.note_quota_exhausted(10)
if not w.quota_paused():
    print("  FAIL  not paused after a quota refusal"); bad += 1
w.note_quota_exhausted(-1)
if w.quota_paused():
    print("  FAIL  still paused after the window expired"); bad += 1
w.QUOTA_STATE.write_text("not json")
if w.quota_paused():
    print("  FAIL  a corrupt state file halts the pipeline — it must fail open"); bad += 1
# Bruce's strategy: a refusal is "not now", not "not today". It must not spend
# the day's allowance and must not retire the candidate, and the pause has to be
# short because the subscription window rolls — capacity returns continuously.
if not hasattr(w, "refund_quota_runs"):
    print("  FAIL  a refused run still spends budget and retires its candidate"); bad += 1
w.QUOTA_STATE = tmp / "unit.json"
w.note_quota_exhausted(10)
try:
    from datetime import datetime, timezone
    until = datetime.fromisoformat(json.loads(w.QUOTA_STATE.read_text())["until"])
    mins = (until - datetime.now(timezone.utc)).total_seconds() / 60
except Exception as e:  # noqa: BLE001
    print(f"  FAIL  cannot read the pause window back: {e}"); bad += 1; mins = -1
if not (5 <= mins <= 30):
    print(f"  FAIL  a pause of 10 lasts {mins:.0f} minutes — the unit is wrong, and the window rolls"); bad += 1
wf_src = open(".github/workflows/fix-one.yml").read()
if "run-name:" not in wf_src or "inputs.repo_key" not in wf_src.split("run-name:")[1][:80]:
    print("  FAIL  run-name does not carry repo_key#issue — refunds would need a log fetch"); bad += 1
print("  ok     quota refusal is visible, refunded, and pauses briefly" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 24. unfilled template titles are refused =="
# langfuse#16160 and #16162 arrived from one account as "bug: <short
# description>", and their bodies are abuse rather than a report. The substance
# check counts characters and invective has plenty, so only the title gives it
# away. This is repo-independent — nobody's placeholder is a workable issue.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys
sys.path.insert(0, sys.argv[1])
import scan
bad = 0
CFG = scan.REPOS.get("langfuse") or next(iter(scan.REPOS.values()))
REJECT = ["bug: <short description>", "[bug] <title>", "feature: <summary>",
          "Bug: <Short Description>", "bug:"]
KEEP = ["bug: UI Trace is not visible in a one glance",
        "[bug] ClickHouse writer drops records permanently after a failover",
        "fix the <div> wrapper in the trace table",
        "bug: cannot configure <model> temperature"]
for t in REJECT:
    ok, why, _ = scan.vet(CFG, "x/y", {"number": 1, "title": t, "body": "x" * 400, "labels": []})
    if ok or "placeholder" not in why:
        print(f"  FAIL  accepted a placeholder title: {t!r} ({why})"); bad += 1
for t in KEEP:
    _, why, _ = scan.vet(CFG, "x/y", {"number": 1, "title": t, "body": "x" * 400, "labels": []})
    if "placeholder" in why:
        print(f"  FAIL  a real title was read as a placeholder: {t!r}"); bad += 1
print("  ok     placeholder titles refused, real ones kept" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo "== 25. the announcement's promise is actually kept =="
# The note we leave says "if someone is already on it, say so and I will drop
# mine". On adk#6730 the issue's own author said so six minutes after we
# announced, and we opened the PR two minutes after that — nothing was watching.
# Two places have to honour it: before publishing, and after.
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import importlib.util, sys, yaml
sys.path.insert(0, sys.argv[1])
import scan
spec = importlib.util.spec_from_file_location("wp", sys.argv[1] + "/watch-prs.py")
wp = importlib.util.module_from_spec(spec); spec.loader.exec_module(wp)
bad = 0

wf = yaml.safe_load(open(".github/workflows/fix-one.yml"))
steps = [st for j in wf["jobs"].values() for st in j.get("steps", [])]
ann = next((s for s in steps if s.get("id") == "announce"), None)
opener = next((s for s in steps if s.get("id") == "openpr"), None)
if ann and "at=" not in (ann.get("run") or ""):
    print("  FAIL  the announcement records no timestamp, so replies cannot be dated"); bad += 1
if opener is None or "ANNOUNCED_AT" not in (opener.get("env") or {}):
    print("  FAIL  Open PR cannot see when we announced"); bad += 1
elif "CLAIM_PHRASES" not in (opener.get("run") or ""):
    print("  FAIL  Open PR does not read replies to the announcement before publishing"); bad += 1

for name in ("someone_claimed_the_issue", "stand_down"):
    if not hasattr(wp, name):
        print(f"  FAIL  watch-prs.py has no {name} — a claim after publishing is ignored"); bad += 1
src = open(sys.argv[1] + "/watch-prs.py").read()
if "body createdAt" not in src:
    print("  FAIL  the PR query omits body, so the linked issue cannot be found"); bad += 1
if "stand_down(pr" not in src.split("def one_pass")[1]:
    print("  FAIL  stand_down is never called from the main loop"); bad += 1

# Behaviour: a claim before we opened must NOT trigger a stand-down.
#
# Synthetic, deliberately. This used to call the live adk#6730 and assert that
# YASHcode-IIITV's claim predated a hardcoded timestamp. On 2026-08-18 they
# commented again — "I will be posting the pr shortly" — and the check began
# failing on correct code, because it was asserting the state of the world
# rather than the behaviour of the function. A test whose truth depends on what
# a stranger does next is not a test.
import json as _json
_real_gh = scan.gh
def _fake_gh(args, **kw):
    if "comments" in " ".join(args):
        return _json.dumps([
            {"u": "someone-else", "at": "2026-08-15T11:22:00Z",
             "b": "i would like to work on this issue"},              # before ours
            {"u": "chelsealong", "at": "2026-08-16T09:00:00Z",
             "b": "I'll take this"},                                   # ours, ignored
        ])
    return _real_gh(args, **kw)
scan.gh = _fake_gh
try:
    pr = {"_repo": "google/adk-python", "number": 6731,
          "createdAt": "2026-08-15T23:59:00Z", "body": "Fixes #6730"}
    who, _ = wp.someone_claimed_the_issue(pr)
    if who is not None:
        print(f"  FAIL  stood down for a claim that predates our PR ({who})"); bad += 1
    # ...and the same claim, made after we opened, must trigger one.
    pr_late = dict(pr, createdAt="2026-08-15T11:00:00Z")
    who2, _ = wp.someone_claimed_the_issue(pr_late)
    if who2 != "someone-else":
        print(f"  FAIL  a claim made after we opened was missed (got {who2})"); bad += 1
finally:
    scan.gh = _real_gh
if wp.someone_claimed_the_issue({"_repo": "x/y", "number": 1, "createdAt": "2020-01-01T00:00:00Z",
                                 "body": "no closing reference"})[0] is not None:
    print("  FAIL  stood down on a PR with no linked issue"); bad += 1
# Independent of claim quality: a PR someone else is already engaged with is the
# maintainers' call, not ours. adk#6697 had a collaborator's endorsement on it.
if not hasattr(wp, "has_outside_engagement"):
    print("  FAIL  stand_down has no engagement guard — it can discard a live review"); bad += 1
else:
    if wp.has_outside_engagement({"comments": {"nodes": [{"author": {"login": "someone"}}]}}) != "someone":
        print("  FAIL  engagement guard misses a human comment"); bad += 1
    if wp.has_outside_engagement({"comments": {"nodes": [{"author": {"login": "a-bot[bot]"}}]}}):
        print("  FAIL  engagement guard counts bots, which would disable stand_down entirely"); bad += 1
    if "has_outside_engagement(pr)" not in open(sys.argv[1] + "/watch-prs.py").read().split("def one_pass")[1]:
        print("  FAIL  the engagement guard is never consulted in the loop"); bad += 1
print("  ok     the promise is enforced, and live reviews are not discarded" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "26. claim intent is judged by a model, and each caller sets the safe default"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, os, pathlib
sys.path.insert(0, sys.argv[1])
bad = 0
scan_src = pathlib.Path(sys.argv[1] + "/scan.py").read_text()
wp_src   = pathlib.Path(sys.argv[1] + "/watch-prs.py").read_text()
if "intent.is_claim" not in scan_src:
    print("  FAIL  scan.claimants is still deciding by phrase list"); bad += 1
if "intent.is_claim" not in wp_src:
    print("  FAIL  watch-prs stand_down is still deciding by phrase list"); bad += 1
# The two call sites face opposite costs and must not share a default.
# scan: an unreachable judge should skip the issue (lose a candidate).
# watch-prs: a "claimed" verdict CLOSES our own PR, so an outage must be silent.
if "default=True" not in scan_src:
    print("  FAIL  scan.claimants must fail closed (default=True)"); bad += 1
if "default=False" not in wp_src:
    print("  FAIL  watch-prs must fail open (default=False) or an outage closes every PR"); bad += 1
import intent
intent._load_key = lambda: ""          # simulate the judge being unreachable
a, _ = intent.is_claim("I will fix this myself", default=True)
b, _ = intent.is_claim("I will fix this myself", default=False)
if (a, b) != (True, False):
    print(f"  FAIL  default is not honoured on failure: got {a} and {b}"); bad += 1
print("  ok     model decides; failures fall the safe way at each call site" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "27. feedback triage skips no-ops without silencing real review requests"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, pathlib, importlib.util
sys.path.insert(0, sys.argv[1])
bad = 0
src = pathlib.Path(sys.argv[1] + "/watch-prs.py").read_text()
if "feedback_needs" not in src:
    print("  FAIL  actionable() still lets every non-empty comment through"); bad += 1
if 'default="CODE"' not in src:
    print("  FAIL  triage must default to CODE — an item ruled unactionable is "
          "marked seen and never reconsidered, so a timeout would retire it"); bad += 1
import intent
intent._load_key = lambda: ""                     # judge unreachable
needs, _ = intent.feedback_needs("Please restore the original value in finally.",
                                 default="CODE")
if needs != "CODE":
    print(f"  FAIL  unreachable judge silenced a review request (got {needs})"); bad += 1
# The window cost three real findings before it was removed: coderabbitai and
# cubic append "Addressed in commit <sha>" AFTER the finding, and a 16k-char
# ClawSweeper review carried its objections in the middle. Anything shorter
# than WHOLE_UNDER must reach the judge intact.
long_body = "x" * 15000
if intent._window(long_body) != long_body:
    print("  FAIL  a 15k-char review is being elided before the judge sees it"); bad += 1
# <details> arrives unterminated from coderabbitai; the log inside it must go.
t = intent._strip_markup("Real finding here.\n<details>\n<summary>Analysis</summary>\n" + "log\n" * 400)
if "log" in t or "Real finding" not in t:
    print("  FAIL  unterminated <details> log is not stripped"); bad += 1
print("  ok     no-ops skipped, real requests survive an outage" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "28. shadow triage records without deciding, and cannot break a dispatch"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, pathlib
sys.path.insert(0, sys.argv[1])
bad = 0
w = pathlib.Path(sys.argv[1] + "/watch.py").read_text()
if "ledger.record" not in w:
    print("  FAIL  dispatches are not being recorded — the ledger never fills"); bad += 1
# record() sits in the dispatch hot path. It must not reach the network, and a
# failure in it must not fail the dispatch it is only observing.
import inspect, ledger
src = inspect.getsource(ledger.record)
for token in ("gh(", "urlopen", "subprocess", "_ask"):
    if token in src:
        print(f"  FAIL  ledger.record touches {token} — the hot path must stay offline"); bad += 1
if "try:" not in w.split("ledger.record")[0][-400:]:
    print("  FAIL  ledger.record is not guarded — it can fail a real dispatch"); bad += 1
# Shadow means shadow: nothing may consult a score to decide anything.
for f in ("watch.py", "scan.py", "watch-prs.py"):
    t = pathlib.Path(sys.argv[1] + "/" + f).read_text()
    if "score_pending" in t and "if " in t.split("score_pending")[1][:80]:
        print(f"  FAIL  {f} appears to branch on a shadow score"); bad += 1
# An unsettled dispatch must never be scored as a negative: fix-one queues
# behind a concurrency group, and calling a queued run a failure would bias
# the comparison in favour of the judge.
if ledger.SETTLE_HOURS < 6:
    print(f"  FAIL  SETTLE_HOURS={ledger.SETTLE_HOURS} labels queued runs as failures"); bad += 1
print("  ok     recorded offline, decides nothing, settles late" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "29. the responder sees every open PR, not the newest page of them"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, pathlib, importlib.util
sys.path.insert(0, sys.argv[1])
bad = 0
src = pathlib.Path(sys.argv[1] + "/watch-prs.py").read_text()
spec = importlib.util.spec_from_file_location("wp", sys.argv[1] + "/watch-prs.py")
wp = importlib.util.module_from_spec(spec); spec.loader.exec_module(wp)
# We held 99 open PRs while open_prs() asked for 50 and merely logged a warning
# when it got 50 back. The half it never saw was the OLD half — which is also
# the half a merge window exists to age out, so the window looked like it was
# working while doing nothing at all. A warning is not a fix.
if "pageInfo" not in src or "hasNextPage" not in src:
    print("  FAIL  open_prs does not page — PRs past the first page are unwatched"); bad += 1
if "raise `first:`" in src:
    print("  FAIL  still only warning about truncation instead of paging past it"); bad += 1
if getattr(wp, "MAX_PR_PAGES", 0) < 4:
    print("  FAIL  page cap too low to cover the PRs we actually hold"); bad += 1
# A failure partway through paging must return the pages that did arrive.
# Dropping them would idle the responder completely on a transient error.
if "_tag(nodes)" not in src:
    print("  FAIL  a mid-page failure discards the PRs already fetched"); bad += 1
print("  ok     paged, bounded, and partial results survive an error" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "30. an issue is dispatched once, and a late hands-off label still stops it"
python3 - "$SCANNER" "$(cd "$(dirname "$0")" && pwd)" <<'PY3' || fail=$((fail+1))
import sys, pathlib, re
from datetime import datetime, timezone, timedelta
sys.path.insert(0, sys.argv[1])
bad = 0
w = pathlib.Path(sys.argv[1] + "/watch.py").read_text()
# Dedup has to live where a dispatch actually starts. It used to live at each
# caller, and the live-sweep caller simply did not do it: the sweep fired
# trigger_fix(), scan.py rebuilt the queue with that issue still in it,
# drain_queues asked already_dispatched() and got False, and the issue went out
# again hours later. langfuse#16160/#16162 and spec-kit#4128/#4131/#4132 each
# burned two sessions that way.
body = w.split("def dispatch_fix")[1].split("def trigger_fix")[0]
if "record_dispatch(" not in body:
    print("  FAIL  dispatch_fix does not record — the queue can hand the issue out twice"); bad += 1
fb = w.split("fell back to local run-fix.sh")[1][:400]
if "record_dispatch(" not in fb:
    print("  FAIL  the local fallback dispatches without recording"); bad += 1

# The queue carries the verdict from detection time. A repo that says "hands
# off" after we queued must still be obeyed: openclaw #124306 was labelled
# no-new-fix-pr at 23:57 and dispatched at 00:09, because the re-vet fetched
# state, title and assignees and never asked about labels.
f = pathlib.Path(sys.argv[2] + "/.github/workflows/fix-one.yml").read_text()
revet = f.split("Re-vet before spending a session")[1].split("- name:")[0]
if "labels" not in revet or "exclude_labels" not in revet:
    print("  FAIL  the re-vet does not re-read labels — a late hands-off label is ignored"); bad += 1

import scan
oc = scan.REPOS["openclaw"]["exclude_labels"]
for need in ("clawsweeper:no-new-fix-pr", "clawsweeper:not-repro-on-main"):
    if need not in oc:
        print(f"  FAIL  openclaw does not exclude {need}"); bad += 1
# Arriving before a self-triaging repo has decided is worse than arriving late.
now = datetime.now(timezone.utc)
def probe(cfg, minutes):
    iss = {"number": 1, "title": "fix: a concrete crash in the gateway path",
           "labels": [], "assignees": [], "comments": 0, "body": "x" * 400,
           "created_at": (now - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")}
    return scan.vet(cfg, "openclaw/openclaw", iss)
ok, why, _ = probe(scan.REPOS["openclaw"], 5)
if ok or "triage" not in why:
    print(f"  FAIL  openclaw takes issues younger than its triage latency ({why[:50]})"); bad += 1
# A repo without the setting must not be delayed at all.
ok2, why2, _ = probe(scan.REPOS["hermes"], 1)
if "triage" in why2:
    print("  FAIL  min_age_minutes is leaking into repos that did not set it"); bad += 1
print("  ok     dispatched once, late labels honoured, triage not raced" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "31. the judge falls down a live model chain and retires what fails"
python3 - "$SCANNER" "$(cd "$(dirname "$0")" && pwd)" <<'PY3' || fail=$((fail+1))
import io, json, pathlib, sys, tempfile, urllib.error
sys.path.insert(0, sys.argv[1])
import intent
bad = 0
if len(intent.MODELS) < 4:
    print("  FAIL  no fallback chain — one outage silences every judgement"); bad += 1
if intent.MODELS[0] != "qwen3.7-max-preview":
    print(f"  FAIL  chain does not start at qwen3.7-max-preview (got {intent.MODELS[0]})"); bad += 1
# qwen3.7-max and qwen3.7-max-2026-05-20 exhausted their free tier on 2026-08-21.
# Free quota does not come back, so they are removed, not demoted.
for gone in ("qwen3.7-max", "qwen3.7-max-2026-05-20"):
    if gone in intent.MODELS:
        print(f"  FAIL  {gone} is exhausted and still on the chain"); bad += 1
# qwen3.7-plus answered a real comment in 32.9s. At the old 25s timeout it was
# on the chain and unreachable, and the whole chain reported ALLFAILED.
# Under the two-strike rule a timeout retires a model for good, so a tight
# ceiling does not cost one slow answer — it costs the model. qwen3.7-plus
# answered a real comment in 32.9s; kimi took 21.4s on a one-liner.
if intent.TIMEOUT < 90:
    print(f"  FAIL  TIMEOUT={intent.TIMEOUT}s — too tight now that a timeout retires the model"); bad += 1

real = intent.urllib.request.urlopen
intent.RETIRED = pathlib.Path(tempfile.mkdtemp()) / "r.json"
seen = []
def fake(req, timeout=None):
    m = json.loads(req.data)["model"]
    seen.append(m)
    if m == intent.MODELS[0]:
        raise urllib.error.HTTPError("u", 403, "F", {}, io.BytesIO(
            b'{"error":{"message":"Free quota exhausted.","type":"AllocationQuota"}}'))
    if m == intent.MODELS[1]:
        raise TimeoutError("slow")
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"claim":true,"why":"x"}'}}]}).encode()
    return R()
try:
    intent.urllib.request.urlopen = fake
    intent._down.clear()
    if intent._ask("s", "u") is None:
        print("  FAIL  the chain gives up instead of reaching a working model"); bad += 1
    # A 403 for exhausted quota is terminal: no second opinion is needed and a
    # second attempt only pays the cost twice.
    if intent.MODELS[0] not in intent.dead_models():
        print("  FAIL  an exhausted-quota 403 does not retire the model at once"); bad += 1
    # Anything else gets two strikes, then goes.
    intent._down.clear(); intent._ask("s", "u")
    if intent.MODELS[1] not in intent.dead_models():
        print(f"  FAIL  {intent.MODELS[1]} still live after {intent.STRIKES_TO_RETIRE} failures"); bad += 1
    # A call in which EVERY model failed is evidence about the network, not
    # about nine separate models. One comment produced a failure on all eight
    # live models in a single call and retired a model that had answered a
    # probe in 6.2s an hour earlier. Strikes from such a call are handed back.
    intent.RETIRED = pathlib.Path(tempfile.mkdtemp()) / "r2.json"
    intent._down.clear()
    intent.urllib.request.urlopen = lambda req, timeout=None: (
        (_ for _ in ()).throw(urllib.error.URLError("network down")))
    intent._ask("s", "u")
    left = {m: v.get("strikes", 0) for m, v in intent._retired().items() if v.get("strikes")}
    if left:
        print(f"  FAIL  a total outage charged strikes to {left}"); bad += 1
    if intent.dead_models():
        print(f"  FAIL  a total outage retired {sorted(intent.dead_models())}"); bad += 1
    if intent.STRIKE_TTL_HOURS < 1:
        print("  FAIL  strikes never expire, so transient failures accumulate forever"); bad += 1
    # Retired means no request at all, not a cheap failure.
    seen.clear(); intent._down.clear(); intent._ask("s", "u")
    if any(m in seen for m in intent.dead_models()):
        print(f"  FAIL  a retired model is still being called: {seen}"); bad += 1
    if intent.live_models() and intent.live_models()[0] in intent.dead_models():
        print("  FAIL  live_models() returns a retired model"); bad += 1
finally:
    intent.urllib.request.urlopen = real
    intent._down.clear()
# The key has to exist where the code runs. It was absent from Actions entirely
# until 2026-08-17, so every cloud re-vet judged commented issues "claimed".
f = pathlib.Path(sys.argv[2] + "/.github/workflows/fix-one.yml").read_text()
revet = f.split("Re-vet before spending a session")[1].split("- name:")[0]
if "QWEN_API_KEY" not in revet:
    print("  FAIL  the re-vet has no QWEN_API_KEY — claimants() fails closed and skips"); bad += 1
print("  ok     chain live-filtered, terminal failures retired at once" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "32. an abandoned PR does not retire the issue it failed to fix"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, pathlib
sys.path.insert(0, sys.argv[1])
bad = 0
src = pathlib.Path(sys.argv[1] + "/scan.py").read_text()
fn = src.split("def linked_prs")[1].split("\ndef ")[0]
# A closed, unmerged PR is someone who tried and gave up; the issue is open
# precisely because nobody finished. AutoGPT#11044 had two of them (#12639,
# #12824) and we skipped it for three weeks. Only OPEN and MERGED disqualify.
if 'state") == "CLOSED"' not in fn and "!= \"CLOSED\"" not in fn:
    print("  FAIL  linked_prs still counts closed-unmerged PRs as disqualifying"); bad += 1
# The search fallback is full text, so the issue number matches line numbers,
# hashes and log output. firecrawl#4316 (2026-08-16) was disqualified by a PR
# from June and one from a year earlier, neither of which mentions it.
if "merged_at" not in fn:
    print("  FAIL  the search path cannot tell a merged PR from an abandoned one"); bad += 1
if "issue_created" not in fn:
    print("  FAIL  the search path has no date guard — older PRs match by coincidence"); bad += 1
import scan
CASES = [
    ("Significant-Gravitas/AutoGPT", 11044, False, "two abandoned PRs"),
    ("google-gemini/gemini-cli", 28445, False, "one abandoned PR"),
    ("google/adk-python", 6530, True, "our own OPEN PR#6531"),
]
for up, num, want_blocked, note in CASES:
    hits = scan.linked_prs(up, num)
    blocked = bool([h for h in hits if not h.startswith("?")])
    if blocked != want_blocked:
        print(f"  FAIL  {up}#{num} ({note}): blocked={blocked}, want {want_blocked} :: {hits[:2]}")
        bad += 1
print("  ok     abandoned PRs reopened, live and merged ones still block" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "33. every configured repo has a fork, and every repo gets the common lessons"
python3 - "$SCANNER" "$(cd "$(dirname "$0")" && pwd)" <<'PY3' || fail=$((fail+1))
import sys, json, pathlib, subprocess
sys.path.insert(0, sys.argv[1])
import scan
bad = 0
root = pathlib.Path(sys.argv[2])
# Adding a repo without forking it first cost litellm, llama-index, mem0 and
# crawl4ai two days: every dispatch passed vetting, spent a budget unit, booted
# a runner and died at `Checkout target fork`. The checklist did not mention it
# and nothing checked, so the repos just looked unproductive.
missing = []
for key, cfg in scan.REPOS.items():
    name = cfg["upstream"].split("/")[1]
    r = subprocess.run(["gh", "api", f"repos/chelsealong/{name}", "--jq", ".parent.full_name"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or r.stdout.strip() != cfg["upstream"]:
        missing.append(f"{key} -> chelsealong/{name}")
if missing:
    print(f"  FAIL  no usable fork for: {', '.join(missing)}"); bad += 1
# Lessons paid for on one repo must reach the others. Only hermes and openclaw
# had files, so fourteen agents ran with "(no prior lessons recorded)".
common = root / "lessons" / "_common.md"
if not common.is_file() or common.stat().st_size < 500:
    print("  FAIL  lessons/_common.md missing or empty — nothing transfers between repos"); bad += 1
wf = (root / ".github/workflows/fix-one.yml").read_text()
step = wf.split("Load lessons for this repo")[1].split("- name:")[0]
if "_common.md" not in step:
    print("  FAIL  fix-one loads only the per-repo file; common lessons never reach the agent"); bad += 1
if "cat \"$f\" >>" not in step:
    print("  FAIL  the per-repo file overwrites rather than appends to the common one"); bad += 1
print("  ok     forks exist and every repo inherits the common lessons" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "34. a stand-down can be taken back, and a claim about another issue is not one"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, importlib.util, json, pathlib, tempfile
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("wp", sys.argv[1] + "/watch-prs.py")
wp = importlib.util.module_from_spec(spec); spec.loader.exec_module(wp)
bad = 0
# adk#6673: arunpshankar wrote on issue #6672 that they would send a PR. They
# meant #6778, a separate issue. We closed a change a collaborator had said was
# moving toward merge, and were asked to reopen it.
for text, issue, want, note in [
    ("I'll send a PR for #6778", 6672, True, "names only another issue"),
    ("I'd like to work on this", 6672, False, "names nothing"),
    ("I'll take #6672, see also #6778", 6672, False, "names this one too"),
]:
    if wp._claim_is_about_another(text, issue) != want:
        print(f"  FAIL  {note}: {text!r} vs #{issue}"); bad += 1
# The request to reopen arrived on a PR that open_prs() can no longer see,
# because it searches is:open. Without a ledger nothing ever reads it again.
if not hasattr(wp, "check_stand_downs") or not hasattr(wp, "_record_stand_down"):
    print("  FAIL  a closed PR is never revisited — a correction cannot reach us"); bad += 1
src = pathlib.Path(sys.argv[1] + "/watch-prs.py").read_text()
if "_record_stand_down(" not in src.split("def stand_down")[1].split("\ndef ")[0]:
    print("  FAIL  stand_down closes without recording, so the ledger stays empty"); bad += 1
if "check_stand_downs()" not in src.split("def one_pass")[1]:
    print("  FAIL  the recheck is never called from the loop"); bad += 1
# Engagement about our PR can live on the issue rather than the PR: the sentence
# that should have stopped us was surajksharma07's, on #6672.
eng = src.split("def has_outside_engagement")[1].split("\ndef ")[0]
if "issues/" not in eng or "linked_issue" not in eng:
    print("  FAIL  the engagement guard still only reads the PR, not the linked issue"); bad += 1
if "unknown (issue comments unreadable)" not in eng:
    print("  FAIL  an API failure reads as 'nobody engaged', which closes live PRs"); bad += 1
print("  ok     corrections can reach us; a claim elsewhere is not a claim here" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "35. the landing count is stored, verified, and never rebuilt from scratch"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, json, pathlib
sys.path.insert(0, sys.argv[1])
import landings
bad = 0
d = landings._load()
if len(d.get("commits") or {}) < 25:
    print(f"  FAIL  ledger holds only {len(d.get('commits') or {})} commits — it should be seeded"); bad += 1
# Every recount before the ledger went wrong in the same three ways. Each is
# guarded here so a future edit cannot quietly reintroduce one.
src = pathlib.Path(sys.argv[1] + "/landings.py").read_text()
if 'c["repo"] != repo' not in src:
    print("  FAIL  fork matches are not filtered — search/commits returns other people's forks"); bad += 1
if "--paginate" not in src:
    print("  FAIL  the commit search is unpaginated and will silently truncate"); bad += 1
if '"behind"' not in src:
    print("  FAIL  commits are not verified as ancestors of the default branch"); bad += 1
if "rate limit" not in src.lower():
    print("  FAIL  no rate-limit retry — the search bucket is 30/min and this needs 30"); bad += 1
# A failed lookup must be visible, not zero. hermes reported 0 PRs and 0
# commits while holding 56 and 11, because both failures returned empty.
if '{"total": None}' not in src:
    print("  FAIL  a failed PR count returns 0 rather than unknown"); bad += 1
# Incremental, not a rebuild: `since` comes from what is already stored.
if "committer-date:>" not in src or "since" not in src:
    print("  FAIL  update() does not narrow by date, so every run is a full recount"); bad += 1
# Landed is a commit on main, which is not the same as a merged PR.
h = [v for v in d["commits"].values() if "hermes" in v["repo"]]
if len(h) < 5:
    print(f"  FAIL  hermes shows {len(h)} landings; its PRs close unmerged and are salvaged"); bad += 1
if "update_landings" not in pathlib.Path(sys.argv[1] + "/health.py").read_text():
    print("  FAIL  nothing keeps the ledger current on a schedule"); bad += 1
print("  ok     stored, fork-filtered, ancestry-verified, incremental" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)

# Credited-but-not-authored is real evidence and is kept SEPARATE on purpose.
# hermes#89815 merged saying "Credit: @rikkarth, @chelsealong, ..." with every
# commit under the maintainer's name; our work went in, our authorship did not.
# Folding it into the commit total would make every figure we quote arguable,
# and the figures exist to survive being checked.
if "credited" not in src:
    print("  FAIL  credited-but-not-authored contributions are not recorded at all"); bad += 1
if 'if any(e in emails for e in ME_EMAILS)' not in src:
    print("  FAIL  nothing excludes salvages that DID preserve authorship — double counting"); bad += 1
cred = d.get("credited") or {}
for key, c in cred.items():
    if key in d["commits"]:
        print(f"  FAIL  {key} counted as both a commit and a credit"); bad += 1
    if not c.get("evidence"):
        print(f"  FAIL  {key} recorded with no quotable evidence line"); bad += 1

# `compare/HEAD...<sha>` answers "identical" when the commit IS the current tip,
# and only "behind" once something lands on top of it. Accepting behind alone
# meant the NEWEST landing was always invisible — adk e4ba7040 was the head of
# main forty-five minutes after import and read as not-landed. The bias is
# permanent and always in the same direction: it undercounts by exactly the
# result we look at most often.
if '("behind", "identical")' not in src:
    print("  FAIL  on_default_branch rejects 'identical' — the newest landing is invisible"); bad += 1
import landings as _L
_real = _L._gh
try:
    _L._gh = lambda *a, **k: "identical\n"
    if not _L.on_default_branch("x/y", "deadbeef"):
        print("  FAIL  a commit that IS the tip is not counted as landed"); bad += 1
    _L._gh = lambda *a, **k: "behind\n"
    if not _L.on_default_branch("x/y", "deadbeef"):
        print("  FAIL  an ancestor of the tip is not counted as landed"); bad += 1
    # The distinction that matters must survive: a commit on our own fork branch
    # answers "diverged", and counting those would inflate every figure.
    _L._gh = lambda *a, **k: "diverged\n"
    if _L.on_default_branch("x/y", "deadbeef"):
        print("  FAIL  a fork-only commit is being counted as landed"); bad += 1
    _L._gh = lambda *a, **k: "ahead\n"
    if _L.on_default_branch("x/y", "deadbeef"):
        print("  FAIL  an unmerged descendant is being counted as landed"); bad += 1
finally:
    _L._gh = _real

# Automated reviewers must never enter the quote record. `clawsweeper` — the
# openclaw review bot — supplied 12 of the first 20 entries, because its login
# has no `[bot]` suffix and its prose is indistinguishable from a careful human
# review. This record exists to be quoted in a filing; a bot in it would forfeit
# the credibility of everything beside it.
if "NOT_HUMAN" not in src or "clawsweeper" not in src:
    print("  FAIL  no named list of non-[bot] automation accounts"); bad += 1
import landings as _LL
for who in ("clawsweeper", "coderabbitai", "adk-bot", "some-name[bot]"):
    if _LL._is_human(who):
        print(f"  FAIL  {who} counts as a human evaluator"); bad += 1
for who in ("teknium1", "sallyom", "mnriem"):
    if not _LL._is_human(who):
        print(f"  FAIL  {who} is a real maintainer and is being filtered out"); bad += 1
for key, q in (d.get("quotes") or {}).items():
    if not _LL._is_human(q.get("who", "")):
        print(f"  FAIL  stored quote from non-human account: {key}"); bad += 1
    if not q.get("quote"):
        print(f"  FAIL  {key} recorded with no verbatim quote"); bad += 1
PY3

say "36. a dispatch is remembered, but not forever"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, json, pathlib, importlib.util, tempfile
from datetime import datetime, timezone, timedelta
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("w", sys.argv[1] + "/watch.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
bad = 0
# Dedup exists because spec-kit#3997 went out three times. But a permanent
# tombstone burns candidates whose dispatch failed for a reason that has since
# gone away: on 2026-08-19 mem0, crawl4ai, llama-index and langfuse held 13
# queued candidates between them and not one had never been dispatched.
# mem0#6995 had been skipped for an abandoned PR — a bug fixed the day before.
tmp = pathlib.Path(tempfile.mkdtemp()) / "d.json"
w.DISPATCHED = tmp
now = datetime.now(timezone.utc)
tmp.write_text(json.dumps({"r": {
    "1": {"at": now.isoformat(), "attempts": 1},
    "2": {"at": (now - timedelta(days=w.RETRY_AFTER_DAYS + 1)).isoformat(), "attempts": 1},
    "3": {"at": (now - timedelta(days=99)).isoformat(), "attempts": w.MAX_ATTEMPTS},
}}))
for num, want, note in [(1, True, "just dispatched"),
                        (2, False, "past the cooldown with attempts left"),
                        (3, True, "attempts exhausted")]:
    if w.already_dispatched("r", num) != want:
        print(f"  FAIL  #{num} ({note}): got {not want}"); bad += 1
if w.MAX_ATTEMPTS > 2:
    print(f"  FAIL  MAX_ATTEMPTS={w.MAX_ATTEMPTS} — retrying more than once burns sessions"); bad += 1
if w.RETRY_AFTER_DAYS < 1:
    print("  FAIL  the cooldown is short enough to re-dispatch inside one day"); bad += 1
# The old file was a bare list of numbers. Reading one must not crash or, worse,
# silently treat every past dispatch as fresh.
tmp.write_text(json.dumps({"r": [7, 8]}))
if not w.already_dispatched("r", 7) or w.already_dispatched("r", 9):
    print("  FAIL  the pre-migration list format is not honoured"); bad += 1
# Recording must produce the new shape, and count attempts.
tmp.write_text("{}")
w.record_dispatch("r", 5); w.record_dispatch("r", 5)
rec = json.loads(tmp.read_text())["r"]["5"]
if rec.get("attempts") != 2 or "at" not in rec:
    print(f"  FAIL  record_dispatch does not track attempts ({rec})"); bad += 1
print("  ok     one retry after a cooldown, then the issue is done" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "37. every streamed line says which phase produced it"
python3 - "$(cd "$(dirname "$0")" && pwd)" <<'PY3' || fail=$((fail+1))
import json, os, pathlib, subprocess, sys, tempfile
bad = 0
wf = pathlib.Path(sys.argv[1] + "/.github/workflows/fix-one.yml").read_text()
# GitHub attributes streamed output to "UNKNOWN STEP", so filtering a run log by
# step name returns nothing. A check for "did this session run its tests?" came
# back as zero test commands on a run that had in fact run pytest six times,
# including the git-stash proof — the wrong answer stood for two hours because
# the question could not be asked. The marker has to be in the text.
for phase in ("gen", "review", "remediate", "rereview"):
    if f"CLAUDE_PHASE: {phase}" not in wf:
        print(f"  FAIL  no CLAUDE_PHASE={phase} on its step"); bad += 1
body = wf.split('cat > "$RUNNER_TEMP/fmt.py" <<\'FMT\'\n')[1].split("\n          FMT")[0]
code = "\n".join(l[10:] if l.startswith(" " * 10) else l for l in body.split("\n"))
f = pathlib.Path(tempfile.mkdtemp()) / "fmt.py"
f.write_text(code)
events = [
    {"message": {"content": [{"type": "tool_use", "name": "Bash",
                              "input": {"command": "uv run pytest -q"}}]}},
    {"message": {"content": [{"type": "text", "text": "Running the tests."}]}},
    {"type": "result", "subtype": "success", "num_turns": 3},
]
inp = "\n".join(json.dumps(e) for e in events) + "\nYou've hit your session limit\n"
r = subprocess.run(["python3", str(f)], input=inp, capture_output=True, text=True,
                   env={**os.environ, "CLAUDE_PHASE": "gen"})
lines = [l for l in r.stdout.splitlines() if l.strip()]
tagged = [l for l in lines if l.startswith("gen ")]
if len(tagged) != 3:
    print(f"  FAIL  {len(tagged)}/3 event kinds carry the phase: {lines}"); bad += 1
# The quota notice is plain text and the guard greps for it — it must pass
# through untouched rather than being prefixed or swallowed.
if not any("hit your session limit" in l for l in lines):
    print("  FAIL  the session-limit notice does not survive the formatter"); bad += 1
print("  ok     phase on every event, raw text still passes through" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "38. every check in this file can actually fail the run"
python3 - "$(cd "$(dirname "$0")" && pwd)" <<'PY3' || fail=$((fail+1))
import pathlib, re, sys
bad = 0
lines = (pathlib.Path(sys.argv[1]) / "verify.sh").read_text().split("\n")
# Checks 26 through 37 — twelve of them, every one I added — ran their
# assertions, printed FAIL, and were then ignored, because the heredoc line
# lacked `|| fail=$((fail+1))`. The script still said "PASS — safe to commit".
# A harness that cannot fail is worse than no harness: it is a claim of
# verification with nothing behind it, and it was reported as green repeatedly.
cur, missing, seen = None, [], 0
for i, l in enumerate(lines):
    m = re.match(r'^(?:say|echo) "(?:== )?(\d+)\.', l)
    if m:
        cur = m.group(1)
    if re.match(r"^python3 - .*<<", l) and cur:
        seen += 1
        if "fail=" not in l:
            missing.append(f"{cur} (line {i + 1})")
        cur = None
if missing:
    print(f"  FAIL  these checks discard their exit status: {', '.join(missing)}"); bad += 1
if seen < 30:
    print(f"  FAIL  only found {seen} python checks — the scan is not seeing the file"); bad += 1
# And the summary must key off that counter, not off the last command.
body = "\n".join(lines)
if 'if [ "$fail" -eq 0 ]' not in body:
    print("  FAIL  the PASS/FAIL summary does not read the failure counter"); bad += 1
print(f"  ok     all {seen} checks report into the counter" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "42. a claim reserves an issue, but not forever"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import pathlib, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, sys.argv[1])
import scan
bad = 0
# Standing down when someone claims an issue is right. Standing down forever is
# not. adk#6877 was filed 11:05 by its reporter, who commented that he already
# had a PR ready; we reached it at 11:09 and withdrew. Forty minutes later no PR
# existed from anyone. On #6878 — same author, same subject — we arrived eleven
# seconds after it opened, he claimed it a minute later, we withdrew again, and
# again no PR appeared. An issue reserved by a claim that produces nothing
# belongs to nobody.
if not hasattr(scan, "_claim_went_cold"):
    print("  FAIL  a claim never expires, so a dropped claim parks the issue for good"); bad += 1
    sys.exit(1)
if scan.CLAIM_COLD_HOURS < 24:
    print(f"  FAIL  CLAIM_COLD_HOURS={scan.CLAIM_COLD_HOURS} is short enough to race a "
          "contributor who is genuinely mid-session"); bad += 1
now = datetime.now(timezone.utc)
iso = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")
# A fresh claim always stands, and must cost no API call to decide that.
real_linked = scan.linked_prs
try:
    scan.linked_prs = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("linked_prs called for a fresh claim"))
    if scan._claim_went_cold("o/r", 1, "someone", iso(2)):
        print("  FAIL  a two-hour-old claim was treated as abandoned"); bad += 1
    # No timestamp is not evidence of age.
    if scan._claim_went_cold("o/r", 1, "someone", ""):
        print("  FAIL  a claim with no timestamp was expired"); bad += 1
    # Old claim WITH a live PR still stands — the claim was honoured.
    scan.linked_prs = lambda *a, **k: ["closing-ref PR#9(OPEN)"]
    if scan._claim_went_cold("o/r", 1, "someone", iso(200)):
        print("  FAIL  a claim that produced a PR was expired anyway"); bad += 1
    # Old claim with nothing to show for it expires.
    scan.linked_prs = lambda *a, **k: []
    if not scan._claim_went_cold("o/r", 1, "someone", iso(200)):
        print("  FAIL  a 200-hour-old claim with no PR still reserves the issue"); bad += 1
    # An unreadable answer must leave the claim standing, not clear it.
    scan.linked_prs = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down"))
    if scan._claim_went_cold("o/r", 1, "someone", iso(200)):
        print("  FAIL  an API failure expires the claim — that direction takes someone's work"); bad += 1
finally:
    scan.linked_prs = real_linked
src = pathlib.Path(sys.argv[1] + "/scan.py").read_text()
if "_claim_went_cold(" not in src.split("def claimants")[1].split("\ndef ")[0]:
    print("  FAIL  claimants() does not consult the expiry"); bad += 1
if "at:.created_at" not in src:
    print("  FAIL  comment timestamps are not fetched, so age cannot be known"); bad += 1
print("  ok     fresh claims hold, dropped ones expire, failures hold" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "39. a step that runs in work/ can still import the pipeline's own modules"
python3 - "$(cd "$(dirname "$0")" && pwd)" <<'PY3' || fail=$((fail+1))
import pathlib, sys, yaml
bad = 0
root = pathlib.Path(sys.argv[1])
wf = yaml.safe_load((root / ".github/workflows/fix-one.yml").read_text())
# The Open PR step runs in work/, the fork checkout. `sys.path.insert(0, '.')`
# there points at the target repo, so `import scan` raised ModuleNotFoundError,
# and because the shell is `bash -e` it took the whole step down: five adk PRs
# were generated, reviewed, approved and then never opened. A guard written to
# keep a promise discarded the work instead — and it failed loudly in the log
# while looking, from outside, like a run that simply produced nothing.
for job in wf["jobs"].values():
    for st in job.get("steps", []):
        run = str(st.get("run") or "")
        if "sys.path.insert" not in run:
            continue
        name = str(st.get("name", "?"))
        wd = st.get("working-directory")
        # Examine the insert LINES, not the whole step. Every one of these steps
        # mentions $GITHUB_WORKSPACE somewhere for file paths, so a substring
        # test over the step body passes no matter what the path insert says —
        # the first version of this check was written that way and did not catch
        # the bug it was written for.
        inserts = [l.strip() for l in run.split("\n") if "sys.path.insert" in l]
        if not wd:
            continue                      # runs at the repo root; '.' is correct
        if not any("GITHUB_WORKSPACE" in l for l in inserts):
            print(f"  FAIL  '{name}' runs in {wd}/ and no sys.path.insert names "
                  f"GITHUB_WORKSPACE: {inserts}")
            bad += 1
print("  ok     every module import resolves from the directory it runs in" if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "40. a paused repo is refused before anything is spent on it"
python3 - "$SCANNER" <<'PY3' || fail=$((fail+1))
import sys, pathlib
sys.path.insert(0, sys.argv[1])
import scan
bad = 0
src = pathlib.Path(sys.argv[1] + "/scan.py").read_text()
# litellm refuses PR creation from this account with "does not have the correct
# permissions to execute CreatePullRequest" — not a block (writes to our own
# PRs there still work) but a repo-level restriction. Five dispatches ran
# generation and adversarial review to completion, ~40 minutes of Claude each,
# and were refused at the final step.
fn = src.split("def vet(")[1].split("\n\ndef ")[0]
first = [l.strip() for l in fn.split("\n") if l.strip() and not l.strip().startswith("#")][:4]
if not any('cfg.get("paused")' in l for l in first):
    print(f"  FAIL  the paused check is not at the top of vet(): {first}"); bad += 1
paused = {k: v.get("paused") for k, v in scan.REPOS.items() if v.get("paused")}
for key, why in paused.items():
    cfg = scan.REPOS[key]
    ok, reason, _ = scan.vet(cfg, cfg["upstream"], {
        "number": 1, "title": "fix: a concrete defect", "labels": [],
        "body": "x" * 500, "comments": 0, "assignees": []})
    if ok or "paused" not in reason:
        print(f"  FAIL  {key} is paused but vet() accepted it ({reason[:50]})"); bad += 1
    if not why or len(str(why)) < 15:
        print(f"  FAIL  {key} is paused with no stated reason — nobody can tell when to resume"); bad += 1
# A pause must not leak to repos that did not ask for one.
live = scan.REPOS["hermes"]
_, r, _ = scan.vet(live, live["upstream"], {
    "number": 1, "title": "fix: x", "labels": [], "body": "x" * 500,
    "comments": 0, "assignees": []})
if "paused" in r:
    print("  FAIL  the pause is leaking into repos that are not paused"); bad += 1
print(f"  ok     {len(paused)} paused repo(s) refused up front, others unaffected"
      if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

say "41. nothing but supply and the quota guard limits dispatch"
python3 - "$SCANNER" "$(cd "$(dirname "$0")" && pwd)" <<'PY3' || fail=$((fail+1))
import importlib.util, pathlib, re, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("w", sys.argv[1] + "/watch.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
bad = 0
# On 2026-08-23 hermes hit its dispatch allowance at 10/10 with FOURTEEN vetted
# candidates still queued, every other repo's queue was empty, and the day
# produced no landings. hermes is where 13 of our 39 landings come from and it
# lands by salvage, which needs volume. The allowance was rationing Claude
# quota, and quota was never the binding constraint — no run hit it in the three
# days to 08-17, and a second account sits behind it.
wf = pathlib.Path(sys.argv[2] + "/.github/workflows/fix-one.yml").read_text()
caps = dict(re.findall(r"^\s+([a-z0-9|-]+)\)\s+cap=(\d+)", wf, re.M))
for repo, floor in (("hermes", 15), ("adk|dify|langfuse|openclaw|comfyui", 10)):
    got = int(caps.get(repo, 0))
    if got < floor:
        print(f"  FAIL  PR cap for {repo} is {got}, still rationing rather than guarding"); bad += 1
# spec-kit's 3 is a promise to a maintainer, not rationing. It must NOT drift up
# with the others: mnriem asked three times that we stop a class of PR there,
# and Bruce set this number when re-enabling the repo.
if caps.get("spec-kit") != "3":
    print(f"  FAIL  spec-kit's relationship cap changed to {caps.get('spec-kit')}"); bad += 1
if "relationship cap" not in wf:
    print("  FAIL  the two kinds of cap are no longer distinguished in the file"); bad += 1
# The dispatch guard has to sit above any real day's supply, or it is a policy
# again under a different name.
for k, v in w.DISPATCH_BUDGET.items():
    if v < 15:
        print(f"  FAIL  dispatch guard for {k} is {v} — low enough to bind on a normal day"); bad += 1
if w.DEFAULT_BUDGET < 15:
    print(f"  FAIL  DEFAULT_BUDGET={w.DEFAULT_BUDGET} binds before supply does"); bad += 1
# And the thing that actually stops us must still be wired.
src = pathlib.Path(sys.argv[1] + "/watch.py").read_text()
if "quota_paused()" not in src.split("def budget_allows")[1][:600]:
    print("  FAIL  budget_allows no longer consults the quota pause"); bad += 1
print("  ok     guards sit above supply, quota pause intact, promises kept"
      if not bad else f"  {bad} problem(s)")
sys.exit(1 if bad else 0)
PY3

echo
if [ "$fail" -eq 0 ]; then echo "  PASS — safe to commit"; exit 0; fi
echo "  $fail FAILURE(S) — do not commit"; exit 1
