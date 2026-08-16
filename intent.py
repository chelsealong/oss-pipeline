#!/usr/bin/env python3
"""Ask a model whether a comment is claiming work, instead of matching phrases.

Three failures in one week made the case that a phrase list cannot do this:

  * "I'd like to attempt a fix" was not in the verb list, so langchain#38814 read
    as unclaimed and we posted a claim on top of two people who were already on it.
  * bare `pr` matched inside "problem", so "I have a similar problem on Windows"
    read as a claim.
  * bare `try` matched "I will try this to run the build locally" — an offer to
    TEST our branch — and the stand-down mechanism closed adk#6697, which a
    collaborator had just called "the right one to land".

Each fix was a patch for that one phrasing. The next phrasing breaks it again.

Design notes that matter more than the code:

  * The regex is kept, as a LOOSE pre-filter for recall, not as the decision. If
    a comment has no first-person marker at all it cannot be a claim, and asking
    about it would be waste. Everything that survives goes to the model.
  * The caller picks which way a failure falls, because the two callers face
    opposite costs. scan.claimants passes default=True: if the judge is down we
    treat the issue as claimed and skip it, losing one candidate. watch-prs
    passes default=False: there a "claimed" verdict CLOSES our own open PR, so
    an unreachable judge must never trigger one. Defaulting both to "closed"
    would have made an API outage close every PR we have open.
  * Every verdict is cached by content hash and logged with the model's reason,
    so a bad call can be traced to the judge rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
CACHE = ROOT / "state" / "intent-cache.json"
LOG = ROOT / "intent.log"
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3.7-flash"
TIMEOUT = 25

# Deliberately loose: anything a claim could possibly contain. Its only job is to
# keep comments with no first-person marker away from the model. The precise
# regex it replaces scored 23/24 on the test set — the miss was "i do like to
# work on this issue", which is exactly the shape a phrase list keeps missing.
MAYBE_CLAIM = re.compile(
    r"\b(i|i'?m|i'?ll|i'?ve|i'?d|me|my|we|we'?ll)\b"
    r"|/assign|\bassign\b"
    # Subjectless claims are common and carry no pronoun at all: "working on it",
    # "on it", "taking this one". The first version of this filter dropped
    # "working on it" before the model ever saw it — a pre-filter that is too
    # tight simply moves the phrase-list problem one layer up.
    r"|\b(working|taking|picking|looking|on it|wip)\b",
    re.I,
)

SYSTEM = (
    "You judge intent in GitHub issue comments. Answer only whether the comment's "
    "author is CLAIMING the work — saying they will implement the fix, are "
    "implementing it, have implemented it, or are asking to be assigned so they "
    "can implement it.\n\n"
    "These are NOT claims:\n"
    "- offering to test, run, verify or reproduce someone else's build, branch or PR\n"
    "- reporting the bug, adding details, or confirming it happens to them too\n"
    "- asking a question, asking for a workaround, or asking whether a PR exists\n"
    "- thanking someone, or complaining\n"
    "- a maintainer quoting or replying to someone else's claim\n\n"
    'Reply with JSON only: {"claim": true|false, "why": "<8 words or fewer>"}'
)


def _load_key() -> str:
    key = os.environ.get("QWEN_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "QWEN_API_KEY":
                return v.strip()
    return ""


def _cache() -> dict:
    try:
        return json.loads(CACHE.read_text()) if CACHE.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(cache: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        # Bounded: keep the most recent 4000 verdicts.
        if len(cache) > 4000:
            cache = dict(list(cache.items())[-4000:])
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def _strip_markup(text: str) -> str:
    """Drop what carries no intent: quoted text, HTML comments, images, badges.

    Bot comments are mostly machinery — `<!-- coderabbit-cli-agent-hint:v3 ... -->`,
    shield badges, collapsed <details> wrappers. Left in, they crowd out the one
    sentence that decides the verdict and cost tokens to no purpose.
    """
    t = re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)
    t = re.sub(r"^\s*>.*$", "", t, flags=re.M)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)      # images and badges
    # Drop <details> WITH ITS CONTENTS, not just the tags. coderabbitai wraps a
    # multi-hundred-line "Analysis chain" — the shell it ran and everything the
    # shell printed — in front of the one sentence that is the actual finding.
    # Stripping only the tags left the log in place, it filled the 1200-char
    # window, and the judge correctly reported "analysis log with no findings"
    # on three real Major-severity defects. The finding lives after the log.
    # Unterminated <details> is common — GitHub renders it fine and coderabbitai
    # emits it. `.*?</details>` simply fails to match there, leaving the whole
    # log in. Take to the close when there is one, to the end when there is not.
    t = re.sub(r"<details>.*?(?:</details>|\Z)", " ", t, flags=re.S | re.I)
    t = re.sub(r"</?(details|summary|sub|sup)>", " ", t, flags=re.I)
    # Fenced blocks are evidence, not intent, and are the other thing that
    # crowds the window. Keep a stub so "there was code here" survives.
    t = re.sub(r"```.*?```", " [code] ", t, flags=re.S)
    return re.sub(r"[ \t]+", " ", t).strip()



# Send the comment whole up to this length. A 14k-char review is ~4k tokens on
# a flash model — nothing next to the Claude session the verdict decides. Every
# windowing scheme tried here lost a real finding to the elision: head-only lost
# the "Addressed in commit <sha>" footers, head+tail lost the middle of a 16k
# ClawSweeper review that did request a change. Windowing is a last resort for
# genuinely huge comments, not an optimisation.
WHOLE_UNDER = 20_000


def _window(t: str, head: int = 6000, tail: int = 6000) -> str:
    """Head AND tail. What decides a feedback comment is usually at the end.

    coderabbitai and cubic append "Addressed in commit <sha>" after the finding;
    gemini closes with "I have no further comments". Reading only the first N
    characters saw the severity banner and the restatement of the diff, and
    called four already-resolved findings actionable. A 14k-char ClawSweeper
    review put its actual objections past a head-only window entirely.
    """
    t = t.strip()
    if len(t) <= WHOLE_UNDER:
        return t
    # A long structured review puts its findings in the middle, between the
    # "what this changes" preamble and the footer of bot commands. ClawSweeper's
    # 16k-char review was read as a status banner because both ends are boiler-
    # plate. Widen rather than sample: tokens here are cheap next to a Claude
    # session, and the alternative is missing the only real objection on a PR.
    return t[:head] + "\n[...]\n" + t[-tail:]


def _ask(system: str, user: str, *, author: str = "?") -> dict | None:
    """One judgement call. None means the judge could not answer — callers decide."""
    api = _load_key()
    if not api:
        _log(f"NOKEY author={author} :: {user[:80]!r}")
        return None
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 200,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, data=payload,
        headers={"Authorization": f"Bearer {api}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            content = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, ValueError, TimeoutError) as e:
        _log(f"ERROR {type(e).__name__} author={author} :: {user[:80]!r}")
        return None
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        _log(f"UNPARSED author={author} :: {content[:120]!r}")
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        _log(f"BADJSON author={author} :: {content[:120]!r}")
        return None



def is_claim(text: str, *, author: str = "?", default: bool = True) -> tuple[bool, str]:
    """(claiming?, why). `default` is the verdict when the judge cannot answer.

    Pass the value that is SAFE for your call site, not the one that is likely.
    """
    body = _strip_markup(text)
    if not body:
        return False, "empty"
    if not MAYBE_CLAIM.search(body):
        return False, "no first-person marker"

    snippet = body[:1200]
    key = hashlib.sha256(snippet.encode("utf-8", "replace")).hexdigest()[:16]
    cache = _cache()
    if key in cache:
        hit = cache[key]
        return bool(hit["claim"]), hit.get("why", "cached")

    verdict = _ask(SYSTEM, snippet, author=author)
    if verdict is None:
        return default, f"judge unavailable — default {default}"
    try:
        claim = bool(verdict["claim"])
        why = str(verdict.get("why", ""))[:60]
    except Exception:  # noqa: BLE001
        return default, f"judge answer unusable — default {default}"

    cache[key] = {"claim": claim, "why": why}
    _save(cache)
    _log(f"{'CLAIM' if claim else 'not  '} author={author} why={why!r} :: {snippet[:70]!r}")
    return claim, why


if __name__ == "__main__":
    cases = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else []
    wrong = 0
    for text, want in cases:
        got, why = is_claim(text)
        mark = "ok  " if got == want else "WRONG"
        if got != want:
            wrong += 1
        print(f"  {mark} want={str(want):<5} got={str(got):<5} {why[:34]:<34} {text[:52]!r}")
    print(f"  {len(cases) - wrong}/{len(cases)} correct")
    sys.exit(1 if wrong else 0)


# ---------------------------------------------------------------------------
# Feedback triage: does a comment on our PR need a Claude session at all?
#
# 64% of respond-pr sessions in the week to 2026-08-16 ended within two minutes:
# the runner checked out the branch, installed Claude, and Claude found nothing
# to do. The rule gate that lets them through cannot tell the difference, because
# the difference is not in the author. `gemini-code-assist` posts both "I'm
# currently reviewing this pull request and will post my feedback shortly" and
# real defect reports; `coderabbitai` posts both a walkthrough banner and the
# findings. An author blocklist would silence the useful half with the noise.
#
# Three outcomes, and only one of them is a skip:
#   CODE    a concrete defect or change request  -> dispatch
#   REPLY   a question or objection aimed at us  -> dispatch (an unanswered
#           review request stalls a PR, which is the whole point of responder)
#   NOTHING status banner, placeholder, "0 issues found", praise -> skip
FEEDBACK_SYSTEM = (
    "You triage comments left on a pull request WE opened. Decide what the "
    "comment requires from us.\n\n"
    "CODE — it reports a defect, requests a change, or makes a concrete "
    "suggestion about the diff.\n"
    "REPLY — it asks us a question, raises an objection, reports a duplicate or "
    "competing PR, or otherwise needs a human answer but no code change.\n"
    "NOTHING — it needs neither. This covers: a bot saying it has started or "
    "will review shortly; a walkthrough, summary or banner with no finding; "
    "\"0 issues found\" or \"all issues addressed\"; a notice that automated "
    "review is disabled; praise, thanks, or confirmation that our fix works; a "
    "review header that only counts findings posted separately as their own "
    "comments.\n\n"
    'Reply with JSON only: {"needs": "CODE"|"REPLY"|"NOTHING", "why": "<8 words or fewer>"}'
)


def feedback_needs(text: str, *, author: str = "?", default: str = "CODE") -> tuple[str, str]:
    """(CODE|REPLY|NOTHING, why). `default` applies when the judge cannot answer.

    Defaults to CODE, not NOTHING: an unreachable judge must not silence a real
    review request. The cost of being wrong that way is one wasted session; the
    cost the other way is a PR that rots unanswered.
    """
    body = _strip_markup(text)
    if not body:
        return "NOTHING", "empty after markup"

    key = "fb:" + hashlib.sha256(_window(body).encode("utf-8", "replace")).hexdigest()[:16]
    cache = _cache()
    if key in cache:
        hit = cache[key]
        return str(hit["needs"]), hit.get("why", "cached")

    verdict = _ask(FEEDBACK_SYSTEM, _window(body), author=author)
    if verdict is None:
        return default, f"judge unavailable — default {default}"
    needs = str(verdict.get("needs", "")).upper()
    if needs not in {"CODE", "REPLY", "NOTHING"}:
        _log(f"BADENUM author={author} :: {needs!r}")
        return default, f"judge returned {needs!r} — default {default}"
    why = str(verdict.get("why", ""))[:60]

    cache[key] = {"needs": needs, "why": why}
    _save(cache)
    _log(f"{needs:<7} author={author} why={why!r} :: {body[:70]!r}")
    return needs, why
