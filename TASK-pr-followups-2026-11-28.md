# Task: upstream PR follow-ups (prepared 2026-11-28)

Hand-off for the agent or person acting as `chelsealong`. The owner asked for these
on 2026-11-28 after a read-only audit of the 31 open PRs on OpenClaw, Hermes and ADK
(see `REVIEW-open-prs-2026-11-28.md`). Nothing below has been posted.

## Rules for whoever runs this

- `AGENTS.md` leaves PR closures and issue comments to the owner. This file is the
  owner's explicit request for the specific actions listed; do nothing beyond them.
- Re-read each PR immediately before acting. If its state, head or thread has changed
  from what is described, stop on that item and report instead of acting.
- No force-pushes. No new PRs. One comment per item; never repost on an ambiguous write.
- Tick each box only after confirming the result on GitHub.

## Already done (no action)

- [x] openclaw/openclaw#161925: `158e8dcd5` adds a #161798 citation to each new rule
      and rejoins the junk-pattern list.
- [x] openclaw/openclaw#161462: `39884bff6` restores `MAI-Image-2.6` in the manual
      selector and the provider guide (dropped by the port; originally `7f812196c`).
      Not run against tests or a live Foundry deployment.

## 1. Edit the description of openclaw/openclaw#161925

Its `PR context and evidence` check requires authored `## What Problem This Solves`
and `## Evidence` sections. Keep everything else in the description unchanged.

- [ ] Description edited

Replace the first paragraph ("Clarify that completeness claims...") with:

```markdown
## What Problem This Solves

Fixes #161798. A test audit can replace weak tests and stay green while its new
quality gates cover less than they claim. In the audit reported there, a validator
checked 20 of 38 mutations, an analyzer let unsupported mock forms escape, helper
parity hid a live-caller gap, a diagnostic copied an obsolete projection, and a
single CI socket reset needed evidence-based triage. This PR adds rules for those
cases to the test-audit skill, each citing #161798.
```

Then put this heading directly above the line "Validation: `git diff --check`...":

```markdown
## Evidence
```

## 2. Request triage-bot re-review

The command must be on its own line. Post the #161925 comment only after step 1.

- [ ] #161925  - [ ] #161462  - [ ] #192685  - [ ] #202424

```sh
gh pr comment 161925 --repo openclaw/openclaw --body "Added an incident citation (#161798) to each new rule and rejoined the junk-pattern list in 158e8dcd5; the description now has the required sections.

@clawsweeper re-review"

gh pr comment 161462 --repo openclaw/openclaw --body "39884bff6 restores the MAI-Image-2.6 entry in the manual selector and the provider guide, which the port to the current entry point had dropped. Not run against a live Foundry deployment; exact-head image_generate proof is still outstanding.

@clawsweeper re-review"

gh pr comment 192685 --repo openclaw/openclaw --body "The real \`himalaya mailbox list\` output has been in the description since 2026-11-04.

@clawsweeper re-review"

gh pr comment 202424 --repo openclaw/openclaw --body "Shipped-entry-point proof was added to the description on 2026-11-28.

@clawsweeper re-review"
```

## 3. Ask about openclaw/openclaw#202906

Main gained `!isStatePostgres()` on the same expression in #204502. Whether that
fully covers this PR was not proven, so ask instead of closing.

- [ ] Comment posted

```sh
gh pr comment 202906 --repo openclaw/openclaw --body "#204502 added a PostgreSQL check to the same expression on main. If that covers #202891, I'm happy to close this, or to reduce it to the PostgreSQL regression test only. Which would you prefer?"
```

## 4. Close obsolete PRs

Checked on 2026-11-28: #191763 was closed by a maintainer as fixed by #194560;
#201623 is merged; the pipeline's own comment on #190917 cites #191105; ADK commit
`5523aa7` reverted the aggregated non-partial event.

**Not rechecked: google/adk-python#7251.** The audit reported a maintainer comment
(about 2026-11-05) suggesting it be closed in favour of an internal change. Find
that comment first; if it is not there, leave the PR open and report.

- [ ] openclaw #190917  - [ ] openclaw #191797  - [ ] openclaw #195500
- [ ] adk #7256  - [ ] adk #7251 (only after confirming the maintainer's comment)

```sh
gh pr close 190917 --repo openclaw/openclaw --comment "Closing: #191105 on main now covers this path, and this PR's regression no longer shows a gap. Thanks for the review time."

gh pr close 191797 --repo openclaw/openclaw --comment "Closing as superseded: #191763 was fixed by #194560 (fs-safe 0.28.0), and the sanitizer proposed here is no longer needed."

gh pr close 195500 --repo openclaw/openclaw --comment "Closing: #201623 removed the rebind this test change targeted, so this is obsolete."

gh pr close 7251 --repo google/adk-python --comment "Closing in favour of the internal change, as suggested: main already emits an error event for a FAILED status without a message. Thanks for the guidance."

gh pr close 7256 --repo google/adk-python --comment "Closing: the equivalent aggregation in dbae28d was reverted in 5523aa7 because it duplicated streamed output, and this PR would reintroduce that behaviour unconditionally. My earlier comment here predates the revert."
```

## 5. Needs a test environment (not attempted)

These need OpenClaw's own tests run on the result. Merge main into the PR branch
(no rebase, no force-push), resolve conflicts, run the focused tests and applicable
lint/type checks, then request re-review.

- [ ] openclaw #198173: conflicts with main. Minor: the MCP label reuses the i18n key
      `chat.composer.menu.skillBlocked` in `chat-composer-plugins-menu.tsx`.
- [ ] openclaw #191745: conflicts with main. The bot's P1 ("preserve the admitted
      reload root during recovery startup") was raised on an older head and needs
      re-review after the merge.
- [ ] openclaw #198216: no conflict. Explain in the description why the test near
      line 368 of `src/config/io.snapshot.promotion.test.ts` changed from
      `[conflict, applied]` to `[conflict]`, then request re-review.

## 6. Leave alone

- Hermes #75773, #76514, #81134, #93386 and ADK #6545, #7249, #7252, #7335: waiting
  on maintainers. No further pings.
- OpenClaw #142692, #150063, #164200: waiting on maintainer CI or security approval.
- OpenClaw #191615, #178932, #191001, #204482, #205629 and ADK #7267, #7285: need
  real-system proof or maintainer interest before more work is worthwhile.

## Report back

List each item as done, skipped (with the reason) or failed (with the exact error),
and link every comment posted.
