#!/usr/bin/env python3
"""A durable record of what has landed, so it is never recounted from scratch.

Every "how many have landed" question so far was answered by re-querying GitHub
from the beginning, and that recount went wrong in three different ways:

  * `search/commits` returns matches in FORKS. Other people's forks of
    hermes-agent carry our commits, and an early count included them.
  * It also caps results. A per-repo total of 20 was reported when the real
    figure was 28, because a single unpaginated query silently truncated.
  * `merged` on our PR is not the same thing as landed. hermes lands 11 commits
    against 1 merged PR (a maintainer cherry-picks into their own PR); adk lands
    via Copybara with the PR CLOSED. Counting merges reports near-zero for both.

So the ledger stores one row per commit, each verified to be an ancestor of the
upstream default branch, and updates incrementally: only commits newer than the
newest one already recorded are fetched. Recounting is no longer the mechanism.

    python3 landings.py            # update, then print the table
    python3 landings.py --report   # print what is stored, no network
    python3 landings.py --rebuild  # full re-scan (rarely needed)

`notes` on a row is for the cases where "landed" needs a caveat — adk#6686 is
the one so far: our test landed under our authorship with its assertion
inverted, while the source fix it accompanied was rejected. The commit is ours
and it is on main, so it counts; pretending the whole PR was accepted would not
be true.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import scan

LEDGER = scan.STATE / "landings.json"
ME_EMAILS = ["chelsealong@126.com", "jialongli001@gmail.com"]


def _gh(args: list[str], *, tries: int = 4, **kw) -> str:
    """One gh call, retried through rate limiting.

    The search bucket is 30 per minute and this walks fifteen repos with two
    author identities each — exactly 30 searches before a single compare call.
    The first build exhausted it and three repos came back empty, which is how
    hermes reported 0 PRs and 0 commits while holding 56 and 11.
    """
    import time
    delay = 20.0
    for attempt in range(1, tries + 1):
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=300, **kw)
        if r.returncode == 0:
            return r.stdout
        err = r.stderr.strip()
        if attempt < tries and ("rate limit" in err.lower() or "was submitted too quickly" in err.lower()
                                or "403" in err or "secondary" in err.lower()):
            print(f"    rate limited; waiting {delay:.0f}s ({attempt}/{tries - 1})", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(err[:200])
    raise RuntimeError("exhausted retries")


def _load() -> dict:
    if not LEDGER.exists():
        return {"commits": {}, "prs": {}, "updated": None}
    try:
        d = json.loads(LEDGER.read_text())
        d.setdefault("commits", {})
        d.setdefault("prs", {})
        return d
    except Exception:  # noqa: BLE001
        return {"commits": {}, "prs": {}, "updated": None}


def _save(d: dict) -> None:
    d["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scan.STATE.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n")


def upstreams() -> list[str]:
    seen, out = set(), []
    for cfg in scan.REPOS.values():
        u = cfg["upstream"]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def on_default_branch(repo: str, sha: str) -> bool:
    """Is this commit an ancestor of the upstream default branch?

    The only trustworthy test. `compare/HEAD...<sha>` answers "behind" when the
    commit is already in HEAD's history, which is exactly what landed means.
    A commit sitting on our fork's branch answers "diverged" — that distinction
    is what stopped four fork-only commits being reported as adk landings.
    """
    try:
        out = _gh(["api", f"repos/{repo}/compare/HEAD...{sha}", "--jq", ".status"])
    except Exception:  # noqa: BLE001
        return False
    return out.strip() == "behind"


def fetch_new(repo: str, since: str | None) -> list[dict]:
    """Commits of ours in this repo, newer than `since`, verified on main."""
    found: dict[str, dict] = {}
    for email in ME_EMAILS:
        q = f"repo:{repo}+author-email:{email.replace('@', '%40')}"
        if since:
            q += f"+committer-date:>{since}"
        try:
            raw = _gh(["api", f"search/commits?q={q}&sort=committer-date&order=desc"
                              "&per_page=100", "--paginate",
                       "--jq", '.items[]? | {sha:.sha, at:.commit.committer.date, '
                               'msg:(.commit.message|split("\\n")[0]), '
                               'repo:.repository.full_name}'])
        except Exception as e:  # noqa: BLE001
            print(f"  {repo}: search failed ({str(e)[:70]})", file=sys.stderr)
            continue
        for line in raw.splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            # Other people's forks carry our commits and the search returns them.
            if c["repo"] != repo:
                continue
            found.setdefault(c["sha"], c)
    out = []
    for sha, c in found.items():
        if on_default_branch(repo, sha):
            out.append(c)
    return out


def pr_counts(repo: str) -> dict:
    try:
        raw = _gh(["search", "prs", "--author", "chelsealong", "--repo", repo,
                   "--limit", "200", "--json", "number,state"])
        prs = json.loads(raw or "[]")
    except Exception as e:  # noqa: BLE001
        # Visibly, not silently. hermes reported 0 PRs and 0 commits in the
        # first build because its calls timed out and both failures returned
        # empty; the table looked complete and was missing 56 PRs and 11
        # commits.
        print(f"  {repo}: PR count failed ({str(e)[:70]})", file=sys.stderr)
        return {"total": None}
    # gh returns these lower-case. Comparing against "OPEN" reported every
    # repo as having zero open PRs while the totals were right — a wrong number
    # that looks plausible is worse than a missing one.
    st = [str(p.get("state", "")).lower() for p in prs]
    return {
        "total": len(prs),
        "open": st.count("open"),
        "merged": st.count("merged"),
        "closed": st.count("closed"),
    }


def update(rebuild: bool = False) -> tuple[int, int]:
    d = _load()
    if rebuild:
        d["commits"] = {}
    added = 0
    for repo in upstreams():
        rows = [v for v in d["commits"].values() if v["repo"] == repo]
        since = max((v["at"] for v in rows), default=None) if rows and not rebuild else None
        for c in fetch_new(repo, since):
            key = f"{repo}@{c['sha'][:10]}"
            if key in d["commits"]:
                continue
            d["commits"][key] = {"repo": repo, "sha": c["sha"][:10],
                                 "at": c["at"], "msg": c["msg"][:90], "notes": ""}
            added += 1
            print(f"  + {repo} {c['sha'][:10]} {c['at'][:10]} {c['msg'][:56]}")
        d["prs"][repo] = pr_counts(repo)
    _save(d)
    return added, len(d["commits"])


def report() -> None:
    d = _load()
    by: dict[str, list] = {}
    for v in d["commits"].values():
        by.setdefault(v["repo"], []).append(v)
    print(f"\n  landings ledger — updated {d.get('updated') or 'never'}\n")
    print(f"  {'upstream':<30}{'PR':>4}{'开着':>6}{'主干提交':>8}   最近落地")
    print("  " + "-" * 74)
    TP = TO = TC = 0
    order = sorted(upstreams(), key=lambda r: -len(by.get(r, [])))
    for repo in order:
        rows = sorted(by.get(repo, []), key=lambda v: v["at"])
        p = d["prs"].get(repo) or {}
        tot, op = p.get("total"), p.get("open")
        TP += tot or 0
        TO += op or 0
        TC += len(rows)
        last = rows[-1]["at"][:10] if rows else "—"
        print(f"  {repo:<30}{('?' if tot is None else tot):>4}"
              f"{('?' if op is None else op):>6}{len(rows):>8}   {last}")
    print("  " + "-" * 74)
    print(f"  {'合计':<30}{TP:>4}{TO:>6}{TC:>8}")
    noted = [v for v in d["commits"].values() if v.get("notes")]
    if noted:
        print("\n  带备注的:")
        for v in noted:
            print(f"    {v['repo']} {v['sha']} — {v['notes']}")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        n, total = update(rebuild="--rebuild" in sys.argv)
        print(f"\n  {n} new, {total} total")
        report()
