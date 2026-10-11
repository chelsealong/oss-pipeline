# OSS pipeline — managed Codex runtime

Production runs on GitHub-hosted Ubuntu runners after the 2026-10-02 cloud
cutover. The Mac can be shut down; its three launchd services are unloaded and
its runtime config is disabled. Cloud canary [36958589558](https://github.com/chelsealong/oss-pipeline/actions/runs/36958589558)
passed real generation, independent review and controller tests before cutover.
The user selected ChatGPT/Codex subscription usage, not paid API-key access. `codex-cloud-preflight.yml`
is manual-only, runs offline checks on Ubuntu, and makes no model calls.
`cloud_state.py` exports/imports a portable queue checkpoint: it preserves call
budgets and deduplication, invalidates stale health, and holds interrupted tasks
for inspection. It never copies credentials. No Claude workflow was re-enabled.

The production workflow is `codex-cloud.yml`. It uses a dedicated ChatGPT login
in encrypted Secret `CODEX_AUTH_JSON` plus the existing `GH_PAT` and
`QWEN_API_KEY`. Set repository variable `CODEX_CLOUD_ENABLED=true` only after a
successful `mode=canary` run, stopping local services, and seeding the dedicated
`codex-state` branch. Missing/false means scheduled production cannot start.
One GitHub concurrency group owns all cloud modes and subscription refreshes.
Canaries wait in cloud until four turns fit the retained spending window before
starting the model host. An explicitly dispatched canary with
`resume_after_canary=true` can restore production after success, provided a
successful preflight exists for the same unchanged main commit. Cancel that
run to withdraw the pending restart; ordinary canaries do not enable production.
An isolated local browser login supplied the initial credentials after device
authorization failed. The existing desktop login was not exported. Ubuntu 24.04
installs Bubblewrap and loads its AppArmor user-namespace profile before running
Codex; a namespace smoke test runs before any model call.
Each live runner detects work for four hours, then drains the active task for
up to 90 minutes before handing over; a twice-hourly schedule recovers the chain.
Runtime state is committed on the separate state branch, including before model
spending and publication. An interrupted task is held for inspection, never replayed.
Refreshed subscription credentials are written back to the encrypted Secret, not
state, logs or artifacts. `CODEX_CLOUD_ENABLED=false` stops new detection on the
next control check and drains active work; cancel the workflow for an immediate stop.

Queue admission reserves one of eight slots for PR feedback. Fixes rejected for
author ownership or required coordination are filtered before cloning; Langfuse's
triage assignment exception remains intact. Repeated detection does not charge
the dispatch budget again. Transient GitHub connection failures cool down for one
minute; auth/quota failures retain the 30-minute circuit. No failed task is replayed.

The user authorized restoration on 2026-10-01 after stopping the Claude pipeline.
Execution uses standalone Codex CLI 0.159.3 and the dedicated subscription login.
The user selected `gpt-6-sol` for generation, independent review and health
probes in both local and cloud deployments. There is no automatic Astra fallback.
The cloud CLI version is pinned independently of IDE updates. All seven legacy
Claude workflows remain disabled. The former local deployment remains under
`~/.local/share/oss-scanner` for recovery; do not start it alongside cloud production.

The cloud supervisor separates discovery from admission and execution:

- `oss-discover`: GitHub-only GraphQL polling with a five-second target period,
  durable discoveries and no model calls; actual latency includes GitHub response time.
- `oss-watch`: candidate screening plus rotating backlog scans; retains repository
  exclusions, duplicate checks and dispatch caps from `scan.py` / `watch.py`.
- `oss-prwatch`: actionable feedback and failing-check detection, every five minutes.
- `oss-fix`: two workers sharing one authenticated host, with durable SQLite jobs under `.runtime/`.

Every coding job has an isolated checkout, bounded generation and independent
review. The controller checks scope, caps, current ownership/eligibility, and
exact reviewed patch before committing and pushing. No force pushes or merges.
Codex-generated work is disclosed; automated review is never described as human.
Independently verified PR replies and necessary updates are authorized. Issue
coordination requests and proposed PR closures remain human follow-ups.

Qwen/DashScope handles cached claim, feedback and light issue classification;
it neither writes nor approves code. Requests, including fallback attempts, are
capped at 30/hour and 150/rolling 24h, with at most two attempts per judgment.
The rolling 24h input+output allowance is 100,000 tokens. Requests reserve a
conservative UTF-8 byte estimate plus output/overhead before HTTP; returned usage
settles the reservation, while failed or unmetered calls retain it. Historical
requests stay in the count; historical tokens are explicitly unknown.
Only models in repository variable `QWEN_FREE_ONLY_MODELS` are admitted. The user
confirmed the provider's free-quota-only switch on October 10; this allowlist
records that confirmation, not an API check of provider settings or balances.
Missing configuration fails closed. Quota/authentication errors retain work.
Concurrent cache writes merge atomically and identical requests share a lock.
Oversized, unavailable or uncertain judgments retain pending feedback/issues;
discussion changes invalidate issue verdicts. Pending candidates rotate fairly.

When the local Qwen request/token allowance or 12 KB input ceiling prevents a
judgement, discovery may queue a provisional candidate. An existing worker then
performs full-text, read-only screening through the shared `gpt-6-sol` subscription
host before checkout/coding. No claim, competing PR, coordination rule or publication
review is waived. Fallback screening is capped at 6 calls/5h and 18 calls/24h,
inside the shared 45-call limit, with two turns retained for coding/review.
Content above 128 KB gets a visible manual handoff; it is never truncated.
Failed identical screenings are not replayed indefinitely. Deferred PR feedback
is refreshed and screened before its checkout as well. Free-only API protection
and disabled repositories remain enforced.

`runtime.status().admission` distinguishes productive admission from authentication
health, reports pending reasons and unsent coordination requests, and flags an
empty worker queue with waiting candidates after 30 minutes without a Codex call.
Cloud logs emit a bounded Actions warning for that condition. ADK duplicate checks
run before preparing coordination requests; the controller never sends those
issue comments automatically.

The existing 45 Codex turns/5h ceiling includes generation and independent
review. Generation reserves its next review slot. Soft allocation targets are
24 new-fix, 10 maintenance, 6 repair and 5 validation turns; idle capacity can
be borrowed and these targets are not additional quotas. The queue prioritizes
retained patches. The existing authenticated host reads official quota without
another login or inference probe: stop fresh generation at 85% used and ordinary
model admission at 95%. Unknown/stale quota falls back to the durable call cap.
These local turn counts are not the provider's token-based subscription balance.
Reviewed publication does not need fresh inference headroom. Budget state and
actual canary spending survive cloud handover; failed work is not blindly replayed.

Repository allocation (user update, October 10): `repo_limits.py` sets Hermes
to 8 new PRs/UTC day, OpenClaw and Google ADK to 5 each, and other active repos
to 3 each. OpenClaw also permits at most 20 open PRs, including drafts. The
daily window resets at 08:00 Asia/Shanghai. Mem0, LiteLLM, Firecrawl, Gemini CLI,
Pydantic AI, vLLM and Crawl4AI receive zero model/task quota, including existing
PR maintenance; pending work is retained in `quota_wait`. Existing PRs are not
closed. New-fix task starts per UTC day are separately capped at Hermes 16,
OpenClaw 10, Google ADK 10 and every other active repo 6, charged only at first
generation. Precheck skips and subsequent reviews/retries of the same task do
not consume another daily task slot. Existing five-hour and global model limits
still apply; historical spending is retained across policy changes.
The subsequent October 10 user instruction disables all Hermes automation as
well, overriding its 8 PR / 16 task allocation with zero. This includes scanning,
judgments, new fixes and existing-PR replies/maintenance; existing PRs remain open.

`state/runtime.json` is local deployment configuration (ignored by git).
`python3 runtime.py` prints queue, heartbeat, model-call counts and recent results.
`./verify.sh` runs the offline commit gate (syntax, workflow YAML and runtime
regression checks). Legacy Claude checks remain archived below the active route.
`python3 codex_worker.py --probe` performs one real, read-only Codex auth probe.
The launchd canary passed on 2026-10-01: failing baseline, generated fix,
independent review, controller test rerun and local commit, with no public PR.
Current cloud deployment additionally requires two concurrent full-text screening
canaries, including an author claim after 12 KB, generation/environment checks and
independent review. All six turns are reserved before starting the shared host.
`run-fix.sh` drains at most one already-queued task; it no longer invokes Claude.

Pause cloud admission with `CODEX_CLOUD_ENABLED=false` and let the active task
drain; cancel the Actions run for an immediate stop. The authoritative queue and
budgets are in branch `codex-state`, not the stopped local database.
The cutover seed retained 50 historical tasks and 6 queued tasks. Task 41's
read-only clone was stopped before generation and held for inspection. Four
cloud canary coding calls were included in the retained budget. Local backup:
`~/.local/share/oss-scanner/takeover/cloud-20261002T031157Z`.
Keep local `.runtime/jobs/` for recovery; interrupted work may already have
reached a fork. Do not blindly reset failed tasks to queued.

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
