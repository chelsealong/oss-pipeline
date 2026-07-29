# oss-pipeline

Automated OSS contribution pipeline: scan upstream trackers for genuinely
unclaimed issues, then fix, test, and open a PR.

Runs on GitHub Actions so it does not depend on a laptop being awake, and uses a
**Claude subscription token — not an API key**, so it consumes no API credit.

## Why Actions and not a local schedule

A local `launchd`/`cron` job only runs while the Mac is awake and logged in. With
`sleep 1` and Power Nap off, a 20-minute scanner misses almost every window, which
destroys the only thing frequent scanning buys you: reaching an issue before
someone else claims it. Actions runs 24/7.

Two other traps this avoids, both hit for real:

- macOS TCC denies scheduled jobs execution inside `~/Desktop`, failing every run
  with `Operation not permitted` while `launchctl list` still reports status 0.
- The claude.ai cloud sandbox scopes GitHub API access to the session's initial
  source repo, so a session started from a fork cannot read its upstream at all
  (`GitHub access is not enabled for this session`), and all GraphQL is blocked.

## Repos

Handled here: **adk, langfuse, langfuse-python, spec-kit**.

Deliberately excluded: **openclaw** and **hermes-agent**. Their issues get claimed
within seconds to minutes — one PR appeared 12 seconds after the issue was filed —
so no scheduler wins those races. They run locally at 3x/day in prepare-only mode
instead, where losing a race costs nothing.

## Required secrets

| Secret | How to get it |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Run `claude setup-token` locally (requires a Claude subscription). No API key needed. |
| `GH_PAT` | A classic PAT with `repo` scope — used to read upstream trackers, push to the forks, and open PRs. |

Add both under *Settings → Secrets and variables → Actions*.

## Schedule

`*/5 * * * *`. Note GitHub's documented floor is 5 minutes and real delivery is
often delayed 5–20 minutes under load, so treat this as "within ~20 minutes",
not "instant". True push-based reaction is impossible for repos you do not own —
issue webhooks require admin on the upstream repo.

## Safety properties

- **Scan and fix are separate jobs.** Scanning is pure Python + `gh` and runs every
  5 minutes; Claude only runs when there is vetted work.
- **Vetting** = unassigned + no linked PR (three independent signals: closing
  references, cross-reference timeline, PR full-text search) + no comment claimants
  + per-repo label/title exclusions.
- **A `partial` (rate-limited) scan is never treated as "no work"** — it keeps the
  previous queue and is skipped for fixing, because an empty queue from a failed
  scan is indistinguishable from a genuinely empty one.
- **The claim is re-verified inside the fix step**, immediately before writing code.
  The queue narrows the race window; it cannot close it.
- **Daily cap of 2 PRs per upstream**, checked against GitHub before running.
- **`max-parallel: 1`** so at most one PR is in flight at a time.
- **PRs always target the real upstream** (`--repo <upstream> --head chelsealong:<branch>`).
  A fork-to-fork PR reaches no maintainer; that mistake was made once already.

## Manual run

Actions → *OSS pipeline* → *Run workflow*. Use `dry_run: true` to resolve a
candidate without running Claude or opening anything.
