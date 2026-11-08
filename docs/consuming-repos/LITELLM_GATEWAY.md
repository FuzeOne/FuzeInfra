# LiteLLM Gateway — consumer guide

The shared, OpenAI-compatible LLM gateway for the FuzeOne fleet. Your service
talks OpenAI protocol to one in-cluster endpoint and **never holds provider
credentials**. Model routing, key custody, and cost caps live on the gateway.

Chart: `helm/litellm` · Argo app: `argocd/applications/litellm.yaml` · namespace `fuzeinfra`

---

## The contract

| | |
|---|---|
| **Service DNS** | `litellm.fuzeinfra.svc.cluster.local` |
| **Port** | `4000` |
| **Base URL** | `http://litellm.fuzeinfra.svc.cluster.local:4000` |
| **Auth** | `Authorization: Bearer $LITELLM_MASTER_KEY` |
| **Protocol** | OpenAI-compatible. `POST /chat/completions` (streaming + non-streaming) and `POST /embeddings`. The `/v1`-prefixed forms work too. Anthropic-format clients use `POST /v1/messages` — see [Anthropic-format clients](#anthropic-format-clients-claude-code) below. |

Plain HTTP, no TLS — this is cluster-internal traffic on the pod network. A
NetworkPolicy restricts it to the namespaces listed in
`networkPolicy.allowedNamespaces` (`helm/litellm/values-contabo.yaml`) — if your
service gets a connection timeout rather than a 401, your namespace is not on
that list yet. Add it in a PR.

> **Correction (2026-07-29).** This section previously said there was "no Ingress,
> no Cloudflare tunnel route, and no CF Access app". That has not been true since
> the admin UI was enabled: `values-contabo.yaml` sets `ingress.enabled: true`, so
> the gateway is reachable at `https://litellm.prod.fuzefront.com` under the
> `*.prod` wildcard — and because LiteLLM serves the UI and the API from one port,
> `/chat/completions` and `/v1/messages` are exposed at that hostname too. The
> gates are Cloudflare Access email-OTP plus `LITELLM_MASTER_KEY` on every API
> call. In-cluster callers should still use the Service DNS above: it is the path
> the NetworkPolicy is written for, and it avoids a tunnel round-trip.

## Models

Ask for these **by name**; the gateway maps each to a real upstream model.

| Model name | Routes to | Use for |
|---|---|---|
| `claude-opus-4-5` | `anthropic/claude-opus-4-5-20251101` | Chat. Legacy alias — kept for compatibility, migrate off it. |
| `claude-opus-5` | `anthropic/claude-opus-5` | Chat. **Current default choice.** |
| `claude-sonnet-5` | `anthropic/claude-sonnet-5` | Chat, cheaper/faster. |
| `claude-haiku-4-5` | `anthropic/claude-haiku-4-5` | Cheap, latency-sensitive. |
| `gpt-4.1` / `gpt-4.1-mini` | `openai/…` | Chat. Directly requestable, but their real job is being fallback targets. |
| `gemini-2.5-pro` / `-flash` / `-flash-lite`, `gemini-2.0-flash` / `-lite` | `gemini/…` | Chat. Last fallback hop. |
| `text-embedding-3-small` | `openai/text-embedding-3-small` | Embeddings. |

Adding a model is a PR against `models:` in `helm/litellm/values.yaml`. That
indirection is the point of running a gateway — do not work around it by calling
a provider directly.

### Cross-provider fallback — you do not implement this

Ask for a Claude model and you get one, unless that provider is failing, in which
case the router silently retries the **same request** against the next model and
returns a normal 200. You never see the upstream failure and you write no failover
code — a client that implements its own would have to hold every provider's key,
which is exactly what the gateway exists to prevent.

The chain is tier-matched, so an outage never quietly upgrades or downgrades what
you get (`routerSettings.fallbacks`):

| Primary | then | then |
|---|---|---|
| `claude-opus-5`, `claude-opus-4-5` | `gpt-4.1` | `gemini-2.5-pro` |
| `claude-sonnet-5` | `gpt-4.1` | `gemini-2.5-flash` |
| `claude-haiku-4-5` | `gpt-4.1-mini` | `gemini-2.5-flash-lite` |

Hops stop at the first success, so a dead last hop costs nothing — Gemini is
currently quota-exhausted and that is fine. This exists because on 2026-07-29 the
Anthropic account's credit ran out and took every chat turn down while the gateway
itself was perfectly healthy.

> **`/embeddings` needs an OpenAI key on the gateway.** Anthropic ships no
> embeddings endpoint, so embeddings cannot be served by an Anthropic key alone.
> If `OPENAI_API_KEY` is absent from the gateway's secret, chat works and
> `/embeddings` returns an auth error.

## Virtual keys — prefer these over the master key

`LITELLM_MASTER_KEY` is the gateway **admin** key. It can mint keys, read the proxy
config and see every consumer's spend. Most consumers need none of that, and spend
booked against it is unattributable — which is why `values-contabo.yaml` notes that
per-consumer cost attribution "is NOT meaningful" today.

A **virtual key** fixes all three: it carries a budget, a model allowlist and its own
line in the spend report. `scripts/mint-litellm-ci-key.sh` is the worked example
(it mints the key `a2a-maintain-ci` that CI uses):

```bash
kubectl -n fuzeinfra port-forward svc/litellm 4000:4000 &
export LITELLM_MASTER_KEY=$(kubectl -n fuzeinfra get secret litellm-secret \
    -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
scripts/mint-litellm-ci-key.sh | gh secret set LITELLM_CI_KEY --repo FuzeOne/FuzeInfra
```

Three things to know before minting your own:

- **A virtual key is NOT GitOps.** It is runtime state in LiteLLM's Postgres database
  (the one `database.enabled` provides), created over the API. It cannot be declared
  in `values.yaml` and Argo will never reconcile it. What *is* version-controlled is
  the mint payload — budget, allowlist, alias — so the key's properties stay
  reviewable even though its existence is out-of-band.
- **Allowlist the fallback targets, not just the model you ask for.** `models` is
  enforced on the model actually *dispatched*. A key allowed only `claude-opus-5`
  works perfectly right up until the router fails over to `gpt-4.1`, and is then
  refused by its own ACL — turning the fallback into an outage on the one day it
  matters.
- **Do not set `rpm_limit` / `tpm_limit`.** LiteLLM's `parallel_request_limiter_v3`
  hook injects `_litellm_rate_limit_descriptors` and friends into the **outbound
  provider payload** whenever a key carries rate limits. OpenAI answers
  `Unrecognized request arguments supplied: _litellm_...`; Anthropic answers
  `_litellm_rate_limit_descriptors: Extra inputs are not permitted`. So a
  rate-limited key does not throttle — it poisons every request *and every fallback
  hop*. Upstream [BerriAI/litellm#28146](https://github.com/BerriAI/litellm/issues/28146),
  open at time of writing. The budget still works; it is enforced on recorded spend,
  not by that hook.

Rotation is manual: LiteLLM enforces a unique `key_alias`, so delete the old key in
the admin UI before re-minting under the same alias.

## Getting `LITELLM_MASTER_KEY`

SealedSecrets are **strictly scoped** — a secret sealed for `fuzeinfra` cannot be
decrypted in your namespace. So the same master-key value is sealed a second
time for yours. Open a FuzeInfra issue mentioning `@fuze` with your namespace
and target Secret name; the operator runs:

```bash
scripts/seal-secret.sh <your-ns>/<your-secret> LITELLM_MASTER_KEY=@/tmp/litellm_master
```

and hands you the encrypted output to commit in **your** repo. The plaintext
never leaves their machine and is never pasted into an issue.

Rotation re-seals **both** files and merges both PRs together. Rotating only the
gateway side 401s every consumer.

## Calling it

Any OpenAI SDK works — point `baseURL` at the gateway.

```ts
const res = await fetch(`${process.env.LITELLM_URL}/chat/completions`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    ...(process.env.LITELLM_MASTER_KEY && {
      authorization: `Bearer ${process.env.LITELLM_MASTER_KEY}`,
    }),
  },
  body: JSON.stringify({
    model: process.env.LITELLM_DEFAULT_MODEL ?? "claude-opus-5",
    messages: [{ role: "user", content: "hello" }],
    stream: true,
  }),
});
```

Smoke-test from inside the cluster:

```bash
kubectl -n fuzeinfra port-forward svc/litellm 4000:4000
```

```bash
curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq '.data[].id'
```

## Anthropic-format clients (Claude Code)

Claude Code speaks the **Anthropic Messages** format, not OpenAI's, so it does not
use `/chat/completions`. Point it at the gateway and it calls `POST /v1/messages`;
LiteLLM bridges that to whichever provider the router picks.

`.github/workflows/a2a-maintain.yml` is the worked example — a CI agent that holds
**no provider key at all**:

```yaml
env:
  ANTHROPIC_BASE_URL: http://litellm.fuzeinfra.svc.cluster.local:4000
  ANTHROPIC_MODEL: claude-opus-5           # must be a name in `models:` above
  ANTHROPIC_DEFAULT_HAIKU_MODEL: claude-haiku-4-5
  CLAUDE_CODE_DISABLE_1M_CONTEXT: "1"
  CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS: "1"

steps:
  - uses: anthropics/claude-code-action@<sha>
    env:
      ANTHROPIC_AUTH_TOKEN: ${{ secrets.LITELLM_CI_KEY }}
    with:
      anthropic_api_key: ${{ secrets.LITELLM_CI_KEY }}
```

Four things that are easy to get wrong:

- **Pass the key twice.** LiteLLM reads `Authorization: Bearer`, which only
  `ANTHROPIC_AUTH_TOKEN` sets — but the action refuses to launch without its
  `anthropic_api_key` input, and that copy (sent as `x-api-key`) is ignored. Omit
  either and it fails, in two different confusing ways.
- **Pin every model alias.** Unpinned, Claude Code uses its own built-in IDs and the
  gateway 400s on a name it has never heard of. The original outage requested
  `claude-opus-5[1m]` — the `[1m]` is the extended-context marker, not a model here.
- **The job must run in-cluster** (`runs-on: staging`). A hosted runner cannot reach
  a ClusterIP Service; `arc-runners` is on the NetworkPolicy allowlist for this.
- **Set `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`.** Pre-release fields
  (`context_management`, beta tool schema fields) are rejected by a non-Anthropic
  upstream, so leaving them on breaks the fallback hop specifically.

Anthropic [does not support routing Claude Code to non-Claude models through a
gateway](https://code.claude.com/docs/en/llm-gateway). The fallback hops here are
configured to work anyway — see `additional_drop_params` in `values.yaml` — but
treat a fallback-served agent run as best-effort, not a supported configuration.

## Gotchas

- **Sampling params on Claude.** Claude Opus 5 / 4.8 / 4.7 **reject**
  `temperature`, `top_p`, and `top_k` — which OpenAI-shaped clients send by
  default. The gateway sets `drop_params: true` so they are silently dropped
  instead of 400ing. Don't turn that off.
- **Don't poll `/health`.** It requires the master key *and* round-trips every
  configured model to the provider, so it bills real tokens per call. The
  unauthenticated `/health/liveliness` and `/health/readiness` are what the
  kubelet probes use.
- **Provider keys belong on the gateway, not in your pod.** If your chart still
  plumbs `ANTHROPIC_API_KEY` into an app container, that is dead config — remove
  it. Your service should only ever hold `LITELLM_MASTER_KEY`.
- **Prod is GitOps.** Never `kubectl patch`/`edit` the live Deployment — Argo
  `selfHeal` reverts it within seconds. Change `helm/litellm`, merge to `main`.

## Operating

Enabling the gateway is gated on its secret existing, exactly like the other
credentialed services in this repo:

1. Seal `deploy/sealed-secrets/litellm-secret.yaml` — the full command is in
   `deploy/sealed-secrets/litellm-secret.yaml.template`.
2. In the **same commit**, flip `enabled: true` in
   `helm/litellm/values-contabo.yaml`.

Flipping first CrashLoops the pod with `CreateContainerConfigError`
(`LITELLM_MASTER_KEY` is a required `secretKeyRef`).

Register the Argo Application once on a new cluster:

```bash
kubectl apply -f argocd/applications/litellm.yaml
```

## Diagnosing it — start here, do not start with a deploy

Run the **`litellm-admin`** workflow (Actions → `litellm-admin` → *Run
workflow*). It answers the common questions against the **running** gateway in
about 45 seconds, with no merge and no Argo sync:

| Action | What it tells you |
|---|---|
| `list-keys` | every virtual key: alias, whether it is restricted to a model subset or has ALL MODELS, and a hashed-token prefix |
| `list-models` | every model the gateway actually serves right now |
| `clear-key-models` | **mutates** — sets `models: []` (all models) on every restricted key |
| `test-model` | proves a model is reachable *by a virtual key*: mints a temporary `models: []` key, calls the model with it, deletes it, and reports **which model actually served** the request |

`test-model` is usually the one you want, because it distinguishes the three
failures that look identical from outside: the key cannot reach the model, the
model is not served at all, or the primary errored and a **fallback hop**
answered (it prints the served model, so a silent fallover is visible).

Why this exists: the same questions used to be answered by editing a PostSync
hook, merging, waiting for `deploy-prod`, waiting for an Argo sync, then reading
the Job's logs — about 15 minutes per hypothesis. On 2026-08-31 a day was spent
that way on a virtual-key `403` that had **already been fixed** by adding the
model to `model_list`; a single `list-keys` would have shown both keys already
carried ALL MODELS. Check the live state before theorising about it.

Two things worth knowing about the mechanics:

- It runs on the `staging` ARC runner and reaches the gateway over cluster DNS
  (`http://litellm.fuzeinfra.svc.cluster.local:4000`). `arc-runners` is on
  `networkPolicy.allowedNamespaces` for exactly this reason.
- It deliberately does **not** use `kubectl exec`. The API server cannot
  currently reach the kubelet on the node hosting the pod, which breaks `exec`,
  `port-forward` **and `logs`** — so a Job on that node can look silent when it
  is running fine (issue #748). Pod-to-pod traffic is unaffected.

No credential is printed: only 8-character *hashed*-token prefixes, and
`test-model`'s temporary key is held in memory and deleted in a `finally` block.
FuzeInfra job logs are public — keep it that way if you extend the script
(`scripts/litellm_admin.py`, pinned by `tests/test_litellm_admin.py`).

### Adding a model is two steps, not one

A model must be in **`model_list`** (`helm/litellm/values.yaml`) *and* reachable
by the calling **key**. They are independent: a virtual key carries its own
`models` allow-list in LiteLLM's database, and the gateway `403`s
(`key not allowed to access model`) **before** the fallback chain runs — so a
restricted key produces a hard failure, not a fallover. `models: []` means "all
models"; the `litellm-sync-key-models` PostSync hook enforces that on every
sync, and `clear-key-models` does it on demand.

## The admin UI

`https://litellm.prod.fuzefront.com/ui`, reached from the Cloudflare App
Launcher tile and gated by the `*.prod.fuzefront.com` email-OTP Access app. Log
in as **`admin`** with the `LITELLM_MASTER_KEY` value — LiteLLM falls back to
the master key when `UI_USERNAME`/`UI_PASSWORD` are unset, which is why neither
is wired into the chart.

**The UI needs Postgres; the gateway does not.** Proxying (`/chat/completions`,
`/embeddings`) is stateless and ran for weeks with no database. The console is
not: the first thing it does after loading is mint a UI session key, and that is
a database write. With no `DATABASE_URL` the request never completes, the page
sits on "Loading", and Cloudflare eventually returns **error 522** for the
origin. The origin log is the tell — every
`/litellm-asset-prefix/_next/static/*` chunk returns 200 and then the log simply
stops, because uvicorn writes its access-log line when a response is *sent*, so
a request that hangs forever leaves no trace.

`database.enabled: true` (`helm/litellm/values-contabo.yaml`) wires
`DATABASE_URL` to a dedicated `litellm` database on the shared
`fuzeinfra-postgres`, created idempotently by the `ensure-database`
initContainer. Credentials come from `fuzeinfra-secrets`, the same way Airflow
connects — no SealedSecret needed, because LiteLLM lives in the `fuzeinfra`
namespace alongside them.

Two consequences worth knowing before you touch it:

- LiteLLM runs `prisma migrate deploy` at startup once `DATABASE_URL` is set, so
  cold starts are slower. The `startupProbe` allows 10 minutes.
- Reverting is one value: `database.enabled: false` restores a working gateway
  with a non-functional UI.

## Related

- [`CHROMADB_PROVISIONING.md`](CHROMADB_PROVISIONING.md) — vector store for RAG.
  Note the Chroma Service is named **`fuzeinfra-chromadb`**, so the correct URL
  is `http://fuzeinfra-chromadb.fuzeinfra.svc.cluster.local:8000`.
