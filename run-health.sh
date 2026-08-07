#!/bin/bash
# Daily health check. Writes a report always; opens a GitHub issue ONLY when
# something is actually wrong, and reuses the same issue rather than filing a
# new one each day — a check that emails you every morning gets muted, and a
# muted check is worse than none.
set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")" || exit 1

REPO="chelsealong/oss-pipeline"
TITLE="Pipeline health: problems detected"
OUT=$(python3 health.py 2>&1); RC=$?
printf '%s\n%s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$OUT" >> health.log

[ $RC -eq 0 ] && { echo "healthy"; exit 0; }

# Only the silent-failure classes warrant interrupting a human. Upstream flakes
# and stale PRs are recorded in the report but are not alerts — they are not
# ours to fix and they recur.
#
# Two output-side classes were added after the pipeline spent a day producing
# PRs nobody could review: "none merged in 7 days" and "sessions spent with no
# PR". Both mean the loop is consuming quota without producing anything, which
# is the same failure as a crash and just as invisible. Duplicate counts stay
# out — they are an observation, and hermes is deliberately being watched at
# full quota for a month rather than alerted on.
# Match only inside the PROBLEM(S) block; the summary lines above it contain
# the same words at zero counts ("session-limit hits=0") and were tripping this.
PROBLEMS=$(printf '%s' "$OUT" | sed -n '/PROBLEM(S)/,$p')
if ! printf '%s' "$PROBLEMS" | grep -qE 'NONE dispatched|is not running|not loaded|no entries in the last hour|is PARTIAL|session-limit refusals|none merged in 7 days|sessions spent in 24h and no PR|cannot authenticate|runs failed in 24h|follow-up due|sweep failures in 24h|non-network sweep failures'; then
  echo "problems are upstream-only; logged, not alerting"; exit 0
fi

BODY=$(printf 'Automated check found a pipeline-side problem.\n\n```\n%s\n```\n' "$OUT")
EXISTING=$(gh issue list --repo "$REPO" --state open --search "$TITLE in:title" \
             --json number --jq '.[0].number' 2>/dev/null)
if [ -n "$EXISTING" ]; then
  gh issue comment "$EXISTING" --repo "$REPO" --body "$BODY" >/dev/null 2>&1 \
    && echo "updated issue #$EXISTING"
else
  gh issue create --repo "$REPO" --title "$TITLE" --body "$BODY" 2>&1 | tail -1
fi
