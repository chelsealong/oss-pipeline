#!/bin/bash
# Codex worker entrypoint. Detection now enqueues exact candidates durably.
set -euo pipefail
cd "$(dirname "$0")"
exec /usr/local/bin/python3 -u codex_worker.py --once "$@"
