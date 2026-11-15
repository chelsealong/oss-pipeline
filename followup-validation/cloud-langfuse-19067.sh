#!/usr/bin/env bash
set -euo pipefail
export CI=1
export NODE_ENV=test
corepack enable
corepack prepare pnpm@12.8.2 --activate
pnpm install --frozen-lockfile
cp .env.dev.example .env
pnpm --filter=@langfuse/shared run db:generate
pnpm --filter=@langfuse/shared run build
pnpm exec prettier --check packages/shared/src/server/queries/clickhouse-sql/fts.ts packages/shared/src/server/queries/clickhouse-sql/clickhouse-filter.events-full-routing.test.ts
source_file=packages/shared/src/server/queries/clickhouse-sql/fts.ts
cp "$source_file" /tmp/langfuse-19067-fts.ts
trap 'cp /tmp/langfuse-19067-fts.ts "$source_file"' EXIT
git show HEAD:"$source_file" > "$source_file"
if pnpm --filter=@langfuse/shared run test clickhouse-filter.events-full-routing.test.ts -t 'uses each representation' > /tmp/langfuse-19067-red.log 2>&1; then
  cat /tmp/langfuse-19067-red.log
  echo 'Original encoded token bug incorrectly passed'
  exit 1
fi
cat /tmp/langfuse-19067-red.log
python3 - <<'PY'
import pathlib,re
s=re.sub(r'\x1b\[[0-9;]*m','',pathlib.Path('/tmp/langfuse-19067-red.log').read_text())
assert '3 failed' in s and 'representation' in s,s[-5000:]
PY
cp /tmp/langfuse-19067-fts.ts "$source_file"
trap - EXIT
pnpm --filter=@langfuse/shared run test clickhouse-filter.events-full-routing.test.ts
pnpm exec turbo run lint --force --filter=@langfuse/shared
pnpm --filter=@langfuse/shared run typecheck
pnpm exec knip
git diff --check
