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
