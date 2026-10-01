# OSS pipeline takeover

## Current operating state

On 2026-10-01 the user explicitly authorized restoring the OSS pipeline with
Codex after the earlier full stop. The current backend is local Codex, using
the existing ChatGPT login. `state/runtime.json` is deployment-only; missing,
invalid or disabled configuration fails closed.

`codex_worker.py` is the single executor, with isolated checkouts, durable
SQLite tasks, separate generation/review phases and controller-only publication.
`local_service.py watch` owns issue detection and periodic reconciliation;
`local_service.py prwatch` handles PR feedback. The legacy Claude GitHub Actions
workflows remain disabled and cloud `state/watcher.json` stays off. Never start
them alongside the local worker. Do not upload the existing desktop Codex
credentials to GitHub.

The user subsequently requested cloud restoration and explicitly selected
ChatGPT/Codex subscription usage. `codex-cloud.yml` is the new cloud backend,
with production gated by the `CODEX_CLOUD_ENABLED` repository variable. Cloud
authentication uses a NEW, independent device login, stored only in the
`CODEX_AUTH_JSON` encrypted Secret; never copy the existing desktop auth cache.
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
worker. Authentication failures, task errors and unavailable worker heartbeat
pause model calls. No automatic claim/reply comments or PR closures: record these
for human follow-up. Code fixes may create/update PRs after independent review,
within upstream policy and the existing repository caps.

Stop by setting deployed `state/runtime.json` enabled=false and unloading the
three local oss-watch/oss-prwatch/oss-fix jobs; stopping a detector alone is not a
full shutdown. Never replay interrupted jobs without inspecting local and remote
work: a process may have pushed before its last database update.

Original stop backup: `/Users/jialong/.local/share/oss-scanner/takeover/20261001T045103Z`.
Restart backup: `/Users/jialong/.local/share/oss-scanner/takeover/restart-20261001T071556Z`.

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
