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
        # adk's CONTRIBUTING.md: "For other issues, please kindly ask before
        # contributing to avoid duplication." Its good-first-issue pool is
        # empty and help-wanted has 2, so effectively every candidate we take
        # falls under that sentence — and we had opened 10 PRs there without a
        # word. We announce before starting instead of waiting for a reply:
        # the wait would cost the 6-40 minute lead that is our whole advantage,
        # and the announcement still does the duplication work it is for —
        # uuzzrm opened a second PR on #6647 a full day after ours.
        "announce_before_work": True,
    },
    "langfuse": {
        "upstream": "langfuse/langfuse",
        "searches": ['label:"good first issue"', "label:bug sort:created-desc"],
        "exclude_labels": set(),
        # issues whose fix lives in a different repo
        "exclude_labels_scope": {"sdk-python", "sdk-js"},
        # langfuse assigns a maintainer to essentially every issue as triage:
        # 79 of 80 open `bug` issues have one (hassiebp 24, nimarb 21,
        # marliessophie 15). Reading an assignee as "someone is already writing
        # this fix" reduced the repo's entire supply to a single candidate.
        # vet() still rejects anything with a linked PR or a comment claimant,
        # which is what actually indicates work in progress.
        "ignore_assignees": True,
        "max_vet": 40,
    },
    "langfuse-python": {
        "upstream": "langfuse/langfuse",  # tracker lives here, fix lands in langfuse-python
        "searches": ["label:sdk-python", "label:integration-langchain", "label:integration-openai"],
        "exclude_labels": set(),
        "implements_in": "langfuse/langfuse-python",
        "max_vet": 40,
    },
    # spec-kit re-enabled 2026-08-14 on Bruce's instruction, at 3 PRs/day.
    # It was removed on 08-13 after its maintainer asked three times that we stop
    # opening PRs for catalog submissions. The exclusions below are the whole
    # reason it can come back — they were written on 08-12 and went away with the
    # config block, so restoring the repo without them would repeat the offence
    # exactly. verify check 19 asserts they stay.
    # Added 2026-08-15 after screening 15 repos on four criteria: OSI licence,
    # no "ask first and wait" rule in CONTRIBUTING, a language we can actually
    # run tests in, and a real external-merge record. Of 14 recently merged PRs
    # sampled, 14 were by non-members merged by someone else — the highest rate
    # of anything measured, ours included. Median +33 lines, which matches what
    # we produce. Median 227h to merge, so do not judge it inside a week.
    "llama-index": {
        "upstream": "run-llama/llama_index",
        "searches": ['label:bug sort:created-desc', "sort:created-desc"],
        # `triage` is not an exclusion here. Its description is "Issue needs to be
        # triaged/prioritized" and it sits on about half the open issues — the
        # same shape as adk's `needs review`, but adk's untriaged pool was old
        # feature requests, whereas this repo merges 14 of 14 sampled external
        # PRs. Dropping half the supply of the most permeable repo we found to
        # avoid a maintainer saying "not a bug" is the wrong trade.
        "exclude_labels": set(),
        "max_vet": 40,
    },
    # 11 of 14 sampled merges were genuine external contributions, median 56h,
    # median +42 lines. Its labels are emoji-prefixed, which the REST `labels`
    # filter matches literally — hence the exact strings below rather than "bug".
    "crawl4ai": {
        "upstream": "unclecode/crawl4ai",
        # The label name contains a space, so it must be quoted — _LABEL_RE stops
        # at whitespace otherwise and "\U0001F41E Bug" arrives as just the emoji.
        "searches": ['label:"\U0001F41E Bug" sort:created-desc'],
        "exclude_labels": {"\u2699\uFE0F In-progress", "\u2699 Done",
                           "\u2753 Question"},
        "max_vet": 40,
    },
    # Added 2026-08-15. I eliminated this on the first pass for a NOASSERTION
    # licence, which was wrong: everything outside `enterprise/` is MIT, and
    # that is one directory to avoid rather than n8n's `.ee.` files scattered
    # through the tree with unlicensed branches on top. 5 of 16 sampled merges
    # were genuine external contributions, median 26h, and all 35 open issues on
    # the first page are unassigned. Median merge is +223, so the size ceiling
    # in fix-one.yml matters here more than the merge rate does.
    "litellm": {
        "upstream": "BerriAI/litellm",
        "searches": ['label:bug sort:created-desc', "sort:created-desc"],
        "exclude_labels": set(),
        # enterprise/ is under a separate licence; nothing there is ours to fix.
        "exclude_title": r"(enterprise|/enterprise)",
        "max_vet": 40,
    },
    # Added 2026-08-15, also a corrected rejection. Its CONTRIBUTING says "for
    # anything beyond a trivial fix, wait for a maintainer to confirm the
    # approach" — scoped to significant work, not the blanket "ask and wait
    # before you pick it up" that keeps n8n out. Apache-2.0, median +32 which is
    # exactly our size. The reservation is the merge rate: 1 of 16 sampled, the
    # same 6% as hermes, so this is on trial and judged on landed commits.
    "mem0": {
        "upstream": "mem0ai/mem0",
        "searches": ['label:bug sort:created-desc'],
        "exclude_labels": {"enhancement"},   # those are the "wait first" class
        "max_vet": 40,
    },
    "spec-kit": {
        "upstream": "github/spec-kit",
        "searches": ["label:bug sort:created-desc", "sort:created-desc"],
        # Catalog submissions go through spec-kit's OWN agentic workflow, which
        # does the validation and opens the PR itself. mnriem asked us to stop
        # three times — #4027/#4025 on 08-10, #4043 on 08-11, then #4060/#4061/
        # #4062 on 08-12: "Please instruct your agents to update its
        # configuration to NOT open PRs against extension submissions." Seven of
        # our PRs there were closed for exactly this. Every spec-kit PR of ours
        # that merged (#3929 #4012 #4016 #4045) is a plain code fix.
        "exclude_labels": {"extension-submission", "preset-submission",
                           "bundle-submission",
                           # `agentic-workflows` marks auto-filed reports that
                           # spec-kit's OWN workflows failed — #4077 is the
                           # catalog submission workflow reporting its own break,
                           # and its body says "Assign this issue to an agent",
                           # meaning theirs. Debugging their submission machinery
                           # is the same intrusion mnriem asked us to stop, one
                           # layer down.
                           "agentic-workflows"},
        # slash-command changes need manual agent-testing evidence pasted into the PR;
        # matched narrowly so ordinary CLI bugs that merely mention templates still
        # qualify. The bracketed prefixes catch a submission before its label lands —
        # #4068 was titled "[Extension]: Add specjudge" with only `enhancement` on it.
        "exclude_title": r"(/speckit|slash[- ]command|^\[(extension|preset|bundle|aw)\])",
    },
    "gemini-cli": {
        "upstream": "google-gemini/gemini-cli",
        # help wanted is 29/30 self-assigned already — its /assign bot works too
        # well. The untapped supply is kind/bug (99 unassigned) and good first
        # issue; neither can be self-assigned, and neither needs to be.
        "searches": ['label:"help wanted"', 'label:"good first issue"',
                     "label:kind/bug sort:created-desc"],
        "max_vet": 40,
        # kind/bug on this tracker also carries auto-filed release-CI reports and
        # end-user support threads. Neither is a code defect we can fix from this
        # checkout, and a session spent on one produces nothing.
        "exclude_title": r"(nightly release failed|release failed for|geminicli\.com feedback|feedback: \[|high memory usage detected|blocking my custom domain|was deleted)",
        "exclude_labels": {"🔒Maintainers only"},
        # Corrected 2026-08-08: there is no require_issue_link workflow here and
        # nothing auto-closes. 891 PRs carrying status/need-issue have merged,
        # and 18 of the 25 most recent merges are from non-members.
        "needs_assignment": False
    },
    # langgraph REMOVED 2026-08-07. Measured, not guessed:
    #   * require_issue_link.yml (live since 2026-03-24) closes every PR
    #     labelled `external` unless the author is assigned to the linked
    #     issue. Since then: 1 external PR merged, 522 closed unmerged.
    #   * That one, #7544, got in because a maintainer manually removed the
    #     label — the override path, not the assignment path. NO PR has ever
    #     passed via `reopen_on_assignment.yml`, which is the route we were
    #     waiting on.
    #   * Of 232 people who publicly asked to take an issue, exactly one ever
    #     merged anything (YassinNouh21) and all five of his merges predate
    #     the gate; his post-gate code PRs were not merged.
    #   * Our own record: 8 claims, 7 comments since 2026-07-30, zero
    #     assignments — and no assign event ever occurred on any of them.
    # Its 2 dispatch units went to hermes, which turns 6 PRs into 5 landed
    # commits. Re-adding it needs a route through the override, not a longer
    # wait for an assignment that does not come.
    "langchain": {
        "upstream": "langchain-ai/langchain",
        # new-contributor is a PR label, not an issue label — it matched nothing.
        # The issues that actually get assigned and merged carry bug + external:
        # ccurme and hwchase17 assigned three external contributors in two days.
        "searches": ['label:"help wanted"', "label:bug sort:created-desc"],
        "max_vet": 40,
        "exclude_labels": {"open-swe", "codex"},
        "needs_assignment": True,
        # Most partner integrations are released from their own repos, so an
        # issue about one cannot be fixed from libs/ here.
        "exclude_title": r"(langchain[- ](google|aws|openai|anthropic|community|mongodb|pinecone|weaviate|chroma|qdrant)|langsmith|langserve|sitemapload|sitemap loader|recursiveurlload|webbaseload|seleniumurlload|playwrightload)",
    },
    "openclaw": {
        "upstream": "openclaw/openclaw",
        "searches": ["label:clawsweeper:queueable-fix", "sort:created-desc"],
        # Wait past ClawSweeper's p90 triage latency (36 min) before taking an
        # untriaged issue, so its verdict is visible rather than raced.
        "min_age_minutes": 40,
        "exclude_labels": {
            "clawsweeper:no-new-fix-pr",
            "clawsweeper:linked-pr-open",
            "clawsweeper:needs-maintainer-review",
            "clawsweeper:needs-product-decision",
            "clawsweeper:needs-security-review",
            "clawsweeper:needs-info",
            # ClawSweeper triages this repo itself and says plainly what it
            # wants. Racing it wastes sessions on issues it has already ruled
            # out: #124314 was filed at 00:09 and dispatched at 00:10, and the
            # agent spent a whole session to conclude "already fixed on main" —
            # which is precisely what `not-repro-on-main` means. The rest are
            # gates we cannot pass: we have no way to run a live repro, an
            # idea-archive entry is parked by design, and bulk-filed marks a
            # filing spree rather than a defect.
            "clawsweeper:not-repro-on-main",
            "clawsweeper:needs-live-repro",
            "clawsweeper:idea-archive",
            "clawsweeper:bulk-filed",
            "r: spam",
        },
    },
    "hermes": {
        "upstream": "NousResearch/hermes-agent",
        "searches": ["label:type/bug sort:created-desc", "sort:created-desc"],
        # The sweeper's labels are three families, and only one disqualifies:
        #   sweeper:cannot-reproduce / implemented-on-main / incoherent /
        #     not-planned   -> genuinely "do not fix"
        #   sweeper:risk-*   -> a risk annotation (compatibility, message
        #     delivery, windows, ...)
        #   sweeper:blast-*  -> blast radius
        # Excluding the whole `sweeper:` prefix therefore dropped 20 of the 33
        # open type/bug issues, including the exact class we have already fixed
        # and landed: #75790 carries sweeper:risk-compatibility and
        # sweeper:risk-platform-windows and its commits are on main.
        "exclude_labels": {
            "invalid", "needs-repro", "needs-decision", "duplicate",
            "sweeper:cannot-reproduce", "sweeper:implemented-on-main",
            "sweeper:incoherent", "sweeper:not-planned",
        },
        "max_vet": 40,
    },
    "firecrawl": {
        "upstream": "firecrawl/firecrawl",
        "searches": ["label:bug sort:created-desc", "sort:created-desc"],
        "exclude_labels": set(),
    },
    # Two inference engines added 2026-08-07. Both were measured against the
    # repos already here and are markedly more open to outsiders: over two weeks
    # vllm merged 74 of 100 sampled PRs from forks, by 60 distinct external
    # authors, median 21h and +28 lines; sglang 19 of 100 by 16 authors, median
    # 20h and +20 lines. Compare openclaw (8 external authors, +180 median) or
    # hermes (2). The small median patch size is the point: it is the size this
    # pipeline actually lands.
    #
    # Neither needs a GPU of ours. sglang runs its 1gpu/2gpu jobs on fork PRs
    # automatically and the logs are readable through the Actions API; vllm gates
    # CI behind a maintainer applying `ready`, observed 2.7-4.4h after opening.
    # So their CI is the test rig — which is also why the excludes below matter:
    # a kernel or quantisation change cannot be reasoned about without hardware,
    # and using someone's GPU cluster to iterate on a guess is rude.
    # vllm SUSPENDED 2026-08-13 on Bruce's instruction. Two PRs, zero landed:
    # its pre-run-check refuses to run CI for an author with fewer than 4 merged
    # PRs there unless a maintainer applies `verified`/`ready`/`ready-run-all-tests`,
    # so a first contribution shows red for a reason that has nothing to do with
    # the patch and can only be cleared by someone else. The DCO sign-off work
    # (git commit -s) stays in fix-one.yml for whenever this comes back.
    # sglang REMOVED 2026-08-15 on Bruce's instruction. Added 2026-08-07 and it
    # never dispatched once in eight days: 19 of the 29 issues vetted already
    # carried a PR, the rest hit the kernel/CUDA/quantization exclusions that
    # exist because we cannot test GPU work. Zero PRs, zero commits. The repo is
    # not hostile — it is contested by people who can run the hardware, which we
    # cannot. vllm was suspended two days earlier for the adjacent reason.
    # Evaluated 2026-08-07 and deliberately NOT added, so it is not re-evaluated:
    #
    #   Arize-ai/phoenix — over six weeks, 300 sampled merges contained 5 PRs
    #     from forks, by 5 different people, none of them returning. And the
    #     licence is Elastic License 2.0, not an OSI licence: it forbids
    #     offering the software as a hosted service and forbids circumventing
    #     its licence-key functionality. Contributing to a commercial product
    #     under those terms is a decision for a person to make, not a default.
    #
    #   openai/openai-agents-python — the best merge machinery measured
    #     anywhere: 104 external PRs in six weeks, 4h median. But 12 open
    #     issues, most of them server-side faults we cannot touch, and its
    #     merged PRs reference no issue at all — contributors read the code and
    #     open a PR directly. This pipeline selects from issues, so the supply
    #     is empty for us. Revisit only if it learns to find work in code.
    #
    # Added 2026-08-07. The most favourable numbers measured so far for the
    # kind of change this pipeline produces: over two weeks 18 of 20 sampled
    # fork PRs were merged by someone other than their author, by 11 distinct
    # outside contributors, at a median of +19 added lines — the smallest median
    # of any repo here, and the size that actually lands. MIT, pure Python, no
    # hardware to reason about.
    #
    # It carries no "good first issue" or "help wanted" labels, so selection
    # runs off `bug` (66 open at the time of adding).
    # pydantic-ai REMOVED 2026-08-12: the org BLOCKED us. Cause is ours and is
    # not in dispute. claim.sh posted the identical line "I'd like to take this
    # one if it's still open — happy to put up a PR." on EIGHT of their issues
    # over five days (#7281 #7211 #7147 #7133 #7284 #7338 #7347 #7397) and
    # delivered zero PRs from any of them. The last went up 3 hours before the
    # block, onto an issue opened by dsfaccini — the maintainer who had, nine
    # hours earlier, apologised to us and asked us not to duplicate work. One
    # of the eight (#7211) their own triage bot had already marked
    # "not-a-bug, signal 3/10". From their side that is a bot squatting on a
    # tracker. Do not re-add.
    "dify": {
        "upstream": "langgenius/dify",
        "searches": ['label:"good first issue"', 'label:"🙏 help wanted"', "label:bug sort:created-desc"],
        "exclude_labels": set(),
        # The PR template asks agent-created PRs to end the description with
        # "From <Tool Name>", so this repo explicitly anticipates them.
        "max_vet": 40,
    },
    "autogpt": {
        "upstream": "Significant-Gravitas/AutoGPT",
        "searches": ['label:"good first issue"', 'label:"help wanted"', "label:bug sort:created-desc"],
        "exclude_labels": set(),
        "max_vet": 40,
    },
    "comfyui": {
        "upstream": "Comfy-Org/ComfyUI",
        "searches": ["sort:created-desc"],
        "exclude_labels": set(),
        # only CPU-verifiable areas are in scope for our pipeline
        "require_body": r"(Traceback|Error|Exception|folder_paths|execution\.py|server\.py|cli_args)",
    },
}

# Missed "Hi, I'd like to work on this issue" on dify#39736 and dispatched a
# fixer at an issue someone had claimed three days earlier. The old pattern
# only covered "I'll take/work on"; people announce intent in many more ways,
# and GitHub's comment box turns ' into the Unicode right single quote, which
# `i'?ll` does not match. Both forms are accepted below.
_APOS = "['\u2019]?"
# scan.py is imported from workflow steps and from verify.sh with varying
# working directories, so a bare `import intent` resolves only by luck. Anchor
# it to this file's own directory.
if str(pathlib.Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import intent

CLAIM_PHRASES = re.compile(
    r"(i" + _APOS + r"(ll|d) (like to )?(take|work on|fix|pick|dig|handle|tackle|attempt|submit|open|raise|try(?:ing)?\s+to\s+\w+|look\s+into)"
    r"|i (would|want|wanna) (like )?to (work on|take|tackle|fix|try\s+to\s+\w+)"
    r"|let me (take|work on|handle|try\s+to\s+\w+)"
    r"|can i (work on|take|pick|be assigned)"
    r"|working on (this|it)|assign (this )?to me|/assign"
    r"|i(['’]ve got|\b[^.!?]{0,80}?\b(have|prepared)) (a|an|my)[\w\s-]{0,24}(fix|pr|pull request|patch|change)\b|opened a pr|raised a pr|pr is up|submitted a pr"
    r"|(i" + _APOS + r"ll|i will|will) (submit|open|raise|create|send) a (pr|pull request|patch)"
    r"|on it\b)",
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


_LABEL_RE = re.compile(r'label:"([^"]+)"|label:(\S+)')


def search_issues(upstream: str, qualifier: str, limit: int,
                  ignore_assignees: bool = False) -> list[dict]:
    """List candidate issues.

    Uses the plain REST issues endpoint (5000 req/hr) instead of the search API
    (30 req/min, and very quick to trip GitHub's *secondary* burst limit). On a
    shared Actions runner IP every search call 403'd and a full scan spent ~10
    minutes in pure backoff — longer than the 5-minute schedule. Switching cut a
    full scan to ~1 minute.

    Label qualifiers map onto the REST `labels` filter; anything the endpoint
    cannot express is applied client-side by vet().
    """
    # This endpoint returns pull requests mixed in with issues — on spec-kit four
    # of the five newest items were PRs — so over-fetch and let the filter below
    # cut it back to `limit` real issues.
    fetch = min(100, max(limit * 4, 30))
    params = ["-f", "state=open", "-f", "sort=created", "-f", "direction=desc",
              "-f", f"per_page={fetch}"]
    labels = [a or b for a, b in _LABEL_RE.findall(qualifier)]
    if labels:
        params += ["-f", f"labels={','.join(labels)}"]

    out = gh(["api", "-X", "GET", f"repos/{upstream}/issues", *params], kind="other")
    items = json.loads(out or "[]")
    # `no:assignee` is search-only, so that filter is applied here too.
    # `no:assignee` is search-only, so that filter is applied here too — but it
    # is a proxy for "someone is already on this", and on repos that assign a
    # triager to every issue the proxy is simply wrong. See langfuse's config.
    if ignore_assignees:
        issues = [i for i in items if "pull_request" not in i]
    else:
        issues = [i for i in items if "pull_request" not in i and not i.get("assignees")]
    return issues[:limit]


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
    # `-X GET` is not optional: `gh api -f` sends a POST, so this endpoint was
    # being asked to CREATE a comment and answered 422 ("body wasn't supplied").
    # The bare `except: return []` below turned that into "nobody has claimed
    # this issue" for every issue, silently, forever — claim detection had never
    # once fired. Any future failure is now logged rather than swallowed.
    try:
        out = gh(["api", "-X", "GET", f"repos/{upstream}/issues/{number}/comments",
                  "-f", "per_page=60", "--jq", "[.[] | {u:.user.login, b:.body}]"])
    except Exception as e:  # noqa: BLE001
        print(f"    claimants({upstream}#{number}) failed: {str(e)[:120]}", file=sys.stderr)
        return []
    who, judged = [], 0
    for c in json.loads(out or "[]"):
        u = c["u"]
        if "[bot]" in u or u in who:
            continue
        # Strip quoted text: a maintainer replying to a claimant quotes their
        # "I'd like to work on this", which would otherwise count as a second
        # claimant and report the issue as "swarmed" for the wrong reason.
        body = re.sub(r"^\s*>.*$", "", c.get("b") or "", flags=re.M)
        # Our own comment is the pipeline's claim. Its wording is ours and never
        # ambiguous, so spend no judgement on it — vet() only needs to know it
        # is there, to set self_claimed.
        if u.lower() == "chelsealong":
            if CLAIM_PHRASES.search(body):
                who.append(u)
            continue
        if judged >= 15:
            # A thread this long is a swarm by any reading; stop paying for it.
            break
        claimed, why = intent.is_claim(body, author=u, default=True)
        judged += 1
        if claimed:
            who.append(u)
            if len([w for w in who if w.lower() != "chelsealong"]) >= 2:
                break
    return who


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
    # An unfilled template title means the reporter did not describe the problem.
    # langfuse#16160 and #16162 both arrived as "bug: <short description>" from
    # one account, and their bodies are abuse rather than a report — the
    # substance check counts characters, and invective has plenty. This is
    # repo-independent: nobody's template placeholder is a workable issue.
    # Some repos triage themselves, and arriving before they finish is worse
    # than arriving late. openclaw's ClawSweeper reaches a verdict at a median
    # of 8 minutes and within 36 at p90; of the 36 issues it ruled on over
    # three days, only 7 were `queueable-fix` and 29 carried at least one
    # hands-off label. Racing it means roughly four dispatches in five land on
    # something it is about to rule out — #124314 was filed at 00:09, taken at
    # 00:10, and cost a full session to conclude "already fixed on main", which
    # is what ClawSweeper's `not-repro-on-main` would have said for free.
    # Speed is only an advantage where nobody else is deciding.
    if mins := cfg.get("min_age_minutes"):
        created = issue.get("created_at") or issue.get("createdAt") or ""
        if created:
            try:
                if age_hours(created) * 60 < mins:
                    return False, f"only {age_hours(created)*60:.0f} min old; " \
                                  f"waiting {mins} min for the repo's own triage", {}
            except Exception:  # noqa: BLE001
                pass

    if re.search(r"<\s*(short description|title|summary|brief description|one line)\s*>"
                 r"|^\s*(bug|feature|\[bug\]|\[feature\])\s*:\s*$", title, re.I):
        return False, "title is an unfilled template placeholder", {}
    if (pat := cfg.get("require_body")) and not re.search(pat, issue.get("body") or "", re.I):
        return False, "body lacks required in-scope signal", {}
    if issue.get("draft"):
        return False, "draft", {}

    # An issue with no body is unfixable, not merely thin. AutoGPT collects
    # client-side error reports whose entire content is the exception string in
    # the title: no repro, no version, no stack, no environment. Three of those
    # (#11089, #11095, #11098) were queued as candidates. Any "fix" for them is
    # a guess at which of several call sites raised, and a guessed fix is the
    # kind of PR that gets closed as invalid. Require enough body to reason from.
    body = (issue.get("body") or "").strip()
    # Checklists in an issue template are boilerplate, not content.
    substance = re.sub(r"^\s*[-*]\s*\[[ xX]\]\s.*$", "", body, flags=re.M)
    substance = re.sub(r"^\s*#{1,6}\s.*$", "", substance, flags=re.M).strip()
    if len(substance) < 80:
        return False, f"body has no substance ({len(substance)} chars after template)", {}

    prs = linked_prs(upstream, num)
    if prs:
        return False, f"already has PR(s): {prs[:3]}", {}

    who = claimants(upstream, num)
    # Our own claim is the pipeline's own comment, not a rival. Counting it as
    # one made claiming an act of self-exclusion: claim.sh commented, the next
    # scan rejected the issue as "claimed by chelsealong", and it left the queue
    # for good. langgraph accumulated eight such claims and zero PRs; adk had
    # two. Gated repos still wait for the assignment — watch.promote_claims
    # tracks that separately — but the issue stays visible either way.
    others = [w for w in who if w.lower() != "chelsealong"]
    if len(others) >= 2:
        return False, f"swarmed ({len(others)} claimants)", {}
    if others:
        return False, f"claimed by {others[0]}", {}

    return True, "clear", {"labels": sorted(labels),
                           "comments": issue.get("comments", 0),
                           "self_claimed": len(who) > len(others)}


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
            for it in search_issues(upstream, qual, limit,
                                    ignore_assignees=cfg.get("ignore_assignees", False)):
                seen.setdefault(it["number"], it)
        except Exception as e:  # noqa: BLE001
            msg = f"search failed [{qual}]: {e}"
            errors.append(msg)
            print(f"  ! {msg}", file=sys.stderr)

    ok, rejected = [], []
    # vet freshest-first and stop early: unclaimed issues are almost always recent,
    # and each vet costs up to three API calls.
    # Per-repo vetting depth. The default takes only the freshest few, which
    # suits a fast tracker where unclaimed issues are recent. It starves a
    # high-backlog, low-velocity one: langfuse carries ~198 open bugs, and of
    # the 15 freshest, 7 already had a PR — so the queue came back empty while
    # most of the backlog was never looked at.
    depth = cfg.get("max_vet", max_vet)
    ordered = sorted(seen.items(), key=lambda kv: kv[1]["created_at"], reverse=True)[:depth]
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
    ap.add_argument("--unclaim", nargs=2, metavar=("KEY", "NUMBER"),
                    help="release a claim so the issue can be retried "
                         "(use when a run failed without doing any work)")
    a = ap.parse_args()

    if a.unclaim:
        key, num = a.unclaim[0], int(a.unclaim[1])
        p = STATE / f"{key}.json"
        if not p.exists():
            print(json.dumps({"error": f"no state for {key}"}))
            return 1
        d = json.loads(p.read_text())
        before = list(d.get("claimed", []))
        d["claimed"] = [n for n in before if n != num]
        p.write_text(json.dumps(d, indent=2) + "\n")
        print(json.dumps({"unclaimed": num, "remaining": d["claimed"]}))
        return 0

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
