#!/bin/bash
# Pipeline health. Two cadences, two questions, two ways of speaking up.
#
#   ./run-health.sh            daily  — "is anything wrong"
#   ./run-health.sh critical   hourly — "is it down"
#
# The split exists because of 2026-09-06: a 30-day PAT expired at 17:04 UTC and
# forty fix-one runs failed before the once-a-day check noticed at 01:06. Eight
# hours blind is a property of the schedule, not of the checks, so the
# down-level subset now runs hourly on cheap local state and one run list.
#
# It also changed HOW it speaks. Every alert used to be another comment on one
# permanently-open issue titled "Pipeline health: problems detected" — by the
# time the PAT died that issue had 22 comments, and comment 23 correctly named
# the failed credential and was read by nobody. A notification that looks
# identical whether the pipeline is idle or dead carries no information. So a
# down-level fault now opens its OWN issue, and closes it on recovery.
set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")" || exit 1

REPO="chelsealong/oss-pipeline"
STANDING_TITLE="Pipeline health: problems detected"
OUTAGE_PREFIX="PIPELINE DOWN"
SIG_FILE="state/outage-signature.txt"

MODE="${1:-daily}"

# Faults that mean the pipeline cannot do its job at all — no amount of waiting
# fixes them and nothing else runs until a human acts. Everything else (stale
# PRs, upstream flakes, a slow week) is a report, not an alarm.
CRITICAL_RE='cannot authenticate|is not running|is not loaded|no entries in the last hour|NONE dispatched|fix-one runs failed'

# Real but not down: worth a daily note, never worth an out-of-hours ping.
DEGRADED_RE='is PARTIAL|session-limit refusals|none merged in 7 days|sessions spent in 24h and no PR|follow-up due|sweep failures in 24h|non-network sweep failures'

open_issue_number() {  # $1 = title-or-prefix to search
  gh issue list --repo "$REPO" --state open --search "$1 in:title" \
    --json number --jq '.[0].number' 2>/dev/null
}

# One representation of an outage, used by both cadences: the daily report can
# see faults the hourly subset does not check (NONE dispatched comes from the
# queue-flow check), so it must be able to raise and clear the same issue
# itself. Delegating to the hourly path instead would have re-run the narrower
# check, found it clean, and closed the issue the daily run had just opened.
#   $1 = full report text   $2 = the PROBLEM(S) block
raise_outage() {
  local out="$1" problems="$2" existing sig last body first
  existing=$(open_issue_number "$OUTAGE_PREFIX")

  if ! printf '%s' "$problems" | grep -qE "$CRITICAL_RE"; then
    return 1                       # nothing down-level here
  fi

  # Hourly and talkative is the same mistake in a new costume: 24 identical
  # comments a day trains the reader to ignore this issue exactly as the old
  # standing one was ignored. Speak once per distinct fault, and again only
  # when the fault itself changes.
  sig=$(printf '%s' "$problems" | grep -E "$CRITICAL_RE" | sort | shasum | cut -d' ' -f1)
  last=$(cat "$SIG_FILE" 2>/dev/null || true)
  body=$(printf 'The pipeline is unable to work.\n\n```\n%s\n```\n' "$out")

  if [ -n "$existing" ]; then
    if [ "$sig" = "$last" ]; then
      echo "outage #$existing unchanged; not commenting again"; return 0
    fi
    gh issue comment "$existing" --repo "$REPO" --body "$body" >/dev/null 2>&1 \
      && echo "outage #$existing changed; commented"
  else
    first=$(printf '%s' "$problems" | grep -E "$CRITICAL_RE" | head -1 \
              | sed 's/^ *- *//' | cut -c1-70)
    gh issue create --repo "$REPO" \
      --title "$OUTAGE_PREFIX — $first" --body "$body" 2>&1 | tail -1
  fi
  printf '%s\n' "$sig" > "$SIG_FILE"
  return 0
}

clear_outage() {
  local existing
  rm -f "$SIG_FILE"
  existing=$(open_issue_number "$OUTAGE_PREFIX")
  [ -z "$existing" ] && return 1
  gh issue close "$existing" --repo "$REPO" \
    --comment "Recovered — the check is clean as of $(date -u +%Y-%m-%dT%H:%MZ)." \
    >/dev/null 2>&1 && echo "closed outage issue #$existing"
}

# ---------------------------------------------------------------- critical
if [ "$MODE" = "critical" ]; then
  OUT=$(python3 health.py --critical 2>&1); RC=$?
  printf '%s\n%s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$OUT" >> health-critical.log

  if [ $RC -eq 0 ]; then
    clear_outage || echo "healthy"
    exit 0
  fi
  PROBLEMS=$(printf '%s' "$OUT" | sed -n '/PROBLEM(S)/,$p')
  raise_outage "$OUT" "$PROBLEMS" || echo "problems are not down-level; logged, not alerting"
  exit 0
fi

# ------------------------------------------------------------------- daily
OUT=$(python3 health.py 2>&1); RC=$?
printf '%s\n%s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$OUT" >> health.log

if [ $RC -eq 0 ]; then
  clear_outage || echo "healthy"
  exit 0
fi

# Match only inside the PROBLEM(S) block; the summary lines above it contain the
# same words at zero counts ("session-limit hits=0") and were tripping this.
PROBLEMS=$(printf '%s' "$OUT" | sed -n '/PROBLEM(S)/,$p')

# Down-level faults go to the outage issue whichever cadence found them; a
# degraded-only day still goes to the standing issue, which is the right home
# for something that is worth recording and not worth waking anyone for.
raise_outage "$OUT" "$PROBLEMS" && exit 0

if ! printf '%s' "$PROBLEMS" | grep -qE "$DEGRADED_RE"; then
  echo "problems are upstream-only; logged, not alerting"; exit 0
fi

BODY=$(printf 'Automated check found a pipeline-side problem.\n\n```\n%s\n```\n' "$OUT")
EXISTING=$(open_issue_number "$STANDING_TITLE")
if [ -n "$EXISTING" ]; then
  gh issue comment "$EXISTING" --repo "$REPO" --body "$BODY" >/dev/null 2>&1 \
    && echo "updated issue #$EXISTING"
else
  gh issue create --repo "$REPO" --title "$STANDING_TITLE" --body "$BODY" 2>&1 | tail -1
fi
