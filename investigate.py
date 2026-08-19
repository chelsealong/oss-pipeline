#!/usr/bin/env python3
"""Investigate an issue against the repo before spending a Claude session on it.

Vetting answers "is this issue available" — open, unassigned, unclaimed, not
already fixed. It says nothing about whether the thing being asked for can be
built, and that is where the sessions go: measured over the week to 2026-08-16,
40% of runs where the generator actually ran produced no branch at all, and
generation is 59% of all Claude time.

The shadow scorer in ledger.py tried to predict that from the issue text alone.
Over 122 settled dispatches it separates — 41% of score>=4 opened a PR against
19% of score<=2 — but not sharply enough to gate on: cutting at 3 would have
dropped 31 dispatches to save, and lost 6 PRs with them, a fifth of the output.
Text alone is not enough information, which is the argument for fetching more
rather than for giving up.

So this reads the repository. It pulls the paths and symbols the issue names,
confirms they exist, fetches the code around them, and asks the model to decide
with that in hand. Three real candidates show the range:

  * llama_index#22721 names `function_calling.py ~L111/~L134` and quotes the
    `# TODO: no validation for streaming outputs` that is still in the file.
    A session was spent to conclude "design-scope discussion"; the code says
    otherwise.
  * llama_index#22733 asks "would it make sense to provide a safe replacement
    primitive" — a design question with no defect. Skippable without opening
    the repo at all.
  * mem0#6989 is a screenshot of a greyed-out Continue button in a hosted web
    app, with no stack, no repro and no code path. It is also, today, mem0's
    only candidate.

The output is deliberately not a yes/no. A verdict that says "do it" and stops
has thrown away everything it just learned, so it returns the files it found
and what it believes the change is — a briefing Claude starts from instead of
rediscovering. Whether that briefing helps is measurable and is not assumed
here: nothing calls this in the dispatch path yet.

    python3 investigate.py <repo_key> <issue>       # one issue, printed
    python3 investigate.py --cases cases.json       # scored against outcomes
"""

from __future__ import annotations

import json
import re
import sys

import intent
import scan

# Paths look like a/b/c.py; symbols like snake_case_name or Class.method. Both
# are noisy on their own — the confirmation step is what makes them useful.
PATH_RE = re.compile(r"\b[\w./-]+/[\w.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|rb|md|yaml|yml|toml)\b")
SYMBOL_RE = re.compile(r"`([A-Za-z_][\w.]{3,60})`|\b([a-z_][a-z0-9_]{5,40})\(\)")

SYSTEM = (
    "You decide whether an autonomous coding agent should spend a session on a "
    "GitHub issue. You are given the issue, and the repository code that was "
    "found for the paths and symbols it names.\n\n"
    "Say NO when: the issue asks whether something should be designed or "
    "supported rather than reporting a defect; the wanted behaviour is a "
    "product or API decision; it needs a reproduction on hardware, an account, "
    "a hosted service or data we do not have; it is a support request or a "
    "screenshot with no code path; the code shown does not contain the problem "
    "described.\n"
    "Say YES when the code confirms a concrete defect or a clearly stated small "
    "change, and a fix can be written and tested from what is here.\n\n"
    "Be specific in `where` and `change` — they are handed to the agent that "
    "writes the patch, so it does not have to find this again. Leave them "
    "empty when the answer is NO.\n\n"
    'Reply with JSON only: {"do": true|false, "why": "<15 words or fewer>", '
    '"where": ["path:line or path"], "change": "<one sentence>"}'
)


def _issue(upstream: str, number: int) -> dict:
    raw = scan.gh(["api", "-X", "GET", f"repos/{upstream}/issues/{number}",
                   "--jq", "{t:.title, b:.body, labels:[.labels[].name]}"])
    d = json.loads(raw or "{}")
    try:
        craw = scan.gh(["api", "-X", "GET", f"repos/{upstream}/issues/{number}/comments",
                        "-f", "per_page=10", "--jq", "[.[] | .body]"])
        d["comments"] = json.loads(craw or "[]")[:6]
    except Exception:  # noqa: BLE001
        d["comments"] = []
    return d


def candidates(text: str) -> tuple[list[str], list[str]]:
    paths = list(dict.fromkeys(PATH_RE.findall(text)))[:4]
    syms = []
    for a, b in SYMBOL_RE.findall(text):
        s = a or b
        if s and s not in syms and "." not in s[:1]:
            syms.append(s)
    return paths, syms[:5]


def fetch_code(upstream: str, paths: list[str], syms: list[str], ref: str = "") -> list[dict]:
    """Whatever of the named code actually exists, with a window of context.

    A path the issue names but the repo does not have is itself a finding: the
    report is stale, which is one of the three ways hermes#82124 went wrong.
    """
    out: list[dict] = []
    for p in paths:
        for attempt in (p, p.split("/", 1)[-1] if "/" in p else p):
            try:
                args = ["api", "-X", "GET", f"repos/{upstream}/contents/{attempt}"]
                if ref:
                    # Evaluation only. Reading today's file for an issue we have
                    # already fixed shows the fix, and the judge correctly says
                    # "already handled" — which measures nothing. Pin to the
                    # commit before the fix landed to ask the question that was
                    # actually being asked at the time.
                    args += ["-f", f"ref={ref}"]
                raw = scan.gh(args + ["--jq", ".content"])
            except Exception:  # noqa: BLE001
                continue
            import base64
            try:
                body = base64.b64decode(raw.strip().strip('"')).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            out.append({"path": attempt, "found": True, "text": body[:6000]})
            break
        else:
            out.append({"path": p, "found": False, "text": ""})
    # Code search is the fallback, and it is where this earns its keep: over a
    # set of nine issues whose fixes actually landed, every wrong verdict said
    # some version of "the code shown does not contain the problem". The
    # judgement was sound each time; the retrieval was not. One query per issue
    # is the budget — the search bucket is 30/min.
    if syms and not any(o["found"] for o in out):
        for sym in syms[:2]:
            q = f"repo:{upstream} {sym}"
            try:
                raw = scan.gh(["api", "-X", "GET", "search/code", "-f", f"q={q}",
                               "-f", "per_page=2", "--jq", "[.items[] | .path]"],
                              kind="search")
                hits = json.loads(raw or "[]")
            except Exception:  # noqa: BLE001
                continue
            # Fetch the file rather than recording the path. A bare path tells
            # the judge nothing it can check the report against, which is how
            # "located by symbol search" still produced "code shown does not
            # contain the problem".
            for path in hits[:2]:
                got = fetch_code(upstream, [path], [], ref)
                out += [g for g in got if g["found"]]
            if any(o["found"] for o in out):
                break
    return out


def investigate(repo_key: str, number: int, ref: str = "") -> dict:
    cfg = scan.REPOS.get(repo_key)
    if not cfg:
        return {"do": True, "why": f"unknown repo {repo_key}", "where": [], "change": ""}
    upstream = cfg["upstream"]
    try:
        iss = _issue(upstream, number)
    except Exception as e:  # noqa: BLE001
        # Fail OPEN. Declining to investigate is not evidence the issue is bad,
        # and the vetting that already passed is the safety net.
        return {"do": True, "why": f"could not read issue ({type(e).__name__})",
                "where": [], "change": ""}

    text = "\n".join([iss.get("t") or "", iss.get("b") or "", *(iss.get("comments") or [])])
    paths, syms = candidates(text)
    code = fetch_code(upstream, paths, syms, ref) if (paths or syms) else []

    parts = [f"# {upstream}#{number}", f"TITLE: {iss.get('t')}",
             f"LABELS: {', '.join(iss.get('labels') or []) or '(none)'}",
             f"BODY:\n{(iss.get('b') or '')[:3000]}"]
    for c in (iss.get("comments") or [])[:3]:
        parts.append(f"COMMENT:\n{(c or '')[:600]}")
    for f in code:
        if f["found"]:
            parts.append(f"FILE {f['path']}:\n{f['text']}")
        else:
            parts.append(f"FILE {f['path']}: NOT FOUND IN REPO — the report may be stale")
    if not code:
        parts.append("NO CODE LOCATED: the issue names no file or symbol that resolves.")

    verdict = intent._ask(SYSTEM, "\n\n".join(parts)[:24000], author=f"{repo_key}#{number}")
    if verdict is None:
        return {"do": True, "why": "judge unavailable — not blocking",
                "where": [], "change": ""}
    return {
        "do": bool(verdict.get("do", True)),
        "why": str(verdict.get("why", ""))[:80],
        "where": [str(w)[:120] for w in (verdict.get("where") or [])][:5],
        "change": str(verdict.get("change", ""))[:300],
        "located": [f["path"] for f in code if f["found"]],
    }


def _run_cases(path: str) -> None:
    cases = json.load(open(path))
    right = 0
    for c in cases:
        v = investigate(c["repo_key"], c["issue"], c.get("ref", ""))
        ok = v["do"] == c["want"]
        right += ok
        print(f"  {'ok  ' if ok else 'WRONG'} {c['repo_key']}#{c['issue']} "
              f"want={c['want']} got={v['do']}  {v['why'][:48]}")
        if v.get("where"):
            print(f"        where: {', '.join(v['where'][:3])}")
    print(f"  {right}/{len(cases)} correct")


if __name__ == "__main__":
    if "--cases" in sys.argv:
        _run_cases(sys.argv[sys.argv.index("--cases") + 1])
    else:
        key, num = sys.argv[1], int(sys.argv[2])
        print(json.dumps(investigate(key, num), indent=2, ensure_ascii=False))
