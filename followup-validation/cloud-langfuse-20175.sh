#!/usr/bin/env bash
set -euo pipefail
export CI=1
export NODE_ENV=test
corepack enable
corepack prepare pnpm@12.16.1 --activate
pnpm install --frozen-lockfile
cp .env.dev.example .env
pnpm --filter=@langfuse/shared run db:generate
pnpm --filter=@langfuse/shared run build
pnpm exec prettier --check worker/src/scripts/upsertDefaultModelPrices.test.ts
prices=worker/src/constants/default-model-prices.json
cp "$prices" /tmp/langfuse-20175-prices.json
trap 'cp /tmp/langfuse-20175-prices.json "$prices"' EXIT
python3 - <<'PY'
import json
p='worker/src/constants/default-model-prices.json'
d=json.load(open(p))
m=next(x for x in d if x['modelName']=='anthropic.claude-haiku-5-5')
t=next(x for x in m['pricingTiers'] if x['name']=='Large Context (>100K)')
assert t['conditions'][0]['value']==100000
t['conditions'][0]['value']=200000
open(p,'w').write(json.dumps(d))
PY
if pnpm --filter worker run test upsertDefaultModelPrices.test.ts -t 'selects the Haiku geographic rate' > /tmp/langfuse-20175-red.log 2>&1; then
  cat /tmp/langfuse-20175-red.log
  echo 'Boundary mutant incorrectly passed'
  exit 1
fi
cat /tmp/langfuse-20175-red.log
python3 - <<'PY'
import pathlib,re
s=re.sub(r'\x1b\[[0-9;]*m','',pathlib.Path('/tmp/langfuse-20175-red.log').read_text())
assert '1 failed' in s and '1 passed' in s and 'Large Context (>100K)' in s,s[-5000:]
PY
cp /tmp/langfuse-20175-prices.json "$prices"
trap - EXIT
pnpm --filter worker run test upsertDefaultModelPrices.test.ts
pnpm exec turbo run lint --force --filter=worker --filter=@langfuse/shared
pnpm --filter worker run typecheck
pnpm exec knip
 git diff --check
