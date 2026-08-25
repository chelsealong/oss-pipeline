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
import traceback
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import re as _re
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
# Windows come from the p90 of how long external PRs actually take to land,
# with margin, because skipping a live PR costs a real chance while answering a
# dead one costs one session.
#
# HOW TO MEASURE THIS, because getting it wrong once already cost a landing.
# Sample by MERGE time (`is:merged sort:updated-desc`), never by creation time.
# Sampling the newest-created merged PRs selects for the ones that merged fast —
# the slow ones have not merged yet and are invisible. On 2026-08-20 that method
# reported openclaw's slowest external merge as 4h, and the window was set to
# 24h on that basis. openclaw#121306 merged the same day at 10 days old, 60x the
# supposed maximum. Re-measured by merge time over 84 PRs: p50 1h, p75 24h,
# p90 140h, longest 507h.
MERGE_WINDOW_HOURS = {
    # hermes lands by salvage — a maintainer cherry-picks our commits into their
    # own PR — and those arrived at 16h, 151h, 3 days and 5 days. Its merged-PR
    # latency (p90 6h) describes other people's direct merges, not our path.
    "NousResearch/hermes-agent": 240,
    # adk: 14 days, and this one is NOT from merged-PR latency — only 3 external
    # PRs there merge at all, because Copybara imports and closes them. Measured
    # instead on actual landings over 204 external PRs: 97% inside 14 days.
    "google/adk-python": 336,
    # ComfyUI: p90 212h (8.8 days) by merge time, against the 7.2 days the
    # biased sample claimed. 10 days.
    "Comfy-Org/ComfyUI": 240,
    # openclaw: p90 140h (5.8 days). 7 days, not the 24h a biased sample gave.
    "openclaw/openclaw": 168,
}


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


def linked_issue(pr: dict):
    """The issue our PR closes, from its body. None when it names none."""
    m = _re.search(r"(?i)(?:closes|fixes|resolves)\s+#(\d+)", pr.get("body") or "")
    return int(m.group(1)) if m else None


def someone_claimed_the_issue(pr: dict):
    """(login, issue) if a human claimed our PR's issue after we opened it.

    The announcement on adk#6730 promised "if someone is already on it, say so
    and I will drop mine". The issue's own author said so six minutes later and
    we published anyway, because nothing was watching. Blocking the open is only
    half the promise — a claim that lands after we publish has to close the PR.
    """
    num = linked_issue(pr)
    if num is None:
        return None, None
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
        if claimed and _claim_is_about_another(text, num):
            # arunpshankar wrote on adk#6672 that they would send a PR. They
            # meant #6778, a separate issue they had just filed at a triager's
            # request. We closed #6673 — a fix that a collaborator had said on
            # the same thread was moving toward merge as-is — and were asked to
            # reopen it. A claim that names a different number is a claim on
            # that number, and this thread is not evidence about ours.
            log(f"  [{repo}#{pr.get('number')}] {c['u']} claims work, but names "
                f"another issue — not treating it as a claim on #{num}")
            continue
        if claimed:
            return c["u"], num
    return None, None


def _claim_is_about_another(text: str, issue: int) -> bool:
    """True when the claim points at a number that is not the issue in question.

    Only fires when EVERY number mentioned is something else. A comment that
    says "I'll take this, see also #123" still claims this one.
    """
    nums = {int(n) for n in _re.findall(r"#(\d{2,7})", text or "")}
    nums.discard(issue)
    return bool(nums) and issue not in {int(n) for n in _re.findall(r"#(\d{2,7})", text or "")}


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
    # The sentence that should have stopped us closing adk#6673 was not on the
    # PR at all: surajksharma07 wrote on issue #6672 that #6673 was moving
    # toward merge as-is. Engagement about our PR can live on the issue, so read
    # the issue's comments for mentions of our number before standing down.
    num, issue = pr.get("number"), linked_issue(pr)
    if not (issue and num):
        return ""
    try:
        raw = scan.gh(["api", f"repos/{pr['_repo']}/issues/{issue}/comments",
                       "--paginate", "--jq",
                       '[.[] | {u: .user.login, b: .body}]'])
        for c in json.loads(raw or "[]"):
            who = c.get("u") or ""
            if who == ME or "[bot]" in who:
                continue
            if f"#{num}" in (c.get("b") or ""):
                return f"{who} (on issue #{issue})"
    except Exception:  # noqa: BLE001
        # An API failure must not read as "nobody is engaged" — that is the
        # direction that closes a live PR.
        return "unknown (issue comments unreadable)"
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
            _record_stand_down(repo, num, who, issue)
            return True
        log(f"  [{repo}#{num}] could not close: {r.stderr.strip()[:120]}")
    except Exception as e:  # noqa: BLE001
        log(f"  [{repo}#{num}] stand-down failed: {e}")
    return False


STOOD_DOWN = scan.STATE / "stood-down.json"
# How long to keep re-reading a PR we closed ourselves. A correction arrives
# within hours if it arrives at all — arunpshankar's came 1h42m after we closed
# adk#6673 — and a PR is stale after a week anyway.
STAND_DOWN_WATCH_DAYS = 7

REOPEN_SYSTEM = (
    "You are reading comments left on a pull request that its own author CLOSED, "
    "because someone appeared to claim the underlying issue. Decide whether any "
    "comment asks for the pull request to be REOPENED, or retracts or corrects "
    "the claim that caused it to be closed.\n\n"
    "Answer true for: an explicit request to reopen; a statement that the "
    "claim was a misunderstanding, was about a different issue, or that the "
    "commenter is not working on this after all; a maintainer saying the change "
    "was wanted or was close to merging.\n"
    "Answer false for: thanks; agreement that closing was right; anything about "
    "a different change; bot notices.\n\n"
    'Reply with JSON only: {"reopen": true|false, "why": "<10 words or fewer>"}'
)


def _record_stand_down(repo: str, num: int, who: str, issue: int) -> None:
    """Remember a PR we closed, so a correction can still reach us.

    open_prs() searches `is:open`, so a closed PR leaves the watcher's view for
    good. When arunpshankar asked us to reopen adk#6673 — a change a
    collaborator had said was moving toward merge — the request landed somewhere
    nothing would ever read again. It was found by hand, days later.
    """
    try:
        d = json.loads(STOOD_DOWN.read_text()) if STOOD_DOWN.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    d[f"{repo}#{num}"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "who": who, "issue": issue, "handled": False,
    }
    try:
        scan.STATE.mkdir(parents=True, exist_ok=True)
        STOOD_DOWN.write_text(json.dumps(d, indent=1) + "\n")
    except Exception as e:  # noqa: BLE001
        log(f"  could not record stand-down for {repo}#{num}: {e}")


def check_stand_downs() -> int:
    """Re-read PRs we closed ourselves and reopen any we were asked to.

    Reopening is the safe direction here. The only person inconvenienced by a
    wrong reopen is us; the cost of missing a real request is a change that was
    heading to merge, thrown away by our own automation.
    """
    import intent
    try:
        d = json.loads(STOOD_DOWN.read_text()) if STOOD_DOWN.exists() else {}
    except Exception:  # noqa: BLE001
        return 0
    now = datetime.now(timezone.utc)
    reopened, dirty = 0, False
    for key, rec in list(d.items()):
        if rec.get("handled"):
            continue
        try:
            age = (now - datetime.fromisoformat(rec["at"])).days
        except Exception:  # noqa: BLE001
            age = 0
        if age > STAND_DOWN_WATCH_DAYS:
            rec["handled"] = True
            dirty = True
            continue
        repo, _, num = key.rpartition("#")
        try:
            raw = scan.gh(["api", f"repos/{repo}/issues/{num}/comments",
                           "--paginate", "--jq",
                           '[.[] | {u: .user.login, at: .created_at, b: .body}]'])
            comments = json.loads(raw or "[]")
        except Exception:  # noqa: BLE001
            continue
        for c in comments:
            if c["u"] == ME or "[bot]" in c["u"] or c["at"] <= rec["at"]:
                continue
            verdict = intent._ask(REOPEN_SYSTEM, intent._strip_markup(c["b"] or ""),
                                  author=c["u"])
            if not verdict or not verdict.get("reopen"):
                continue
            log(f"  [{key}] {c['u']} asks us to reopen: {verdict.get('why','')}")
            if DRY_RUN:
                log(f"  DRY_RUN would reopen {key}")
            else:
                r = subprocess.run(["gh", "pr", "reopen", str(num), "--repo", repo],
                                   capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    log(f"  [{key}] reopen failed: {r.stderr.strip()[:120]}")
                    break
                note = (f"Reopened — thank you for the correction. I closed this on "
                        f"@{rec['who']}'s note about #{rec['issue']} and read it as a "
                        "claim on this change; that was my mistake, not yours.")
                subprocess.run(["gh", "pr", "comment", str(num), "--repo", repo,
                                "--body", note], capture_output=True, text=True, timeout=60)
                log(f"  [{key}] reopened")
            rec["handled"] = True
            dirty = True
            reopened += 1
            break
    if dirty:
        try:
            STOOD_DOWN.write_text(json.dumps(d, indent=1) + "\n")
        except Exception:  # noqa: BLE001
            pass
    return reopened


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


# 50 per page, so this caps the watcher at 400 open PRs. Well above the 99 we
# hold today, and a bound rather than an unbounded loop against a live API.
MAX_PR_PAGES = 8


def _tag(nodes: list[dict]) -> list[dict]:
    """Attach _repo to whatever pages did arrive, so a mid-page failure still
    returns usable work instead of discarding it."""
    out = []
    for pr in nodes:
        if pr:
            pr["_repo"] = (pr.get("repository") or {}).get("nameWithOwner", "?")
            out.append(pr)
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
    q = ('{search(type:ISSUE, first:50, after:%%s, query:"is:pr is:open author:%s %s")'
         '{pageInfo{hasNextPage endCursor} nodes{'
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

    # Page. The previous version asked for 50 and logged a warning when it got
    # 50 back, which is how 99 open PRs came to be watched 50 at a time: every
    # sweep re-read the newest 50 and the other 49 were never seen at all. The
    # PRs that fall off a newest-first page are the old ones — exactly the ones
    # a merge window is meant to age out, so both features were dead together.
    nodes: list[dict] = []
    cursor, pages = "null", 0
    while pages < MAX_PR_PAGES:
        try:
            data = json.loads(scan.gh(["api", "graphql", "-f", f"query={q % cursor}"]))
        except Exception as e:  # noqa: BLE001
            log(f"  PR search failed: {str(e)[:160]}")
            return nodes and _tag(nodes) or []
        search = (data.get("data") or {}).get("search") or {}
        nodes += search.get("nodes") or []
        pages += 1
        info = search.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = json.dumps(info["endCursor"])      # GraphQL wants it quoted
    else:
        log(f"  WARNING: stopped after {MAX_PR_PAGES} pages ({len(nodes)} PRs); "
            "some open PRs are still unwatched")
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


# Cached per repo for one pass. A check that is failing on OTHER people's open
# PRs is broken for everyone, not caused by us.
_BROKEN_FOR_ALL: dict = {}


def _norm_check(name: str) -> str:
    """Collapse shard numbers so `slice 10/12` and `slice 9/12` compare equal.

    hermes runs its suite in twelve shards. Four of our PRs — an arXiv link fix,
    two Desktop changes and a skills validator — all failed `Run tests slice
    10/12`, on the same test in tests/gateway/test_goal_continuation_drain.py,
    which none of them goes anywhere near. Other authors were failing slices
    9/12 and 2/12 in the same hour. Compared literally, no other PR failed
    "10/12" and ours looked unique; normalised, it is plainly the suite.
    """
    return _re.sub(r"\d+", "N", (name or "").lower()).strip()


def broken_for_everyone(repo: str) -> set:
    """Normalised names of checks failing on other authors' open PRs here."""
    if repo in _BROKEN_FOR_ALL:
        return _BROKEN_FOR_ALL[repo]
    q = ('{search(query:"repo:%s is:pr is:open sort:created-desc", type:ISSUE, first:30)'
         "{nodes{... on PullRequest{author{login} commits(last:1){nodes{commit{"
         "statusCheckRollup{contexts(last:40){nodes{... on CheckRun{name conclusion}}}}"
         "}}}}}}}" % repo)
    out = set()
    try:
        data = json.loads(scan.gh(["api", "graphql", "-f", f"query={q}"]))
        for pr in (data["data"]["search"]["nodes"] or []):
            if not pr or ((pr.get("author") or {}).get("login")) == ME:
                continue
            roll = ((pr.get("commits") or {}).get("nodes") or [{}])[0]
            roll = ((roll or {}).get("commit") or {}).get("statusCheckRollup") or {}
            for c in ((roll.get("contexts") or {}).get("nodes") or []):
                if c and c.get("conclusion") == "FAILURE" and c.get("name"):
                    out.add(_norm_check(c["name"]))
    except Exception as e:  # noqa: BLE001
        # Empty, not "everything is broken". Guessing that a check is not ours
        # is the direction that ignores a regression we caused.
        log(f"  could not sample {repo}'s other PRs: {str(e)[:90]}")
        return set()
    _BROKEN_FOR_ALL[repo] = out
    return out


# Checks established by investigation to fail for forks regardless of the diff.
# The "failing for other authors too" signal cannot see these: a fork-only
# failure never appears on a PR opened from a branch in the org. Each entry here
# is a finding written down in lessons/<repo>.md, not a guess — openclaw's
# check-sqlite-session-flip-proof was traced to fork isolation and recorded, and
# then this filter went on treating it as ours anyway.
FORK_ONLY_CHECKS = {
    "openclaw/openclaw": (r"check-sqlite-session-flip-proof",),
}


def _is_ours(repo: str, name: str) -> bool:
    """Is this failing check plausibly caused by our change?

    Takes the repo slug, not the PR: the REST pagination path has a repo string
    and no PR dict, and when this took a `pr` that path referenced an
    out-of-scope name. It raised NameError on every openclaw PR (>=100 check
    contexts) for two days and 375 passes, and because the raise happened inside
    one_pass, save_seen never ran either.
    """
    name = name or ""
    if NOT_OUR_CHECKS.search(name):
        return False
    repo = repo or ""
    for pat in FORK_ONLY_CHECKS.get(repo, ()):
        if _re.search(pat, name, _re.I):
            return False
    return _norm_check(name) not in broken_for_everyone(repo)


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
        out.append({"name": name, "sha": sha, "ours": _is_ours(pr.get("_repo", ""), name)})
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
            out.append({"name": r["n"], "sha": sha, "ours": _is_ours(repo, r["n"])})
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
            out.append({"name": r["n"], "sha": sha, "ours": _is_ours(repo, r["n"])})
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

    # A PR we closed ourselves is invisible to open_prs(), so a correction can
    # only reach us here. Cheap: only unhandled entries from the last week.
    try:
        if (n := check_stand_downs()):
            log(f"reopened {n} PR(s) we had closed in error")
    except Exception as e:  # noqa: BLE001
        log(f"stand-down recheck failed: {str(e)[:120]}")

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
            # A bare message hid a NameError for two days and 375 passes: the
            # text "name 'pr' is not defined" names the symbol but not the
            # function, and there is more than one place it could come from.
            # Log the frame that raised.
            log(f"pass failed: {str(e)[:200]}")
            for line in traceback.format_exc().strip().splitlines()[-6:]:
                log(f"    {line}")

        if not a.loop:
            return 0
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
