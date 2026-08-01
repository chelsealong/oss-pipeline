#!/usr/bin/env python3
"""Watch our own open PRs for reviewer feedback and act on it.

Opening a PR is only half the work: maintainers and review bots ask for changes,
proof, or clarification, and an unanswered request stalls the PR indefinitely.
This polls every open PR authored by the account, notices genuinely new feedback,
and dispatches the cloud responder to deal with it.

Feedback here is NOT just humans. On openclaw the reviewer *is* a bot
(`clawsweeper`), and its findings are real — it caught a genuine P1 on #115138 and
put #116260 in `status: 📣 needs proof`. So bots are triaged, not ignored.

Nor is it only things anyone wrote. A failing check is feedback, and it was
invisible here for as long as this file existed: 6 of 15 open PRs were red while
this watcher reported nothing to do. The hard part is not noticing the red — it
is knowing whose red it is. Vercel, codecov and snyk fail on every fork whatever
our code does, and an aggregate gate like AutoGPT's "Check PR Status" mirrors
them, so it fails permanently too. See NOT_OUR_CHECKS.

Detection runs locally because it needs only the GitHub API, which works over the
VPN. The response runs on a GitHub runner, because Anthropic returns 403 to this
machine's VPN exit and `claude -p` cannot run here at all.

    ./watch-prs.py              # one pass (what launchd runs)
    ./watch-prs.py --loop 300   # poll every 300s
    DRY_RUN=1 ./watch-prs.py    # classify and print, dispatch nothing
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import scan  # REPOS, gh(), STATE

ROOT = pathlib.Path(__file__).resolve().parent
SEEN = scan.STATE / "pr-seen.json"
LOG = ROOT / "watch-prs.log"
PIPELINE_REPO = "chelsealong/oss-pipeline"
ME = "chelsealong"
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# Accounts whose comments are pure status, never a request to act on.
# NOTE: GraphQL returns bot logins WITHOUT the "[bot]" suffix that REST shows,
# so compare on the stripped form or google-cla slips through as actionable.
NOISE_AUTHORS = {
    "github-actions", "dependabot", "codecov",
    "google-cla",               # CLA state is handled by the identity guard
    "vercel", "netlify", "sonarcloud",
}


def _norm(login: str) -> str:
    return login[:-5] if login.endswith("[bot]") else login

# At most this many responder dispatches per PR per day. Review threads can
# ping-pong, and an agent that answers every message becomes noise on someone
# else's repo.
MAX_RESPONSES_PER_PR_PER_DAY = int(os.environ.get("MAX_PR_RESPONSES", "3"))


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def upstreams() -> list[str]:
    seen, out = set(), []
    for cfg in scan.REPOS.values():
        for r in (cfg["upstream"], cfg.get("implements_in") or cfg["upstream"]):
            if r not in seen:
                seen.add(r); out.append(r)
    return out


def open_prs() -> list[dict]:
    """Our open PRs across every tracked upstream, with all feedback attached.

    One GraphQL query per repo (cheap: 1 point each) rather than three REST calls
    per PR for comments/reviews/review-comments.
    """
    # Search rather than listing each repo's newest PRs: openclaw alone takes
    # ~440 PRs/day, so ours never appear in a "first: 20" window and were being
    # missed entirely. One search covers every repo at once.
    repo_filter = " ".join(f"repo:{r}" for r in upstreams())
    q = ('{search(type:ISSUE, first:50, query:"is:pr is:open author:%s %s"){nodes{'
         '... on PullRequest{'
         ' number title url updatedAt author{login} headRefName isDraft'
         ' repository{nameWithOwner}'
         ' labels(first:20){nodes{name}}'
         ' comments(last:20){nodes{id createdAt author{login} body}}'
         ' reviews(last:10){nodes{id submittedAt state author{login} body}}'
         ' reviewThreads(last:20){nodes{comments(last:5){nodes{id createdAt author{login} body path}}}}'
         # A red CI run is feedback too, and it is the kind nobody types. It was
         # invisible here: 6 of 15 open PRs were failing checks while this
         # watcher reported nothing to do, because it only ever looked at things
         # humans wrote.
         ' commits(last:1){nodes{commit{oid statusCheckRollup{state contexts(last:100){nodes{'
         '  ... on CheckRun{name conclusion}'
         '  ... on StatusContext{context state}}}}}}}'
         '}}}}' % (ME, repo_filter))
    try:
        data = json.loads(scan.gh(["api", "graphql", "-f", f"query={q}"]))
    except Exception as e:  # noqa: BLE001
        log(f"  PR search failed: {str(e)[:160]}")
        return []
    prs: list[dict] = []
    for pr in ((data.get("data") or {}).get("search") or {}).get("nodes") or []:
        if not pr:
            continue
        pr["_repo"] = (pr.get("repository") or {}).get("nameWithOwner", "?")
        prs.append(pr)
    return prs


# Checks that fail on a fork no matter what our code does, and checks that are
# somebody else's infrastructure. Answering these would spend an agent session
# per red cross on things we cannot fix and were never asked to.
#
# Measured on our own open PRs: AutoGPT#13750 was failing Vercel (no deploy
# credentials for a fork), codecov (no upload token), and snyk (org licence) —
# alongside `test (3.13)`, which was genuinely ours. Only the last one is worth
# a session, so the filter has to separate them rather than react to the rollup.
# Aggregate gates are the subtle case. AutoGPT's "Check PR Status" runs
# check_actions_status.py, which queries every check run on the commit and fails
# if any of them failed. On a fork Vercel/codecov/snyk fail permanently, so the
# gate fails permanently — and it is not a defect in our code, it is a mirror of
# the others. Treating it as ours would dispatch a session every single day, on
# every fork PR, forever.
NOT_OUR_CHECKS = re.compile(
    r"vercel|netlify|cloudflare|deploy|preview"
    r"|codecov|coveralls|sonarcloud|snyk|codeql|semgrep|socket"
    r"|\bcla\b|license/|dco"
    r"|dependabot|renovate"
    r"|triage|gemini|claude-review"
    r"|check pr status|\ball checks\b|required checks?|ci[- ]status|merge[- ]queue",
    re.I,
)


def failing_checks(pr: dict) -> list[dict]:
    """Failing checks on the PR's newest commit, split into ours and not-ours.

    Keyed by commit SHA, so a push that fixes the build retires the old item
    instead of leaving it to be answered forever.
    """
    try:
        commit = ((pr.get("commits") or {}).get("nodes") or [{}])[0]["commit"]
    except (IndexError, KeyError, TypeError):
        return []
    rollup = commit.get("statusCheckRollup") or {}
    if rollup.get("state") != "FAILURE":
        return []
    sha = commit.get("oid", "")[:12]

    out = []
    for c in ((rollup.get("contexts") or {}).get("nodes") or []):
        name = c.get("name") or c.get("context") or ""
        bad = c.get("conclusion") in ("FAILURE", "TIMED_OUT") or c.get("state") == "FAILURE"
        if not name or not bad:
            continue
        out.append({"name": name, "sha": sha, "ours": not NOT_OUR_CHECKS.search(name)})
    return out


def feedback_items(pr: dict) -> list[dict]:
    """Flatten every piece of feedback into (id, author, when, kind, body)."""
    items: list[dict] = []
    for chk in failing_checks(pr):
        # Not-ours checks are still recorded (so they are not re-examined every
        # five minutes) but are marked unactionable rather than dispatched.
        items.append({"id": f"check:{chk['sha']}:{chk['name']}",
                      "author": "ci", "when": "", "kind": "check",
                      "body": f"Check `{chk['name']}` is failing on commit {chk['sha']}.",
                      "ours": chk["ours"]})
    for c in ((pr.get("comments") or {}).get("nodes") or []):
        items.append({"id": c["id"], "author": (c.get("author") or {}).get("login", "?"),
                      "when": c.get("createdAt", ""), "kind": "comment",
                      "body": c.get("body") or ""})
    for r in ((pr.get("reviews") or {}).get("nodes") or []):
        items.append({"id": r["id"], "author": (r.get("author") or {}).get("login", "?"),
                      "when": r.get("submittedAt") or "", "kind": f"review:{r.get('state')}",
                      "body": r.get("body") or ""})
    for t in ((pr.get("reviewThreads") or {}).get("nodes") or []):
        for c in ((t.get("comments") or {}).get("nodes") or []):
            items.append({"id": c["id"], "author": (c.get("author") or {}).get("login", "?"),
                          "when": c.get("createdAt", ""), "kind": f"inline:{c.get('path','')}",
                          "body": c.get("body") or ""})
    return items


def actionable(item: dict, pr: dict) -> tuple[bool, str]:
    """Whether this feedback warrants a response."""
    author = item["author"]
    if item["kind"] == "check":
        if not item.get("ours"):
            return False, "check fails for every fork (no secrets/licence), not ours to fix"
        return True, "our own check is failing"
    if author == ME:
        return False, "our own comment (replying would loop)"
    if _norm(author) in NOISE_AUTHORS:
        return False, f"status-only author {author}"
    # An APPROVED review with no body needs nothing.
    if item["kind"] == "review:APPROVED" and not item["body"].strip():
        return False, "bare approval"
    if not item["body"].strip():
        return False, "empty body"
    return True, "actionable"


def load_seen() -> dict:
    if SEEN.exists():
        try:
            return json.loads(SEEN.read_text())
        except Exception:  # noqa: BLE001
            log("pr-seen.json unreadable; starting fresh")
    return {}


def save_seen(d: dict) -> None:
    scan.STATE.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(d, indent=2) + "\n")


def dispatch(repo: str, number: int, note: str) -> bool:
    if DRY_RUN:
        log(f"  DRY_RUN would dispatch responder for {repo}#{number} ({note})")
        return True
    try:
        r = subprocess.run(
            ["gh", "workflow", "run", "respond-pr.yml", "--repo", PIPELINE_REPO,
             "-f", f"upstream={repo}", "-f", f"pr={number}"],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log(f"  -> dispatched respond-pr.yml for {repo}#{number}")
            return True
        log(f"  dispatch failed for {repo}#{number}: {r.stderr.strip()[:160]}")
    except Exception as e:  # noqa: BLE001
        log(f"  dispatch error for {repo}#{number}: {e}")
    return False


def one_pass(seen: dict) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    dispatched = 0

    for pr in open_prs():
        repo, num = pr["_repo"], pr["number"]
        key = f"{repo}#{num}"
        rec = seen.setdefault(key, {"ids": [], "responses": {}})
        known = set(rec["ids"])

        fresh = []
        for item in feedback_items(pr):
            if item["id"] in known:
                continue
            known.add(item["id"])
            ok, why = actionable(item, pr)
            if ok:
                fresh.append(item)
            else:
                log(f"  [{key}] skip {item['kind']} by {item['author']} — {why}")
        rec["ids"] = sorted(known)

        if not fresh:
            continue

        used = rec["responses"].get(today, 0)
        if used >= MAX_RESPONSES_PER_PR_PER_DAY:
            log(f"  [{key}] {len(fresh)} new item(s) but daily response cap reached ({used})")
            continue

        who = ", ".join(sorted({i["author"] for i in fresh}))
        labels = [l["name"] for l in ((pr.get("labels") or {}).get("nodes") or [])]
        log(f"  [{key}] {len(fresh)} new item(s) from {who}; labels: {', '.join(labels) or '-'}")
        if dispatch(repo, num, who):
            # A dry run must not consume the day's budget; dispatch() returns
            # True in DRY_RUN so the flow can be exercised, which had already
            # pushed AutoGPT#13752 to its cap without ever contacting anything.
            if not DRY_RUN:
                rec["responses"][today] = used + 1
            dispatched += 1

    return dispatched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, default=0,
                    help="seconds between passes; 0 = single pass")
    a = ap.parse_args()

    seen = load_seen()
    bootstrap = not seen
    if bootstrap:
        log("bootstrap: recording existing feedback without responding to it")

    while True:
        try:
            n = one_pass(seen)
            if bootstrap:
                # First pass only learns history; otherwise every old review
                # would be answered at once.
                for rec in seen.values():
                    rec["responses"] = {}
                log(f"bootstrap complete ({sum(len(r['ids']) for r in seen.values())} items recorded)")
                bootstrap = False
            elif n:
                log(f"pass complete: {n} dispatch(es)")
            if DRY_RUN:
                log("DRY_RUN: state not persisted")
            else:
                save_seen(seen)
        except Exception as e:  # noqa: BLE001
            log(f"pass failed: {str(e)[:200]}")

        if not a.loop:
            return 0
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
