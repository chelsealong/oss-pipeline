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
    # "identical" means the commit IS the current tip, which is as landed as it
    # gets — and it is what the newest landing always reports. adk e4ba7040 was
    # the head of main forty-five minutes after being imported and this returned
    # False for it, so the freshest result was the one the ledger could not see.
    return out.strip() in ("behind", "identical")


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



# ---------------------------------------------------------------------------
# Credited but not authored.
#
# hermes#89815 merged on 2026-08-19 saying "Supersedes #75649, #86637, #87672,
# #88642" and "Credit: @rikkarth, @chelsealong, @lilShawtty-byte,
# @olympusbuildz". Our analysis and fix went in; every commit is under the
# maintainer's name. Counting commits on main — which is the only honest
# measure of authorship — that is a zero, and the ledger was right to leave the
# total at 33.
#
# It is still a real contribution, and it is the kind of evidence an
# immigration filing rests on. So it is recorded, and recorded SEPARATELY. A
# number that survives being checked line by line is worth more than a larger
# one that collapses, and merging the two categories would make every figure we
# quote arguable.
#
# The double-counting trap: most PRs here are salvages where authorship WAS
# preserved (teknium1 writes "with your authorship preserved"), and those
# already appear as our commits. A PR belongs in this list only when NONE of
# its commits carries one of our addresses.
CREDIT_MARKS = ("chelsealong",)


def credited(repo: str) -> list[dict]:
    """Merged PRs by others that name us and carry no commit of ours."""
    q = f"repo:{repo} is:pr is:merged chelsealong in:body"
    try:
        raw = _gh(["api", "graphql", "-f", "query=" + (
            '{search(query:"%s", type:ISSUE, first:40){nodes{... on PullRequest{'
            "number title author{login} mergedAt body "
            "commits(first:30){nodes{commit{author{email}}}}}}}}" % q)])
        nodes = json.loads(raw)["data"]["search"]["nodes"]
    except Exception as e:  # noqa: BLE001
        print(f"  {repo}: credit search failed ({str(e)[:70]})", file=sys.stderr)
        return []
    out = []
    for pr in nodes:
        if not pr or not pr.get("number"):
            continue
        if (pr.get("author") or {}).get("login") == "chelsealong":
            continue
        emails = {(c["commit"]["author"] or {}).get("email", "")
                  for c in (pr.get("commits") or {}).get("nodes", [])}
        if any(e in emails for e in ME_EMAILS):
            continue          # authorship preserved — already counted as a commit
        body = pr.get("body") or ""
        # Require an explicit naming, not an incidental mention.
        line = next((l for l in body.splitlines()
                     if any(m in l.lower() for m in CREDIT_MARKS)
                     and any(k in l.lower() for k in
                             ("credit", "co-auth", "thanks", "supersede", "authorship",
                              "based on", "originally"))), "")
        if not line:
            continue
        out.append({"repo": repo, "pr": pr["number"], "by": pr["author"]["login"],
                    "at": (pr.get("mergedAt") or "")[:10],
                    "title": (pr.get("title") or "")[:80],
                    "evidence": line.strip()[:200]})
    return out


def update_credited() -> int:
    d = _load()
    d.setdefault("credited", {})
    added = 0
    for repo in upstreams():
        for c in credited(repo):
            key = f"{c['repo']}#{c['pr']}"
            if key in d["credited"]:
                continue
            d["credited"][key] = c
            added += 1
            print(f"  ~ credited (no commit of ours): {key} by {c['by']} — {c['title'][:44]}")
    if added:
        _save(d)
    return added



# ---------------------------------------------------------------------------
# What maintainers actually said.
#
# Numbers are the weakest evidence we hold. "31 commits" invites the question of
# whether they mattered; sallyom writing "this is the best-fix owner-boundary
# repair" about openclaw#124954, or spec-kit's mnriem — who had asked us three
# times to stop opening a certain kind of PR — replying "Thank you!" to a code
# fix, does not.
#
# Only humans with standing in the project, and only substantive evaluation. A
# bot's approval is not evidence; ComfyUI's approves nearly everything. A
# template thank-you is not evidence either, which is why the judgement is left
# to a model rather than a keyword list: "thanks for the contribution" and "this
# is the right fix of the three proposed" both contain "thank".
QUOTE_SYSTEM = (
    "You are reading a comment left by a maintainer on a pull request from an "
    "outside contributor. Decide whether it contains a SUBSTANTIVE evaluation of "
    "the contributor's work — a judgement about its quality, correctness, or "
    "value — as opposed to routine process.\n\n"
    "NOT substantive: a templated thank-you; a request for changes; a note that "
    "CI failed; an automated summary; asking a question; saying it was merged "
    "with no assessment.\n"
    "Substantive: calling the fix correct, best, or the right approach; saying it "
    "found a real bug others missed; praising the analysis, the tests or the "
    "report; thanking us for a specific thing they name.\n\n"
    "Quote the single strongest sentence VERBATIM. Do not paraphrase.\n\n"
    "Also say whether the evaluation is favourable to the contributor. Record "
    "both kinds: a refusal explaining why a patch was wrong is as worth keeping "
    "as praise, and only recording praise would make the record useless.\n\n"
    'Reply with JSON only: {"substantive": true|false, "favourable": true|false, '
    '"quote": "", "who_matters": "<their role in one or two words>"}'
)


def merge_authority(repo: str) -> set:
    """Logins that have actually merged pull requests here.

    authorAssociation is the wrong test and it cost us the best quote we have.
    sallyom merged openclaw#124954 by squash and wrote "this is the best-fix
    owner-boundary repair" — and GitHub reports them as CONTRIBUTOR, because
    that field describes their relationship to the repo as an AUTHOR, not their
    permissions. Who merges things is the signal.
    """
    out = set()
    # Whoever merged one of OUR pull requests unquestionably has standing here,
    # and this is the set we actually care about. Sampling the repo's newest 60
    # merges alone missed sallyom, who squash-merged openclaw#124954 and left
    # the strongest quote we have.
    for q, path in ((f"repo:{repo} author:chelsealong is:pr is:merged",
                     ["data", "search", "nodes"]),):
        try:
            raw = _gh(["api", "graphql", "-f", "query=" + (
                '{search(query:"%s", type:ISSUE, first:50){nodes{... on PullRequest{'
                "mergedBy{login}}}}}" % q)])
            d = json.loads(raw)
            for n in d["data"]["search"]["nodes"]:
                if n and n.get("mergedBy"):
                    out.add(n["mergedBy"]["login"])
        except Exception:  # noqa: BLE001
            pass
    try:
        raw = _gh(["api", "graphql", "-f", "query=" + (
            '{repository(owner:"%s",name:"%s"){pullRequests(states:MERGED,first:100,'
            "orderBy:{field:UPDATED_AT,direction:DESC}){nodes{mergedBy{login}}}}}"
            % tuple(repo.split("/")))])
        for n in json.loads(raw)["data"]["repository"]["pullRequests"]["nodes"]:
            if n and n.get("mergedBy"):
                out.add(n["mergedBy"]["login"])
    except Exception:  # noqa: BLE001
        pass
    return {w for w in out if w}


def quotes(repo: str) -> list[dict]:
    """Substantive maintainer evaluations on our PRs in this repo."""
    import intent
    q = f"repo:{repo} author:chelsealong is:pr"
    try:
        raw = _gh(["api", "graphql", "-f", "query=" + (
            '{search(query:"%s", type:ISSUE, first:50){nodes{... on PullRequest{'
            "number url "
            "comments(first:40){nodes{author{login} authorAssociation bodyText createdAt}} "
            "reviews(first:20){nodes{author{login} authorAssociation state bodyText createdAt}}"
            "}}}}" % q)])
        nodes = json.loads(raw)["data"]["search"]["nodes"]
    except Exception as e:  # noqa: BLE001
        print(f"  {repo}: quote search failed ({str(e)[:70]})", file=sys.stderr)
        return []
    authority = merge_authority(repo)
    out = []
    for pr in nodes:
        if not pr or not pr.get("number"):
            continue
        said = []
        for c in (pr.get("comments") or {}).get("nodes", []) or []:
            said.append(c)
        for r in (pr.get("reviews") or {}).get("nodes", []) or []:
            said.append(r)
        for c in said:
            who = ((c.get("author") or {}).get("login") or "")
            assoc = c.get("authorAssociation") or ""
            body = (c.get("bodyText") or "").strip()
            # Standing matters, and bots have none. ComfyUI's bot approves
            # almost everything, so its approval says nothing about the code.
            if not who or "[bot]" in who or who == "chelsealong":
                continue
            if assoc not in ("COLLABORATOR", "MEMBER", "OWNER") and who not in authority:
                continue
            if len(body) < 25:
                continue
            v = intent._ask(QUOTE_SYSTEM, intent._strip_markup(body)[:4000], author=who)
            if not v or not v.get("substantive") or not v.get("quote"):
                continue
            out.append({"repo": repo, "pr": pr["number"], "url": pr.get("url", ""),
                        "who": who, "assoc": assoc,
                        "favourable": bool(v.get("favourable", True)),
                        "role": str(v.get("who_matters", ""))[:60],
                        "at": (c.get("createdAt") or "")[:10],
                        "quote": str(v["quote"])[:400]})
    return out


def update_quotes(repos: list | None = None) -> int:
    d = _load()
    d.setdefault("quotes", {})
    added = 0
    for repo in (repos or upstreams()):
        for qt in quotes(repo):
            key = f"{qt['repo']}#{qt['pr']}@{qt['who']}@{qt['at']}"
            if key in d["quotes"]:
                continue
            d["quotes"][key] = qt
            added += 1
            print(f"  \u201c{qt['quote'][:70]}\u201d — {qt['who']} on {qt['repo']}#{qt['pr']}")
    if added:
        _save(d)
    return added


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
    cred = list((d.get("credited") or {}).values())
    if cred:
        print(f"\n  credited but NOT authored — {len(cred)}, deliberately not in the total above:")
        for c in sorted(cred, key=lambda x: x.get("at", "")):
            print(f"    {c['at']}  {c['repo']}#{c['pr']} by {c['by']}")
            print(f"        {c['evidence'][:110]}")
    qt = list((d.get("quotes") or {}).values())
    if qt:
        for good in (True, False):
            sub = [q for q in qt if bool(q.get("favourable", True)) is good]
            if not sub:
                continue
            print(f"\n  what maintainers said — {'favourable' if good else 'critical'}, {len(sub)}:")
            for q in sorted(sub, key=lambda x: x.get("at", "")):
                print(f"    {q['at']}  {q['repo']}#{q['pr']}  {q['who']}")
                print(f"        \u201c{q['quote'][:150]}\u201d")
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
        c = update_credited()
        print(f"\n  {n} new commit(s), {total} total; {c} new credited-only")
        report()
