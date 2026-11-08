# CRIT Log → `@fuze` Auto-Fix Pipeline

Grafana detects CRIT/FATAL/PANIC log lines in Loki and routes them, through the
canonical **`@fuze`** mechanism, to the repo that owns the emitting namespace.
The actual diagnosis + fix runs inside that repo's `@fuze` handler
(`fuze-cluster.yml` for FuzeInfra, `fuze.yml` for a consumer), which routes through
`fuze-code-action`'s **multi-provider cascade** — so the pipeline is
**vendor-independent** and never pins a single LLM CLI.

## Architecture

```
Loki logs
    ↓  (every 5 min)
Grafana unified alerting            receiver: fuze-autofix
    ↓  (severity=critical fired)
CF Worker crit-alert-bridge          ← bearer token auth
    ↓  (repository_dispatch: grafana-crit-alert)
GitHub Actions: grafana-crit-fix.yml
    ↓  kubectl port-forward → Loki HTTP API (fetch 30 min of CRIT lines, for context)
    ↓  resolve owning repo from namespace annotation
    ↓  open ONE idempotent @fuze issue in the owning repo
    ↓
owning repo's @fuze handler → diagnose + fix PR (vendor-independent)
    +  Email → izzy.weinberg@gmail.com (issue opened)
```

## Per-repo routing (which repo gets the issue?)

The shared cluster hosts many consumers, each in its own namespace. A CRIT log
should land in the issue tracker of whoever **owns** the emitting service — not
always FuzeInfra.

The owning repo is declared by an annotation on the namespace:

```
fuzeinfra.io/owner-repo: <owner>/<repo>
```

`grafana-crit-fix.yml` resolves it (it already has cluster access via
`KUBE_CONFIG`) — the CF Worker stays thin and never queries the cluster:

1. Parse `namespace` from the alert labels (`client_payload.labels`, treated as data).
2. `kubectl get ns "$ns" -o jsonpath='{.metadata.annotations.fuzeinfra\.io/owner-repo}'`.
3. Validate it matches `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`.
4. File the issue there: `gh issue create --repo "$owner_repo" …` (with cross-repo dedup).

**Fallback** to `FuzeOne/FuzeInfra` when the namespace is missing/unknown, not
found, unannotated, or the value is invalid.

**One path for every owner — always via `@fuze`:** the workflow opens ONE
idempotent `crit-autofix`-labelled issue in the owning repo whose body
**`@fuze`-mentions**. The fix then runs in that repo's `@fuze` handler:
- **FuzeInfra-owned** crit (`is_self`): the issue lands here and is answered by the
  **cluster-capable** handler (`fuze-cluster.yml`) — it holds the prod kubeconfig +
  kubectl/helm/terraform + destructive-op guard shims and reconciles the chart via a PR.
- **Consumer-owned** crit: the issue lands in the owner repo, answered by its
  unprivileged handler (`fuze.yml`). FuzeInfra never edits a consumer's code/chart.

Dedup is per owning repo (skip if it already has an open `crit-autofix` issue).
A pre-check against `.github/crit-ignore-patterns.json` still short-circuits
known-good log lines before any issue is opened.

> The former in-workflow `claude --print` call (and its `fix`/`issue`/`ignore`
> action branching + the Telegram suppress-approval flow) was removed: it pinned a
> single vendor. The fix capability now lives in the `@fuze` handler instead.

### Onboarding a consumer namespace

Annotate the namespace once so its crit logs route to its own repo:

```bash
kubectl annotate namespace <namespace> \
  fuzeinfra.io/owner-repo=<owner>/<repo> --overwrite
# e.g.
kubectl annotate namespace fuzefront \
  fuzeinfra.io/owner-repo=FuzeOne/FuzeFront --overwrite
```

FuzeInfra's own `fuzeinfra` namespace is annotated automatically by Argo CD
(`managedNamespaceMetadata` in `argocd/applications/fuzeinfra-prod.yaml`).

> **Token scope:** filing into a consumer repo needs the workflow's `GH_TOKEN`
> (a PAT) to have `issues:write` there — the default `GITHUB_TOKEN` is scoped to
> FuzeInfra only. The workflow uses `secrets.GH_TOKEN || secrets.GITHUB_TOKEN`;
> set `GH_TOKEN` to a PAT spanning the consumer repos (the existing automation
> PAT covers `izzywdev/*`).

### Traceability

Every auto-opened issue/PR links the **specific run** (not just the workflow) via
a `**Handling run:**` line + footer, so you can trace which run produced it.

## Why CF Worker as bridge (not direct Loki query)

The GHA workflow runner has existing `KUBE_CONFIG` access to the k3s cluster. Fetching logs via `kubectl port-forward → curl Loki HTTP API` reuses that access without exposing Loki publicly.

Alternative (querying Loki from the CF Worker directly) would require:
- Exposing a Loki HTTP endpoint through the CF tunnel with a separate Access bypass
- Managing Loki credentials in the Worker
- Loki queries that may time out in Worker's 30s CPU budget

The Worker stays thin: validate bearer token, check `status == "firing"`, forward alert metadata to GitHub. Log fetching happens inside GHA; the fix runs in the `@fuze` handler.

## Components

### 1. Grafana Alert Rule (`helm/fuzeinfra/templates/configmaps-monitoring.yaml`)

- ConfigMap `fuzeinfra-grafana-alerting` with three provisioning files:
  - `contact-points.yaml` — webhook contact point `fuze-autofix` → `https://crit-alert.prod.fuzefront.com/`
  - `policy.yaml` — routes `severity=critical` to `fuze-autofix`, repeat every 4h
  - `rules.yaml` — LogQL: `sum(count_over_time({namespace=~".+"} |~ "(?i)(CRIT|CRITICAL|FATAL|PANIC)" [5m])) > 0`
- Alert evaluates every 5 minutes; fires immediately on first hit (`for: 0s`)
- `noDataState: OK` — quiet cluster never triggers

### 2. CF Worker Bridge (`terraform/contabo/crit-alert-bridge.js`)

- Cloudflare Worker at `crit-alert.prod.fuzefront.com`
- Validates `Authorization: Bearer $BRIDGE_TOKEN`
- Ignores resolved alerts (`status != "firing"`)
- Calls `POST /repos/FuzeOne/FuzeInfra/dispatches`. Event type depends on `?source=`:
  default → `grafana-crit-alert` (Grafana log alerts); `?source=alertmanager` →
  `alertmanager-alert` (Prometheus/Alertmanager metric alerts — see below)
- Worker secrets managed in Terraform state via `cloudflare_worker_script` bindings

### 3. GHA Workflow (`.github/workflows/grafana-crit-fix.yml`)

Triggered by `repository_dispatch` event type `grafana-crit-alert`.

Steps:
1. `kubectl` configured from `KUBE_CONFIG` secret (base64-decoded)
2. `kubectl port-forward svc/fuzeinfra-loki 3100:3100` → `curl` Loki query API for last 30 min (context for the issue)
3. Resolve the owning repo from the namespace annotation (see [Per-repo routing](#per-repo-routing-which-repo-gets-the-issue))
4. Pre-check the log lines against `.github/crit-ignore-patterns.json`; skip silently if all known-good
5. Open ONE idempotent `@fuze` issue in the owning repo with the alert + log excerpt (as data)
6. Email via Gmail SMTP (`dawidd6/action-send-mail`) that the `@fuze` issue was opened

The fix itself runs in the owning repo's `@fuze` handler — this workflow no longer
invokes any LLM directly.

### 4. Terraform Resources (`terraform/contabo/cloudflare.tf`)

- `cloudflare_worker_script.crit_alert_bridge` — deploys the Worker with secret bindings
- `cloudflare_worker_route.crit_alert_bridge` — routes `crit-alert.prod.fuzefront.com/*` to the Worker
- `cloudflare_zero_trust_access_application.crit_alert_bridge` — CF Access app for the endpoint
- `cloudflare_zero_trust_access_policy.crit_alert_bridge_bypass` — bypass policy so Grafana can POST without OTP
- `null_resource.crit_bridge_token_secret` — kubectl-patches `CRIT_BRIDGE_TOKEN` into `fuzeinfra-secrets`

All resources are conditional on `var.crit_bridge_token != ""`.

## Setup

### GitHub Secrets Required

| Secret | Value |
|--------|-------|
| `KUBE_CONFIG` | base64-encoded k3s kubeconfig (already set by terraform apply) |
| `GH_TOKEN` | PAT authored by a repo OWNER/MEMBER/COLLABORATOR so the opened issue passes the `@fuze` handler's author gate; also needed to file into consumer repos |
| `GMAIL_USERNAME` | `izzy.weinberg@gmail.com` |
| `GMAIL_APP_PASSWORD` | Gmail App Password (myaccount.google.com/apppasswords) |

> No LLM provider key is needed here anymore — the fix runs in the `@fuze` handler,
> which resolves its own credential via `fuze-code-action`/`llm-endpoint`.

### Terraform Variables Required (`terraform/contabo/terraform.tfvars`)

```hcl
# Generate with: openssl rand -hex 32
crit_bridge_token = "your-random-secret-here"
```

### Deploy

```bash
cd terraform/contabo

# Apply only the CRIT alert Worker resources (safe — no VPS or tunnel changes)
terraform apply \
  -target=cloudflare_worker_script.crit_alert_bridge \
  -target=cloudflare_worker_route.crit_alert_bridge \
  -target=cloudflare_zero_trust_access_application.crit_alert_bridge \
  -target=cloudflare_zero_trust_access_policy.crit_alert_bridge_bypass \
  -target=null_resource.crit_bridge_token_secret
```

Then merge the Helm changes via Git → ArgoCD auto-syncs Grafana with the alerting provisioning.

## Testing

Trigger a synthetic CRIT log to verify the pipeline end-to-end:

```bash
# Inject a CRIT log line into any pod
kubectl exec -n fuzeinfra deploy/fuzeinfra-grafana -c grafana -- \
  sh -c 'echo "CRITICAL: synthetic test alert $(date)" >&2'

# Wait up to 5 min for Grafana alert to fire
# Check GHA: Actions → Grafana CRIT Log Auto-Fix
```

Or POST directly to the Worker to test bypass + GHA dispatch:

```bash
curl -X POST https://crit-alert.prod.fuzefront.com/ \
  -H "Authorization: Bearer $BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"firing","alerts":[{"annotations":{"summary":"Test CRIT alert"},"startsAt":"2026-06-21T00:00:00Z","labels":{"severity":"critical"}}]}'
```

## Sibling pipeline: Alertmanager metric alerts → `@fuze`

The same `@fuze` routing is wired for **Prometheus/Alertmanager metric alerts**
(Longhorn/Pod/Node/PVC — `helm/fuzeinfra/rules/*.yml`), which previously died in a
no-op Alertmanager receiver. This is **additive** (the previous `default` receiver
and inhibition rules are unchanged) and GitOps-native:

```
Prometheus rules → Alertmanager (receiver github-fuze, severity=critical, continue:true)
    ↓  webhook_configs → https://crit-alert.prod.fuzefront.com/?source=alertmanager
CF Worker crit-alert-bridge (bearer via mounted secret file)
    ↓  repository_dispatch: alertmanager-alert
GitHub Actions: alertmanager-fuze.yml
    ↓  resolve owning repo from the alert's namespace annotation
    ↓  open ONE idempotent @fuze issue in that repo (per-product routing)
```

- **Enable/config:** `alertmanager.githubBridge.enabled` mounts the bridge token
  (`fuzeinfra-tunnel-secrets` key `CRIT_BRIDGE_TOKEN`) as a file at
  `/etc/alertmanager/secrets/crit_bridge_token` — Alertmanager does not env-expand
  its config, so a `credentials_file` is used. Off by default; **on** in
  `values-contabo.yaml`, which also wires the `github-fuze` receiver + route.
- **Auth reuses** the existing `CRIT_BRIDGE_TOKEN` (same one Grafana uses) — no new
  secret to mint.
- **Owner resolution** is identical to the log path (namespace `fuzeinfra.io/owner-repo`
  annotation → owner repo; infra alerts with no namespace fall back to FuzeInfra).
- Dedup is per `(owner repo, alertname)` via the `alertmanager-autofix` label.

## Self-monitoring rules (Grafana-managed, independent of Prometheus)

Prometheus rules cannot fire when Prometheus is the casualty, so two rules live in Grafana
(`helm/fuzeinfra/templates/configmaps-monitoring.yaml`, `rules.yaml`) and use the same
`severity=critical` → `fuze-autofix` route as above:

- **Prometheus Unreachable** (`prometheusunreachable`, 1m eval, `for: 10m`): queries
  `up{job="prometheus"}` on the `fuzeinfra-prometheus` datasource; `noDataState` and
  `execErrState` are both `Alerting`, so a down or unreachable Prometheus fires it.
- **Storage Mount Failure** (`storagemountfailure`, Loki): `longhorn-system` log lines matching
  `fsck.*(found errors|could not correct)|MountVolume.*failed|FailedAttachVolume`.
  The exact prod log text is unverified; check with `cluster-query.yml`, e.g.
  `-n longhorn-system logs -l app=longhorn-csi-plugin --tail=200 -c longhorn-csi-plugin`.
  Promtail only tails `/var/log/pods` (the journal job has no host mount), so kubelet-only
  messages and Events never reach Loki; only pod logs do.

Dedup: `grafana-crit-fix.yml` treats `CRIT Log Detected` as the catch-all (one open
`crit-autofix` issue per repo). Any other alertname is deduplicated per `(namespace, alertname)`
via the `grafana-autofix` label and a body marker, and its concurrency group is keyed on the
alertname, so one open generic issue (or a queued generic run) can no longer suppress or evict
an unrelated critical alert. See also `docs/design/alert-to-agent-session.md`.
