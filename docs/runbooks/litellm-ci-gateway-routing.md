# Runbook — route hosted-runner CI through the public LiteLLM gateway

## What this is

`fuze.yml` (and the other `fuze-code-action` call sites) run on GitHub-hosted
runners. Those runners cannot resolve the in-cluster gateway DNS name
(`litellm.fuzeinfra.svc.cluster.local`), so `./.github/actions/llm-endpoint`
probes it, fails, and falls back to a **direct vendor key**. When that vendor is
out of credit the agent run fails — the incident this whole path exists to end.

The gateway is also reachable publicly at `https://litellm.prod.fuzefront.com`,
behind **Cloudflare Access**. This runbook turns on routing a hosted runner
through that public host using a Cloudflare Access **service token**, so CI uses
the gateway (with its cross-provider failover) instead of a single vendor key.

## What is already in place (code + infra — no action needed)

- **Service token + policy** — terraform
  `cloudflare_zero_trust_access_service_token.litellm_ci`, bound to the
  `litellm_service` Access app (`litellm.prod.fuzefront.com`) by the
  `litellm_service_ci_token` policy (`terraform/contabo/cloudflare.tf`). It sits
  alongside the human Google/email-OTP policies, so the admin console is
  unchanged.
- **`llm-endpoint` CF-Access support** — the action accepts
  `cf-access-client-id` / `cf-access-client-secret`, masks the secret on entry,
  uses them on its readiness probe, and returns them only through its pre-masked
  `custom-headers` output (the same masked-output channel as `auth-token`).
- **`fuze-code-action` wiring** — forwards those inputs to `llm-endpoint` and
  sets the returned headers as `ANTHROPIC_CUSTOM_HEADERS` on the claude rung, so
  the model requests clear Access too (the probe passing is not enough on its
  own — Claude Code v2.1.227+ parses newline-separated `Name: Value`).
- **`fuze.yml`** forwards `secrets.CF_ACCESS_CLIENT_ID` /
  `secrets.CF_ACCESS_CLIENT_SECRET`, and both names are registered in
  `scripts/provision_secrets.py`'s `FLEET_SOURCED` (+ the `SRC_` block in
  `provision-secrets.yml`), so FuzeSDLC provisions them fleet-wide — no manifest
  edit.

With the two secrets **unset**, the `cf-access-client-*` inputs are empty, no CF
headers are sent, and behaviour is byte-identical to before (in-cluster probe →
vendor fallback). Activation is entirely the human steps below.

## Activation (human steps — deliberately not automated)

`CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` are **fleet-sourced** (FuzeSDLC
`provision_secrets.py` `FLEET_SOURCED`): FuzeSDLC holds the source of truth and
fans them out to every repo that installs `fuze.yml`. The `client_secret` lives
only in terraform state (S3 backend); extract it from a **local terminal** and
never print, paste, or commit it.

1. **Set the two source secrets on FuzeSDLC** (`FuzeOne/FuzeSDLC` → Settings →
   Secrets → Actions), with the values from FuzeInfra terraform:

   ```bash
   cd terraform/contabo
   terraform output -raw litellm_ci_service_token_client_id      # -> FuzeSDLC secret CF_ACCESS_CLIENT_ID
   terraform output -raw litellm_ci_service_token_client_secret  # -> FuzeSDLC secret CF_ACCESS_CLIENT_SECRET
   ```

   `provision-secrets.yml` maps `secrets.CF_ACCESS_CLIENT_ID` → `SRC_CF_ACCESS_CLIENT_ID`
   (and the secret) for the provisioner.

2. **Fan out to consuming repos** — run the FuzeSDLC **`provision-secrets.yml`**
   workflow (`workflow_dispatch`). It seals `CF_ACCESS_CLIENT_ID` /
   `CF_ACCESS_CLIENT_SECRET` to each repo installing `fuze.yml`; the plaintext
   never leaves the job. (A local `python scripts/provision_secrets.py --apply`
   works too, but it reads the values from `SRC_`-prefixed env vars —
   `export SRC_CF_ACCESS_CLIENT_ID=…`.)

3. **Point CI at the public gateway** — set the repo (or org) Actions **variable**
   `FUZE_LITELLM_BASE_URL` to `https://litellm.prod.fuzefront.com`.

   > Do **not** do step 3 without steps 1–2. With the secrets unset, the probe
   > would 302 to the Access login page and still fall back — a config that looks
   > deliberate but is worse than leaving the var unset.

4. **Verify** — trigger a `@fuze` run (or re-run any `fuze-code-action` job) and
   read its `llm-endpoint` notice: it should report `mode=litellm vendor=litellm`
   routing through `https://litellm.prod.fuzefront.com`, not a
   `fallback-*` mode. `.github/workflows/litellm-check-keys.yml`
   (`workflow_dispatch`) is a quick independent gateway reachability check.

## Rollback

Unset `FUZE_LITELLM_BASE_URL` (reverts to the in-cluster default → vendor
fallback). The secrets can stay; they are inert without the var. To fully revoke,
delete the token in terraform (`litellm_ci`) and re-apply.
