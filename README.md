# OSS pipeline — local Codex runtime

The user authorized restoration on 2026-10-01 after stopping the Claude pipeline.
Execution now uses standalone Codex CLI 0.159.3 and the existing ChatGPT login.
The deployment pins `gpt-6-astra`, matching the user's configured model.
The CLI is installed under the deployed `.runtime/toolchain/`, independent of IDE updates. GitHub Actions
Claude workflows stay disabled. This runtime requires the Mac to be awake and
logged in; it resumes via launchd after login. Credentials never leave this Mac.

Three launchd services run from `~/.local/share/oss-scanner`:

- `oss-watch`: new issue detection plus rotating backlog scans; retains repository
  exclusions, duplicate checks and dispatch caps from `scan.py` / `watch.py`.
- `oss-prwatch`: actionable feedback and failing-check detection, every five minutes.
- `oss-fix`: one serial worker with durable SQLite jobs under `.runtime/`.

Every coding job has an isolated checkout, bounded generation and independent
review. The controller checks scope, caps, current ownership/eligibility, and
exact reviewed patch before committing and pushing. No force pushes or merges.
Codex-generated work is disclosed; automated review is never described as human.
Conversational replies, coordination requests and proposed PR closures are kept
for human follow-up, not automatically posted.

The existing Qwen/DashScope judge retains cached semantic claim/feedback checks.
Actual HTTP requests (including fallbacks) are capped at 120/hour. The existing
45 coding sessions/5h limit includes both generation and review. Judge calls pause
when the worker is unhealthy, stale, rate-capped or disabled. A task failure opens
a 30-minute circuit; failed/interrupted tasks are preserved and not replayed.

`state/runtime.json` is local deployment configuration (ignored by git).
`python3 runtime.py` prints queue, heartbeat, model-call counts and recent results.
`./verify.sh` runs the offline commit gate (syntax, workflow YAML and runtime
regression checks). Legacy Claude checks remain archived below the active route.
`python3 codex_worker.py --probe` performs one real, read-only Codex auth probe.
The launchd canary passed on 2026-10-01: failing baseline, generated fix,
independent review, controller test rerun and local commit, with no public PR.
`run-fix.sh` drains at most one already-queued task; it no longer invokes Claude.

Pause: set `enabled` to false in the deployed config and unload oss-watch,
oss-prwatch and oss-fix. Keep `.runtime/jobs/` for recovery; interrupted work may
already have reached a fork. Do not blindly reset failed tasks to queued.

The older workflow YAML and scripts are retained for provenance, not active
execution. The archived pre-Codex README follows and is not current configuration.

---

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
