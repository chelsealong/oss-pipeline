#!/bin/bash
# Autonomous fixer: pop a pre-vetted issue, then drive Claude Code headlessly
# through fix -> test -> ship (PR only where that repo's policy allows it).
#
# Pairs with run-scan.sh: the scanner finds work every 20 minutes, this consumes
# one item per repo per invocation. Must live outside ~/Desktop (macOS TCC denies
# launchd execution in protected folders).
#
#   ./run-fix.sh <key> [...]   specific repos
#   ./run-fix.sh               every repo in ROTATION, one issue each
#
# Env:
#   DRY_RUN=1   resolve a candidate and print what would run, invoke nothing
set -uo pipefail

export PATH="/Users/jialong/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SCANNER="$(cd "$(dirname "$0")" && pwd)"
OSS="$HOME/Desktop/oss-contributions"
LOG="$SCANNER/fix.log"
LOCK="$SCANNER/.fix.lock"
DRY_RUN="${DRY_RUN:-0}"

# acceptEdits alone does NOT permit Bash — a run under it has every `gh` call
# denied with "This command requires approval" — so the tools this workflow
# needs are granted explicitly.
#
# ROOT CAUSE OF THE 403, finally identified: the egress IP. A plain
# `curl https://api.anthropic.com/v1/models` returned 403 from a Tokyo VPN exit
# (AS60068 Datacamp) — no credentials involved. Anthropic blocks datacenter
# address space, and Claude Code surfaces it as "Failed to authenticate. API
# Error: 403 Request not allowed". Two earlier theories were WRONG and are
# recorded here so nobody retries them: (a) bypassPermissions clashing with repos
# that ship .claude hooks, (b) a random ~1-in-4 service blip. Both were artefacts
# of the VPN switching nodes. Fix the network, not the flags — see network_ok().
ALLOWED_TOOLS="${ALLOWED_TOOLS:-Bash Read Write Edit Glob Grep WebFetch}"

# Retry policy for the transient 403 described above. A genuine 403 aborts in
# well under 30s; anything slower has started real work and must not be retried.
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
EARLY_FAIL_SECS="${EARLY_FAIL_SECS:-60}"
# Overridable so the retry path can be exercised with a stub instead of
# burning real invocations.
CLAUDE_BIN="${CLAUDE_BIN:-claude}"

# Absent on purpose:
#   transformers   upstream explicitly asks autonomous agents not to open PRs
#   gemini-cli, langgraph, langchain
#                  assignment-gated — a PR without a prior assignment is
#                  auto-closed by a bot, so opening one is worse than useless.
#                  Pass them explicitly to have the agent request assignment.
declare -a ROTATION=(adk langfuse langfuse-python spec-kit openclaw hermes firecrawl comfyui dify autogpt)

# Per-repo PRs/day. Instant triggering (watch.py fires this the moment a
# candidate is accepted) means a busy day could otherwise produce dozens of PRs,
# which is exactly how an account attracts spam enforcement. These mirror the
# limits each repo's own policy or observed tolerance implies.
daily_cap_for() {
  case "$1" in
    adk) echo 5 ;;          # highest merge odds so far: #6498 landed, three PRs live
    openclaw) echo 6 ;;     # 1 merged, 2 alive, 0 rejected — best acceptance rate
    dify|autogpt) echo 5 ;;
    vllm|sglang|pydantic-ai) echo 3 ;;
    hermes) echo 7 ;;    # raised by decision; duplicate breaker is the guard
    firecrawl|comfyui) echo 1 ;;
    *) echo 2 ;;
  esac
}

# hermes closes competing work as `duplicate`. This USED TO auto-downgrade the
# run to prepare-only at two duplicates in a rolling 30 days; that behaviour was
# removed by explicit decision — hermes stays at its full quota for a one-month
# observation period. The count is still measured and reported so the month
# produces data rather than a hunch. Reinstate the downgrade if duplicates climb:
# repeated duplicates, not volume, are what triggers GitHub spam enforcement.
duplicate_count_30d() {
  local upstream="$1" n
  n=$(gh pr list --repo "$upstream" --author chelsealong --state closed --limit 40 \
        --json labels,closedAt \
        --jq "[.[] | select(.closedAt > \"$(date -u -v-30d +%Y-%m-%d)\")
               | select([.labels[].name] | any(. == \"duplicate\" or . == \"invalid\"))] | length" \
        2>/dev/null || echo 0)
  echo "${n:-0}"
}

checkout_for() {
  case "$1" in
    adk)             echo "$OSS/adk-python" ;;
    langfuse)        echo "$HOME/Desktop/langfuse" ;;
    langfuse-python) echo "$OSS/langfuse-python" ;;
    langchain)       echo "$OSS/langchain" ;;
    spec-kit)        echo "$OSS/spec-kit" ;;
    gemini-cli)      echo "$OSS/gemini-cli" ;;
    openclaw)        echo "$OSS/openclaw" ;;
    hermes)          echo "$OSS/hermes-agent" ;;
    firecrawl)       echo "$OSS/firecrawl" ;;
    comfyui)         echo "$OSS/ComfyUI" ;;
    dify)            echo "$OSS/dify" ;;
    autogpt)         echo "$OSS/AutoGPT" ;;
    *)               echo "" ;;
  esac
}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

# Anthropic rejects requests from datacenter/VPN address space with HTTP 403,
# which surfaces inside Claude Code as the misleading
# "Failed to authenticate. API Error: 403 Request not allowed" and killed every
# run for hours while the egress IP was a Tokyo VPN endpoint (AS60068 Datacamp).
# An unauthenticated probe distinguishes the cases cleanly:
#   401 -> reachable, simply unauthenticated  => fine, proceed
#   403 -> egress blocked                     => no point starting a run
#   000 -> no connectivity at all
# Checking first turns a whole class of confusing failures into one clear line,
# and avoids consuming a queued candidate on a run that cannot possibly work.
network_ok() {
  local code
  code=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
           https://api.anthropic.com/v1/models 2>/dev/null || echo 000)
  case "$code" in
    401|200) return 0 ;;
    403)
      log "PREFLIGHT FAIL - api.anthropic.com returned 403: egress IP is blocked"
      log "                (usually a VPN/datacenter exit). Route api.anthropic.com"
      log "                direct, or switch to a residential node, then re-run."
      return 1 ;;
    000)
      log "PREFLIGHT FAIL - cannot reach api.anthropic.com (no connectivity)"
      return 1 ;;
    *)
      log "PREFLIGHT WARN - api.anthropic.com returned $code; proceeding anyway"
      return 0 ;;
  esac
}

# The only identity that may author contributions. A second account
# (chelsealong21) owns jialongli001@gmail.com — commits made with that email are
# attributed to the wrong account.
EXPECTED_GIT_NAME="chelsealong"
EXPECTED_GIT_EMAIL="chelsealong@126.com"

# Guard against the failure that produced adk-python#6516: the commit identity
# silently drifted, and the PR was opened before anyone noticed the CLA check
# validates the COMMIT EMAIL rather than the GitHub username. Verify identity
# once per run instead of discovering it from a red check afterwards.
check_identity() {
  local dir="$1" key="$2" name email
  name=$(git -C "$dir" config user.name 2>/dev/null)
  email=$(git -C "$dir" config user.email 2>/dev/null)
  if [ "$email" != "$EXPECTED_GIT_EMAIL" ] || [ "$name" != "$EXPECTED_GIT_NAME" ]; then
    log "[$key] ABORT - git identity is '$name <$email>', expected '$EXPECTED_GIT_NAME <$EXPECTED_GIT_EMAIL>'."
    log "[$key]         A PR authored under the wrong email is attributed to the wrong"
    log "[$key]         account and will fail CLA checks. Fix with:"
    log "[$key]           git config --global user.email $EXPECTED_GIT_EMAIL"
    return 1
  fi
  return 0
}

jget() {
  printf '%s' "$1" | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('$2',''))
except Exception: print('')" 2>/dev/null
}

run_one() {
  local key="$1" dir cand num url upstream implements needs_assign scanned rc qf
  dir="$(checkout_for "$key")"
  if [ -z "$dir" ] || [ ! -d "$dir/.git" ]; then
    log "[$key] SKIP - no checkout at '${dir:-?}'"; return 0
  fi

  check_identity "$dir" "$key" || return 0

  # Refuse to act on a degraded queue: a rate-limited scan is indistinguishable
  # from "no work", and acting on a stale pick is how claim races are lost.
  qf="$SCANNER/queue/$key.json"
  if [ -f "$qf" ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('$qf')).get('partial') else 1)" 2>/dev/null; then
    log "[$key] SKIP - last scan was PARTIAL (rate-limited); queue may be stale"; return 0
  fi

  # Daily cap, checked against GitHub rather than a local counter so a manual
  # PR counts too.
  local upstream_guess cap opened
  upstream_guess=$(python3 -c "
import scan; c=scan.REPOS.get('$key',{})
print(c.get('implements_in') or c.get('upstream',''))" 2>/dev/null)
  if [ -n "$upstream_guess" ]; then
    cap=$(daily_cap_for "$key")
    opened=$(gh pr list --repo "$upstream_guess" --author chelsealong --state all \
               --search "created:>=$(date -u +%Y-%m-%d)" --json number --jq 'length' 2>/dev/null || echo 0)
    if [ "${opened:-0}" -ge "$cap" ]; then
      log "[$key] SKIP - daily cap reached ($opened/$cap PRs on $upstream_guess today)"
      return 0
    fi
  fi

  cand="$(cd "$SCANNER" && python3 scan.py --pop "$key" --claim 2>/dev/null)"
  if [ -z "$cand" ] || printf '%s' "$cand" | grep -q '"error"'; then
    log "[$key] nothing to do - $(printf '%s' "$cand" | tr -d '\n' | head -c 140)"; return 0
  fi

  num=$(jget "$cand" number);            url=$(jget "$cand" url)
  upstream=$(jget "$cand" upstream);     implements=$(jget "$cand" implements_in)
  needs_assign=$(jget "$cand" needs_assignment); scanned=$(jget "$cand" scanned_at)
  [ -z "$num" ] && { log "[$key] SKIP - unparseable candidate"; return 0; }

  local ship_note=""
  if [ "$key" = "hermes" ]; then
    local dups
    dups=$(duplicate_count_30d "$upstream")
    if [ "${dups:-0}" -ge 2 ]; then
      # Observed, not enforced. The run proceeds at full quota by decision.
      log "[$key] NOTE: $dups PR(s) closed as duplicate/invalid in the last 30 days"
      ship_note="

CONTEXT: $dups of this author's recent PRs on this repo were closed as duplicate.
Duplicate saturation here is severe — a PR has appeared twelve seconds after the
issue was filed. Be correspondingly strict in the claim re-check, especially the
file/symbol search: a competing PR often describes the same fix in different
words and never references the issue number. If in doubt, do nothing."
    fi
  fi

  log "[$key] START #$num $url (needs_assignment=$needs_assign, vetted $scanned)"

  local prompt
  prompt=$(cat <<EOF
Run the oss-daily-issue-fix workflow for repo key '$key', on ONE issue only.

This issue was already vetted by the pre-scan queue. Do NOT search for a
different issue and do NOT re-triage the tracker:

  issue:            #$num
  url:              $url
  upstream:         $upstream
  implements_in:    $implements
  needs_assignment: $needs_assign
  vetted_at:        $scanned

The working directory is already the correct local checkout.

MANDATORY, in this order:

1. Read this repo's profile in the oss-daily-issue-fix skill and follow it
   exactly - especially its risky-area exclusions and its ship policy. Some
   repos are prepare-only and must NOT have a PR opened at all.

2. Re-verify the claim NOW, before writing any code. The vetting above is only
   as fresh as vetted_at, and issues get claimed within minutes on busy repos.
   Check all three signals: closedByPullRequestsReferences; a scan of open PRs
   for this issue number AND for the files you intend to touch; and the issue
   comments. If ANY signal shows it is taken, STOP and report that - do not
   open a rival PR.

3. If needs_assignment is True and you do not already hold the assignment, do
   NOT implement. Leave one short, plain comment asking to be assigned, then
   stop for this run.

4. Implement the minimal fix. Add or update a test and PROVE IT FAILS WITHOUT
   THE FIX: stash the source change, run the test, confirm it fails, restore.
   State that explicitly.

5. Run the repo's real lint and test commands. If something fails for reasons
   unrelated to your change, confirm it fails identically on unmodified
   upstream/main and say so. Never push a broken change.

6. Ship per the repo's policy. When opening a PR, ALWAYS target the real
   upstream: --repo $upstream --head chelsealong:<branch>. NEVER open a PR
   whose base is your own fork - that reaches no maintainer and has already
   happened once. Include the repo's required disclosure of AI assistance.

7. COMMIT MESSAGES MUST NOT CONTAIN A Co-Authored-By TRAILER, and in particular
   never 'Co-Authored-By: ... <noreply@anthropic.com>'. A CLA bot counts that
   trailer as a second contributor, and since the address has signed no CLA the
   whole check fails: adk-python#6516 sat red on 'Missing CLA from one or more
   contributors' purely because of it, while the human author was already ✅.
   langchain rejects the same trailer outright via an org ruleset. Plain commit
   messages only.

8. Never force-push. Never merge. Exactly one issue this run.

If the correct outcome is to do nothing, do nothing and say why. That is a
normal result, not a failure.

Finish with a one-paragraph summary: what changed, the test evidence, and the
PR URL - or the precise reason no PR was opened.$ship_note
EOF
)

  if [ "$DRY_RUN" = "1" ]; then
    log "[$key] DRY_RUN - would run claude in $dir for #$num ($url)"
    printf '[%s] DRY_RUN candidate #%s  %s\n         checkout: %s\n' "$key" "$num" "$url" "$dir"
    return 0
  fi

  local tmo; tmo="$(command -v gtimeout || command -v timeout || true)"
  local attempt=1 started elapsed back

  # Roughly one invocation in four dies within seconds with
  # "Failed to authenticate. API Error: 403 Request not allowed". It is NOT
  # deterministic — measured across permission modes and working directories,
  # the same command both succeeds and fails — so retry rather than reconfigure.
  #
  # Only EARLY failures are retried. A run that failed after doing real work may
  # already have pushed a branch or opened a PR, and re-running it would risk a
  # duplicate; those are left for the next scheduled run.
  while :; do
    started=$(date +%s)
    if [ -n "$tmo" ]; then
      ( cd "$dir" && "$tmo" 3600 "$CLAUDE_BIN" -p "$prompt" --permission-mode acceptEdits --allowedTools "$ALLOWED_TOOLS" ) >>"$LOG" 2>&1
    else
      ( cd "$dir" && "$CLAUDE_BIN" -p "$prompt" --permission-mode acceptEdits --allowedTools "$ALLOWED_TOOLS" ) >>"$LOG" 2>&1
    fi
    rc=$?
    elapsed=$(( $(date +%s) - started ))

    [ $rc -eq 0 ] && break
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ] || [ "$elapsed" -gt "$EARLY_FAIL_SECS" ]; then
      [ "$elapsed" -gt "$EARLY_FAIL_SECS" ] && \
        log "[$key] failed after ${elapsed}s — not retrying (may have done partial work)"
      break
    fi
    # A blocked egress will not recover within seconds, and retrying just burns
    # the lock; re-probe and bail out instead of grinding through attempts.
    if ! network_ok; then
      log "[$key] network went down mid-run; abandoning (candidate will be released)"
      break
    fi
    back=$(( 120 * attempt ))     # 2min, 4min - long enough to outlast a blip
    log "[$key] attempt $attempt failed in ${elapsed}s (rc=$rc); retrying in ${back}s"
    sleep "$back"
    attempt=$(( attempt + 1 ))
  done
  [ "$attempt" -gt 1 ] && [ $rc -eq 0 ] && log "[$key] succeeded on attempt $attempt"
  [ $rc -eq 124 ] && log "[$key] TIMEOUT after 3600s on #$num"

  # A crashed or timed-out run did no work, but the candidate was already marked
  # claimed at pop time and would otherwise never be retried. A transient
  # "403 Request not allowed" burned #115700 exactly this way. Release it.
  if [ $rc -ne 0 ]; then
    (cd "$SCANNER" && python3 scan.py --unclaim "$key" "$num" >/dev/null 2>&1) \
      && log "[$key] released claim on #$num for retry (run failed rc=$rc)"
  fi

  log "[$key] END #$num (exit $rc)"
  return 0
}

main() {
  # One fixer at a time: concurrent runs would fight over the same checkouts.
  exec 9>"$LOCK"
  if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
    log "another fixer is running; exiting"; exit 0
  fi

  log "=== fixer start (${*:-full rotation}) ==="

  # One probe per run, before any candidate is popped.
  if ! network_ok; then
    log "=== fixer end (skipped: network) ==="
    exit 0
  fi
  if [ $# -gt 0 ]; then for k in "$@"; do run_one "$k"; done
  else for k in "${ROTATION[@]}"; do run_one "$k"; done; fi
  log "=== fixer end ==="
}

main "$@"
