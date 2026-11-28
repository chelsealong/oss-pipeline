# Fable review and upstream follow-ups — 2026-11-29

## Pipeline review

Fable supplied the `review/pipeline-recovery` branch, not a new pull request object. After reviewing the changes and handoff, merged by fast-forward to `main` at `2fa9b5b2ffd16705e2787fd16f28f48d1b6143d0`.

Full verification: 309 tests run, 308 passed, 1 skipped. Cloud preflight succeeded: https://github.com/chelsealong/oss-pipeline/actions/runs/46162156579 . This proves the checked configuration/preflight, not restored production throughput. Production remains disabled pending its separate canary and recovery work.

The queue-ordering, default concurrent-PR cap, lookup/feedback/model-circuit changes are reasonable incremental fixes. They do not establish that every previously diagnosed production bottleneck is resolved.

## Verified public actions

| Item | Result | Evidence |
| --- | --- | --- |
| OpenClaw #161925 | Added required problem/evidence description sections and requested re-review | https://github.com/openclaw/openclaw/pull/161925#issuecomment-7241812326 |
| OpenClaw #161462 | Requested re-review; retained live Foundry / exact-head image-generation proof limitation | https://github.com/openclaw/openclaw/pull/161462#issuecomment-7241813246 |
| OpenClaw #192685 | Requested re-review and identified the existing real Himalaya mailbox-list evidence | https://github.com/openclaw/openclaw/pull/192685#issuecomment-7241813887 |
| OpenClaw #202424 | Requested re-review with existing shipped-entry-point proof | https://github.com/openclaw/openclaw/pull/202424#issuecomment-7241814800 |
| OpenClaw #202906 | Asked whether #204502 supersedes it or a regression-only change is wanted; left open | https://github.com/openclaw/openclaw/pull/202906#issuecomment-7241815498 |
| OpenClaw #190917 | Closed after reviewing #191105 and the existing regression evidence | https://github.com/openclaw/openclaw/pull/190917#issuecomment-7241834073 |
| OpenClaw #191797 | Closed after confirming maintainer's #191763 statement about #194560 | https://github.com/openclaw/openclaw/pull/191797#issuecomment-7241835859 |
| OpenClaw #195500 | Closed after confirming #201623 removed the targeted rebind | https://github.com/openclaw/openclaw/pull/195500#issuecomment-7241837088 |
| ADK #7256 | Closed; equivalent unconditional aggregation was reverted for duplicated output | https://github.com/google/adk-python/pull/7256#issuecomment-7241841699 |
| ADK #7251 | Skipped: no alleged maintainer closure instruction found in issue/review comments; remains open | https://github.com/google/adk-python/pull/7251 |
| OpenClaw #198216 | Added explanation, freshly verified 13 tests plus format/lint/types, and requested re-review; clean backed-up upgrade remains unproven | https://github.com/openclaw/openclaw/pull/198216#issuecomment-7242057867 |
| OpenClaw #198173 | Resolved conflicts, pushed tested head, updated evidence and requested re-review; 76 tests plus format/types passed, lint zero errors and one existing warning | https://github.com/openclaw/openclaw/pull/198173#issuecomment-7242154043 |
| OpenClaw #191745 | Resolved conflicts, pushed tested head, updated evidence and requested re-review; 187 tests plus format/lint/types passed | https://github.com/openclaw/openclaw/pull/191745#issuecomment-7242141452 |

No new upstream PRs or force-pushes. The handoff's leave-alone PRs were not changed. Validation uses isolated GitHub-hosted Docker runners without model API keys or production credentials.

## Validation detail

- #198216: exact unchanged head `906352a45e1e088225c0efb4746c76fc52c54743`; all 13 tests, changed-file format/lint, and targeted type check passed. https://github.com/chelsealong/oss-pipeline/actions/runs/46163373252/job/141035429473
- #191745: repaired head `d465e50e1e8889d2a97e3cae71e076d48e113f03`; 67 selected scheduler, reload, and voice-call tests plus format/lint/types passed. Separate Gateway reload-recovery validation passed all 120 tests (187 total selected tests): https://github.com/chelsealong/oss-pipeline/actions/runs/46163767427/job/141036648740 . https://github.com/chelsealong/oss-pipeline/actions/runs/46163373252/job/141035429510
- #198173: first repair passed all 76 tests and formatting, but lint reported `eslint(max-lines)` at 1008 lines. Removed duplicate mock setup and reused the existing mock return type, without removing any tests or assertions; final head `8a3d2481c6240e88e25912c7e9e6b311f523b07e` passed all 76 tests, formatting and targeted types; lint has no errors. https://github.com/chelsealong/oss-pipeline/actions/runs/46163932003/job/141037161689 . One UI reactivity warning comes from unchanged test code; it is not being suppressed.

## Review decisions

- Preserved both new upstream MCP `pluginIdsByServer` metadata and this PR's `policyBlockedServerNames`; retained upstream's read-only inventory argument.
- Preserved upstream `pluginInvocationContext.exit` when resolving the service-startup helper conflict. Deferred delivery still uses the service scheduler; startup retains the admitted reload transaction.
- Kept the existing `chat.composer.menu.skillBlocked` translation key: its English displayed text is the generic “Not available for this agent,” so using it for a policy-blocked MCP entry is not a user-visible wording error.
- The independent-review statements in older upstream PR descriptions are historical. This turn performed a source review and fresh isolated checks; it does not claim a new independent reviewer or live-provider test.
