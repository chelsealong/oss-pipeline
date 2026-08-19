#!/usr/bin/env python3
"""Check the tests a session wrote, and whether it proved they catch anything.

Tests are 56% of the lines we ship — over 26 pull requests, +1533 test against
+1206 source, with 19 of the 26 carrying more test than source. Nothing has
ever inspected them.

The prompt asks the agent to prove a test fails without the fix, by stashing
the source change and re-running. Whether it actually does so has never been
checked. Sampling five PR-producing runs found the proof present in all five,
but at 1, 2, 5, 6 and 7 stash commands against 2, 6, 13, 17 and 17 test
commands — the ratios are uneven enough that "usually" is not "always", and
"usually" is not something to rely on for a claim we make in the PR body.

Two checks, deliberately different in kind:

  * `proof_ran` is mechanical. Did a test command execute at all, and was one
    preceded by a stash or a checkout of the source file? A rule answers this
    exactly; a model would only add doubt.
  * `verdict` is a judgement, and the one that matters. hermes#82124 was closed
    with "this fix is a no-op" because its test asserted that a line had run
    rather than what the code then did. That test passed, and failed without
    the change, so every mechanical check we have would have waved it through.

Neither blocks. The output goes to the adversarial reviewer as evidence, which
already has the authority to refuse — adding a second gate over the same diff
would just mean two things can silently disagree.

    python3 testcheck.py <diff-file> [session-log]
"""

from __future__ import annotations

import json
import re
import sys

import intent

TEST_CMD = re.compile(
    r"\b(pytest|npm (?:run )?test|pnpm[\w\s-]*test|yarn test|tox|go test|"
    r"cargo test|vitest|jest|bun test|make test|python -m pytest|uv run[\w\s.-]*pytest)\b",
    re.I)
# The proof is: remove the source change, watch the test fail, put it back.
PROOF = re.compile(r"\bgit (?:stash|checkout\s+HEAD)", re.I)
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test\.|\.test\.|test_|\.spec\.)")

SYSTEM = (
    "You are reviewing the TEST portion of a patch an agent wrote for an "
    "open-source project. Judge only the tests.\n\n"
    "A test is BAD when it asserts that an implementation detail happened "
    "rather than what the code does: that a particular function was called, "
    "that a flag was set to the value it already had, that a mock received a "
    "message. Such a test fails without the change and still proves nothing — "
    "one of these got a pull request closed as a no-op.\n"
    "A test is BAD when it re-states the implementation, when its assertions "
    "are so loose that a wrong value would pass, or when it does not exercise "
    "the situation the issue described.\n"
    "A test is GOOD when it sets up the reported situation and asserts the "
    "observable result, so that a regression in behaviour breaks it.\n\n"
    'Reply with JSON only: {"sound": true|false, "concern": "<20 words or '
    'fewer, empty when sound>"}'
)


def split_diff(diff: str) -> tuple[str, str]:
    """(test hunks, source hunks) from a unified diff."""
    tests, src, cur, is_test = [], [], [], False
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            (tests if is_test else src).extend(cur)
            cur = [line]
            m = re.search(r"b/(\S+)", line)
            is_test = bool(m and TEST_PATH.search(m.group(1)))
        else:
            cur.append(line)
    (tests if is_test else src).extend(cur)
    return "".join(tests), "".join(src)


def proof_ran(log: str) -> dict:
    """Did this session run its tests, and show one failing without the fix?"""
    lines = [l for l in log.splitlines() if "[Bash]" in l]
    tests = [l for l in lines if TEST_CMD.search(l)]
    proofs = [l for l in lines if PROOF.search(l)]
    # The proof is only a proof if a test ran while the change was removed:
    # either in the same command, or in one that follows a stash.
    inline = any(PROOF.search(l) and TEST_CMD.search(l) for l in lines)
    sequenced = False
    for i, l in enumerate(lines):
        if PROOF.search(l) and not TEST_CMD.search(l):
            if any(TEST_CMD.search(x) for x in lines[i + 1:i + 4]):
                sequenced = True
                break
    return {"tests_run": len(tests), "proof_cmds": len(proofs),
            "proved": bool(inline or sequenced)}


def check(diff: str, log: str = "") -> dict:
    tests, _ = split_diff(diff)
    out = proof_ran(log) if log else {"tests_run": 0, "proof_cmds": 0, "proved": False}
    if not tests.strip():
        out.update(sound=None, concern="no test changes in this diff")
        return out
    verdict = intent._ask(SYSTEM, tests[:14000], author="testcheck")
    if verdict is None:
        # Silence, not an accusation. The reviewer decides; an unreachable
        # judge must not read as "these tests are bad".
        out.update(sound=None, concern="")
        return out
    out.update(sound=bool(verdict.get("sound", True)),
               concern=str(verdict.get("concern", ""))[:160])
    return out


def as_markdown(r: dict) -> str:
    lines = ["## Test evidence from the generating session\n"]
    if r.get("tests_run"):
        lines.append(f"- test commands executed: {r['tests_run']}")
        lines.append(f"- removed the change and re-ran: "
                     f"{'yes' if r.get('proved') else 'NO — the tests were never shown to fail without the fix'}")
    else:
        lines.append("- **no test command ran in this session**")
    if r.get("sound") is False:
        lines.append(f"- a reviewer of the tests alone objected: {r.get('concern')}")
        lines.append("  Treat this as a lead, not a verdict — read the tests and decide.")
    elif r.get("sound") is True:
        lines.append("- a reviewer of the tests alone found no objection")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    diff = open(sys.argv[1], errors="replace").read() if len(sys.argv) > 1 else ""
    log = open(sys.argv[2], errors="replace").read() if len(sys.argv) > 2 else ""
    r = check(diff, log)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    print(as_markdown(r))
