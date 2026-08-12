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

Run these against a recent **external fork** PR, not a maintainer's:

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
