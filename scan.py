#!/usr/bin/env python3
"""Pre-scan tracked OSS repos for genuinely unclaimed, well-scoped issues.

Runs cheap and often (API only, no clones, no LLM) so the expensive fix job can
skip triage entirely and just pop a vetted candidate.

    ./scan.py                 # scan all repos, write queue/
    ./scan.py --repo langfuse # scan one
    ./scan.py --pop adk       # print the best unclaimed candidate as JSON
    ./scan.py --pop adk --claim   # ...and mark it claimed so a second run skips it

Vetting mirrors the rules already encoded in the routine prompts: unassigned,
no linked PR by three independent signals, not swarmed, no excluded labels.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
STATE = ROOT / "state"

# Per-repo rules. `search` fragments are GitHub issue-search qualifiers.
REPOS: dict[str, dict] = {
    "adk": {
        "upstream": "google/adk-python",
        "searches": ['label:"good first issue"', 'label:"help wanted"', "sort:created-desc"],
        "exclude_labels": {"spam", "needs review"},
        "exclude_title": r"^\s*(question|how do i|help)\b",
    },
    "langfuse": {
        "upstream": "langfuse/langfuse",
        "searches": ['label:"good first issue"', "label:bug sort:created-desc"],
        "exclude_labels": set(),
        # issues whose fix lives in a different repo
        "exclude_labels_scope": {"sdk-python", "sdk-js"},
    },
    "langfuse-python": {
        "upstream": "langfuse/langfuse",  # tracker lives here, fix lands in langfuse-python
        "searches": ["label:sdk-python", "label:integration-langchain", "label:integration-openai"],
        "exclude_labels": set(),
        "implements_in": "langfuse/langfuse-python",
    },
    "spec-kit": {
        "upstream": "github/spec-kit",
        "searches": ["label:bug sort:created-desc", "sort:created-desc"],
        "exclude_labels": set(),
        # slash-command changes need manual agent-testing evidence pasted into the PR;
        # matched narrowly so ordinary CLI bugs that merely mention templates still qualify
        "exclude_title": r"(/speckit|slash[- ]command)",
    },
    "gemini-cli": {
        "upstream": "google-gemini/gemini-cli",
        "searches": ['label:"help wanted" label:effort/small', 'label:"help wanted"'],
        "exclude_labels": {"🔒Maintainers only"},
        "needs_assignment": True,  # PR without prior assignment is auto-closed
    },
    "langgraph": {
        "upstream": "langchain-ai/langgraph",
        "searches": ['label:"help wanted"', "label:bug label:external"],
        "exclude_labels": {"open-swe", "open-swe-auto", "open-swe-dev", "open-swe-max", "codex"},
        "needs_assignment": True,
    },
    "langchain": {
        "upstream": "langchain-ai/langchain",
        "searches": ['label:"help wanted"', "label:new-contributor"],
        "exclude_labels": {"open-swe", "codex"},
        "needs_assignment": True,
    },
    "openclaw": {
        "upstream": "openclaw/openclaw",
        "searches": ["label:clawsweeper:queueable-fix", "sort:created-desc"],
        "exclude_labels": {
            "clawsweeper:no-new-fix-pr",
            "clawsweeper:linked-pr-open",
            "clawsweeper:needs-maintainer-review",
            "clawsweeper:needs-product-decision",
            "clawsweeper:needs-security-review",
            "clawsweeper:needs-info",
            "r: spam",
        },
    },
    "hermes": {
        "upstream": "NousResearch/hermes-agent",
        "searches": ["label:type/bug sort:created-desc", "sort:created-desc"],
        "exclude_labels": {"invalid", "needs-repro", "needs-decision", "duplicate"},
        "exclude_label_prefix": "sweeper:",
    },
    "firecrawl": {
        "upstream": "firecrawl/firecrawl",
        "searches": ["label:bug sort:created-desc", "sort:created-desc"],
        "exclude_labels": set(),
    },
    "comfyui": {
        "upstream": "Comfy-Org/ComfyUI",
        "searches": ["sort:created-desc"],
        "exclude_labels": set(),
        # only CPU-verifiable areas are in scope for our pipeline
        "require_body": r"(Traceback|Error|Exception|folder_paths|execution\.py|server\.py|cli_args)",
    },
}

CLAIM_PHRASES = re.compile(
    r"(i'?ll (take|work on|fix|pick)|working on (this|it)|assign (this )?to me|/assign"
    r"|i have a (fix|pr)|opened a pr|raised a pr|pr is up|submitted a pr|on it\b)",
    re.I,
)


class RateLimited(RuntimeError):
    """GitHub secondary rate limit — retryable after a pause."""


# GitHub's search API allows ~30 req/min and enforces a stricter burst ("secondary")
# limit; other REST endpoints are far more generous. Pace each class separately.
_LAST_CALL: dict[str, float] = {}
_MIN_GAP = {"search": 2.5, "other": 0.35}


def _pace(kind: str) -> None:
    import time

    gap = _MIN_GAP[kind]
    prev = _LAST_CALL.get(kind, 0.0)
    wait = gap - (time.monotonic() - prev)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[kind] = time.monotonic()


def gh(args: list[str], timeout: int = 60, kind: str = "other", retries: int = 3) -> str:
    import time

    for attempt in range(retries + 1):
        _pace(kind)
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        err = r.stderr.strip()
        if "secondary rate limit" in err.lower() or "rate limit" in err.lower():
            if attempt == retries:
                raise RateLimited(err[:200])
            back = 45 * (attempt + 1)
            print(f"    rate limited; sleeping {back}s (attempt {attempt + 1}/{retries})",
                  file=sys.stderr)
            time.sleep(back)
            continue
        raise RuntimeError(err[:400])
    raise RateLimited("exhausted retries")


def search_issues(upstream: str, qualifier: str, limit: int) -> list[dict]:
    q = f"repo:{upstream} is:open is:issue no:assignee {qualifier}"
    out = gh(["api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", f"per_page={limit}",
              "--jq", ".items"], kind="search")
    return json.loads(out or "[]")


def linked_prs(upstream: str, number: int) -> list[str]:
    """Three independent signals; any hit disqualifies the issue."""
    hits: list[str] = []
    owner, name = upstream.split("/")

    # (1) structured closing references + cross-referenced PRs
    gql = (
        '{repository(owner:"%s",name:"%s"){issue(number:%d){'
        "closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state}}"
        "timelineItems(itemTypes:[CROSS_REFERENCED_EVENT],last:30){nodes{"
        "... on CrossReferencedEvent{source{... on PullRequest{number state}}}}}"
        "}}}" % (owner, name, number)
    )
    try:
        d = json.loads(gh(["api", "graphql", "-f", f"query={gql}"]))
        iss = d["data"]["repository"]["issue"] or {}
        for n in (iss.get("closedByPullRequestsReferences") or {}).get("nodes", []) or []:
            hits.append(f"closing-ref PR#{n['number']}({n['state']})")
        for n in (iss.get("timelineItems") or {}).get("nodes", []) or []:
            src = (n or {}).get("source") or {}
            if src.get("number"):
                hits.append(f"xref PR#{src['number']}({src.get('state')})")
    except Exception as e:  # noqa: BLE001
        hits.append(f"?graphql-failed:{e}"[:80])

    if hits:  # already disqualified; skip the expensive search call
        return hits

    # (2) PR full-text search by issue number
    try:
        q = f"repo:{upstream} is:pr {number}"
        out = gh(["api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=10",
                  "--jq", "[.items[] | {n:.number,s:.state}]"], kind="search")
        for it in json.loads(out or "[]"):
            hits.append(f"search PR#{it['n']}({it['s']})")
    except RateLimited:
        raise
    except Exception:  # noqa: BLE001
        pass

    return hits


def claimants(upstream: str, number: int) -> list[str]:
    try:
        out = gh(["api", f"repos/{upstream}/issues/{number}/comments", "-f", "per_page=60",
                  "--jq", "[.[] | {u:.user.login, b:.body}]"])
    except Exception:  # noqa: BLE001
        return []
    return [c["u"] for c in json.loads(out or "[]") if CLAIM_PHRASES.search(c.get("b") or "")]


def vet(cfg: dict, upstream: str, issue: dict) -> tuple[bool, str, dict]:
    labels = {l["name"] for l in issue.get("labels", [])}
    num, title = issue["number"], issue.get("title", "")

    if labels & cfg.get("exclude_labels", set()):
        return False, f"excluded label {sorted(labels & cfg['exclude_labels'])}", {}
    pfx = cfg.get("exclude_label_prefix")
    if pfx and any(l.startswith(pfx) for l in labels):
        return False, f"excluded label prefix {pfx}", {}
    if labels & cfg.get("exclude_labels_scope", set()):
        return False, "fix belongs to a different repo", {}
    if (pat := cfg.get("exclude_title")) and re.search(pat, title, re.I):
        return False, "title matches exclusion", {}
    if (pat := cfg.get("require_body")) and not re.search(pat, issue.get("body") or "", re.I):
        return False, "body lacks required in-scope signal", {}
    if issue.get("draft"):
        return False, "draft", {}

    prs = linked_prs(upstream, num)
    if prs:
        return False, f"already has PR(s): {prs[:3]}", {}

    who = claimants(upstream, num)
    if len(who) >= 2:
        return False, f"swarmed ({len(who)} claimants)", {}
    if who:
        return False, f"claimed by {who[0]}", {}

    return True, "clear", {"labels": sorted(labels), "comments": issue.get("comments", 0)}


def age_hours(iso: str) -> float:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def scan_repo(key: str, limit: int, max_vet: int) -> dict:
    cfg = REPOS[key]
    upstream = cfg["upstream"]
    seen: dict[int, dict] = {}
    errors: list[str] = []
    for qual in cfg["searches"]:
        try:
            for it in search_issues(upstream, qual, limit):
                seen.setdefault(it["number"], it)
        except Exception as e:  # noqa: BLE001
            msg = f"search failed [{qual}]: {e}"
            errors.append(msg)
            print(f"  ! {msg}", file=sys.stderr)

    ok, rejected = [], []
    # vet freshest-first and stop early: unclaimed issues are almost always recent,
    # and each vet costs up to three API calls.
    ordered = sorted(seen.items(), key=lambda kv: kv[1]["created_at"], reverse=True)[:max_vet]
    for num, it in ordered:
        try:
            passed, why, extra = vet(cfg, upstream, it)
        except RateLimited as e:
            errors.append(f"vet aborted at #{num}: {e}")
            print(f"  ! vet aborted at #{num} (rate limited)", file=sys.stderr)
            break
        rec = {
            "number": num,
            "title": it.get("title", "")[:160],
            "url": it.get("html_url"),
            "created_at": it.get("created_at"),
            "age_hours": round(age_hours(it["created_at"]), 1),
            "reason": why,
            **extra,
        }
        (ok if passed else rejected).append(rec)

    # freshest first: unclaimed issues get taken within hours
    ok.sort(key=lambda r: r["age_hours"])
    return {
        "repo": key,
        "upstream": upstream,
        "implements_in": cfg.get("implements_in", upstream),
        "needs_assignment": cfg.get("needs_assignment", False),
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "partial": bool(errors),
        "errors": errors,
        "candidates": ok,
        "rejected": rejected,
    }


def load_state(key: str) -> set[int]:
    p = STATE / f"{key}.json"
    if not p.exists():
        return set()
    return set(json.loads(p.read_text()).get("claimed", []))


def save_claim(key: str, number: int) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / f"{key}.json"
    d = json.loads(p.read_text()) if p.exists() else {"claimed": []}
    if number not in d["claimed"]:
        d["claimed"].append(number)
    p.write_text(json.dumps(d, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", action="append", help="repo key (default: all)")
    ap.add_argument("--limit", type=int, default=25, help="issues per search")
    ap.add_argument("--max-vet", type=int, default=8,
                    help="max issues to vet per repo (each costs up to 3 API calls)")
    ap.add_argument("--pop", metavar="KEY", help="print best unclaimed candidate for KEY")
    ap.add_argument("--claim", action="store_true", help="with --pop, mark it claimed")
    a = ap.parse_args()

    if a.pop:
        f = QUEUE / f"{a.pop}.json"
        if not f.exists():
            print(json.dumps({"error": f"no queue for {a.pop}; run a scan first"}))
            return 1
        q = json.loads(f.read_text())
        done = load_state(a.pop)
        for c in q["candidates"]:
            if c["number"] not in done:
                if a.claim:
                    save_claim(a.pop, c["number"])
                print(json.dumps({**c, "upstream": q["upstream"],
                                  "implements_in": q["implements_in"],
                                  "needs_assignment": q["needs_assignment"],
                                  "scanned_at": q["scanned_at"]}, indent=2))
                return 0
        print(json.dumps({"error": "no unclaimed candidates", "scanned_at": q["scanned_at"]}))
        return 1

    QUEUE.mkdir(parents=True, exist_ok=True)
    keys = a.repo or list(REPOS)
    total, degraded = 0, []
    for k in keys:
        if k not in REPOS:
            print(f"unknown repo key: {k}", file=sys.stderr)
            continue
        print(f"scanning {k} ({REPOS[k]['upstream']}) ...", file=sys.stderr)
        res = scan_repo(k, a.limit, a.max_vet)
        n = len(res["candidates"])
        out = QUEUE / f"{k}.json"

        # A rate-limited scan yields zero candidates, which is indistinguishable from
        # "no work available" once written. Never let that clobber a good queue.
        if res["partial"] and n == 0 and out.exists():
            prev = json.loads(out.read_text())
            if prev.get("candidates"):
                degraded.append(k)
                prev["stale_since"] = prev.get("stale_since") or prev.get("scanned_at")
                prev["last_failed_scan"] = res["scanned_at"]
                prev["errors"] = res["errors"]
                out.write_text(json.dumps(prev, indent=2) + "\n")
                print(f"  ! scan degraded — kept previous queue "
                      f"({len(prev['candidates'])} candidate(s) from {prev['scanned_at']})",
                      file=sys.stderr)
                continue

        out.write_text(json.dumps(res, indent=2) + "\n")
        total += n
        flag = " [PARTIAL]" if res["partial"] else ""
        top = res["candidates"][0]["title"][:70] if n else "-"
        print(f"  {n:>2} candidate(s), {len(res['rejected'])} rejected{flag} | top: {top}",
              file=sys.stderr)

    print(f"\n{total} candidate(s) queued in {QUEUE}", file=sys.stderr)
    if degraded:
        print(f"WARNING: degraded scans kept stale queues for: {', '.join(degraded)}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
