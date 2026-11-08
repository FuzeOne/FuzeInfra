#!/usr/bin/env bash
#
# mint-litellm-fuze-key.sh — mint the scoped LiteLLM virtual key for the @fuze handler.
#
# The @fuze workflow (fuze.yml) and all automated CI workflows that route
# through the LiteLLM gateway use this key. It is scoped to only the models
# the handler uses (plus their fallback chain) with a budget attached, so a
# runaway session can't drain the gateway's provider credits.
#
# Sister script to mint-litellm-ci-key.sh (which mints the a2a-maintain key).
# The same caveats about rpm_limit / tpm_limit apply — see that script.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#   # from a machine with cluster access:
#   kubectl -n fuzeinfra port-forward svc/litellm 4000:4000 &
#   export LITELLM_MASTER_KEY=$(kubectl -n fuzeinfra get secret litellm-secret \
#       -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
#   scripts/mint-litellm-fuze-key.sh | gh secret set LITELLM_FUZE_KEY --repo FuzeOne/FuzeInfra
#
# The key is printed to STDOUT and nothing else is — every diagnostic goes to
# stderr — so it pipes straight into `gh secret set` without landing in a file,
# your shell history, or argv.
#
# ── Options ──────────────────────────────────────────────────────────────────
#   --alias <name>    Key alias (default: fuze-handler). The gateway's spend
#                     report groups by this, so it also serves as cost attribution.
#   --budget <usd>    Max spend before the key is refused (default: 100).
#   --window <dur>    Budget window, LiteLLM duration string (default: 30d).
#   --base-url <url>  Gateway base URL (default: http://localhost:4000).
#   --dry-run         Print the request body and exit without calling anything.
#
set -euo pipefail

ALIAS="fuze-handler"
BUDGET="100"
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
# Must include the fallback targets, not just the primary model. A virtual
# key's `models` list is enforced on the model ACTUALLY DISPATCHED, so a key
# allowed only the claude-* names would pass in normal operation and then be
# REJECTED at the exact moment the router fails over — turning the cross-
# provider fallback into an outage on the one day it matters.
#
# THIS is the source of truth, not .github/workflows/fuze.yml. The canonical
# `@fuze` entrypoint (installed fleet-wide) holds no model pins of its own —
# it resolves the gateway through ./.github/actions/fuze-code-action, which
# lets the caller ask for whatever it likes; this allowlist is what the
# gateway actually enforces at dispatch. Cross-checked against
# helm/litellm/values.yaml (`models` + `routerSettings.fallbacks`) by
# tests/test_litellm_fuze_routing.py, which fails if the two drift apart.
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
# LiteLLM's parallel_request_limiter_v3 hook injects `_litellm_*` params into
# the OUTBOUND provider payload whenever a key carries rate limits. Strict
# providers reject the request outright (OpenAI: "Unrecognized request
# arguments"; Anthropic: "_litellm_rate_limit_descriptors: Extra inputs are not
# permitted"). A rate-limited key does not merely throttle — it poisons every
# request, including every fallback hop. See BerriAI/litellm#28146.
# ─────────────────────────────────────────────────────────────────────────────
BODY=$(cat <<JSON
{
  "key_alias": "${ALIAS}",
  "models": ${MODELS},
  "max_budget": ${BUDGET},
  "budget_duration": "${WINDOW}",
  "metadata": {
    "owner": "FuzeInfra CI",
    "workflow": ".github/workflows/fuze.yml",
    "purpose": "@fuze mention handler; routes through LiteLLM for provider independence"
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
    echo "  * alias already exists — LiteLLM enforces unique key_alias. Delete the old" >&2
    echo "    key in the admin UI (Keys -> ${ALIAS} -> Delete), then re-run." >&2
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
  echo "Minted. Set it as the repo secret:"
  echo "  scripts/mint-litellm-fuze-key.sh | gh secret set LITELLM_FUZE_KEY --repo FuzeOne/FuzeInfra"
  echo ""
  echo "Propagate to every consuming repo that uses @fuze (FuzeSDLC propagation"
  echo "sets this as a required secret in docs/workflows/standard/fuze.yml)."
} >&2

printf '%s\n' "$key"
