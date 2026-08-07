#!/bin/bash
# Request assignment on issues in the repos that gate PRs behind it.
#
# langgraph, langchain and gemini-cli auto-close external PRs that have no
# linked, assigned issue (`require_issue_link.yml`, and its companion
# `reopen_on_assignment.yml` reopens them once an assignment exists). So the only
# route to a merged PR there is: claim first, implement after. Nothing here
# writes code or opens a PR — it only asks for the assignment.
#
# gemini-cli is fully self-service: commenting `/assign` makes its
# gemini-self-assign-issue.yml bot assign you immediately, provided the issue
# carries `help wanted`, is unassigned, and you are under its concurrent cap.
# langgraph/langchain need a human maintainer, so there the comment is a polite
# request and the wait is real.
#
#   ./claim.sh                 all gated repos
#   ./claim.sh gemini-cli      one repo
#   DRY_RUN=1 ./claim.sh       show what would be claimed, comment nothing
set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SCANNER="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCANNER/claim.log"
LOCK="$SCANNER/.claim.lock"
DRY_RUN="${DRY_RUN:-0}"

# pydantic-ai added 2026-08-07: its pr-guard.yml closes any PR from a
# non-collaborator whose linked issue is not assigned to the author. Our first
# PR there, #7282, was open for 18 seconds — and the review bot that ran anyway
# called the change "a straightforward bug fix ... with good test coverage".
# Nothing was wrong with the work; it was opened without asking.
declare -a GATED=(gemini-cli langchain pydantic-ai)

# Keep at most this many open assignments per repo. gemini-cli enforces its own
# cap and will refuse politely; the others have no bot, and hoarding assignments
# on a tracker where maintainers grant few is antisocial.
MAX_OPEN_CLAIMS="${MAX_OPEN_CLAIMS:-3}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

upstream_of() {
  python3 -c "
import scan; c = scan.REPOS.get('$1', {})
print(c.get('upstream', ''))" 2>/dev/null
}

claim_one_repo() {
  local key="$1" upstream held cand num title
  upstream="$(cd "$SCANNER" && upstream_of "$key")"
  [ -z "$upstream" ] && { log "[$key] SKIP - unknown repo key"; return 0; }

  held=$(gh api "repos/$upstream/issues?assignee=chelsealong&state=open&per_page=20" \
           --jq 'length' 2>/dev/null || echo 0)
  if [ "${held:-0}" -ge "$MAX_OPEN_CLAIMS" ]; then
    log "[$key] SKIP - already holding $held assignment(s), cap $MAX_OPEN_CLAIMS"
    return 0
  fi

  # Take from the vetted queue: those entries already passed the no-linked-PR and
  # no-claimant checks, so we are not asking for work someone else is doing.
  cand="$(cd "$SCANNER" && python3 scan.py --pop "$key" --claim 2>/dev/null)"
  if [ -z "$cand" ] || printf '%s' "$cand" | grep -q '"error"'; then
    log "[$key] nothing to claim - $(printf '%s' "$cand" | tr -d '\n' | head -c 120)"
    return 0
  fi
  num=$(printf '%s' "$cand" | python3 -c 'import json,sys;print(json.load(sys.stdin)["number"])' 2>/dev/null)
  title=$(printf '%s' "$cand" | python3 -c 'import json,sys;print(json.load(sys.stdin)["title"][:60])' 2>/dev/null)
  [ -z "$num" ] && { log "[$key] SKIP - unparseable candidate"; return 0; }

  # Don't ask twice on the same thread.
  if gh api "repos/$upstream/issues/$num/comments" --jq '.[].user.login' 2>/dev/null \
       | grep -qx 'chelsealong'; then
    log "[$key] SKIP #$num - already commented there"
    return 0
  fi

  local body
  if [ "$key" = "gemini-cli" ]; then
    # Exact token its bot matches on; anything else is ignored.
    body="/assign"
  else
    body="I'd like to take this one if it's still open — happy to put up a PR."
  fi

  if [ "$DRY_RUN" = "1" ]; then
    log "[$key] DRY_RUN - would comment on #$num: $body"
    printf '  [%s] #%s  %s\n         comment: %s\n' "$key" "$num" "$title" "$body"
    return 0
  fi

  if gh issue comment "$num" --repo "$upstream" --body "$body" >/dev/null 2>&1; then
    log "[$key] claimed #$num ($title)"
  else
    log "[$key] FAILED to comment on #$num"
    (cd "$SCANNER" && python3 scan.py --unclaim "$key" "$num" >/dev/null 2>&1) \
      && log "[$key] released #$num for retry"
  fi
}

main() {
  # One claimer at a time: watch.py fires this per accepted candidate and two
  # concurrent runs would race the same queue entry.
  exec 9>"$LOCK"
  if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
    log "another claim run is active; exiting"; exit 0
  fi
  log "=== claim run start (${*:-all gated}) ==="
  if [ $# -gt 0 ]; then for k in "$@"; do claim_one_repo "$k"; done
  else for k in "${GATED[@]}"; do claim_one_repo "$k"; done; fi
  log "=== claim run end ==="
}

main "$@"
