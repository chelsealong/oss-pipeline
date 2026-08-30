# hermes-agent

## How work actually lands here

Not by our PR being merged. A maintainer cherry-picks our commits into their
own PR and merges that, so our PR shows CLOSED and theirs shows a self-merge.
Both landed changes went this way and kept our authorship:

- `#75790` → teknium1's `#75910`, merged 16h after we opened, carries 2 of our commits
- `#75792` → kshitijk4poor's `#78390`, merged 151h later, carries 3

So never measure this repo by PR merge state. Count commits on the upstream
default branch authored by `chelsealong@126.com`. `health.py check_landed`
does this; a naive merge count reports zero here.

## The feedback loop is fast and technical — use it

Its AI triage names the function and the line. Two real defects in our own
patches were found and fixed the same hour:

- `#81134` — the zh injection patterns could not match across a line break:
  `_FILLER_CJK` used `.` and the inline `.{0,6}` spans, neither of which
  matches `\n`, while the English originals used `\s+` and did. Fixed in
  `9fe6778d`, 6 minutes after the review.
- `#81204` — `extractOoxmlPreviewText` read the whole archive into memory
  before any cap applied, while the pre-existing path capped at 512 KiB.
  Fixed in `d8f4449f`, 13 minutes after.

`#74733` was called the `best fix` of three competing PRs by that same triage.

## Size is not the constraint here

Merged PRs run a median of +350 and up to +2005 — the opposite of openclaw's
+61. Our +520 and +254 patches are in normal range, and fix-one.yml applies no
size ceiling to this repo deliberately.

## A no-op that passes its own test — `#82124`

Closed by OutThisLife on 2026-08-09 with the clearest rejection we have had:

> this fix is a no-op and I'd rather not merge something that closes the issue
> without changing behavior.

Three separate mistakes, each generalisable:

1. **The precedent did not transfer.** We called `setFocusable(true)` on the
   HUD window because the pet overlay does. But the pet overlay creates itself
   with `focusable: false` on purpose and flips it while its composer wants the
   keyboard. Electron defaults `focusable` to `true` and `spawnHudWindow()`
   never sets it, so our call set the value to what it already was. Copying a
   pattern requires knowing why the original site needs it.
2. **The test was a tautology.** It asserted the helper calls
   `setFocusable(true)`. That fails without the helper — so it passes the
   "would it fail without the source change" check — and still proves nothing
   about behaviour. Assert the observable effect, not that a line executed.
3. **The report was stale.** The `click-through.ts` it quoted is no longer on
   main; it was replaced in `717b49c`, which is not an ancestor of the build in
   the report. The maintainer could not reproduce on current main. Read the code
   an issue quotes on current main before fixing it.

fix-one.yml's adversarial reviewer now opens with a NO-OP front covering all
three, and the TEST HONESTY front says explicitly that failing-without-the-change
is necessary but not sufficient.

## Why 34 PRs produced 5 commits, and all five on one day

Measured 2026-08-11. Nothing has landed since 2026-08-01, while the PR
count went from ~5 to 34.

The repo is not the problem. hermes merges 45-135 PRs a day; teknium1 has
opened 253 PRs since 08-05 and kshitijk4poor 114. None of the six issues
behind our oldest open PRs has been closed by anyone else, so the work
was not taken. Engagement is real too: 10 of our 30 open PRs have a human
on them, and on `#81591` an unrelated user cherry-picked our fix and
confirmed it end to end.

Size is not the differentiator here either. The three external PRs merged
by someone other than their author in the last three days run +13, +108
and +1679.

What separates them from us:

| author | merged | comments on threads they did not open |
|---|---:|---:|
| helix4u | 117 | 22 |
| embwl0x | 5 | 39 (35 on issues) |
| victor-kyriazakos | 5 | 2 |
| **us** | **0** | **0** |

We answer reviewers on our own PRs — 13 of 30 carry a reply from us — and
we have never once spoken on a thread we did not open. Every contributor
who lands work here is a participant in the tracker first. embwl0x has
commented on 35 issues and merged 5 PRs; we have commented on none and
merged none.

Volume made it worse. The two that landed were salvaged on 2026-08-01,
when we held four or five open PRs. The quota then went to 10/day, the
open count reached 30, and the landing rate went to zero — in a repo
where a single author's stack competes with maintainers pushing 250 PRs a
week. Cap is 4/day now.

**Do not read this as "comment more to look busy."** The measurable thing
is that our PRs arrive with no prior presence on the thread. Where the
pipeline already has an announce step (adk), that is the shape to reuse.

Caveat on method: GitHub's `commenter:` search index is unreliable — it
reported 2 comments for us when a direct count over the same PRs found 13.
The 0 above is the `commenter:X -author:X` form, which agrees with a
manual check.

## Salvage lands 3-5 days late — do not judge inside that window

On 2026-08-12 I concluded hermes was failing: 34 PRs, "nothing landed since
08-01", 6% conversion, and cut it to 4 PRs/day. The evidence was that our
commits on main stopped at 08-01.

On 2026-08-15 two more landed:

| commit | we wrote it | teknium1's PR | merged |
|---|---|---|---|
| `f0748b45` stop empty REST transcript refresh | 08-10 | #86588 | 08-15 03:23 |
| `49d72a02` verify Windows gateway cold-start | 08-12 | #86687 | 08-15 05:03 |

Three and five days between our PR and the salvage. The first two salvages
took 16h and 151h, and I generalised from the 16h one. hermes is now the
repo with the most landed commits of any (7), and both of these arrived
*after* the cut.

**The rule: a repo whose landing path is salvage cannot be judged on a
window shorter than a week.** Our PR staying open means nothing there —
the maintainer's PR is where the work appears, under their number, and
`is:merged` on our account never shows it. Count commits on the default
branch, and give it seven days before drawing a conclusion.

Cap back to 7/day on 2026-08-15. Not 10: the run to 30+ open PRs in one
repo was its own problem, and the 20,623-PR backlog there means volume
still buys nothing.
- [2026-08-30] issue #98243 blocked by review:  FRONT 0 (NO-OP) — FAILS, this is the deciding issue.  `hermes_cli/model_switch.py::list_authenticated_providers()` already contains a post-pass (lines ~3890-3906, added in commit d474ba5615, "Surface a custom / 

- [2026-08-30] issue #98273 blocked by review:  FRONT 0 — NO-OP (fails the PR):  The added `term.refresh(0, term.rows - 1)` after `webgl.dispose()` almost certainly duplicates a repaint the real @xterm/addon-webgl + @xterm/xterm 

- [2026-08-30] #98295 skipped: issue #98295 (mechanical wheel/touchpad scrolling stuck at 1 row/event in native terminals due to nativeStep's 40ms acceleration window in ui-tui/src/lib/wheelAccel.ts) is already fixed by open PR #83675 ("fix(tui): accelerate mechanical wheel scrolling in nat
- [2026-08-30] #98299 skipped: issue #98299 asks for /v1/runs on the API server to gain the full native GoalManager lifecycle (goal creation via /goal, judge wait/continue verdicts, process/session/timed wait parking, wake delivery, pause/resume/clear, budget accounting, and goal-lifecycle 
- [2026-08-30] #98308 skipped: issue #98308 (Volcengine Ark plan/v3 rejects empty assistant content after replayed pure-reasoning turns, in agent/codex_responses_adapter.py::_chat_messages_to_responses_input) is a duplicate of issue #89761, which already has an open PR #89783 ("fix(codex): 
