#!/usr/bin/env python3
"""Long-lived watcher: detect brand-new upstream issues within seconds.

Replaces per-repo REST polling for the DETECTION half of the pipeline. One
batched GraphQL query covers every tracked repo and costs exactly 1 rate-limit
point regardless of how many repos are in it (verified up to 100 aliases), so a
5-second cadence uses ~720 of 5000 points/hr — 14%, leaving the rest for vetting
and PR creation.

Why not the obvious alternatives (all measured, 2026-07-29):
  * REST per repo    — 11 subprocesses per sweep, and `gh api` never sends
                       If-None-Match so it can never earn a free 304.
  * issues.atom      — returns HTTP 406 with an empty body on every repo.
  * Events API       —官方 docs say 30s-6h latency; measured 0-10% recall of
                       opened issues. Unusable for this.
  * GH Archive       — capture rate collapsed to ~0.18% during 2026.
  * Actions cron     — `schedule` is delayed and may be dropped under load; a
                       */5 schedule was observed firing twice in six hours.

Detection only. Vetting and fixing stay where they were: a detected issue is
vetted with scan.vet() and, if clean, appended to queue/<key>.json for
run-fix.sh to pop.

    ./watch.py                 # run forever (this is what launchd starts)
    ./watch.py --once          # single sweep, useful for testing
    ./watch.py --interval 5    # seconds between sweeps (default 5)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

import scan  # reuse REPOS, vet(), gh(), QUEUE, STATE

ROOT = pathlib.Path(__file__).resolve().parent
SEEN = scan.STATE / "seen.json"
# Set NO_TRIGGER=1 to detect and queue without starting fixes (for testing).
import os
import subprocess
import re
NO_TRIGGER = os.environ.get("NO_TRIGGER") == "1"
LOG = ROOT / "watch.log"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def build_query(keys: list[str], per_repo: int) -> str:
    """One query, one alias per repo. GraphQL `issues` natively excludes PRs,
    which the REST issues endpoint does not."""
    parts = []
    for i, k in enumerate(keys):
        owner, name = scan.REPOS[k]["upstream"].split("/")
        parts.append(
            f'r{i}: repository(owner:"{owner}", name:"{name}") {{'
            f"  issues(first:{per_repo}, states:OPEN,"
            f"         orderBy:{{field:CREATED_AT, direction:DESC}}) {{"
            f"    nodes {{ number title url createdAt body"
            f"             assignees(first:1){{ totalCount }}"
            f"             comments {{ totalCount }}"
            f"             labels(first:20){{ nodes {{ name }} }} }}"
            f"  }}"
            f"}}"
        )
    return "{ rateLimit { cost remaining } " + " ".join(parts) + " }"


def required_labels(cfg: dict) -> set[str]:
    """Labels an issue MUST carry for this repo, derived from its `searches`.

    scan.py expresses scope through search qualifiers, but the watcher fetches
    the newest issues wholesale, so those qualifiers would otherwise be lost.
    That is not cosmetic: langfuse-python's issues live in the langfuse/langfuse
    tracker but only the `sdk-python`-labelled ones can be fixed from its
    checkout — without this, a TypeScript UI bug was accepted into the Python
    SDK's queue (observed with #15566).

    If ANY of the repo's searches is unscoped (e.g. `sort:created-desc`), the
    repo genuinely wants everything and no label is required.
    """
    req: set[str] = set()
    for q in cfg["searches"]:
        labels = {a or b for a, b in scan._LABEL_RE.findall(q)}
        if not labels:
            return set()          # an unscoped search means "consider anything"
        req |= labels
    return req


def to_rest_shape(node: dict) -> dict:
    """scan.vet() expects the REST issue shape."""
    return {
        "number": node["number"],
        "title": node.get("title") or "",
        "body": node.get("body") or "",
        "html_url": node.get("url"),
        "created_at": node["createdAt"],
        "comments": (node.get("comments") or {}).get("totalCount", 0),
        "labels": [{"name": l["name"]} for l in (node.get("labels") or {}).get("nodes", [])],
        "assignees": [1] * (node.get("assignees") or {}).get("totalCount", 0),
    }


# Repos that auto-close a PR unless the author already holds an assignment on a
# linked issue (require_issue_link.yml on the LangChain repos;
# gemini-self-assign-issue.yml on gemini-cli). Sending these to run-fix.sh would
# spend a full Claude run only to discover it must ask for an assignment instead,
# so route them to the cheap claim path.
# Repos that close an unassigned PR automatically. Opening one there is not a
# race we lose, it is a PR closed within seconds by a bot — pydantic-ai#7282 was
# open for 18 seconds. Its pr-guard.yml bypasses MEMBER/OWNER/COLLABORATOR and
# requires everyone else to be assigned to the linked issue first, which is why
# maintainers' PRs there show no assignee and ours must have one.
GATED = {"langchain"}

# Where fix-one.yml lives.
PIPELINE_REPO = "chelsealong/oss-pipeline"

# Daily dispatch budget per repo. The scarce resource is not detection (330+
# candidates a day, 1 rate-limit point per sweep) but the Claude subscription's
# session limit — a run that hits it dies with "You've hit your session limit".
# So spend it where a PR actually has a chance, judged on measured outcomes:
#
#   adk        #6498 landed, three PRs live          -> best odds; PR cap 5/day,
#                                                       dispatch budget above it so
#                                                       skipped runs do not eat it
#   langfuse   #12953 merged                        -> good
#   spec-kit   ~73% community merge share, no CLA    -> good
#   openclaw   3 PRs: one merged by a maintainer (#116958, squashed ~5h after
#              opening), two alive and well-rated, ZERO rejected. Its reviewer
#              bot is demanding but fair, and lessons/openclaw.md now records
#              what passes there -> raised to 6 PRs/day
#   comfyui/firecrawl  thin funnels                  -> low
#   hermes     195 candidates/day BUT: 12-second self-claims, ~92% of merges to
#              insiders, and our one PR was closed as `duplicate`. Highest volume,
#              worst odds — it would otherwise eat the whole budget. Raised to
#              5 PRs/day by explicit decision; the rolling 30-day duplicate
#              circuit breaker in run-fix.sh is what keeps that safe, since
#              repeated duplicates are the actual ban vector there.
DISPATCH_BUDGET = {
    "adk": 7, "langfuse": 6, "langfuse-python": 6,
    "openclaw": 6,
    "dify": 6, "autogpt": 4,
    "comfyui": 8, "firecrawl": 2,
    "hermes": 6,
    # Dispatch headroom over each repo's PR/day cap; verify check 12 asserts
    # budget >= cap so a cap can never be unreachable.
    "sglang": 5,
    "spec-kit": 5,   # cap is 3/day; headroom for runs that produce no patch
    "langchain": 4,
    "gemini-cli": 4,
}
DEFAULT_BUDGET = 2
BUDGET_FILE = scan.STATE / "dispatch-budget.json"


def budget_allows(key: str) -> bool:
    """Consume one unit of today's dispatch budget for `key`, if any is left."""
    # UTC, not local. This machine is UTC+8, so date.today() rolled over at
    # 16:00 UTC while every other daily boundary in the pipeline — fix-one.yml's
    # PR cap, run-fix.sh's cap, watch-prs.py's response cap, health.py's whole
    # report — uses UTC. The budget therefore reset eight hours early and allowed
    # up to two days' dispatches inside one UTC day, against caps counted in UTC.
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        d = json.loads(BUDGET_FILE.read_text()) if BUDGET_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    if d.get("date") != today:
        d = {"date": today, "used": {}}
    used = d["used"].get(key, 0)
    cap = DISPATCH_BUDGET.get(key, DEFAULT_BUDGET)
    if used >= cap:
        log(f"  [{key}] daily dispatch budget reached ({used}/{cap}); leaving in queue")
        return False
    return True


def budget_charge(key: str) -> None:
    """Spend one unit. Called only once a dispatch has actually started.

    budget_allows used to consume as it checked, and both drain_queues and
    trigger_fix called it — so every drained candidate was charged twice, and
    a failed `gh workflow run` was charged anyway.
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        d = json.loads(BUDGET_FILE.read_text()) if BUDGET_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    if d.get("date") != today:
        d = {"date": today, "used": {}}
    used = d["used"].get(key, 0)
    d["used"][key] = used + 1
    scan.STATE.mkdir(parents=True, exist_ok=True)
    BUDGET_FILE.write_text(json.dumps(d, indent=2) + "\n")
    log(f"  [{key}] dispatch budget {used + 1}/{DISPATCH_BUDGET.get(key, DEFAULT_BUDGET)}")


def dispatch_fix(key: str, number: int) -> bool:
    """Fire fix-one.yml at one issue. Returns True if the dispatch was accepted.

    Split out of trigger_fix so promote_claims can reach it: trigger_fix
    short-circuits every GATED repo into claim.sh, which is right for a
    freshly vetted issue and wrong for one we have already been assigned.
    """
    import subprocess

    if os.environ.get("DRY_RUN") == "1":
        log(f"  DRY_RUN: would dispatch fix-one.yml for {key}#{number}")
        return True
    try:
        r = subprocess.run(
            ["gh", "workflow", "run", "fix-one.yml",
             "--repo", PIPELINE_REPO,
             "-f", f"repo_key={key}",
             "-f", f"issue={number}"],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log(f"  -> dispatched fix-one.yml for {key}#{number}")
            return True
        log(f"  dispatch failed for {key}#{number}: {r.stderr.strip()[:160]}")
    except Exception as e:  # noqa: BLE001
        log(f"  dispatch error for {key}#{number}: {e}")
    return False


def trigger_fix(key: str, number: int) -> bool:
    """Act on a freshly vetted issue immediately, in the right place.

    Waiting for a schedule is what loses these: a prepared fix for hermes#74265
    sat on the fork at 18:03 and a competing PR appeared at 18:26.

    Routing matters because the two halves have different network needs.
      * Claiming needs only the GitHub API, which this machine's VPN reaches, so
        it runs locally and instantly.
      * Fixing needs Claude, and Anthropic returns 403 to this VPN exit
        (AS60068 Datacamp) — `claude -p` cannot run here at all. GitHub runners
        exit from AS8075 Microsoft and are accepted (probe-claude.yml saw 401 on
        the unauthenticated probe and a working `claude -p`), so the fix is
        dispatched there. Dispatching is a GitHub API call, so the VPN is fine,
        and it sidesteps `schedule`, measured at twice in six hours for */5.
    """
    import subprocess

    if key in GATED:
        script = ROOT / "claim.sh"
        if not script.exists():
            log(f"  cannot claim {key}: {script} missing")
            return False
        try:
            subprocess.Popen([str(script), key],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            log(f"  -> triggered claim.sh for {key}")
            budget_charge(key)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"  failed to trigger claim.sh for {key}: {e}")
        return False

    if not budget_allows(key):
        return False

    if dispatch_fix(key, number):
        budget_charge(key)
        return True

    # Local fallback: correct whenever the egress happens to be clean, and the
    # only route if GitHub dispatch is unavailable. run-fix.sh probes the network
    # itself and skips without consuming the candidate when it is blocked.
    script = ROOT / "run-fix.sh"
    if script.exists():
        try:
            subprocess.Popen([str(script), key],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            log(f"  -> fell back to local run-fix.sh for {key}")
            budget_charge(key)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"  local fallback failed for {key}: {e}")
    return False


# A claim that has gone this long without an assignment is not going to get
# one. Dropping it returns the issue to the queue so a repo with a hard gate
# stops silently consuming detection capacity.
CLAIM_EXPIRY_DAYS = 21
ME = "chelsealong"


def _assignment(upstream: str, number: int):
    """(state, assignees, updated_at) for one issue, or None on API failure.

    None means "ask again later", never "nothing there" — the bare-except that
    turned a 422 into an empty claimant list is exactly how claim detection
    stayed broken for weeks.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{upstream}/issues/{number}", "-X", "GET",
             "--jq", '[.state, ([.assignees[].login]|join(",")), .updated_at]|@tsv'],
            capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        log(f"  assignment({upstream}#{number}) errored: {e}")
        return None
    if r.returncode != 0:
        log(f"  assignment({upstream}#{number}) failed: {r.stderr.strip()[:120]}")
        return None
    parts = r.stdout.strip().split("\t")
    if len(parts) < 3:
        return None
    state, who, updated = parts[0], parts[1], parts[2]
    return state, [w for w in who.split(",") if w], updated


def promote_claims(keys: list[str]) -> int:
    """Dispatch a gated issue once the maintainer has actually assigned us.

    claim.sh only asks; until this existed nothing consumed the answer.
    drain_queues skips GATED outright, so an assignment that was granted
    produced no PR, and vet() then rejected the same issue as "claimed by
    chelsealong" so it never returned through the queue either. langgraph
    sat at eight claims, seven comments and zero PRs for eight days inside
    that gap, and pydantic-ai had just claimed three more into it.
    """
    from datetime import datetime, timezone

    dry = os.environ.get("DRY_RUN") == "1"
    now = datetime.now(timezone.utc)
    n = 0
    for key in keys:
        if key not in GATED:
            continue
        upstream = (scan.REPOS.get(key) or {}).get("upstream")
        p = scan.STATE / f"{key}.json"
        if not upstream or not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            continue
        claimed = list(d.get("claimed", []))
        keep: list[int] = []
        for num in claimed:
            info = _assignment(upstream, num)
            if info is None:
                keep.append(num)          # transient — retry next cycle
                continue
            state, assignees, updated = info
            low = [a.lower() for a in assignees]
            if state != "open":
                log(f"  [{key}] #{num} is {state} upstream — dropping claim")
                continue
            if ME in low:
                if already_dispatched(key, num):
                    keep.append(num)
                    continue
                if not budget_allows(key):
                    keep.append(num)
                    continue
                log(f"  [{key}] #{num} assigned to {ME} — dispatching the fix")
                if dispatch_fix(key, num):
                    if not dry:
                        budget_charge(key)
                        record_dispatch(key, num)
                    n += 1
                keep.append(num)
                continue
            if assignees:
                log(f"  [{key}] #{num} went to {','.join(assignees)} — dropping claim")
                continue
            try:
                age = (now - datetime.fromisoformat(
                    updated.replace("Z", "+00:00"))).days
            except Exception:  # noqa: BLE001
                age = 0
            if age >= CLAIM_EXPIRY_DAYS:
                log(f"  [{key}] #{num} unassigned after {age}d — expiring claim")
                continue
            keep.append(num)
        if keep != claimed and not dry:
            d["claimed"] = keep
            p.write_text(json.dumps(d, indent=2) + "\n")
    return n


def load_seen() -> dict[str, list[int]]:
    if SEEN.exists():
        try:
            return json.loads(SEEN.read_text())
        except Exception:  # noqa: BLE001
            log("seen.json unreadable; starting fresh")
    return {}


def save_seen(seen: dict[str, list[int]]) -> None:
    scan.STATE.mkdir(parents=True, exist_ok=True)
    # Keep it bounded — only the newest matter for dedup.
    trimmed = {k: sorted(v)[-400:] for k, v in seen.items()}
    SEEN.write_text(json.dumps(trimmed, indent=2) + "\n")


def append_candidate(key: str, rec: dict) -> None:
    """Add a vetted issue to the queue run-fix.sh already reads."""
    scan.QUEUE.mkdir(parents=True, exist_ok=True)
    f = scan.QUEUE / f"{key}.json"
    cfg = scan.REPOS[key]
    if f.exists():
        q = json.loads(f.read_text())
    else:
        q = {
            "repo": key,
            "upstream": cfg["upstream"],
            "implements_in": cfg.get("implements_in", cfg["upstream"]),
            "needs_assignment": cfg.get("needs_assignment", False),
            "candidates": [],
            "rejected": [],
        }
    if any(c["number"] == rec["number"] for c in q["candidates"]):
        return
    q["candidates"].insert(0, rec)          # freshest first
    q["scanned_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    q["partial"] = False
    f.write_text(json.dumps(q, indent=2) + "\n")


def sweep(keys: list[str], seen: dict[str, list[int]], per_repo: int,
          bootstrap: bool) -> tuple[int, int]:
    """Returns (new_detected, accepted)."""
    query = build_query(keys, per_repo)
    raw = scan.gh(["api", "graphql", "-f", f"query={query}"], kind="other")
    data = json.loads(raw)["data"]

    new_count = accepted = 0
    for i, key in enumerate(keys):
        repo = data.get(f"r{i}")
        if not repo:
            continue
        known = set(seen.get(key, []))
        for node in (repo.get("issues") or {}).get("nodes", []):
            num = node["number"]
            if num in known:
                continue
            known.add(num)
            if bootstrap:
                continue          # first sweep only records history, never acts
            new_count += 1
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00")))
            lag = age.total_seconds()

            issue = to_rest_shape(node)

            req = required_labels(scan.REPOS[key])
            if req and not (req & {l["name"] for l in issue["labels"]}):
                log(f"  rejected [{key}] #{num} — lacks required label "
                    f"{sorted(req)} (out of scope for this checkout)")
                continue

            try:
                ok, why, extra = scan.vet(scan.REPOS[key], scan.REPOS[key]["upstream"], issue)
            except Exception as e:  # noqa: BLE001
                log(f"  [{key}] #{num} vet failed: {e}")
                continue

            if ok:
                accepted += 1
                append_candidate(key, {
                    "number": num,
                    "title": issue["title"][:160],
                    "url": issue["html_url"],
                    "created_at": node["createdAt"],
                    "age_hours": round(lag / 3600, 2),
                    "reason": "clear",
                    "detected_after_s": round(lag, 1),
                    **extra,
                })
                log(f"  ACCEPTED [{key}] #{num} after {lag:.1f}s — {issue['title'][:60]}")
                if not NO_TRIGGER:
                    trigger_fix(key, num)
            else:
                log(f"  rejected [{key}] #{num} ({lag:.0f}s old) — {why[:70]}")
        seen[key] = sorted(known)

    return new_count, accepted




DISPATCHED = scan.STATE / "dispatched.json"


def _dispatched() -> dict:
    try:
        return json.loads(DISPATCHED.read_text()) if DISPATCHED.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def record_dispatch(key: str, number: int) -> None:
    """Remember that this issue was already sent to a fixer.

    The queue cannot carry this memory: scan.py rebuilds queue/<key>.json from
    scratch every cycle, so a candidate drain removes reappears twenty minutes
    later and is dispatched again. spec-kit#3997 went out three times that way
    and burned three agent sessions on one issue.
    """
    d = _dispatched()
    lst = d.setdefault(key, [])
    if number not in lst:
        lst.append(number)
        # Bounded: only recent history matters for dedup, and an unbounded file
        # is its own failure mode.
        d[key] = lst[-400:]
        scan.STATE.mkdir(parents=True, exist_ok=True)
        DISPATCHED.write_text(json.dumps(d, indent=2) + "\n")


def already_dispatched(key: str, number: int) -> bool:
    return number in _dispatched().get(key, [])



REFUNDED = scan.STATE / "budget-refunds.json"


def reconcile_budget() -> int:
    """Give back budget spent on runs that never had a chance to work.

    A unit is deducted the moment a fix is dispatched, which is right: it stops
    a retry storm. But it means an infrastructure failure spends the day's
    allowance for nothing. On 2026-08-07, 23 of 27 runs died before the agent
    started — sixteen on `gh: Bad credentials` during the 26 minutes between a
    PAT being regenerated on github.com and the new value reaching the repo
    secret, seven on Claude's org policy — and hermes, openclaw, comfyui and
    langfuse all hit their daily cap having produced nothing. The budget said
    the work was done; no work had been attempted.

    So: a run that failed on credentials is not work. Refund it. Each run is
    refunded at most once, tracked by run id.
    """
    try:
        raw = subprocess.run(
            ["gh", "run", "list", "--repo", PIPELINE_REPO, "--workflow", "fix-one.yml",
             "--limit", "40", "--json", "databaseId,conclusion,createdAt"],
            capture_output=True, text=True, timeout=90)
        runs = json.loads(raw.stdout or "[]") if raw.returncode == 0 else []
    except Exception as e:  # noqa: BLE001
        log(f"  budget reconcile: cannot list runs ({str(e)[:80]})")
        return 0

    today = datetime.now(timezone.utc).date().isoformat()
    try:
        done = json.loads(REFUNDED.read_text()) if REFUNDED.exists() else {}
    except Exception:  # noqa: BLE001
        done = {}
    if done.get("date") != today:
        done = {"date": today, "runs": []}
    seen_ids = set(done["runs"])

    try:
        budget = json.loads(BUDGET_FILE.read_text()) if BUDGET_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        return 0
    if budget.get("date") != today:
        return 0

    refunded = 0
    for r in runs:
        rid = r.get("databaseId")
        if (r.get("conclusion") != "failure" or not rid or rid in seen_ids
                or (r.get("createdAt") or "") < today):
            continue
        try:
            lg = subprocess.run(
                ["gh", "run", "view", str(rid), "--repo", PIPELINE_REPO, "--log"],
                capture_output=True, text=True, timeout=120)
            text = lg.stdout or ""
        except Exception:  # noqa: BLE001
            continue
        if not re.search(r"Bad credentials|organization has disabled|subscription access"
                         r"|Invalid bearer token|Failed to authenticate", text, re.I):
            continue                      # a real failure; that unit was earned
        m = re.search(r"repo_key=(\S+) issue=", text)
        key = m.group(1) if m else None
        seen_ids.add(rid)
        if key and budget.get("used", {}).get(key, 0) > 0:
            budget["used"][key] -= 1
            refunded += 1
            log(f"  [{key}] refunded 1 budget unit — run {rid} failed on credentials, not on work")

    if refunded:
        BUDGET_FILE.write_text(json.dumps(budget, indent=2) + "\n")
    done["runs"] = sorted(seen_ids)[-200:]
    scan.STATE.mkdir(parents=True, exist_ok=True)
    REFUNDED.write_text(json.dumps(done, indent=2) + "\n")
    return refunded


def drain_queues(keys: list[str]) -> int:
    """Dispatch queued candidates that the live sweep never gets to.

    The sweep only inspects each repo's newest `--per-repo` issues, so it only
    ever acts on issues created while it is watching. Everything else lands in
    queue/<key>.json — and nothing consumed that queue: its only reader is
    run-fix.sh, which runs `claude` locally, which cannot authenticate from this
    machine at all.

    The consequence was invisible because the busy repos looked healthy. On
    2026-08-06, 8 of 13 repos dispatched nothing while 24 vetted candidates sat
    in queues; langfuse had 9 after its assignee filter was fixed and dispatched
    none, because a low-velocity tracker never puts anything in the live window.

    Budget stays the only gate, so this cannot run away: a repo whose sweep
    already spent its allowance drains nothing.
    """
    # A dry run must not consume the queue. This function removes each candidate
    # it dispatches and writes the queue back, so exercising it with trigger_fix
    # stubbed out silently deleted four vetted candidates (adk#6606,
    # spec-kit#3997, dify#40008/#40028) that were never sent anywhere. The same
    # mistake was fixed in watch-prs.py days earlier and not carried over here.
    dry = os.environ.get("DRY_RUN") == "1"
    sent = 0
    for key in keys:
        if key in GATED:
            continue                      # those go through claim.sh, not here
        f = scan.QUEUE / f"{key}.json"
        if not f.exists():
            continue
        try:
            q = json.loads(f.read_text())
        except Exception as e:  # noqa: BLE001
            log(f"  [{key}] queue unreadable: {str(e)[:100]}")
            continue
        if q.get("partial"):
            # A rate-limited scan is indistinguishable from "no work"; acting on
            # its leftovers is how a stale pick gets dispatched.
            continue
        cands = q.get("candidates") or []
        if not cands:
            continue
        # Freshest first. This used to be oldest-first, to avoid re-treading
        # what the live sweep had just seen — but record_dispatch now dedupes
        # exactly, so that reason is gone, and the ordering was actively
        # harmful: on a busy tracker an issue is old precisely because nobody
        # could act on it. Draining gemini-cli oldest-first spent its whole
        # daily budget on "Possible bug", "Where is oauth authentication?" and
        # a report in Spanish, while five well-scoped crashes waited.
        for rec in list(cands):
            num = rec["number"]
            if already_dispatched(key, num):
                # Drop it from the queue so the next rebuild does not resurrect
                # it, but spend nothing on it.
                q["candidates"] = [c for c in q["candidates"] if c["number"] != num]
                continue
            if not budget_allows(key):
                break
            log(f"  [{key}] draining queued #{num} (queued {rec.get('age_hours', '?')}h old issue)")
            # Only spend the ledger entry on a dispatch that actually started.
            # Recording it unconditionally meant a failed `gh workflow run`
            # retired the candidate for good and still charged the budget.
            if not trigger_fix(key, num):
                continue
            if not dry:
                record_dispatch(key, num)
            q["candidates"] = [c for c in q["candidates"] if c["number"] != num]
            sent += 1
        if not dry:
            f.write_text(json.dumps(q, indent=2) + "\n")
    return sent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--per-repo", type=int, default=5,
                    help="newest N issues to inspect per repo per sweep")
    ap.add_argument("--repo", action="append", help="limit to these keys")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    keys = a.repo or list(scan.REPOS)
    seen = load_seen()
    bootstrap = not seen
    if bootstrap:
        log(f"bootstrap sweep: recording current issues for {len(keys)} repo(s) "
            "without acting on them")

    log(f"watching {len(keys)} repo(s) every {a.interval}s: {' '.join(keys)}")
    sweeps = 0
    try:
        while True:
            t0 = time.monotonic()
            try:
                new, ok = sweep(keys, seen, a.per_repo, bootstrap)
                save_seen(seen)
                sweeps += 1
                if bootstrap:
                    log(f"bootstrap complete ({sum(len(v) for v in seen.values())} issues recorded)")
                    bootstrap = False
                elif new:
                    log(f"sweep {sweeps}: {new} new, {ok} accepted")
                elif sweeps % 120 == 0:      # ~every 10 min at 5s
                    log(f"sweep {sweeps}: idle")

                # Drain on the same cadence as the idle log rather than every
                # sweep: queued work is by definition not urgent, and one pass
                # per ~10 minutes is plenty to spend a daily allowance.
                if not bootstrap and sweeps % 120 == 0:
                    # Reconcile before draining, so refunded units are available
                    # to the drain that follows rather than a cycle later.
                    reconcile_budget()
                    n = drain_queues(keys)
                    if n:
                        log(f"drained {n} queued candidate(s)")
                    # The other half of the gated flow: claim.sh asks, this
                    # acts on the answer. Without it an assignment that was
                    # granted produced nothing at all.
                    m = promote_claims(keys)
                    if m:
                        log(f"promoted {m} assigned claim(s)")
            except Exception as e:  # noqa: BLE001
                # One bad sweep must not kill a long-lived watcher.
                log(f"sweep failed: {str(e)[:200]}")
                time.sleep(min(60, a.interval * 6))

            if a.once:
                return 0
            time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
