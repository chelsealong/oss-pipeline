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


def is_claim(text: str, *, author: str = "?", default: bool = True) -> tuple[bool, str]:
    """(claiming?, why). `default` is the verdict when the judge cannot answer.

    Pass the value that is SAFE for your call site, not the one that is likely.
    """
    body = re.sub(r"^\s*>.*$", "", text or "", flags=re.M).strip()
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

    api = _load_key()
    if not api:
        _log(f"NOKEY author={author} :: {snippet[:80]!r}")
        return default, f"no API key — defaulting to {default}"

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": snippet}],
        "max_tokens": 200,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, data=payload,
        headers={"Authorization": f"Bearer {api}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode())
        content = data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, ValueError, TimeoutError) as e:
        _log(f"ERROR {type(e).__name__} author={author} :: {snippet[:80]!r}")
        return default, f"judge unavailable ({type(e).__name__}) — default {default}"

    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        _log(f"UNPARSED author={author} :: {content[:120]!r}")
        return default, f"judge returned no JSON — default {default}"
    try:
        verdict = json.loads(m.group(0))
        claim = bool(verdict["claim"])
        why = str(verdict.get("why", ""))[:60]
    except Exception:  # noqa: BLE001
        _log(f"BADJSON author={author} :: {content[:120]!r}")
        return default, f"judge returned bad JSON — default {default}"

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
