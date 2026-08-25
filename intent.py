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
import datetime as _dt
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
# Tried in order, first answer wins. Two kinds of failure, handled differently
# because they mean different things:
#
#   * A 403 "Free quota exhausted" is PERMANENT — the allowance does not come
#     back. Retrying it costs 0.5s on every judgement forever and, worse, is
#     re-probed from scratch on each watcher restart. Such a model is written to
#     state and never tried again.
#   * A timeout or a network error is transient, so the model is skipped for
#     COOLDOWN seconds and then re-entered from the top.
#
# Order set by Bruce on 2026-08-21 after qwen3.7-max and qwen3.7-max-2026-05-20
# exhausted their free tier. Those two are removed rather than demoted: there is
# nothing to wait for.
MODELS = [
    "qwen3.7-max-preview",
    "qwen3.7-max-2026-05-17",
    "qwen3.7-plus-2026-05-26",
    "qwen3.7-plus",
    "qwen3.7-max-2026-06-08",
    "kimi-k2.7-code",
    "glm-5.2",
    "qwen3.7-flash",
    "deepseek-v4-flash-0731",
]
MODEL = MODELS[0]          # kept for logs and for anything that reads a name
COOLDOWN = 300.0
# 90s, up from 25. qwen3.7-plus answered a real comment in 32.9s and kimi took
# 21.4s on a one-liner, so 25 left models on the chain that could never be
# reached — the whole chain logged ALLFAILED on a long comment from steipete.
# Generous on purpose: under the two-strike rule a timeout retires a model
# permanently, so a ceiling that is merely tight does not cost one slow answer,
# it costs the model.
TIMEOUT = 90
RETIRED = ROOT / "state" / "models-retired.json"
# Two strikes retires a model, but only for failures that say something about
# the MODEL. The distinction had to be added within a day of the rule:
#
# One comment (lorengordon's, on a long thread) produced a failure on all eight
# live models in a single call — two timeouts and six URLErrors. Eight models do
# not break at once; that was the network, or that one input. Every one of them
# took a strike, and qwen3.7-max-2026-05-17 was retired on the second, an hour
# after answering a probe in 6.2s. A few outages at that rate empty the chain,
# and an empty chain means every judgement falls to its default.
#
# So:
#   * HTTP 403 "quota exhausted" retires at once. It is a statement of fact and
#     it will not read differently on a second attempt.
#   * A timeout or network error counts a strike, but strikes EXPIRE, and a call
#     where every model failed records none at all — that is evidence about the
#     network, not about nine separate models.
STRIKES_TO_RETIRE = 2
STRIKE_TTL_HOURS = 24
_down: dict[str, float] = {}


def _retired() -> dict:
    """{model: {"strikes": n, "why": "", "retired": bool}}"""
    try:
        return json.loads(RETIRED.read_text()) if RETIRED.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_retired(d: dict) -> None:
    try:
        RETIRED.parent.mkdir(parents=True, exist_ok=True)
        RETIRED.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        pass


def dead_models() -> set:
    return {m for m, v in _retired().items() if v.get("retired")}


def _strike(model: str, why: str, *, immediate: bool = False) -> None:
    """Record a failure. Retire at STRIKES_TO_RETIRE, or at once if terminal."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    d = _retired()
    rec = d.setdefault(model, {"strikes": 0, "why": "", "retired": False, "at": ""})
    if rec.get("retired"):
        return
    # Expire stale strikes. A model that failed once yesterday and works today
    # has not accumulated evidence against itself.
    if not immediate and rec.get("at"):
        try:
            age = (now - datetime.fromisoformat(rec["at"])).total_seconds() / 3600
            if age > STRIKE_TTL_HOURS:
                rec["strikes"] = 0
        except Exception:  # noqa: BLE001
            pass
    rec["strikes"] = int(rec.get("strikes", 0)) + 1
    rec["why"] = why[:120]
    rec["at"] = now.isoformat(timespec="seconds")
    if immediate or rec["strikes"] >= STRIKES_TO_RETIRE:
        rec["retired"] = True
        _log(f"RETIRED {model} after {rec['strikes']} strike(s) — {why[:80]}")
    _save_retired(d)


def _unstrike(models: list) -> None:
    """Undo strikes from a call in which everything failed.

    Eight models failing in one call is evidence about the network. Charging
    each of them for it is how a chain empties itself over an afternoon.
    """
    d = _retired()
    changed = False
    for m in models:
        rec = d.get(m)
        if rec and not rec.get("retired") and rec.get("strikes"):
            rec["strikes"] = max(0, int(rec["strikes"]) - 1)
            changed = True
    if changed:
        _save_retired(d)
        _log(f"UNSTRIKE {len(models)} model(s) — every model failed, so this was not the models")


def live_models() -> list:
    """The chain with retired entries removed. This is what actually runs."""
    dead = dead_models()
    return [m for m in MODELS if m not in dead]

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
    # Timestamped to match every other log in the pipeline. Without one, this
    # file could not be windowed at all: health.py's tail_since() found no
    # datable lines, so a check reading intent.log always saw zero events and
    # its "ran with no API key" arm could never fire.
    try:
        stamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        with LOG.open("a") as f:
            f.write(f"{stamp} {msg}\n")
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
    """One judgement call, down the model list. None means none of them answered."""
    api = _load_key()
    if not api:
        _log(f"NOKEY author={author} :: {user[:80]!r}")
        return None
    import time as _time
    now = _time.monotonic()
    dead = dead_models()
    tried = []
    for model in MODELS:
        if model in dead:
            continue                       # retired; no request is made at all
        if _down.get(model, 0.0) > now:
            continue                       # still cooling down from a failure
        tried.append(model)
        got = _ask_one(model, system, user, api, author=author)
        if got is not None:
            if model is not MODELS[0]:
                # Retired models are skipped without entering `tried`, so this
                # read "FALLBACK to X (tried [])" -- which looks like we fell
                # back without trying anything. Name the reason instead.
                skipped = [m for m in MODELS[:MODELS.index(model)] if m in dead]
                failed = tried[:-1]
                why = []
                if skipped:
                    why.append(f"retired: {','.join(skipped)}")
                if failed:
                    why.append(f"failed now: {','.join(failed)}")
                _log(f"FALLBACK to {model} ({'; '.join(why) or 'first live model'})")
            return got
        _down[model] = now + COOLDOWN
    if not tried:
        # Everything live is cooling down. Clear the transient marks so the next
        # call is a real attempt — a stale cooldown must not become a permanent
        # outage. Exhausted models are NOT cleared: that state is real.
        if _down:
            _down.clear()
            _log(f"ALLDOWN author={author}; cooldowns cleared ({len(dead)} retired)")
        else:
            _log(f"NOMODELS author={author}; all {len(dead)} models retired")
    else:
        # Nothing answered. If more than one was tried, the common factor is not
        # the models — hand the strikes back before they retire a working chain.
        if len(tried) > 1:
            _unstrike([m for m in tried if not _retired().get(m, {}).get("retired")])
        _log(f"ALLFAILED author={author} tried={tried}")
    return None


def _ask_one(model: str, system: str, user: str, api: str, *, author: str) -> dict | None:
    payload = json.dumps({
        "model": model,
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
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        # The message is the only way to tell "your allowance is gone" from
        # "try again later", and the two deserve opposite treatment.
        quota = e.code in (402, 403) and ("quota" in body.lower() or "allocation" in body.lower())
        _strike(model, f"HTTP {e.code} {body[:60]}", immediate=quota)
        return None
    except (urllib.error.URLError, OSError, KeyError, ValueError, TimeoutError) as e:
        _strike(model, type(e).__name__)
        _log(f"ERROR {model} {type(e).__name__} author={author} :: {user[:80]!r}")
        return None
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        _log(f"UNPARSED {model} author={author} :: {content[:120]!r}")
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        _log(f"BADJSON {model} author={author} :: {content[:120]!r}")
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
