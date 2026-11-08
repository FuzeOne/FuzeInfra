#!/usr/bin/env bash
#
# mint-litellm-ci-key.sh — mint the scoped LiteLLM virtual key that CI agents use.
#
# CI currently authenticates to the gateway with LITELLM_MASTER_KEY. That is the
# ADMIN key: it can mint keys, read the proxy config and see every consumer's
# spend. A CI job needs none of that. This mints a virtual key that can do
# exactly one thing — call the chat models a2a-maintain pins — with a budget
# attached and its own line in the gateway's cost report.
#
# WHY A SCRIPT AND NOT THE CHART: a virtual key is runtime state. LiteLLM stores
# it in its Postgres database (the one helm/litellm `database.enabled` provides),
# minted over the API. It cannot be declared in values.yaml and Argo will never
# reconcile it. What IS version-controlled is this payload — the budget, the
# model allowlist and the alias — so the key's PROPERTIES are reviewable even
# though its existence is out-of-band. tests/test_litellm_ci_routing.py asserts
# the allowlist below still covers what the workflow asks for.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#   # from a machine with cluster access:
#   kubectl -n fuzeinfra port-forward svc/litellm 4000:4000 &
#   export LITELLM_MASTER_KEY=$(kubectl -n fuzeinfra get secret litellm-secret \
#       -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
#   scripts/mint-litellm-ci-key.sh | gh secret set LITELLM_CI_KEY --repo FuzeOne/FuzeInfra
#
# The key is printed to STDOUT and nothing else is — every diagnostic goes to
# stderr — so it pipes straight into `gh secret set` without ever landing in a
# file, your shell history or argv.
#
# ── Options ──────────────────────────────────────────────────────────────────
#   --alias <name>    Key alias (default: a2a-maintain-ci). Also the label the
#                     gateway's spend report groups by.
#   --budget <usd>    Max spend before the key is refused (default: 25).
#   --window <dur>    Budget window, LiteLLM duration string (default: 30d).
#   --base-url <url>  Gateway base URL (default: http://localhost:4000).
#   --dry-run         Print the request body and exit without calling anything.
#
set -euo pipefail

ALIAS="a2a-maintain-ci"
BUDGET="25"
WINDOW="30d"
BASE_URL="${LITELLM_BASE_URL:-http://localhost:4000}"
DRY_RUN=false

while [ $# -gt 0 ]; do
  case "$1" in
    --alias)    ALIAS="$2"; shift 2 ;;
    --budget)   BUDGET="$2"; shift 2 ;;
    --window)   WINDOW="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=true; shift ;;
    -h|--help)  sed -n '2,45p' "$0" >&2; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# ─────────────────────────────────────────────────────────────────────────────
# The model allowlist.
#
# It MUST include the fallback targets, not just the models the workflow asks
# for by name. A virtual key's `models` list is enforced on the model actually
# dispatched, so a key allowed only the claude-* names would pass in normal
# operation and then be REJECTED at the exact moment the router failed over —
# turning the cross-provider fallback into an outage on the one day it matters.
#
# Derived from .github/workflows/a2a-maintain.yml (the pinned aliases) plus
# helm/litellm/values.yaml `routerSettings.fallbacks` (their hops).
# tests/test_litellm_ci_routing.py fails if the two drift apart.
# ─────────────────────────────────────────────────────────────────────────────
MODELS='[
  "claude-opus-5",
  "claude-sonnet-5",
  "claude-haiku-4-5",
  "gpt-4.1",
  "gpt-4.1-mini",
  "gemini-flash-latest",
  "gemini-flash-lite-latest"
]'

# ─────────────────────────────────────────────────────────────────────────────
# DELIBERATELY NO rpm_limit / tpm_limit.
#
# They look like the obvious companion to a budget, and they are a trap here.
# LiteLLM's parallel_request_limiter_v3 hook injects `_litellm_rate_limit_
# descriptors`, `_litellm_tpm_reserved_model`, `_litellm_tpm_reserved_scopes`
# and `_litellm_tpm_reserved_tokens` into the OUTBOUND provider payload whenever
# a key carries rate limits. Strict providers reject the request outright:
#
#   OpenAI    -> "Unrecognized request arguments supplied: _litellm_rate_limit_..."
#   Anthropic -> "_litellm_rate_limit_descriptors: Extra inputs are not permitted"
#
# So a rate-limited key does not merely throttle — it poisons every request,
# including every fallback hop, which is precisely the machinery this repo added
# on 2026-07-29 to survive a dead provider. Upstream: BerriAI/litellm#28146,
# open and unfixed as of this writing. The budget below still applies; it is
# enforced on recorded spend, not by that hook.
#
# If you are reading this because you want rate limits: check whether #28146 has
# shipped a fix, and if it has, verify a fallback hop still completes BEFORE
# adding them.
# ─────────────────────────────────────────────────────────────────────────────
BODY=$(cat <<JSON
{
  "key_alias": "${ALIAS}",
  "models": ${MODELS},
  "max_budget": ${BUDGET},
  "budget_duration": "${WINDOW}",
  "metadata": {
    "owner": "FuzeInfra CI",
    "workflow": ".github/workflows/a2a-maintain.yml",
    "purpose": "a2a-maintainer agent; scoped replacement for LITELLM_MASTER_KEY"
  }
}
JSON
)

if [ "$DRY_RUN" = true ]; then
  echo "POST ${BASE_URL}/key/generate" >&2
  echo "$BODY"
  exit 0
fi

if [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  echo "ERROR: LITELLM_MASTER_KEY is not set — minting requires the admin key." >&2
  echo "  export LITELLM_MASTER_KEY=\$(kubectl -n fuzeinfra get secret litellm-secret \\" >&2
  echo "      -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)" >&2
  exit 1
fi

echo "Minting virtual key '${ALIAS}' (budget \$${BUDGET}/${WINDOW}) at ${BASE_URL}..." >&2

resp=$(curl -sS --fail-with-body --max-time 30 \
  -X POST "${BASE_URL}/key/generate" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "content-type: application/json" \
  -d "$BODY") || {
    echo "" >&2
    echo "Mint failed. The usual causes, in order:" >&2
    echo "  * alias already exists — LiteLLM enforces unique key_alias. ROTATION IS" >&2
    echo "    DELIBERATELY MANUAL: delete the old key in the admin UI (Keys ->" >&2
    echo "    ${ALIAS} -> Delete), then re-run. Deleting by alias over the API is" >&2
    echo "    not scripted here because that path is unverified against this build." >&2
    echo "  * 401 — LITELLM_MASTER_KEY is stale; re-read it from the Secret." >&2
    echo "  * connection refused — no port-forward, or the gateway has no ready pod." >&2
    exit 1
  }

key=$(printf '%s' "$resp" | jq -r '.key // empty')
if [ -z "$key" ]; then
  echo "ERROR: gateway accepted the request but returned no key. Response:" >&2
  printf '%s\n' "$resp" >&2
  exit 1
fi

{
  echo ""
  echo "Minted. Set it as the repo secret (the key is on stdout, pipe it):"
  echo "  scripts/mint-litellm-ci-key.sh | gh secret set LITELLM_CI_KEY --repo FuzeOne/FuzeInfra"
  echo ""
  echo "a2a-maintain prefers LITELLM_CI_KEY and falls back to LITELLM_MASTER_KEY"
  echo "with a warning, so nothing breaks in between. Once this is set, REMOVE the"
  echo "LITELLM_MASTER_KEY repo secret — leaving it there keeps an admin key in CI."
} >&2

printf '%s\n' "$key"
