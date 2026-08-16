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

import scan
if str(pathlib.Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import intent  # REPOS, gh(), STATE

ROOT = pathlib.Path(__file__).resolve().parent
SEEN = scan.STATE / "pr-seen.json"
LOG = ROOT / "watch-prs.log"
PIPELINE_REPO = "chelsealong/oss-pipeline"
ME = "chelsealong"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
RESEED = False

# Accounts whose comments are pure status, never a request to act on.
# NOTE: GraphQL returns bot logins WITHOUT the "[bot]" suffix that REST shows,
# so compare on the stripped form or google-cla slips through as actionable.
NOISE_AUTHORS = {
    "github-actions", "dependabot", "codecov",
    "google-cla",               # CLA state is handled by the identity guard
    "vercel", "netlify", "sonarcloud",
}


# How long a repo actually looks at a PR. Measured, not guessed: across the 40
# most recent hermes merges the median age at merge is 0.2h, 37 of 40 landed
# inside 24h and ALL 40 inside 48h — the oldest was 46.5h. Past that a PR there
# is one of 20,623 open ones and nothing we write on it changes the outcome, so
# spending an agent session answering its triage bot is waste.
#
# Set to 72 rather than the observed maximum of 46.5h, on Bruce's call: 40
# merges is a small sample, the falsification pass did find older external
# merges (evgyur at 7.6 days, a batch of afourniernv's), and the asymmetry
# favours margin — skipping a live PR costs a real chance, while answering a
# dead one costs one session. Repos absent from this table have no window.
MERGE_WINDOW_HOURS = {"NousResearch/hermes-agent": 72}


def pr_age_hours(pr: dict):
    created = pr.get("createdAt")
    if not created:
        return None
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def someone_claimed_the_issue(pr: dict):
    """(login, issue) if a human claimed our PR's issue after we opened it.

    The announcement on adk#6730 promised "if someone is already on it, say so
    and I will drop mine". The issue's own author said so six minutes later and
    we published anyway, because nothing was watching. Blocking the open is only
    half the promise — a claim that lands after we publish has to close the PR.
    """
    import re as _re
    body = pr.get("body") or ""
    m = _re.search(r"(?i)(?:closes|fixes|resolves)\s+#(\d+)", body)
    if not m:
        return None, None
    num = int(m.group(1))
    repo = pr.get("_repo") or ""
    opened = pr.get("createdAt") or ""
    try:
        raw = scan.gh(["api", f"repos/{repo}/issues/{num}/comments", "--paginate",
                       "--jq", '[.[] | {u: .user.login, at: .created_at, b: .body}]'])
        comments = json.loads(raw or "[]")
    except Exception:  # noqa: BLE001
        return None, None
    for c in comments:
        if c["u"] == ME or "[bot]" in c["u"] or c["at"] <= opened:
            continue
        # Quoted text is someone repeating a claim, not making one.
        text = _re.sub(r"^\s*>.*$", "", c["b"] or "", flags=_re.M)
        # default=False, unlike scan.claimants. A True here closes a PR of ours,
        # so an unreachable judge must produce silence, not a mass close.
        claimed, why = intent.is_claim(text, author=c["u"], default=False)
        if claimed:
            return c["u"], num
    return None, None


def has_outside_engagement(pr: dict) -> str:
    """Who besides us has spoken on this PR.

    A second guard on stand_down, independent of how good the claim detection
    is. adk#6697 was closed automatically while a collaborator had written "that
    PR looks like the right one to land" and the reporter was mid-way through
    verifying it against AlloyDB. Even a correct claim elsewhere should not
    discard a review already in progress — that is the maintainer's call, not
    ours. Cheap: the PR's comments are already in the payload.
    """
    for c in ((pr.get("comments") or {}).get("nodes") or []):
        who = ((c.get("author") or {}).get("login") or "")
        if who and who != ME and "[bot]" not in who:
            return who
    for rv in ((pr.get("reviews") or {}).get("nodes") or []):
        who = ((rv.get("author") or {}).get("login") or "")
        if who and who != ME and "[bot]" not in who:
            return who
    return ""


def stand_down(pr: dict, who: str, issue: int) -> bool:
    """Close our PR because someone else claimed the issue. Keeps the promise."""
    import subprocess
    repo, num = pr["_repo"], pr["number"]
    note = (f"Closing this — @{who} said on #{issue} that they want to work on it, "
            "and the note I left there promised to drop mine if someone was already "
            "on it. If any of this diff is useful, take it freely.")
    try:
        subprocess.run(["gh", "pr", "comment", str(num), "--repo", repo, "--body", note],
                       capture_output=True, text=True, timeout=60)
        r = subprocess.run(["gh", "pr", "close", str(num), "--repo", repo],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log(f"  [{repo}#{num}] stood down — {who} claimed #{issue}")
            return True
        log(f"  [{repo}#{num}] could not close: {r.stderr.strip()[:120]}")
    except Exception as e:  # noqa: BLE001
        log(f"  [{repo}#{num}] stand-down failed: {e}")
    return False


def past_merge_window(pr: dict):
    """(True, why) if this repo has stopped looking at PRs this old."""
    window = MERGE_WINDOW_HOURS.get(pr.get("_repo") or "")
    if window is None:
        return False, ""
    age = pr_age_hours(pr)
    if age is None or age <= window:
        return False, ""
    return True, f"{age:.0f}h old, past {pr['_repo']}'s {window}h merge window"


def _norm(login: str) -> str:
    return login[:-5] if login.endswith("[bot]") else login

# At most this many responder dispatches per PR per day. Review threads can
# ping-pong, and an agent that answers every message becomes noise on someone
# else's repo.
MAX_RESPONSES_PER_PR_PER_DAY = int(os.environ.get("MAX_PR_RESPONSES", "3"))
# Separate, smaller budget for our own failing checks. Kept small because a
# check that stays red after two attempts needs a human, not a third agent.
MAX_CHECK_RESPONSES_PER_PR_PER_DAY = int(os.environ.get("MAX_PR_CHECK_RESPONSES", "2"))


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
         ' number title url body createdAt updatedAt author{login} headRefName isDraft'
         ' repository{nameWithOwner}'
         ' labels(first:20){nodes{name}}'
         ' comments(last:20){nodes{id createdAt updatedAt author{login} body}}'
         ' reviews(last:10){nodes{id submittedAt state author{login} body}}'
         ' reviewThreads(last:20){nodes{comments(last:5){nodes{id createdAt updatedAt author{login} body path}}}}'
         # A red CI run is feedback too, and it is the kind nobody types. It was
         # invisible here: 6 of 15 open PRs were failing checks while this
         # watcher reported nothing to do, because it only ever looked at things
         # humans wrote.
         ' commits(last:1){nodes{commit{oid committedDate author{user{login}}'
         '  statusCheckRollup{state contexts(last:100){nodes{'
         '  ... on CheckRun{name conclusion}'
         '  ... on StatusContext{context state}}}}}}}'
         '}}}}' % (ME, repo_filter))
    try:
        data = json.loads(scan.gh(["api", "graphql", "-f", f"query={q}"]))
    except Exception as e:  # noqa: BLE001
        log(f"  PR search failed: {str(e)[:160]}")
        return []
    nodes = ((data.get("data") or {}).get("search") or {}).get("nodes") or []
    # Silent truncation is this codebase's recurring bug: contexts(last:40) hid
    # the only failing check on a PR that had 46 of them, and nothing said so.
    # A full page means there may be more we never looked at.
    if len(nodes) >= 50:
        log(f"  WARNING: PR search returned a full page ({len(nodes)}); "
            "some open PRs are not being watched — raise `first:`")
    prs: list[dict] = []
    for pr in nodes:
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
    r"|codecov|coveralls|sonarcloud|snyk|semgrep|socket"
    # NOT codeql. It was in this list on the assumption that scanners fail
    # on forks for infrastructure reasons, but it passed on AutoGPT#13750 and
    # failed on #13752 in the same hour — it is content-dependent, and what it
    # reported was six high-severity path-expression alerts on a file-upload
    # change we had just written. Whether or not they are true positives, an
    # alert on our own diff is ours to answer.
    r"|\bcla\b|license/|dco"
    r"|dependabot|renovate"
    r"|triage|gemini|claude-review"
    # Gates by concept, not by one repo's wording. Matching the literal
    # phrase "check pr status" caught AutoGPT and missed openclaw/ci-gate,
    # which is the same thing under a different name. Anchored so a real
    # job called gateway-tests or api-gateway-integration still counts as
    # ours — it is a gate only if "gate" is its own word.
    r"|check pr status|pr status check|\ball checks\b|required[- ]checks?"
    r"|ci[- ]status|merge[- ]queue|\bci[- ]?gate\b|\bmerge[- ]?gate\b|[-_/ ]gate$",
    re.I,
)


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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

    ctx = (rollup.get("contexts") or {}).get("nodes") or []
    # 100 is GraphQL's hard page limit for contexts, and openclaw PRs carry
    # 109-143 checks — so on exactly the repo we care most about, an unknown
    # slice of the results was being dropped without a word. Fall back to REST,
    # which paginates, whenever the page comes back full.
    if len(ctx) >= 100:
        return _failing_checks_rest(pr.get("_repo", ""), commit.get("oid", ""), sha)

    out = []
    for c in ctx:
        name = c.get("name") or c.get("context") or ""
        bad = c.get("conclusion") in ("FAILURE", "TIMED_OUT") or c.get("state") == "FAILURE"
        if not name or not bad:
            continue
        out.append({"name": name, "sha": sha, "ours": not NOT_OUR_CHECKS.search(name)})
    return out


def _failing_checks_rest(repo: str, oid: str, sha: str) -> list[dict]:
    """Same answer as the GraphQL path, but paginated.

    Two endpoints, because they hold different things: Actions jobs are
    check-runs, while Vercel and codecov report as commit statuses.
    """
    out, seen = [], set()
    if not repo or not oid:
        return out
    # `--paginate` with `--jq` emits one result PER PAGE, so wrapping the filter
    # in [] yields several arrays concatenated — not valid JSON. Emit one object
    # per line and parse line by line instead.
    runs = []
    try:
        raw = scan.gh(["api", "--paginate",
                       f"repos/{repo}/commits/{oid}/check-runs?per_page=100",
                       "--jq", ".check_runs[] | {n:.name, c:.conclusion}"])
        for line in (raw or "").splitlines():
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    except Exception as e:  # noqa: BLE001
        log(f"  check-run enumeration failed for {repo}@{sha}: {str(e)[:120]}")
        runs = []
    for r in runs:
        if r.get("c") in ("failure", "timed_out") and r.get("n") not in seen:
            seen.add(r["n"])
            out.append({"name": r["n"], "sha": sha, "ours": not NOT_OUR_CHECKS.search(r["n"])})
    sts = []
    try:
        raw = scan.gh(["api", f"repos/{repo}/commits/{oid}/status?per_page=100",
                       "--jq", ".statuses[] | {n:.context, c:.state}"])
        for line in (raw or "").splitlines():
            line = line.strip()
            if line:
                sts.append(json.loads(line))
    except Exception:  # noqa: BLE001
        sts = []
    for r in sts:
        if r.get("c") == "failure" and r.get("n") not in seen:
            seen.add(r["n"])
            out.append({"name": r["n"], "sha": sha, "ours": not NOT_OUR_CHECKS.search(r["n"])})
    return out


def last_commit_author(pr: dict) -> str | None:
    try:
        c = ((pr.get("commits") or {}).get("nodes") or [{}])[0]["commit"]
        return ((c.get("author") or {}).get("user") or {}).get("login")
    except (IndexError, KeyError, TypeError):
        return None


def feedback_items(pr: dict) -> list[dict]:
    """Flatten every piece of feedback into (id, author, when, kind, body)."""
    items: list[dict] = []
    if (st := standing_item(pr)):
        items.append(st)
    for chk in failing_checks(pr):
        # Not-ours checks are still recorded (so they are not re-examined every
        # five minutes) but are marked unactionable rather than dispatched.
        # The day is part of the key on purpose. Without it a failed repair
        # attempt was permanent: the id is (sha, check), a failed attempt pushes
        # no commit, so the sha never changes and the item is never reconsidered.
        # AutoGPT#13752's CodeQL alerts and openclaw#116260's check-lint were
        # both retired that way — dispatched once, unfixed, never looked at
        # again. One retry per day, still bounded by the check budget.
        items.append({"id": f"check:{chk['sha']}:{chk['name']}:{_utc_day()}",
                      "author": "ci", "when": "", "kind": "check",
                      "body": f"Check `{chk['name']}` is failing on commit {chk['sha']}.",
                      "ours": chk["ours"]})
    # Keyed by id AND updatedAt, not id alone. ClawSweeper posts a placeholder
    # and then EDITS THAT SAME COMMENT with the real review — it says so in the
    # placeholder text. Deduping on id meant every re-review it ever published
    # was invisible: openclaw#115138 sat four days on a comment created 07-28
    # whose body was an 08-01 verdict reading "Blocked - 8 items remain".
    for c in ((pr.get("comments") or {}).get("nodes") or []):
        items.append({"id": f"{c['id']}@{c.get('updatedAt') or c.get('createdAt','')}",
                      "author": (c.get("author") or {}).get("login", "?"),
                      "when": c.get("updatedAt") or c.get("createdAt", ""), "kind": "comment",
                      "body": c.get("body") or ""})
    for r in ((pr.get("reviews") or {}).get("nodes") or []):
        items.append({"id": r["id"], "author": (r.get("author") or {}).get("login", "?"),
                      "when": r.get("submittedAt") or "", "kind": f"review:{r.get('state')}",
                      "body": r.get("body") or ""})
    for t in ((pr.get("reviewThreads") or {}).get("nodes") or []):
        for c in ((t.get("comments") or {}).get("nodes") or []):
            items.append({"id": f"{c['id']}@{c.get('updatedAt') or c.get('createdAt','')}",
                          "author": (c.get("author") or {}).get("login", "?"),
                          "when": c.get("updatedAt") or c.get("createdAt", ""),
                          "kind": f"inline:{c.get('path','')}",
                          "body": c.get("body") or ""})
    return items


# A label that says the PR is waiting on us is a standing obligation, not an
# event. openclaw#116260 sat at `status: 📣 needs proof` for seven days with
# green CI and no new comment, so an event-driven watcher had nothing to react
# to and never looked at it again. Re-offer such a PR once a day.
STANDING_OBLIGATION = ("needs proof", "waiting on author", "changes requested")


def standing_item(pr: dict) -> dict | None:
    labels = [l["name"].lower() for l in ((pr.get("labels") or {}).get("nodes") or [])]
    hit = next((s for s in STANDING_OBLIGATION if any(s in l for l in labels)), None)
    if not hit:
        return None
    return {"id": f"standing:{hit}:{_utc_day()}",
            "author": "ci", "when": "", "kind": "check", "ours": True,
            "body": f"This PR carries a label meaning it is waiting on us: {hit!r}. "
                    "No new comment has arrived — the request is the standing one. "
                    "Re-read the reviewer's last verdict and satisfy it."}


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

    # Everything above is cheap and certain. What remains is the majority, and
    # the rules cannot separate it: 64% of responder sessions in the week to
    # 2026-08-16 ended inside two minutes because Claude checked out the branch,
    # read the comment, and found nothing to do. The difference is not the
    # author — gemini-code-assist posts both "I'm currently reviewing this pull
    # request and will post my feedback shortly" and real defect reports, and
    # coderabbitai posts both a walkthrough banner and the findings, so an
    # author blocklist silences the useful half with the noise. It is not a
    # phrase either: the strongest single signal turned out to be a footer,
    # "Addressed in commit <sha>", appended to a finding we have already fixed.
    #
    # Measured over 49 real comments on our open PRs: 30 of 31 no-op comments
    # skipped, zero actionable ones lost. default=CODE, so an unreachable judge
    # dispatches — an item ruled unactionable here is marked seen and never
    # reconsidered, and that door must not close on an API timeout.
    needs, why = intent.feedback_needs(item["body"], author=author, default="CODE")
    if needs == "NOTHING":
        return False, f"no response needed: {why}"
    return True, f"actionable ({needs.lower()}: {why})"


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
    if RESEED:
        log(f"  reseed: recording {repo}#{number} ({note}) without responding")
        return True
    if DRY_RUN:
        log(f"  DRY_RUN would dispatch responder for {repo}#{number} ({note})")
        return True
    try:
        r = subprocess.run(
            # Pass the reason. respond-pr rebuilds its own context from the
            # thread and otherwise cannot tell why it was woken: a PR labelled
            # "needs proof" has no new comment, so two runs read the thread,
            # found nothing newer than our own last message, and closed as
            # "status only" while the PR sat waiting on us.
            ["gh", "workflow", "run", "respond-pr.yml", "--repo", PIPELINE_REPO,
             "-f", f"upstream={repo}", "-f", f"pr={number}", "-f", f"reason={note[:200]}"],
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

        # If someone else pushed the newest commit, they have taken the branch
        # over — and that is the single best sign a PR is about to land, which
        # makes it the worst possible moment to push. On openclaw#117176 a
        # maintainer validated the head at 120/120 green, then rebased and added
        # two commits of their own; the responder was dispatched at it anyway
        # and had to be cancelled by hand before it could write over their work.
        if (author := last_commit_author(pr)) and author != ME:
            log(f"  [{key}] newest commit is by {author}, not us — hands off")
            continue

        # Keeping the announcement's promise: if someone claimed the issue after
        # we opened, the PR comes down. This runs before the merge-window check —
        # standing down matters even on a PR too old to be worth answering.
        who, claimed_issue = someone_claimed_the_issue(pr)
        if who and not (engaged := has_outside_engagement(pr)):
            stand_down(pr, who, claimed_issue)
            continue
        elif who:
            log(f"  [{key}] {who} claimed the issue, but {engaged} is already "
                "engaged here — leaving it to the maintainers")

        # Nothing we say after the window closes has ever changed an outcome.
        stale, why = past_merge_window(pr)
        if stale:
            log(f"  [{key}] {why} — not spending a session")
            continue
        rec = seen.setdefault(key, {"ids": [], "responses": {}})
        known = set(rec["ids"])

        fresh = []
        for item in feedback_items(pr):
            if item["id"] in known:
                continue
            ok, why = actionable(item, pr)
            if ok:
                # NOT marked seen yet. Marking happened here, before the daily
                # cap was consulted, so anything the cap turned away was recorded
                # as handled and never reconsidered — openclaw#117176 was refused
                # six times in one day and its genuinely broken test
                # (checks-node-compact-small-4) was silently retired on the first
                # refusal. An item is seen once it has been acted on, not once it
                # has been noticed.
                fresh.append(item)
            else:
                # Unactionable is a final judgement, so record it now and stop
                # re-evaluating it every five minutes.
                known.add(item["id"])
                log(f"  [{key}] skip {item['kind']} by {item['author']} — {why}")
        rec["ids"] = sorted(known)

        if not fresh:
            continue

        # A failing check of ours gets its own small budget rather than
        # competing with conversation. openclaw#117176 spent today at its cap
        # answering comments and was turned away six times while
        # `checks-node-compact-small-4` — a real broken test on our own branch —
        # was never once looked at. Talking is not more urgent than a red build.
        has_check = any(i["kind"] == "check" for i in fresh)
        used = rec["responses"].get(today, 0)
        used_checks = rec.setdefault("check_responses", {}).get(today, 0)
        if has_check and used_checks < MAX_CHECK_RESPONSES_PER_PR_PER_DAY:
            budget = "check"
        elif used < MAX_RESPONSES_PER_PR_PER_DAY:
            budget = "general"
        else:
            log(f"  [{key}] {len(fresh)} new item(s) but daily cap reached "
                f"(general {used}/{MAX_RESPONSES_PER_PR_PER_DAY}, "
                f"check {used_checks}/{MAX_CHECK_RESPONSES_PER_PR_PER_DAY})")
            continue

        who = ", ".join(sorted({i["author"] for i in fresh}))
        labels = [l["name"] for l in ((pr.get("labels") or {}).get("nodes") or [])]
        log(f"  [{key}] {len(fresh)} new item(s) from {who} [{budget} budget]; "
            f"labels: {', '.join(labels) or '-'}")
        if dispatch(repo, num, who):
            # A dry run must not consume the day's budget; dispatch() returns
            # True in DRY_RUN so the flow can be exercised, which had already
            # pushed AutoGPT#13752 to its cap without ever contacting anything.
            if RESEED:
                rec["ids"] = sorted(set(rec["ids"]) | {i["id"] for i in fresh})
            elif not DRY_RUN:
                if budget == "check":
                    rec["check_responses"][today] = used_checks + 1
                else:
                    rec["responses"][today] = used + 1
                rec["ids"] = sorted(set(rec["ids"]) | {i["id"] for i in fresh})
            dispatched += 1

    return dispatched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reseed", action="store_true",
                    help="record everything currently visible as seen, dispatch nothing. "
                         "Needed after changing the dedupe key: the new keys make every "
                         "existing comment look new, and without this the next pass would "
                         "answer months of already-handled feedback in one burst.")
    ap.add_argument("--loop", type=float, default=0,
                    help="seconds between passes; 0 = single pass")
    a = ap.parse_args()

    global RESEED
    RESEED = a.reseed
    seen = load_seen()
    bootstrap = (not seen) or a.reseed
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
