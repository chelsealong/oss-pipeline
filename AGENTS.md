# OSS pipeline takeover

## Current operating state

On 2026-10-01 the user explicitly authorized restoring the OSS pipeline with
Codex after the earlier full stop. Production moved to GitHub-hosted Codex on
2026-10-02 after cloud canary run 36958589558 passed. All three local launchd
services are unloaded and the local config is disabled. `state/runtime.json`
is deployment-only; missing,
invalid or disabled configuration fails closed.

`codex_host.py` owns one authenticated Codex app-server process. Two bounded
`codex_worker.py` controllers use separate ephemeral threads and isolated
checkouts, durable SQLite tasks, separate generation/review phases and
controller-only publication. Never run two independent CLI authentication
owners on the same refreshable login. Task claims and budgets are atomic;
at most one task per upstream repository runs at a time.
`local_service.py watch` owns issue detection and periodic reconciliation;
`local_service.py prwatch` handles PR feedback. The legacy Claude GitHub Actions
workflows remain disabled and cloud `state/watcher.json` stays off. Never start
them alongside the local worker. Do not upload the existing desktop Codex
credentials to GitHub.

The user subsequently requested cloud restoration and explicitly selected
ChatGPT/Codex subscription usage. `codex-cloud.yml` is the new cloud backend,
with production gated by the `CODEX_CLOUD_ENABLED` repository variable. Cloud
authentication uses a NEW, independent ChatGPT login, stored in the
`CODEX_AUTH_JSON` encrypted Secret; never copy the existing desktop auth cache.
Device authorization failed; an isolated local browser login succeeded instead.
Its initial local cache is not the current token source after cloud refreshes.
Keep production off until the cloud canary passes and the local services have
stopped. Seed `codex-state` from the final local snapshot before enabling it.
The cloud runner checkpoints task transitions and call reservations before
model requests/publication, rotates refreshed login credentials, drains work
before handover, and chains its successor. Never run both backends live.

The user selected `gpt-6-sol` for pipeline coding and review on 2026-10-01.
Use it for both backends and probes; do not fall back to Astra or inherit the
interactive IDE's model. Continue using the ChatGPT subscription, not API billing.

Qwen/DashScope is used only for the existing cached claim/feedback judge. Every
actual request is capped (including fallback requests) and requires a healthy
worker. Authentication/quota failures and unavailable worker heartbeat pause
model calls. Ordinary read-network failures retry only the affected task, at
most three attempts; publication ambiguity is never automatically replayed.
Repository-level PR creation denials place queued fixes in `publication_wait`
without consuming other repositories' queue space. Existing-PR responses remain
eligible. Expired holds restore tasks only when queue capacity permits. A draft
PR test does not prove ordinary PR creation permission: GitHub's concurrent
open-PR limits exclude drafts. Never bypass upstream limits or close production
PRs merely to free capacity. The preflight's optional temporary PR probe is off
by default and requires an explicit user request, including its immediate cleanup.
On 2026-10-05 the cloud GitHub PAT was synchronized with the local GitHub
credential (not the desktop Codex login). The encrypted preflight comparison
confirmed equality, but ordinary Hermes PR creation still failed both locally
and in cloud probe 37251066168. The earlier successful PR #132982 was a draft.
There were 137 open non-draft Hermes PRs; a concurrent PR cap is a supported
explanation, not a confirmed exact limit (only repository admins can read it).
Retain the repository hold; a new token or successful draft is not clearance.
No automatic claim/reply comments or PR closures: record these
for human follow-up. Code fixes may create/update PRs after independent review,
within upstream policy and the existing repository caps.

Stop cloud admission by setting `CODEX_CLOUD_ENABLED=false`; the runner drains
active work before exiting. Cancel its workflow for an immediate stop. Do not
restart local services until the cloud runner has stopped and its latest state
has been reconciled. Never replay interrupted jobs without inspecting local and remote
work: a process may have pushed before its last database update.

Original stop backup: `/Users/jialong/.local/share/oss-scanner/takeover/20261001T045103Z`.
Restart backup: `/Users/jialong/.local/share/oss-scanner/takeover/restart-20261001T071556Z`.
Cloud cutover backup: `/Users/jialong/.local/share/oss-scanner/takeover/cloud-20261002T031157Z`.
The seed retained 50 historical tasks and 6 queued tasks. Task 41 was stopped
during a read-only clone, before generation, and remains held as an error.
Four cloud canary coding sessions were added to the retained spending ledger.
The live queue and budgets are now in GitHub branch `codex-state`; the local DB
is a historical snapshot and must not be used as the current queue.

## Recovery and validation

The 2026-10-05 repair charges repository dispatch budgets only when an eligible
task starts generation, not for precheck skips. Existing daily PR caps and
repository shares remain in force. Feedback event identity includes the PR
head SHA, so unchanged feedback is not regenerated daily. Fix and response
queues reserve space for each other and rotate across repositories.

Validation can install ignored dependencies inside the checkout and use the
task's writable Cargo/Rustup, Node and Python caches. Dependency manifests and
lockfiles cannot be published as incidental changes. Documentation fixes use
appropriate documentation checks. Baseline failure evidence cannot waive
mandatory upstream checks. A repairable review rejection permits one correction
followed by a fresh independent review; publication still requires approval.

Per-attempt patches, untracked source files and bounded logs are encrypted with
`OSS_ARTIFACT_KEY` before entering the public `codex-state` branch or Actions
artifacts. Preserve this key; rotating it without re-encrypting history destroys
recovery. Never upload plaintext agent logs, auth caches or tool caches.
Actions recovery artifacts expire after 14 days; state-branch evidence persists.

Explicit maintainer permission can satisfy ADK coordination. Otherwise record
the issue URL and proposed request for human follow-up; never post automatically.
Check `runtime.status()` for active phases, 24-hour outcomes and human follow-ups.
Run `verify_codex.py` with PyYAML and cryptography, then the cloud preflight and
two concurrent canaries before restoring production after runtime changes.

The 2026-10-07 repair distinguishes `capacity_wait`, `human_wait` and
`validation_wait`. Waits do not occupy active queue slots or call budgets.
`task_recovery.py` rechecks capacity and verifies remote branches/PRs before
resuming positively identified pre-publication interruptions, at most three
attempts. Preserve ambiguous/published tasks for manual reconciliation.
Expired skips may reuse their durable task after three days, bounded by three
attempts; the historical watcher ledger must not permanently suppress these.

Human handoffs retain encrypted patches, tests, base SHAs and patch digests.
The human review workflow requires a named human's explicit exact-patch
attestation. Never dispatch it as an agent, fabricate approval, or use this
task's general repair authorization as patch-specific human review. A changed
base or patch invalidates that review and publication still needs independent
technical review and normal upstream eligibility. No automatic public messages.

Langfuse validation uses disposable synthetic Docker services and a secretless
bubblewrap process. No host socket, production data or external volume mounts
enter upstream scripts. Stop the web process and remove only the owned project
and its synthetic volumes when the task ends. Browser checks must actually run.
The preflight's `validation_canary` exercises database/web/Chromium setup without
model calls or publication. Never report unavailable validation as passing.

Cloud successors may follow service failures only after every auth owner stops
and state plus refreshed tokens are persisted. Three automatic failure restarts
per hour open a cooldown; scheduled runs retain this ledger. A failed final
checkpoint cannot dispatch an immediate successor. Preserve all spending.

## Locations and sources of truth

- Versioned pipeline: `/Users/jialong/.local/share/oss-pipeline`.
- Deployed local scripts and live historical state:
  `/Users/jialong/.local/share/oss-scanner`. These differ in places from the
  versioned checkout. Inspect differences before replacing either copy.
- Contribution clones: `/Users/jialong/Desktop/oss-contributions` and
  `/Users/jialong/Desktop/langfuse`.
- Evidence document: `/Users/jialong/Desktop/research/niw-oss-evidence.md`.
- Takeover audit: `/Users/jialong/Desktop/research/niw-evidence-data/pr-audit-2026-10-01`.

The old README and Desktop CLAUDE.md describe historical configurations and are
not authoritative about current execution. In particular, local and cloud
watchers were both running despite a comment saying the local one was stopped.

## Audit rules

Use `audit_pr_landings.py` for a read-only, paginated GitHub snapshot. It does not
import `scan` or `intent` and makes no model calls. Retain raw responses, capture
repository default-branch heads, and expose failed requests as incomplete data.

Never equate closed-unmerged PRs with rejected contributions: Google ADK uses
Copybara and Hermes maintainers cherry-pick into their own PRs. Count source PRs
separately from authored default-branch commits and maintainer salvage PRs.
Match explicit provenance and verified commits; title similarity alone is only
a candidate match. Preserve partial acceptance and credited-only cases.

Do not rerun `landings.py --quotes` or `quotes()` for an audit: quote judgement
calls the external model chain. The old `landings.py` PR counter has a 200-result
cap per repository and is unsuitable for a complete PR inventory.

Preserve existing branches, worktrees, attribution, and upstream contribution
rules. Never force-push, merge our own upstream PRs, or publish routine comments
as part of a read-only audit. Migration to Codex must keep existing duplicate,
assignment, scope, and rate-limit protections and accurate AI disclosure.
