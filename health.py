#!/usr/bin/env python3
"""Daily health check for the OSS contribution pipeline.

The pipeline runs unattended, so its dangerous failure mode is not crashing —
it is continuing to look healthy while producing nothing. That has already
happened: for four hours the watcher ran stale code, kept logging accepted
candidates, and dispatched every one of them into a path blocked by a 403. Logs
looked normal; output was zero.

Every check below is derived from a failure that actually occurred:

  agents_loaded      launchd reported status 0 for an agent whose every run
                     failed with "Operation not permitted" (macOS TCC denies
                     execution under ~/Desktop). Never trust `launchctl list`;
                     confirm the job's own log is being written.
  watcher_fresh      the watcher is a long-lived process; a silent death shows
                     up as a log that stopped advancing, not as an error.
  detect_vs_dispatch the four-hour outage: candidates accepted, none dispatched.
  queue_not_partial  a rate-limited scan yields zero candidates, which reads as
                     "no work available" unless `partial` is checked.
  quota              "You've hit your session limit" kills a run in seconds.
  prs_not_behind     two adk PRs were cut 358 commits back, before upstream
                     pinned its actions, so the org security scan failed on the
                     merge ref. mergeStateStatus == BEHIND is the early warning.
  stale_prs          a PR nobody has touched for days needs a human, not a loop.

    ./health.py            print the report
    ./health.py --json     machine-readable
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPORTS = ROOT / "health"
ME = "chelsealong"
# Every identity our commits have carried. adk's Copybara preserves the author
# it finds on the PR's commits, and #6498 went in under jialongli001@gmail.com
# while #6649 carried chelsealong@126.com — searching one address undercounted
# adk by one from 2026-07-28 until this was noticed on 08-13. Add, never
# replace: an old address keeps mattering for as long as its commits are on a
# default branch.
ME_EMAILS = ["chelsealong@126.com", "jialongli001@gmail.com"]
ME_EMAIL = ME_EMAILS[0]
# Upstreams whose default branch is worth checking for our landed commits.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scan  # REPOS — REPO_LIST is derived from it, never hand-maintained

# Derived from the scanner's config so it cannot drift. It had: spec-kit was
# still listed after removal while ComfyUI — which has a landed commit — was
# never added, so the landed count silently omitted a repo.
#
# RETIRED keeps repos we no longer scan but where our commits are on a default
# branch. That output is real and stays visible; dropping it would make the
# programme look like it lost work it did not lose.
RETIRED = ["github/spec-kit"]
REPO_LIST = sorted(
    {c.get("implements_in") or c["upstream"] for c in scan.REPOS.values()} | set(RETIRED)
)
AGENTS = ["oss-watch", "oss-scan", "oss-fix", "oss-claim", "oss-prwatch"]


def sh(cmd: list[str], timeout: int = 60) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def now() -> datetime:
    return datetime.now(timezone.utc)


def tail_since(path: pathlib.Path, hours: float) -> list[str]:
    """Log lines from the last `hours`. Lines start with an ISO timestamp."""
    if not path.exists():
        return []
    cutoff = now() - timedelta(hours=hours)
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            out.append(line)
    return out


def check_agents() -> tuple[list[str], dict]:
    problems, detail = [], {}
    listing = sh(["launchctl", "list"])
    for a in AGENTS:
        loaded = any(a in ln for ln in listing.splitlines())
        detail[a] = "loaded" if loaded else "MISSING"
        if not loaded:
            problems.append(f"launchd agent {a} is not loaded")
    return problems, detail


def check_watcher_fresh() -> tuple[list[str], dict]:
    """A dead long-lived process shows up as a log that stopped advancing."""
    log = ROOT / "watch.log"
    lines = tail_since(log, 1)
    alive = bool(sh(["pgrep", "-f", "watch.py"]).strip())
    problems = []
    if not alive:
        problems.append("watch.py is not running")
    if not lines:
        problems.append("watch.log has had no entries in the last hour "
                        "(process may be wedged even if pgrep finds it)")
    return problems, {"process_alive": alive, "lines_last_hour": len(lines)}


def check_detect_vs_dispatch() -> tuple[list[str], dict]:
    """The silent outage: candidates accepted but nothing acted on."""
    lines = tail_since(ROOT / "watch.log", 24)
    accepted = sum(1 for l in lines if "ACCEPTED" in l)
    dispatched = sum(1 for l in lines if "dispatched fix-one" in l)
    claimed = sum(1 for l in lines if "triggered claim.sh" in l)
    budget_held = sum(1 for l in lines if "dispatch budget reached" in l)
    problems = []
    # Budget-limited is a deliberate hold, not a fault.
    if accepted and not (dispatched or claimed or budget_held):
        problems.append(
            f"{accepted} candidates accepted in 24h but NONE dispatched, claimed, "
            "or budget-held — the dispatch path is broken (this is exactly the "
            "four-hour stale-code outage)")
    return problems, {"accepted_24h": accepted, "dispatched_24h": dispatched,
                      "claimed_24h": claimed, "budget_held_24h": budget_held}


def check_queues() -> tuple[list[str], dict]:
    problems, detail = [], {}
    for f in sorted((ROOT / "queue").glob("*.json")):
        try:
            q = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            problems.append(f"queue/{f.name} is unreadable")
            continue
        detail[q.get("repo", f.stem)] = {
            "candidates": len(q.get("candidates", [])),
            "partial": bool(q.get("partial")),
            "scanned_at": q.get("scanned_at", "?"),
        }
        if q.get("partial"):
            problems.append(f"{q.get('repo')} queue is PARTIAL — its last scan was "
                            "rate-limited, so an empty queue there means 'scan "
                            "broken', not 'no work'")
    return problems, detail


def check_quota() -> tuple[list[str], dict]:
    hits = [l for l in tail_since(ROOT / "fix.log", 24) if "session limit" in l.lower()]
    problems = []
    if len(hits) >= 3:
        problems.append(f"{len(hits)} session-limit refusals in 24h — the "
                        "subscription quota is the binding constraint, not the code")
    return problems, {"session_limit_hits_24h": len(hits)}


def check_duplicates() -> tuple[list[str], dict]:
    """Rolling 30-day duplicate/invalid closures per repo.

    hermes auto-downgraded to prepare-only at two of these; that guard was
    removed by decision in favour of a one-month observation at full quota. It is
    still measured here, because repeated duplicates — not volume — are what
    triggers GitHub spam enforcement, and a month of "we think it's fine" is not
    data. Reinstate the downgrade if this climbs.
    """
    detail, problems = {}, []
    for repo in ("NousResearch/hermes-agent", "openclaw/openclaw", "google/adk-python"):
        raw = sh(["gh", "pr", "list", "--repo", repo, "--author", ME,
                  "--state", "closed", "--limit", "40", "--json", "labels,closedAt"], 60)
        n = 0
        if raw:
            cutoff = (now() - timedelta(days=30)).isoformat()
            try:
                for pr in json.loads(raw):
                    if (pr.get("closedAt") or "") < cutoff:
                        continue
                    names = {l["name"] for l in pr.get("labels", [])}
                    if names & {"duplicate", "invalid"}:
                        n += 1
            except Exception:  # noqa: BLE001
                pass
        detail[repo] = n
        if n >= 3:
            problems.append(f"{repo}: {n} PRs closed as duplicate/invalid in 30 days "
                            "— repeated duplicates are the spam-enforcement vector; "
                            "consider reinstating the prepare-only downgrade")
    return problems, detail


def check_agent_reachable() -> tuple[list[str], dict]:
    """Whether the agent step can run at all.

    On 2026-08-07 the organisation disabled Claude subscription access for
    Claude Code. Twenty consecutive fix-one runs died on
    "Your organization has disabled Claude subscription access", each one
    spending a dispatch-budget unit and a runner and producing nothing. The
    daily report said everything was fine.

    Nothing here caught it. `check_quota` matches only "session limit", which is
    a different failure. `check_session_waste` alerts on sessions spent with no
    PR — but it counts a session as spent when the *generator step succeeded*,
    and a run that cannot authenticate never gets that far, so its counter stayed
    at zero and the threshold was never reached. The statistic excluded exactly
    the case it was meant to detect.

    So this counts run outcomes, which cannot be argued with, and looks for the
    auth strings directly.
    """
    raw = sh(["gh", "run", "list", "--repo", "chelsealong/oss-pipeline",
              "--workflow", "fix-one.yml", "--limit", "40",
              "--json", "databaseId,conclusion,createdAt"], 90)
    d = {"completed_24h": 0, "failed_24h": 0, "auth_failures": 0, "which": ""}
    if not raw:
        return [], d
    cutoff = (now() - timedelta(hours=24)).isoformat()
    try:
        runs = [r for r in json.loads(raw)
                if (r.get("createdAt") or "") > cutoff and r.get("conclusion")]
    except Exception:  # noqa: BLE001
        return [], d

    d["completed_24h"] = len(runs)
    failed = [r for r in runs if r["conclusion"] == "failure"]
    d["failed_24h"] = len(failed)

    # Sample three, and name which credential failed. Both kinds happened on
    # 2026-08-07 within the same half hour and were initially reported as one:
    # three runs died on Claude's org policy, then sixteen on `gh: Bad
    # credentials` because the GitHub PAT was regenerated on github.com 26
    # minutes before the new value was written to the repo secret. Diagnosing
    # "auth is broken" without saying WHICH sends the fix to the wrong place.
    d["which"] = ""
    for r in failed[:3]:
        log = sh(["gh", "run", "view", str(r["databaseId"]),
                  "--repo", "chelsealong/oss-pipeline", "--log"], 120) or ""
        if re.search(r"Bad credentials", log, re.I):
            d["auth_failures"] += 1
            d["which"] = "GH_PAT (gh: Bad credentials)"
        elif re.search(r"organization has disabled|subscription access|"
                       r"Invalid bearer token|Failed to authenticate", log, re.I):
            d["auth_failures"] += 1
            d["which"] = d["which"] or "CLAUDE_CODE_OAUTH_TOKEN"

    problems = []
    if d["auth_failures"]:
        problems.append(f"fix-one cannot authenticate — {d['which']} — the agent step "
                        "is failing for every run and each one still spends a "
                        "dispatch-budget unit")
    elif d["completed_24h"] >= 6 and d["failed_24h"] * 2 > d["completed_24h"]:
        problems.append(f"{d['failed_24h']} of {d['completed_24h']} fix-one runs failed in 24h — "
                        "the pipeline is spending budget on runs that cannot succeed")
    return problems, d


def check_session_waste() -> tuple[list[str], dict]:
    """How many agent sessions were spent, and how many produced nothing.

    A measured baseline: over 40 runs, 34 spent a generator session and only 6
    opened a PR. The expensive failures were not bad fixes but sessions that
    decided nothing — 5 of 18 sampled runs ended "no outcome written" because
    the agent backgrounded a build and ended its turn waiting for it, and
    another 3 rediscovered facts the GitHub API answers for free. Both classes
    are now guarded; this tracks whether the guards hold.
    """
    raw = sh(["gh", "run", "list", "--repo", "chelsealong/oss-pipeline",
              "--workflow", "fix-one.yml", "--limit", "40",
              "--json", "databaseId,conclusion,createdAt"], 90)
    detail = {"dispatched": 0, "sessions_spent": 0, "prevet_saved": 0,
              "no_outcome": 0, "quota": 0, "prs": 0}
    if not raw:
        return [], detail
    cutoff = (now() - timedelta(hours=24)).isoformat()
    try:
        runs = [r for r in json.loads(raw)
                if (r.get("createdAt") or "") > cutoff and r.get("conclusion")]
    except Exception:  # noqa: BLE001
        return [], detail

    for r in runs:
        steps = sh(["gh", "run", "view", str(r["databaseId"]),
                    "--repo", "chelsealong/oss-pipeline", "--json", "jobs",
                    "--jq", r'[.jobs[].steps[]|"\(.name)=\(.conclusion)"]|join("|")'], 60)
        if not steps:
            continue
        detail["dispatched"] += 1
        if "Skip — taken since dispatch=success" in steps:
            detail["prevet_saved"] += 1
        if "Generate fix and push branch (no PR yet)=success" in steps:
            detail["sessions_spent"] += 1
        # NOT the step's conclusion. That step exits 0 on several legitimate
        # early paths — issue closed, a rival PR appeared, prepare-only repos —
        # so "success" and "a PR exists" are different facts. On 2026-08-07 it
        # reported 3 PRs on a day when GitHub had none, which hid that a
        # reviewed dify fix had been silently dropped by a broken rival check.
        pass

    # Count what GitHub says exists, not what a step reported.
    raw_prs = sh(["gh", "api", "graphql", "-f",
                  'query={search(type:ISSUE,first:50,query:"is:pr author:' + ME +
                  ' created:>=' + (now() - timedelta(hours=24)).date().isoformat() +
                  '"){issueCount}}', "--jq", ".data.search.issueCount"], 60)
    try:
        detail["prs"] = int((raw_prs or "0").strip())
    except ValueError:
        detail["prs"] = 0

    problems = []
    spent = detail["sessions_spent"]
    if spent >= 8 and detail["prs"] == 0:
        problems.append(f"{spent} agent sessions spent in 24h and no PR opened — "
                        "the loop is burning quota without producing output")
    return problems, detail


def check_merge_throughput() -> tuple[list[str], dict]:
    """Opened vs merged, and who the queue is actually waiting on.

    Added because the pipeline was measured on PRs opened, which is the half we
    control and the half that does not matter. On 2026-08-01 it had 15 open PRs
    and 2 merges, and in 9 of the 15 the last speaker was us — the bottleneck
    had moved to maintainer review, where opening more PRs makes things worse,
    not better.

    Counts adk's Copybara merges: that repo merges internally, so GitHub reports
    merged=false and adds a `merged` label. Missing this undercounts the only
    metric the quota decision rests on.
    """
    q = ('{search(type:ISSUE, first:80, query:"is:pr author:%s"){nodes{'
         '... on PullRequest{number state merged mergedAt closedAt createdAt'
         ' repository{nameWithOwner} labels(first:20){nodes{name}}'
         ' comments(last:1){nodes{author{login}}}}}}}' % ME)
    raw = sh(["gh", "api", "graphql", "-f", f"query={q}"], 90)
    d = {"open": 0, "merged_total": 0, "merged_7d": 0, "merged_24h": 0,
         "opened_24h": 0, "closed_unmerged": 0, "awaiting_them": 0, "oldest_open_days": 0}
    if not raw:
        return [], d
    try:
        nodes = [n for n in json.loads(raw)["data"]["search"]["nodes"] if n]
    except Exception:  # noqa: BLE001
        return [], d

    day = (now() - timedelta(hours=24)).isoformat()
    week = (now() - timedelta(days=7)).isoformat()
    for pr in nodes:
        repo = (pr.get("repository") or {}).get("nameWithOwner", "")
        if repo.startswith(f"{ME}/"):
            continue                      # our own fork-side test PRs
        names = {l["name"] for l in (pr.get("labels") or {}).get("nodes", [])}
        merged = bool(pr.get("merged")) or "merged" in names
        when = pr.get("mergedAt") or pr.get("closedAt") or ""
        if (pr.get("createdAt") or "") > day:
            d["opened_24h"] += 1
        if merged:
            d["merged_total"] += 1
            if when > week:
                d["merged_7d"] += 1
            if when > day:
                d["merged_24h"] += 1
        elif pr.get("state") == "CLOSED":
            d["closed_unmerged"] += 1
        elif pr.get("state") == "OPEN":
            d["open"] += 1
            age = (now() - datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))).days
            d["oldest_open_days"] = max(d["oldest_open_days"], age)
            last = ((pr.get("comments") or {}).get("nodes") or [{}])
            who = ((last[0] if last else {}).get("author") or {}).get("login")
            if who in (ME, None):
                d["awaiting_them"] += 1

    problems = []
    if d["open"] >= 12 and d["merged_7d"] == 0:
        problems.append(f"{d['open']} PRs open and none merged in 7 days — "
                        "opening more dilutes review attention rather than adding output")
    return problems, d


def check_credited() -> tuple[list[str], dict]:
    """Merged upstream PRs that credit us without carrying our commits.

    hermes#86244 is the case this exists for: teknium1 wrote the fix himself and
    the body says "credit to @chelsealong for the #85709 analysis". The work
    landed and our name is on it, but the commit is his, so check_landed cannot
    see it.

    Kept strictly apart from the commit count and never added to it. Two reasons
    to distrust this number:

      * a salvage PR mentions us because it CARRIES our commits — those are
        already counted as landed, so they are excluded here by checking whether
        the PR has a commit of ours;
      * most mentions are machinery. AutoGPT#13761 and #13764 matched only
        because a PR-overlap bot listed our open PRs in a comment. Only the PR
        body counts, and only when it reads like credit.

    A soft metric is fine as long as it is labelled soft.
    """
    repos = sorted({c.get("implements_in") or c["upstream"] for c in scan.REPOS.values()} | set(RETIRED))
    qual = "+".join(f"repo:{r}" for r in repos)
    raw = sh(["gh", "api",
              f"search/issues?q=type:pr+is:merged+{ME}+-author:{ME}+{qual}&per_page=50",
              "-X", "GET", "--jq",
              '[.items[] | {n:.number, repo:(.repository_url|split("/")[4:6]|join("/")),'
              ' by:.user.login, at:.closed_at, body:(.body // "")}]'], 90)
    try:
        items = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001
        return [], {"credited": 0, "items": []}

    CREDIT = re.compile(r"(credit|thanks|thank you|reported by|analysis|found by|"
                        r"spotted by|based on|per @)", re.I)
    out = []
    for it in items:
        # The body is the only place a human writes credit; bot comments are noise.
        if ME not in it["body"] or not CREDIT.search(it["body"]):
            continue
        # A salvage carries our commits and is already counted as landed.
        mine = sh(["gh", "api", f"repos/{it['repo']}/pulls/{it['n']}/commits",
                   "--jq", f'[.[] | select(.commit.author.email | test("{ME_EMAILS[0].replace("%40","@")}"))] | length'], 45)
        try:
            if int(mine or 0) > 0:
                continue
        except ValueError:
            pass
        out.append({"repo": it["repo"], "number": it["n"], "by": it["by"], "at": it["at"][:10]})
    return [], {"credited": len(out), "items": out}


def check_landed() -> tuple[list[str], dict]:
    """Commits of ours on each upstream's default branch.

    The only honest measure of output, because "merged" undercounts badly and
    the quota decision was about to be made on that undercount:

      * adk merges externally-authored PRs through Copybara, which closes the PR
        with a `merged` label and merged=false.
      * hermes "salvages" them — a maintainer re-lands the branch under their own
        PR so privileged CI can run, keeps our commit authorship, and records the
        contributor in contributors/emails/. Our #75790 was closed at 17:47 and
        the identical work merged as #75910 at 17:47:46. Counting PR state alone
        reported that day as zero merges.

    Commits carry our author email through both paths, so this catches what PR
    state cannot. It cannot see a squash that rewrites authorship — nothing can,
    short of diffing content — so treat it as a floor, not a ceiling.
    """
    detail, total = {}, 0
    for cfg in REPO_LIST:
        # Every identity, not just the current one: adk's Copybara preserves
        # whatever author the PR's commits carried, and #6498 went in under the
        # old gmail address while #6649 carried the 126.com one. Searching a
        # single address undercounted adk by one for sixteen days.
        dates = []
        for addr in ME_EMAILS:
            raw = sh(["gh", "api", f"repos/{cfg}/commits?author={addr}&per_page=100",
                      "--jq", "[.[] | .commit.author.date]"], 60)
            try:
                dates += json.loads(raw) if raw else []
            except Exception:  # noqa: BLE001
                pass
        dates = sorted(set(dates), reverse=True)
        if dates:
            detail[cfg] = {"total": len(dates), "latest": dates[0][:16]}
            total += len(dates)
    return [], {"by_repo": detail, "total": total}


def check_sweep_health() -> tuple[list[str], dict]:
    """How often the 5-second sweep fails to reach GitHub.

    A single failure is nothing — the loop retries in seconds. A sustained one
    is the pipeline's oldest failure mode wearing a new hat: the watcher keeps
    running, the log keeps being written, and nothing is detected. Sweep
    failures were logged and never counted, so a day of them would have looked
    identical to a quiet day.

    Measured baseline on 2026-08-07: 22 in 24 hours, all network — 14 "error
    connecting to api.github.com", the rest connection resets and a TLS
    handshake timeout. That is the normal rate for this machine's egress, so
    the threshold sits above it rather than at zero.
    """
    lines = tail_since(ROOT / "watch.log", 24)
    fails = [l for l in lines if "sweep failed" in l]
    net = [l for l in fails if re.search(
        r"error connecting|connection reset|read tcp|TLS handshake|timeout|EOF|"
        r"Temporary failure|no such host", l, re.I)]
    other = len(fails) - len(net)
    d = {"sweep_failures_24h": len(fails), "network": len(net), "other": other}
    problems = []
    if len(fails) >= 60:
        problems.append(f"{len(fails)} sweep failures in 24h — detection is degraded; "
                        "the watcher is running but not reliably reaching GitHub")
    elif other >= 10:
        problems.append(f"{other} non-network sweep failures in 24h — these are not "
                        "egress flakes and should be read")
    return problems, d


def check_prwatch_health(hours: float = 24) -> tuple[list[str], dict]:
    """Does the PR watcher's pass actually COMPLETE?

    Every other watcher check in this file reads watch.log. Nothing read
    watch-prs.log, so on 2026-08-23 a NameError in the REST check-run path
    (`_is_ours` took a PR dict, the paginated path had only a repo string)
    killed one_pass on its 375th consecutive attempt without a single alert.
    Liveness looked perfect: the log advanced every five minutes, because a
    failing pass logs just as much as a working one.

    Two consequences, both worth their own alarm:
      - zero completions means the PRs late in the iteration were never read;
      - save_seen() sits inside the same try, so nothing was ever recorded, and
        the same feedback was re-dispatched 2,488 times across 44 PRs in two
        days. One PR was dispatched 197 times.

    So: alert on failure RATIO, not failure count, and alert separately when a
    single PR is dispatched far more often than it could have new feedback.
    """
    lines = tail_since(ROOT / "watch-prs.log", hours)
    failed = sum(1 for l in lines if "pass failed" in l)
    complete = sum(1 for l in lines if "pass complete" in l or "bootstrap complete" in l)
    disp = collections.Counter(
        m.group(1) for l in lines
        if (m := re.search(r"dispatched respond-pr\.yml for (\S+)", l)))
    worst, worst_n = (disp.most_common(1) or [("-", 0)])[0]
    problems = []
    if not lines:
        problems.append(f"watch-prs.log has had no entries in {hours:g}h — "
                        "the PR watcher is not running")
    elif failed and not complete:
        problems.append(
            f"{failed} PR-watch passes failed and NONE completed in {hours:g}h — "
            "one_pass is raising every time; save_seen never runs, so feedback "
            "is re-dispatched indefinitely")
    elif failed > complete:
        problems.append(f"{failed} PR-watch passes failed vs {complete} completed "
                        f"in {hours:g}h — the pass is failing more often than not")
    # A PR can legitimately be re-dispatched a few times a day. Not 197.
    if worst_n >= 20:
        problems.append(f"{worst} was dispatched {worst_n} times in {hours:g}h — "
                        "the seen-state is not being persisted")
    return problems, {"passes_failed": failed, "passes_completed": complete,
                      "dispatches": sum(disp.values()),
                      "distinct_prs_dispatched": len(disp),
                      "most_dispatched": worst, "most_dispatched_n": worst_n}


def check_judge_chain(hours: float = 24) -> tuple[list[str], dict]:
    """Is the qwen judgement chain still answering, and why did models die?

    intent.log had no reader. Two failures hide there. First, if every model is
    retired or cooling down, `_ask` returns None and every caller falls back to
    its own default -- claims are believed, feedback is treated as CODE -- which
    looks exactly like normal operation in every other log. Second, retirement
    is permanent, so the REASON matters: a 403 "Free quota exhausted" never comes
    back, but a TimeoutError is this machine's egress and probably will. Dropping
    a model for two timeouts throws away capacity that was never actually gone.

    Waiting for the chain to be EMPTY is waiting too long. On 2026-09-02 a
    single outage struck all four live models within the same second; two more
    such minutes and there would have been nothing left, and the first visible
    symptom would have been silently worse judgements, not an error. So this
    alerts on headroom, not on exhaustion.
    """
    lines = tail_since(ROOT / "intent.log", hours)
    nokey = sum(1 for l in lines if "NOKEY" in l)
    allfailed = sum(1 for l in lines if "ALLFAILED" in l)
    cooldowns = sum(1 for l in lines if " COOLDOWN " in l)
    creds = [l for l in lines if "CREDENTIAL" in l]
    problems, transient = [], []
    live = dead = []
    try:
        sys.path.insert(0, str(ROOT))
        import intent as _intent
        live = list(_intent.live_models())
        dead = sorted(_intent.dead_models())
        retired = json.loads((ROOT / "state" / "models-retired.json").read_text())
        transient = [m for m in dead
                     if re.search(r"Timeout|URLError|connection|reset|EOF",
                                  str(retired.get(m, {}).get("why", "")), re.I)]
    except Exception as e:  # noqa: BLE001
        problems.append(f"cannot read the judge chain state: {str(e)[:120]}")
        retired = {}
    MIN_LIVE = 3
    if not live:
        problems.append("every judgement model is retired or down — is_claim and "
                        "feedback_needs are returning their callers' defaults, "
                        "which no other log distinguishes from a real verdict")
    elif len(live) < MIN_LIVE:
        problems.append(f"only {len(live)} model(s) left on the chain ({', '.join(live)}) "
                        f"— below the {MIN_LIVE} needed to survive one outage, and the "
                        "chain emptying is silent at every call site")
    if creds:
        problems.append(f"{len(creds)} credential failure(s) in {hours:g}h — the key is "
                        "rejected, and no model is at fault: " + creds[-1][-90:].strip())
    if nokey:
        problems.append(f"{nokey} judgement(s) in {hours:g}h ran with no API key")
    if transient:
        problems.append(f"retired on a transient error, not quota: {', '.join(transient)} "
                        "— these are recoverable and were dropped permanently")
    # A chain that fails end to end is evidence about the network. It is not a
    # fault on its own, but a sustained rate means every caller is quietly
    # running on its own default.
    if len(lines) >= 20 and allfailed / max(1, len(lines)) > 0.15:
        problems.append(f"{allfailed} of {len(lines)} judgements in {hours:g}h failed on "
                        "every live model — callers are falling back to defaults")
    return problems, {"live": live, "n_live": len(live), "min_live": MIN_LIVE,
                      "dead": dead, "retired_on_transient": transient,
                      "credential_failures": len(creds), "allfailed_%gh" % hours: allfailed,
                      "cooldowns_%gh" % hours: cooldowns,
                      "nokey_%gh" % hours: nokey, "judgements_%gh" % hours: len(lines)}


def check_followups() -> tuple[list[str], dict]:
    """Dated reminders that must not depend on anyone remembering.

    A note in a memory file only surfaces if a session happens to load it. This
    is read every day by the health check, so a commitment made to a maintainer
    on one day is still visible on the day it comes due.
    """
    f = ROOT / "followups.json"
    if not f.exists():
        return [], {"due": 0, "items": []}
    try:
        items = json.loads(f.read_text()).get("items", [])
    except Exception:  # noqa: BLE001
        return ["followups.json is unreadable"], {"due": 0, "items": []}
    today = now().date().isoformat()
    due = [i for i in items if (i.get("due") or "9999") <= today]
    problems = [f"follow-up due: {i['what'][:180]}" for i in due]
    return problems, {"due": len(due), "items": [i.get("due") for i in items]}


def check_prs() -> tuple[list[str], dict]:
    """Open PRs: staleness, and the branch-base problem that failed adk's scan."""
    q = ('{search(type:ISSUE, first:50, query:"is:pr is:open author:%s"){nodes{'
         '... on PullRequest{number url updatedAt mergeable '
         'repository{nameWithOwner} '
         'commits(last:1){nodes{commit{statusCheckRollup{state}}}}}}}}' % ME)
    raw = sh(["gh", "api", "graphql", "-f", f"query={q}"], timeout=90)
    problems, detail = [], []
    if not raw:
        return ["could not query open PRs (network?)"], []
    try:
        nodes = json.loads(raw)["data"]["search"]["nodes"]
    except Exception:  # noqa: BLE001
        return ["open-PR query returned unexpected data"], []

    for pr in nodes:
        if not pr:
            continue
        repo = (pr.get("repository") or {}).get("nameWithOwner", "?")
        num = pr["number"]
        upd = pr.get("updatedAt", "")
        rollup = "?"
        try:
            rollup = ((pr["commits"]["nodes"][0]["commit"].get("statusCheckRollup")
                       or {}).get("state")) or "NONE"
        except Exception:  # noqa: BLE001
            pass
        age_d = 0.0
        if upd:
            age_d = (now() - datetime.fromisoformat(upd.replace("Z", "+00:00"))).days
        detail.append({"pr": f"{repo}#{num}", "checks": rollup,
                       "idle_days": age_d, "url": pr.get("url")})
        if rollup == "FAILURE":
            problems.append(f"{repo}#{num} has failing checks")
        if age_d >= 3:
            problems.append(f"{repo}#{num} untouched for {age_d}d — likely needs a human")
    return problems, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    checks = {
        "agents": check_agents(),
        "watcher": check_watcher_fresh(),
        "detect_vs_dispatch": check_detect_vs_dispatch(),
        "queues": check_queues(),
        "quota": check_quota(),
        "duplicates_30d": check_duplicates(),
        "agent_reachable": check_agent_reachable(),
        "session_waste": check_session_waste(),
        "throughput": check_merge_throughput(),
        "sweep_health": check_sweep_health(),
        "prwatch_health": check_prwatch_health(),
        "judge_chain": check_judge_chain(),
        "followups": check_followups(),
        "landed": check_landed(),
        # Soft, and reported separately on purpose: credit is not a commit.
        "credited": check_credited(),
        "open_prs": check_prs(),
    }
    problems = [p for probs, _ in checks.values() for p in probs]
    report = {
        "generated_at": now().isoformat(timespec="seconds"),
        "ok": not problems,
        "problems": problems,
        "detail": {k: d for k, (_, d) in checks.items()},
    }

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{now():%Y-%m-%d}.json").write_text(json.dumps(report, indent=2) + "\n")

    if a.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    d = report["detail"]
    print(f"OSS pipeline health — {report['generated_at']}")
    print(f"  agents      : {', '.join(f'{k}={v}' for k, v in d['agents'].items())}")
    w = d["watcher"]
    print(f"  watcher     : alive={w['process_alive']} lines/hr={w['lines_last_hour']}")
    dv = d["detect_vs_dispatch"]
    print(f"  24h flow    : accepted={dv['accepted_24h']} dispatched={dv['dispatched_24h']} "
          f"claimed={dv['claimed_24h']} budget-held={dv['budget_held_24h']}")
    print(f"  quota       : session-limit hits={d['quota']['session_limit_hits_24h']}")
    live = [q for q in d["queues"].values() if q["candidates"]]
    print(f"  queues      : {len(live)} with work, "
          f"{sum(1 for q in d['queues'].values() if q['partial'])} partial")
    print(f"  dup 30d     : " + ", ".join(f"{k.split('/')[-1]}={v}" for k, v in d["duplicates_30d"].items()))
    ar = d["agent_reachable"]
    print(f"  agent 可达  : 24h 完成 {ar['completed_24h']} 失败 {ar['failed_24h']} "
          f"认证失败样本 {ar['auth_failures']}" + (f" [{ar['which']}]" if ar.get("which") else ""))
    w = d["session_waste"]
    print(f"  sessions 24h: dispatched={w['dispatched']} spent={w['sessions_spent']} "
          f"saved-by-prevet={w['prevet_saved']} -> PRs={w['prs']}")
    tp = d["throughput"]
    print(f"  throughput  : open={tp['open']} merged 24h/7d/all={tp['merged_24h']}/{tp['merged_7d']}/{tp['merged_total']} "
          f"opened24h={tp['opened_24h']} closed-unmerged={tp['closed_unmerged']}")
    print(f"                waiting-on-them={tp['awaiting_them']}/{tp['open']} oldest-open={tp['oldest_open_days']}d")
    sw = d["sweep_health"]
    print(f"  sweep 健康  : 24h 失败 {sw['sweep_failures_24h']} 次 "
          f"(网络 {sw['network']}, 其他 {sw['other']})")
    fu = d["followups"]
    if fu["items"]:
        print(f"  follow-ups : {fu['due']} due today, scheduled {', '.join(str(x) for x in fu['items'])}")
    ld = d["landed"]
    print(f"  landed      : {ld['total']} commit(s) on upstream default branches — "
          + ", ".join(f"{k.split('/')[-1]}={v['total']}" for k, v in ld["by_repo"].items()))
    print(f"  open PRs    : {len(d['open_prs'])}")
    for p in d["open_prs"]:
        print(f"      {p['pr']:<34} checks={p['checks']:<8} idle={p['idle_days']}d")

    if problems:
        print(f"\n  {len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"    - {p}")
    else:
        print("\n  no problems detected")
    return 0 if report["ok"] else 1



def update_landings() -> None:
    """Keep the durable landing ledger current, incrementally.

    health.py already runs on a schedule, so the ledger stays fresh without a
    new launchd job. Only commits newer than the newest recorded are fetched,
    which is the whole point — the count is never rebuilt from scratch again.
    """
    try:
        import landings
        n, total = landings.update()
        print(f"  landings   : ledger +{n}, {total} total on upstream default branches")
    except Exception as e:  # noqa: BLE001
        print(f"  landings   : ledger update failed ({str(e)[:90]})")

if __name__ == "__main__":
    update_landings()
    sys.exit(main())
