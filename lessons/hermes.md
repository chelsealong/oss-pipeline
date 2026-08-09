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
