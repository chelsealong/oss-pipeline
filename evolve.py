#!/usr/bin/env python3
"""Re-measure the per-repo constants this pipeline runs on, weekly.

Every number that governs behaviour — how long to keep answering reviewers on a
PR, how large a patch may be, how long to wait before touching a fresh issue —
was measured once, by hand, on the day someone noticed it mattered, and then
frozen in source. Repos change. openclaw's merge window was set from a biased
sample and was wrong by a factor of sixty; ComfyUI's was 7 days when its p90 was
8.8. Both were caught by accident.

So this measures them again, from current data, and reports the drift. It does
NOT apply anything. Silent self-modification is the failure mode to avoid here:
a system that rewrites its own operating parameters without an audit trail
cannot be debugged when it gets worse, and "it got worse" is exactly the case
that matters. The output is a diff to read and a reason for each number.

Three findings from the literature shaped this, and are worth keeping in view:

  * Darwin Gödel Machine (arXiv 2505.22954) evolves an agent against SWE-bench
    and names benchmark overfitting as its main limitation. "Govern the
    Repository, Not the Agent" (arXiv 2606.28235) argues the opposite: assess an
    agent INSIDE the target repository, because agent contributions concentrate
    friction there rather than in a benchmark. Every number here is per-repo for
    that reason.
  * "Why Are Agentic Pull Requests Merged or Rejected?" (arXiv 2605.22534),
    over 930k agentic PRs, found the single largest rejection category is
    UNKNOWN at 38.8% — closed with no stated reason. A loop that learns only
    from what maintainers write down learns nothing four times in ten. So the
    fitness signal here is the OUTCOME (did a commit of ours reach the default
    branch), not the explanation.
  * The sampling trap is already recorded in lessons/_common.md and is repeated
    in code below, because it cost a real landing: sample by MERGE time, never
    by creation time. Sampling newest-created merged PRs selects for the ones
    that merged fast, since the slow ones have not merged yet and cannot appear.

    python3 evolve.py                  # measure everything, print the report
    python3 evolve.py --repo hermes    # one repo
    python3 evolve.py --json           # machine-readable, for a weekly job
"""

from __future__ import annotations

import json
import pathlib
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone

import scan

BOT = re.compile(
    r"bot|coderabbit|cubic|greptile|codecov|codspeed|cla|clawsweeper|"
    r"gemini-code|dependabot|renovate|vercel|github-actions|sonarcloud",
    re.I,
)
HISTORY = scan.STATE / "evolve-history.json"
# Enough PRs that a percentile means something; small enough to stay inside the
# search budget when walking fifteen repos.
SAMPLE = 100


def _gh(args: list[str]) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return r.stdout


def _hours(a: str, b: str) -> float:
    return (datetime.fromisoformat(b.replace("Z", "+00:00"))
            - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 3600


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def external_merges(upstream: str) -> list[dict]:
    """Recent merged PRs from outside contributors, sampled by MERGE time.

    `sort:updated-desc` on `is:merged` approximates merge order. Sorting by
    creation date instead would select for PRs that merged quickly — the slow
    ones are still open and cannot appear — and that bias put openclaw's
    slowest observed merge at 4h when its p90 is 140h.
    """
    # Plain %-formatting, not an f-string. The f-string version needed doubled
    # braces for every GraphQL block and one pair was wrong, which GitHub
    # answered with "Expected one of SCHEMA, SCALAR, TYPE..." — a parse error
    # that says nothing about which brace.
    q = ('{search(query:"repo:%s is:pr is:merged sort:updated-desc", '
         "type:ISSUE, first:%d){nodes{... on PullRequest{"
         "number createdAt mergedAt additions authorAssociation author{login} "
         "reviews(last:10){nodes{author{login} state}}}}}}" % (upstream, SAMPLE))
    try:
        data = json.loads(_gh(["api", "graphql", "-f", f"query={q}"]))
        nodes = data["data"]["search"]["nodes"] or []
    except Exception as e:  # noqa: BLE001
        print(f"  {upstream}: merge sample failed ({str(e)[:70]})", file=sys.stderr)
        return []
    out = []
    for pr in nodes:
        if not pr or not pr.get("mergedAt") or not pr.get("number"):
            continue
        if pr.get("authorAssociation") not in ("NONE", "CONTRIBUTOR",
                                               "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"):
            continue
        login = (pr.get("author") or {}).get("login") or ""
        if BOT.search(login):
            continue
        approvals = [r["author"]["login"] for r in (pr.get("reviews") or {}).get("nodes", [])
                     if r and r.get("state") == "APPROVED" and r.get("author")
                     and not BOT.search(r["author"]["login"])]
        out.append({
            "number": pr["number"],
            "hours": _hours(pr["createdAt"], pr["mergedAt"]),
            "additions": pr.get("additions") or 0,
            "human_approved": bool(approvals),
        })
    return out


def our_landing_latency(upstream: str) -> list[float]:
    """Hours from OUR PR opening to OUR commit reaching the default branch.

    This exists because the first run of this tool proposed cutting hermes from
    240h to 24h, which would have re-broken something fixed days earlier. Its
    external PRs really do merge inside 4 hours — but our work does not arrive
    that way there. A maintainer cherry-picks our commits into their own PR, and
    those salvages landed at 16h, 151h, 3 days and 5 days. The repo's merge
    latency describes a population we are not in.
    adk is the same shape for a different reason: barely any external PR merges
    at all, because Copybara imports the change and closes the PR.
    So measure OUR path first, and only fall back to the repo's when there is
    not enough of it. Measuring the wrong population is the mistake this whole
    file is supposed to stop making.
    """
    import landings
    led = landings._load()
    commits = [v for v in (led.get("commits") or {}).values() if v["repo"] == upstream]
    if len(commits) < 5:
        return []
    # Match each landed commit to the newest PR of ours opened before it. Exact
    # PR-to-commit attribution is not available for salvage — the commit arrives
    # under the maintainer's PR — so this is an upper bound on latency, which is
    # the safe direction for a window.
    try:
        raw = _gh(["search", "prs", "--author", "chelsealong", "--repo", upstream,
                   "--limit", "200", "--json", "number,createdAt"])
        prs = sorted(json.loads(raw or "[]"), key=lambda p: p["createdAt"])
    except Exception:  # noqa: BLE001
        return []
    if not prs:
        return []
    out = []
    for c in commits:
        landed = c["at"]
        before = [p for p in prs if p["createdAt"] <= landed]
        if not before:
            continue
        try:
            out.append(_hours(before[-1]["createdAt"], landed))
        except Exception:  # noqa: BLE001
            continue
    return [h for h in out if h >= 0]


def our_outcomes(repo_key: str, upstream: str) -> dict:
    """Our own acceptance in this repo — the fitness signal that matters.

    Landed commits, not merged PRs. Two of the highest-yield repos never show a
    merged PR of ours: adk imports through Copybara and closes the PR, hermes
    maintainers cherry-pick our commits into their own. Counting merges reports
    near-zero for both.
    """
    import landings
    led = landings._load()
    commits = [v for v in (led.get("commits") or {}).values() if v["repo"] == upstream]
    prs = (led.get("prs") or {}).get(upstream) or {}
    total = prs.get("total")
    return {
        "landed": len(commits),
        "prs": total,
        "conversion": round(len(commits) / total, 3) if total else None,
        "last_landing": max((c["at"][:10] for c in commits), default=None),
    }


def measure(repo_key: str) -> dict:
    cfg = scan.REPOS.get(repo_key)
    if not cfg:
        return {}
    upstream = cfg["upstream"]
    merges = external_merges(upstream)
    ours = our_landing_latency(upstream)
    # Our own path when we have enough of it; the repo's otherwise.
    hours = ours if len(ours) >= 5 else [m["hours"] for m in merges]
    latency_source = "ours" if len(ours) >= 5 else "repo"
    adds = [m["additions"] for m in merges]
    approved = [m for m in merges if m["human_approved"]]
    return {
        "repo": repo_key,
        "upstream": upstream,
        "sample": len(merges),
        "latency_from": latency_source,
        "latency_n": len(hours),
        # Window: past p90 of how long an external PR actually takes to land.
        # Skipping a live PR costs a real chance; answering a dead one costs one
        # session, so the margin goes upward.
        "merge_p50_h": round(_pct(hours, 0.50), 1),
        "merge_p90_h": round(_pct(hours, 0.90), 1),
        "merge_max_h": round(max(hours), 1) if hours else 0,
        # Size: what this repo actually merges from outsiders. Ours should sit at
        # the small end of that, not at its median.
        "adds_p50": int(_pct(adds, 0.50)),
        "adds_p75": int(_pct(adds, 0.75)),
        # Whether a human approval is effectively required. Where it is,
        # bot approvals mean nothing and volume will not help.
        "human_approval_rate": round(len(approved) / len(merges), 2) if merges else None,
        "ours": our_outcomes(repo_key, upstream),
    }


def current_constants() -> dict:
    """What the pipeline is running on right now, read from source."""
    import importlib.util
    root = pathlib.Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("wp", root / "watch-prs.py")
    wp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wp)
    wf = ""
    for cand in (root / "fix-one.yml",
                 root.parent / "oss-pipeline" / ".github/workflows/fix-one.yml"):
        if cand.is_file():
            wf = cand.read_text()
            break
    sizes = {k: int(v) for k, v in re.findall(r"^\s+([a-z0-9-]+)\)\s+max_added=(\d+)", wf, re.M)}
    caps: dict = {}
    for keys, v in re.findall(r"^\s+([a-z0-9|-]+)\)\s+cap=(\d+)", wf, re.M):
        for k in keys.split("|"):
            caps[k] = int(v)
    return {"windows": dict(wp.MERGE_WINDOW_HOURS), "sizes": sizes, "caps": caps}


def report(keys: list[str], as_json: bool = False) -> dict:
    cur = current_constants()
    rows = []
    for k in keys:
        m = measure(k)
        if not m or not m["sample"]:
            continue
        up = m["upstream"]
        win_now = cur["windows"].get(up)
        # Round up to whole days past p90, because a window is checked in hours
        # against a PR's age and a fractional boundary reads as arbitrary.
        win_should = max(24, int((m["merge_p90_h"] // 24 + 1) * 24))
        size_now = cur["sizes"].get(k)
        m["drift"] = {
            "window_now": win_now, "window_measured": win_should,
            "window_off": (win_now is not None and abs(win_now - win_should) >= 48),
            "size_now": size_now,
            # Their p50 is what a typical accepted outside patch looks like;
            # aiming at it rather than under it is how openclaw's +421 got read
            # as a risk instead of a fix.
            "size_measured": m["adds_p50"],
            "size_off": (size_now is not None and size_now > m["adds_p50"] * 1.5),
            "needs_human_approval": (m["human_approval_rate"] or 0) >= 0.8,
        }
        rows.append(m)

    hist = {}
    try:
        hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    except Exception:  # noqa: BLE001
        hist = {}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hist[stamp] = {r["repo"]: {k: r[k] for k in
                               ("merge_p90_h", "adds_p50", "human_approval_rate")} for r in rows}
    # Bounded: the last 26 weeks is more history than any decision needs.
    if len(hist) > 26:
        hist = dict(sorted(hist.items())[-26:])
    try:
        scan.STATE.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(hist, indent=1) + "\n")
    except Exception:  # noqa: BLE001
        pass

    out = {"at": stamp, "repos": rows}
    if as_json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return out

    print(f"\n  measured {stamp[:16]} — nothing here is applied automatically\n")
    print(f"  {'repo':<16}{'n':>4}{'p50h':>7}{'p90h':>7}{'src':>5}{'+p50':>6}"
          f"{'human':>7}{'landed':>7}  drift")
    print("  " + "-" * 78)
    for r in rows:
        d = r["drift"]
        flags = []
        if d["window_off"]:
            flags.append(f"window {d['window_now']}h -> {d['window_measured']}h")
        if d["size_off"]:
            flags.append(f"size {d['size_now']} -> {d['size_measured']}")
        print(f"  {r['repo']:<16}{r['sample']:>4}{r['merge_p50_h']:>7.0f}"
              f"{r['merge_p90_h']:>7.0f}{r['latency_from']:>5}{r['adds_p50']:>6}"
              f"{(r['human_approval_rate'] or 0):>7.0%}{r['ours']['landed']:>7}"
              f"  {'; '.join(flags)}")
    print()
    gated = [r["repo"] for r in rows if r["drift"]["needs_human_approval"]]
    if gated:
        print(f"  human approval effectively required ({len(gated)}): {', '.join(gated)}")
        print("    In these, a bot's approval is not a signal and volume does not help.")
    drifted = [r for r in rows if r["drift"]["window_off"] or r["drift"]["size_off"]]
    if drifted:
        print(f"\n  {len(drifted)} repo(s) drifted from what is in source. To apply, edit")
        print("  MERGE_WINDOW_HOURS in watch-prs.py and max_added in fix-one.yml,")
        print("  then run verify.sh — checks 14 and 21 assert these against measurement.")
    else:
        print("  no constant has drifted enough to be worth changing")
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    keys = list(scan.REPOS)
    if "--repo" in args:
        keys = [args[args.index("--repo") + 1]]
    keys = [k for k in keys if not scan.REPOS[k].get("paused")]
    report(keys, as_json="--json" in args)
