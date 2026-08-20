# Lessons that apply to every repo

Loaded on every run, before the repo-specific file. Everything here was paid
for on a real PR that was rejected, closed, or silently wasted a session. A
repo we have never touched starts with this knowledge instead of rediscovering
it — which is the whole point: two repos out of sixteen had lesson files, so
fourteen agents began every run blind.

Add to this file only what generalises. Anything that names a specific check,
label, maintainer or merge window belongs in `<repo>.md`.

## A change that cannot fail is not a fix

`hermes#82124` was closed with: *"this fix is a no-op and I'd rather not merge
something that closes the issue without changing behavior."* Three separate
errors, each of which recurs:

1. **A precedent does not transfer until you know why the original needs it.**
   We called `setFocusable(true)` on a window because a sibling component does.
   That sibling creates itself with `focusable: false` on purpose and flips it
   while its composer wants the keyboard. Our target defaults to `true` and
   never sets it, so the call assigned the value it already had. Copying a
   pattern requires reading the site you copied it from.
2. **A test that asserts a line ran proves nothing.** Ours asserted the helper
   calls `setFocusable(true)`. It fails without the helper — so it passes the
   "would this fail without the change" check — and still says nothing about
   behaviour. Assert the observable effect.
3. **Read the code on current main before fixing what an issue quotes.** The
   file the report quoted had been replaced three weeks earlier. The maintainer
   could not reproduce it, because it no longer existed.

## Never touch dependencies, lockfiles, or CI configuration

Not to fix a vulnerability, not to unbreak an unrelated red check, not to
"sync with main". These are the maintainer's decisions and their blast radius
is the whole project. A PR that edits one of them is reviewed as a supply-chain
change no matter how small the diff, and ours are not. If the fix genuinely
requires a dependency change, say so in the issue and stop.

## Size is a gate before quality is considered

Every repo has a size at which an external PR stops being read on its merits
and starts being read as a risk. Where it has been measured it is low:
openclaw's external merges run a median of +61 lines. Nothing forces a fix to
be large — a large diff usually means the change is carrying refactoring,
renames or defensive extras that nobody asked for. Cut those first, not last.

## Bring evidence, not description

"Fixed the crash" is not reviewable. The command you ran, the output before,
the output after, and the failing assertion the test now catches — that is.
Where a repo's automation scores PRs, the scored artefact is the body, and a
body without evidence scores low regardless of how good the diff is.

## Fail-open means inverting the predicate, not extending a denylist

When a guard wrongly rejects something, the reflex is to add the case to a list
of exceptions. That list is wrong again on the next input nobody thought of.
Ask instead what the guard is actually for and whether it should be asking the
opposite question. This applies to our own pipeline as much as to the code we
submit — a phrase list for detecting claims failed three times in one week and
was replaced by a model that judges intent.

## Some things are not ours to settle

A design disagreement, an API shape, a naming argument, a product decision. If
the issue's resolution depends on someone deciding what the software *should*
do, the fix is not available to us no matter how clear the code path is. Skip
it and say why. The same applies to an issue whose reporter is mid-conversation
with a maintainer.

## Someone else's presence outranks our speed

If a human has commented on the thread with a plan, is verifying a build, or
has an open PR, the issue is theirs even if their PR is worse or slower. We
detect issues within seconds; that is not a claim on them. An offer to *test*
someone's branch is not a claim on the issue, and a closed unmerged PR is not
one either — those are the two mistakes at opposite ends.

## Read the CONTRIBUTING file and obey the parts that cost nothing

Testing plan sections, issue links, commit message shape, "ask before starting
on issues not labelled good-first-issue". These are cheap to honour and they
are the difference between a PR that gets read and one that gets closed with a
template reply. Where a repo asks to be asked first, being fast is not a
defence — spec-kit was lost for two days that way.


## Sample by when something finished, not by when it started

Measuring how long a repo takes to merge an outside PR by looking at its
newest-created merged PRs gives an answer that is wrong in a predictable
direction: the slow ones have not merged yet, so they cannot appear in the
sample at all. Only the fast ones are visible, and the estimate collapses
towards zero.

That method reported openclaw's slowest external merge as 4 hours, and a
24-hour cutoff was set on it. openclaw#121306 merged the same day at ten days
old — sixty times the supposed maximum. Re-sampled by MERGE time over 84 PRs:
p50 1h, p75 24h, p90 140h, longest 507h.

The same shape appears whenever a population is sampled through the event that
removes it from the population. Ask what is missing from the sample before
trusting a distribution built from it.
