#!/usr/bin/env python3
"""Record what we dispatched, what a cheap judge predicted, and what happened.

Written to answer one question with evidence instead of argument: does a
four-second read of an issue predict what a thirty-minute Claude session
concludes about it?

Of the fix-one runs sampled on 2026-08-16, 40% of the sessions where the
generator actually ran produced no branch at all. Claude read the repository,
worked, and gave up. Those sessions are the single largest sink of Claude time
we have. If a flash model can tell in advance which issues they will be, the
saving is large; if it cannot, the idea should be dropped rather than tuned.

Neither answer was available from history. The `run-name` that ties a run to
its issue was added days ago, and before it a run that ended without a PR was
indistinguishable from one that never started work: 36% of dispatches are
turned away by the re-vet inside fix-one, and those are not the generator's
failures. Filtering to clean labels left 20 examples, of which 6 were negative.

So the scoring runs in SHADOW: it is written down and it changes nothing. At
roughly 55 dispatches a day the ledger reaches a few hundred labelled examples
in a week, which is enough to see whether the scores separate the two outcomes.
Only then is there anything to decide.

Deliberately not done here:

  * No thresholds. A cutoff written before the data exists is the thing this
    file was built to avoid.
  * No blocking. `record()` does no network at all, so a dispatch cannot be
    slowed or failed by the judge. Scoring and reconciliation run afterwards.
  * Reconciliation reads the outcome from GitHub rather than from the runner,
    because the runner is ephemeral and the interesting failure — a session
    that produced nothing — leaves nothing behind on it.

Usage:
    ledger.record(key, number)      # at dispatch, cheap and offline
    ledger.score_pending(limit=20)  # end of a pass: ask the judge
    ledger.reconcile()              # end of a pass: fill in what happened
    python3 ledger.py               # print the current separation
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import scan

LEDGER = scan.STATE / "triage-ledger.json"

# A dispatched issue gets this long to turn into a PR before the absence of one
# counts as a real negative. fix-one queues behind a concurrency group and can
# wait hours; calling an issue a failure while its run is still queued would
# poison the labels in exactly the direction that makes the judge look good.
SETTLE_HOURS = 12

SYSTEM = (
    "You are estimating how likely it is that an autonomous coding agent can fix "
    "a GitHub issue, working from the issue text and the repository alone, in a "
    "single session of about thirty minutes, without asking anyone anything.\n\n"
    "Score 0 to 5:\n"
    "5 — the issue names the file or function and the wanted behaviour is stated\n"
    "4 — a clear, small, self-contained defect with enough detail to locate it\n"
    "3 — plausible but the agent must first find where the behaviour lives\n"
    "2 — vague, broad, or needs a judgement call about intended design\n"
    "1 — needs reproduction on hardware, an account, or data we do not have\n"
    "0 — a question, a support request, a feature debate, or no actionable ask\n\n"
    "Judge only what the text supports. Do not reward length or politeness.\n"
    'Reply with JSON only: {"score": 0-5, "why": "<12 words or fewer>"}'
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        return json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"  ledger save failed: {e}", file=sys.stderr)


def record(key: str, number: int) -> None:
    """Note a dispatch. No network, no judgement — this sits in the hot path."""
    d = _load()
    k = f"{key}#{number}"
    if k in d:
        return
    d[k] = {"at": _now(), "score": None, "why": None, "pr": None, "settled": False}
    _save(d)


def score_pending(limit: int = 20) -> int:
    """Ask the judge about dispatches it has not seen yet. Shadow only."""
    import intent

    d = _load()
    todo = [k for k, v in d.items() if v.get("score") is None][:limit]
    done = 0
    for k in todo:
        key, _, num = k.rpartition("#")
        cfg = scan.REPOS.get(key)
        if not cfg:
            d[k]["score"] = -1
            d[k]["why"] = "repo no longer configured"
            continue
        try:
            raw = scan.gh(["api", "-X", "GET", f"repos/{cfg['upstream']}/issues/{num}",
                           "--jq", "{t:.title, b:.body}"])
            issue = json.loads(raw or "{}")
        except Exception as e:  # noqa: BLE001
            print(f"  ledger: cannot read {k}: {str(e)[:80]}", file=sys.stderr)
            continue
        text = f"{issue.get('t') or ''}\n\n{(issue.get('b') or '')[:4000]}"
        verdict = intent._ask(SYSTEM, text, author=k)
        if verdict is None:
            continue                      # try again next pass
        try:
            d[k]["score"] = int(verdict["score"])
            d[k]["why"] = str(verdict.get("why", ""))[:60]
        except Exception:  # noqa: BLE001
            continue
        done += 1
    if done:
        _save(d)
    return done


def _our_pr_issues() -> dict[str, set[int]]:
    """Issue numbers our open or closed PRs reference, per upstream repo."""
    out: dict[str, set[int]] = {}
    try:
        raw = scan.gh(["search", "prs", "--author", "chelsealong", "--limit", "400",
                       "--json", "body,title,repository"])
        for p in json.loads(raw or "[]"):
            repo = (p.get("repository") or {}).get("nameWithOwner", "")
            blob = f"{p.get('title') or ''} {p.get('body') or ''}"
            for n in re.findall(r"#(\d{2,7})", blob):
                out.setdefault(repo, set()).add(int(n))
    except Exception as e:  # noqa: BLE001
        print(f"  ledger: PR search failed: {str(e)[:100]}", file=sys.stderr)
    return out


def reconcile() -> int:
    """Fill in the outcome for dispatches old enough to have settled."""
    d = _load()
    pending = [k for k, v in d.items() if not v.get("settled")]
    if not pending:
        return 0
    seen = _our_pr_issues()
    if not seen:
        return 0                          # search failed; do not label blindly
    now = datetime.now(timezone.utc)
    changed = 0
    for k in pending:
        key, _, num = k.rpartition("#")
        cfg = scan.REPOS.get(key)
        if not cfg:
            continue
        got = int(num) in seen.get(cfg["upstream"], set())
        if got:
            d[k]["pr"] = True
            d[k]["settled"] = True
            changed += 1
            continue
        try:
            age = (now - datetime.fromisoformat(d[k]["at"])).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            continue
        if age >= SETTLE_HOURS:
            d[k]["pr"] = False
            d[k]["settled"] = True
            changed += 1
    if changed:
        _save(d)
    return changed


def report() -> None:
    d = _load()
    rows = [v for v in d.values()
            if v.get("settled") and isinstance(v.get("score"), int) and v["score"] >= 0]
    print(f"  ledger: {len(d)} dispatch(es) recorded, {len(rows)} settled and scored")
    if not rows:
        print("  nothing to compare yet — the ledger needs settled outcomes")
        return
    by = {}
    for v in rows:
        s = by.setdefault(v["score"], [0, 0])
        s[0] += 1
        s[1] += 1 if v["pr"] else 0
    print("\n  score   n   opened a PR")
    for s in sorted(by, reverse=True):
        n, hit = by[s]
        print(f"    {s}   {n:>3}   {hit:>3}  ({hit / n * 100:>3.0f}%)")
    lo = [v["pr"] for v in rows if v["score"] <= 2]
    hi = [v["pr"] for v in rows if v["score"] >= 4]
    if lo and hi:
        print(f"\n  score<=2: {sum(lo)}/{len(lo)} opened a PR ({sum(lo) / len(lo) * 100:.0f}%)")
        print(f"  score>=4: {sum(hi)}/{len(hi)} opened a PR ({sum(hi) / len(hi) * 100:.0f}%)")
        print("\n  A gap here is the case for gating. No gap is the case for deleting this.")
    else:
        print("\n  not enough spread yet to compare the ends")


if __name__ == "__main__":
    if "--score" in sys.argv:
        print(f"  scored {score_pending(limit=int(sys.argv[-1]) if sys.argv[-1].isdigit() else 20)}")
    if "--reconcile" in sys.argv:
        print(f"  settled {reconcile()}")
    report()
