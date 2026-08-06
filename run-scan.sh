#!/bin/bash
# launchd entrypoint. Must live OUTSIDE ~/Desktop: macOS TCC denies launchd
# execution there ("Operation not permitted"), which silently killed every
# scheduled run on 2026-07-28/29.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || echo /usr/bin/python3)
# Reconciliation sweep. watch.py is the primary detector (GraphQL, ~5s); this
# full pass catches anything the watcher missed while the Mac was asleep or
# the process was down. Covers every repo, not just the two run locally before.
# --limit 10 --max-vet 6 was too shallow for a low-velocity tracker: langfuse
# spent all six vets on issues that were already taken and reported zero
# candidates, while a --limit 40 --max-vet 25 pass over the same repo found
# eight. The repo looked empty for days when it was only unexamined.
#
# 15 is a deliberate ceiling, not a maximum-effort setting. Each vet costs about
# three API calls (linked_prs, claimants, issue), so 13 repos x 15 vets x 3
# calls x 3 cycles/hour is roughly 1,755 of the 5,000/hour REST budget — 35%,
# leaving room for the watcher, the PR responder and the health check. 25 would
# take 58% and leave too little.
exec "$PY" scan.py --limit 30 --max-vet 15 >> scan.log 2>&1
