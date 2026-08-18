# Before adding a repo

Merge statistics say whether a repo *accepts* outside work. They say nothing
about whether it will accept a PR you open without asking. Three of the four
gated repos here were discovered by having a PR closed, not by evaluation:

- **pydantic-ai** — `pr-guard.yml` closes any PR from a non-collaborator whose
  linked issue is not assigned to the author. Ours lived 18 seconds. The review
  bot ran anyway and called the change "a straightforward bug fix … with good
  test coverage", so nothing was wrong with the work.
- **gemini-cli, langgraph, langchain** — same class, found the same way.
- **vllm** — no assignment gate, but DCO on every commit. A missing
  `Signed-off-by` fails the check.

None of that is visible in merge rate, patch size or time-to-merge, which is
what gets measured when deciding to add a repo.

## Checklist

Run these against a recent **external fork** PR, not a maintainer's.

`verify.sh` check 33 enforces step 0 mechanically. The rest are still read-and-
do, so do them before the first dispatch rather than after the first failure.

0. **Create the fork, first, before anything else.** litellm, llama-index, mem0
   and crawl4ai were added on 08-15 with no fork under `chelsealong`. Every
   dispatch for two days did the same thing: passed vetting, consumed a daily
   budget unit, started a runner, failed at `Checkout target fork`, and ended.
   Seven runs on one day alone. Nothing surfaced it, because the failure was
   after the cheap steps and before Claude, so it neither burned quota nor
   produced an obvious symptom — the repos simply looked unproductive.

   `gh repo fork` is not reliable here: it printed nothing, exited 0, and
   created no fork. Use the API and read the response back:

   ```
   gh api -X POST repos/<owner>/<name>/forks --jq '.full_name'
   gh api repos/chelsealong/<name> --jq '.parent.full_name'   # confirm
   ```

   Check the fork's default branch too. litellm's is
   `litellm_internal_staging`, not `main`, and that is also where its PRs go —
   fix-one derives it from `git remote show upstream`, but a config that hard-
   codes `main` anywhere would target the wrong base.

1. **Assignment gate** — read `.github/workflows/*` for a guard
   (`pr-guard`, `duplicate`, `unlinked`). Then read bot comments on a real fork
   PR for "wait to be assigned" / "closed automatically". The workflow is the
   rule; the comment is the proof. If gated, add the key to `GATED` in
   `watch.py` and to `claim.sh`, so it claims first and never opens cold.
2. **CLA / DCO** — list the check runs *and* the commit statuses on that PR
   (`cla-assistant`, `license/cla`, `cla/google`, `DCO`). A filename search
   misses DCO entirely, because it is enforced as a status rather than a
   workflow in the repo.
3. **CI authorisation** — does CI run for forks, or wait for a label? vllm needs
   a maintainer to apply `ready`, observed 2.7–4.4h after opening. That is not a
   blocker, but it sets the feedback loop.
4. **Licence** — confirm it is OSI. Arize Phoenix is Elastic License 2.0, which
   forbids hosting the software as a service. Contributing free automated work
   under those terms is a decision for a person, not a default.
5. **Issue supply** — do merged PRs reference issues at all? openai-agents-python
   merges 104 external PRs in six weeks and none of them close an issue, because
   contributors read the code instead. This pipeline selects from issues, so
   that repo is unusable to it no matter how open it is.

`verify.sh` check 8 asserts that the gates found this way stay encoded.

## Contribution-count gates

vllm's `pre-run-check` fails every PR whose author has fewer than 4 merged
PRs there, unless a maintainer applies `verified`, `ready` or
`ready-run-all-tests`. Discovered on vllm#51474 — our first PR to that repo,
which reported CI failure for a reason that has nothing to do with the patch:

    PR must have the 'verified', 'ready', or 'ready-run-all-tests' label to run
    pre-commit, or the author must have at least 4 merged PRs (found 0).

Treat this exactly like a CLA check. It is a gating requirement, not a defect:
never try to satisfy it in code, never explain it away to the maintainer, and
never report the PR as broken on our side. It clears when a maintainer labels
the PR, and every first contribution to such a repo will show red until then.

Check for it before adding a repo: read `.github/workflows` for a job that
counts the author's merged PRs or requires a hand-applied label to run CI.

## Some repos submit through their own agent, and PRs bypass it

spec-kit takes community catalog entries — extensions, presets, bundles — via
its own agentic workflow: the workflow validates the submission and opens the
PR itself. A PR from us for the same submission sidesteps that and is always
closed. `mnriem` said so three times across seven of our PRs:

> I appreciate you filing the PR, but community extension submission flow
> through an agentic workflow that does the validation and the creation of the
> PR. I am going to close this PR as it sidesteps that process  — #4027, 08-10

> Please instruct your agents to update its configuration to NOT open PRs
> against extension submissions. Thank you!  — #4062, 08-12

The tell is on the issue, not the PR: `extension-submission`,
`preset-submission`, `bundle-submission` labels, and titles beginning
`[Extension]:` / `[Preset]:` / `[Bundle]:`. Both are needed — #4068 carried only
`enhancement` when we picked it up, and the submission label landed later.

Every spec-kit PR of ours that merged (#3929, #4012, #4016, #4045) is a plain
code fix, and those drew "Thank you!". The repo is one of our best converters;
the problem was entirely which issues we took from it.

**When adding a repo, look for a submission path that is not "open a PR".**
Search its `.github/workflows` for a job that creates PRs from issues, and read
what its issue templates promise will happen next. If the project opens the PR,
our job is to stay out of the way. And when a maintainer asks us to stop
something, it belongs in `scan.py` and in verify.sh the same day — verify check
19 exists because this one was asked three times before it was encoded.

## We were blocked from an org. This is how.

pydantic blocked `chelsealong` on 2026-08-12 at 06:54 UTC. The cause is not in
dispute and is entirely ours.

`claim.sh` posted one sentence — *"I'd like to take this one if it's still open
— happy to put up a PR."* — verbatim, on eight pydantic-ai issues over five
days: #7281, #7211, #7147, #7133, #7284, #7338, #7347, #7397. **Zero PRs came
out of any of them.** Our only PR there, #7282, had already been closed by their
`pr-guard` bot in 18 seconds.

Two details make it worse:

- #7397 was opened by `dsfaccini`, the maintainer who nine hours earlier had
  apologised to us on #7338 and asked us not to duplicate work in flight. We
  answered that by dropping the same canned line on his own issue. The block
  came three hours later.
- #7211 had already been marked `not-a-bug, signal 3/10` by pydantic's own
  triage bot. We claimed it anyway.

From the other side that is indistinguishable from a bot squatting on a tracker,
and the block was a reasonable response.

**What the API cannot tell you.** An org block is invisible from the blocked
side: our comments stayed visible and unminimised, the repo stayed readable, the
fork stayed alive, `involves:chelsealong` kept returning rows. I checked all of
that and reported "no evidence of a block" — which was wrong. The only signals
are the email GitHub sends the blocked account, and a 403 on the next write.
Never conclude "not blocked" from a read-only check.

**The structural fix.** Claiming is now gated on delivery: `claim.sh` refuses to
claim in a repo where we already hold `MAX_UNFULFILLED` (default 2) claims with
no PR behind them. langchain was sitting at 5 comments / 4 claims / 0 PRs — the
same pattern, one repo away from the same outcome — and is now blocked from
claiming further by that gate. Claiming is suspended outright for the moment.

A claim is a promise made in public. Volume without delivery is the failure
mode, and it does not need malice to get you removed.
