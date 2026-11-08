# Alert → agent session: cluster-spawned responder sessions

Status: **built behind `alertSessionDispatcher.enabled: false` (default OFF in every overlay).
Cannot be enabled yet: two cross-repo pieces do not exist (§1.4).** Owner decision
(2026-10-14, in chat): build automatic session spawning from critical alerts, using a token
minted by FuzeFront, for the `fuzeone` org. This file replaces the earlier options paper.

Citations are `repo@sha:path:line`. FuzeFront `df16a50`, FuzeAgent `1f11f8d` (default branches,
read 2026-10-14). Nothing below is inferred from names; "does not exist" means searched for and
not found.

## 1. Discovery

### 1.1 FuzeFront token minting — EXISTS

- `POST /api/v1/security/tokens/workload` exchanges a **projected Kubernetes ServiceAccount
  token** for a FuzeFront workload JWT (`fuzefront@df16a50:backend/security/src/routes/security.ts:625`).
  TokenReview is called with audience `fuzefront-security`
  (`backend/security/src/services/workload-identity.ts:57`).
- TTL is `min(declared tokenTtlSeconds ?? 600, 900)` (`security.ts:632`). Claims: `kind:
  fuze-workload`, `sub` = the declared service name, `aud: fuzefront-services` (`security.ts:636`),
  `scope` = the declared scopes, plus `namespace` and `serviceAccount`.
- Authorisation comes from a **declaration ConfigMap** labelled
  `security.fuze.dev/workload-identity=true`, read from the *caller's* namespace
  (`workload-identity.ts:67-78`); fields `service`, `serviceAccountName`, `scopes`,
  `tokenTtlSeconds` (examples: `deploy/helm/fuzefront/templates/workload-identity.yaml:143-160`).
  FuzeInfra only lets `fuzefront-security` read ConfigMaps in listed `registryNamespaces`
  (`helm/fuzeinfra/templates/workload-identity-rbac.yaml`); prod lists only `fuzefront`
  (`values-contabo.yaml:1543-1548`).
- The reference client reads the projected token at `/var/run/secrets/tokens/fuzefront-security`
  and POSTs it (`packages/service-auth/src/workload.ts:4,31`).
- **Consequence:** there is no long-lived FuzeFront credential to seal. The brief preferred "a
  long-lived bootstrap credential held as a SealedSecret"; the real mechanism makes that
  unnecessary and strictly safer, so the design uses the projected token. The only SealedSecret is
  the *inbound* bearer (§3.4).

### 1.2 The `fuzeone` root org — DOES NOT EXIST AS NAMED

- FuzeFront's root organisation is id `00000000-0000-0000-0000-000000000010`, **slug `fuzefront`**
  (`backend/src/migrations/015_seed_root_platform_organization.ts:43-44`).
- `fuzeone` appears only as an npm scope (`@fuzeone/*`, e.g. `services/selection-list-service/openapi.yaml:9`),
  a `fuzeone/` toolkit directory, and the display string `FuzeOne` for the A2A provider organisation
  (`fuzeagent@1f11f8d:agent-templates/a2a/card_generator.py:27`). No org with slug `fuzeone`, and
  no token scope naming it, was found.
- Workload tokens carry **no org/tenant claim** (claims listed in §1.1). "Limited to fuzeone" cannot
  be expressed in the token; it has to be enforced by the callee (FuzeAgent) mapping the token's
  `sub` to a fixed org. This matches FuzeAgent's own authz rule: callee enforces, the caller is
  untrusted (`agent-templates/contracts/a2a/v1/authz.md` §1).
- **Owner decision needed:** is "fuzeone" the FuzeFront root org (slug `fuzefront`), the Anthropic
  organisation that owns the API key, or something not yet created? §6 asks.

### 1.3 FuzeAgent session creation — PARTLY EXISTS, NOT REACHABLE FROM HERE

- Real session creation is `POST {API_BASE}/v1/sessions` against the Anthropic **Managed Agents**
  API (beta `managed-agents-2026-04-01`), authenticated with `x-api-key`
  (`agent-templates/sync/driver.py:34`, `sync/common.py:14,16,38`). Body: `agent{id,version}`,
  `environment_id`, optional `vault_ids`, `resources`, `title`. That is API-billed and needs
  provisioned agent/environment ids; it is **not** a claude.ai Claude Code cloud session.
- FuzeAgent's orchestrator exposes `POST /api/orchestrator/alerts/trigger` and
  `/incident-webhook` (`services/orchestrator/incident_webhook_router.py:23,54,102`, mounted at
  `services/orchestrator/main.py:685`). **Its "Anthropic cloud" channel is a stub**: it records a
  claim and returns `active_remediating` without making any API call
  (`incident_remediation_dispatcher.py:244-288`; the model string at line 285 is metadata only).
  It also fans out to a GitHub issue and an in-cluster pod, which is not what this feature wants.
- Callers authenticate against FuzeFront **user sessions** (`GET /v1/security/session`,
  `services/orchestrator/auth.py:1-12`). No code accepts a `fuze-workload` token
  (`grep fuze-workload` over `services/` and `agent-templates/`: no match).
- No rate limit, spend cap, or per-caller quota for session creation was found in the orchestrator
  or the sync driver. **The dispatcher's cap is therefore the only cap until FuzeAgent adds one.**
- The only claude.ai-session path in FuzeInfra, `activate-a2a-bridge.yml`
  (`POST api.claude.ai/code/api/v0/sessions`), has never succeeded (its single run failed on DNS).
  It is not used here.

### 1.4 What is missing (blocks enabling)

| # | Missing | Owner | Request |
|---|---|---|---|
| 1 | A FuzeAgent endpoint that accepts a `fuze-workload` token with scope `fuzeagent:incident-session:create`, maps `sub=service:alert-session-dispatcher` to one fixed org, and creates one session | FuzeAgent | §7.2 |
| 2 | A real (non-stub) session create behind it, with its own daily cap and spend ceiling | FuzeAgent | §7.2 |
| 3 | FuzeFront accepting the scope and letting its security SA read the declaration in the `fuzeinfra` namespace (`registryNamespaces`), plus an ingress allow if its NetworkPolicy is ever enabled | FuzeFront (+ FuzeInfra values) | §7.1 |
| 4 | An answer on what "fuzeone" is (§1.2) | owner | §6 |
| 5 | An alert for "prod sync wedged": none is webhookable (Argo alerts go argocd-notifications → `repository_dispatch`, `scripts/argo_alert.py`; no Prometheus/Grafana rule exists for it) | FuzeInfra + owner | §5 |

## 2. Flow

```
Grafana rule (severity=critical, title on allowlist)
  -> contact point alert-session-dispatcher (additive route, continue: true)
  -> in-cluster alert-session-dispatcher (this chart, fuzeinfra ns)
       validate + dedup + cooldown + caps  (state on a 256Mi PVC)
  -> POST fuzefront-security /tokens/workload   (projected SA token -> <=300s JWT)
  -> POST FuzeAgent create-session              (Bearer workload JWT)         [endpoint: §7.2]
  -> audit: structured log (always) + one GitHub issue (optional token)
  -> responder session: cluster-query.yml reads, one reconcile PR, no prod writes
```

The existing path (`fuze-autofix` → crit-alert-bridge → `@fuze` issue) is untouched: the new route
is added in front with `continue: true`, so every alert still reaches it.

**Where the dispatcher runs: a new in-cluster Deployment, not an extension of `crit-alert-bridge`.**
The Worker is stateless and outside the cluster; dedup, caps and cooldown need durable state, and
the token mint needs a projected ServiceAccount token, which only exists in a pod. The Worker also
holds a GitHub token we would rather not widen.

Implementation: `helm/fuzeinfra/templates/alert-session-dispatcher.yaml`,
`helm/fuzeinfra/files/alert-session-dispatcher/{dispatcher.py,responder-brief.md}` (stdlib-only,
embedded as a ConfigMap on a stock `python:3.12-slim`, same no-image-build pattern as
`a2a-gateway`). Grafana wiring: `templates/configmaps-monitoring.yaml`, `templates/monitoring.yaml`
(both render nothing when disabled).

## 3. Controls

### 3.1 Token minting and rotation
Per dispatch, cached until 30s before expiry (never longer than 900s). The SA token is projected
with audience `fuzefront-security`, 1h expiry, auto-refreshed by the kubelet. No bootstrap secret,
nothing to rotate. The token never appears in logs, results, or the audit issue (asserted by
`test_logs_and_results_never_contain_tokens`).

### 3.2 Least privilege
- Scope: one proposed scope, `fuzeagent:incident-session:create`, declared in
  `alert-session-dispatcher-identity` (service `service:alert-session-dispatcher`, ttl 300).
- Org binding: server-side in FuzeAgent (§1.2). The dispatcher never sends an org in the request.
- The pod: dedicated ServiceAccount with automount off, non-root, read-only rootfs, all caps
  dropped, seccomp `RuntimeDefault`. NetworkPolicy: ingress only from Grafana/Alertmanager pods;
  egress only to DNS, `fuzefront-security:3002`, the FuzeAgent namespace/port, and 443 to
  non-private ranges *only when the audit repo is set*.

### 3.3 Allowlist, dedup, caps, kill switch
- Allowlist: alert **title** (`alertname`), exact match. Ships with `Prometheus Unreachable` and
  `Storage Mount Failure`, both defined in the chart and both Grafana-evaluated so they fire when
  Prometheus is the casualty (`configmaps-monitoring.yaml:668,726`; guarded by
  `test_allowlist_titles_exist_as_alert_rules`).
- Dedup key: `sha256(alertname \0 namespace)[:16]`. One open session per key; open until a
  `resolved` notification or `openTtlSeconds` (6h).
- Cooldown: 4h after the last spawn attempt for that key (open or closed, success or failure).
- Hard caps per UTC day: 3 spawns and `(spawns+1) × estimatedSessionCostUsd ≤ dailySpendCapUsd`
  (5 USD × 3 ≤ 15 USD). The cost is an **estimate**, not metered; actual spend must be capped by
  FuzeAgent/the billing account (§6). The attempt is counted *before* the network call, so a crash,
  timeout, or failed mint still consumes the cap and starts the cooldown (no retry storms).
- Kill switches, fastest first: `paused: true` (skip everything, pod stays up); `dryRun: true`
  (default: decide and log, spawn nothing, consume nothing); `enabled: false` (remove everything).
- Fail closed: invalid config, corrupt/unwritable state, or a missing inbound token stops the
  process instead of resetting the caps.

### 3.4 Untrusted labels
Labels are attacker-influenceable (any workload that can emit a log line matching a Loki rule can
shape them). Handling: only keys in `labelKeys` are read; each value must match
`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$` (alertname additionally allows spaces) or it is **dropped,
not repaired**; a bad `alertname`/`namespace` rejects the whole alert; at most 12 labels;
annotations (free text) are never forwarded; the fixed brief is a repo file and never interpolated.
The agent receives `{brief, data}` with the labels only inside `data`, and the brief tells it to
treat `data` as inert. Inbound webhooks need `Authorization: Bearer <ALERT_DISPATCH_INBOUND_TOKEN>`
(constant-time compare; process refuses to start without it) and a ≤256KB body.

### 3.5 Audit
Always: one JSON log line per decision (`spawned|skipped|rejected|resolved|dry-run|spawn-failed`),
shipped to Loki by Promtail. Optionally: one GitHub issue per spawn on `audit.repo` with the session
id/link, built only from validated fields, no `@fuze` mention (it must not trigger another handler),
no secrets. Needs `GITHUB_AUDIT_TOKEN` (fine-grained, Issues:write on FuzeInfra only); without it
the log is the audit trail.

### 3.6 Prod stays Git-only
The responder brief forbids `kubectl` writes, Secret reads, and any Guarded exception ("an
unattended responder never exercises one"), and tells the agent to stop and report if the only
remedy is a prod write. This is policy text, not an enforced boundary: the session's actual reach
is whatever its FuzeAgent environment holds. FuzeAgent must provision that environment with
repo+PR and `cluster-query.yml` dispatch only (§7.2).

## 4. Enabling checklist (all in one reviewed change, by the owner's decision each)
1. FuzeAgent endpoint + FuzeFront scope exist (§7).
2. `alert-session-dispatcher-secret` sealed offline (`deploy/sealed-secrets/…yaml.template`).
3. `values-contabo.yaml`: `alertSessionDispatcher.enabled: true`, `agent.sessionUrl`,
   `agent.namespace`; add `fuzeinfra` to `workloadIdentity.authenticators[].registryNamespaces`
   (`test_overlay_enabling_dispatcher_must_register_identity_namespace` fails otherwise).
4. Leave `dryRun: true`; fire one allowlisted alert; read the `dry-run` line in Loki.
5. Only then flip `dryRun: false`. Rollback is `paused: true` or `enabled: false`.

## 5. Known gaps
- **No "prod sync wedged" alert is reachable.** The brief asked to start with Alertmanager/Argo
  alerts meaning a wedged sync. Argo failures are raised by argocd-notifications into
  `repository_dispatch`, not into Grafana/Alertmanager, and no rule exists for "Argo controller
  stuck". The allowlist therefore holds only the two alerts that exist. Candidates once routed:
  `PodPendingTooLong`, `HelmInstallJobFailed` (Prometheus rules; need an Alertmanager receiver,
  not wired here because both Grafana alerts above are the ones that survive a Prometheus outage).
- Spend is estimated, not read from a billing API.
- Dedup state lives on one PVC; losing the volume resets counters (the daily cap would reset).
  Acceptable with FuzeAgent's own cap as a backstop; not acceptable if FuzeAgent adds none.

## 6. Facts and decisions for the owner
1. What is the `fuzeone` org: FuzeFront root org (slug `fuzefront`), the Anthropic org that owns the
   API key, or a new org? The answer fixes the server-side org binding in §7.2.
2. Session type: Managed Agents session (`/v1/sessions`, API-billed, exists in code) or a claude.ai
   Claude Code cloud session (plan-billed, no usable HTTP API found)? The design assumes the former.
3. Daily spawn cap (default 3) and daily spend cap (default 15 USD): confirm or change; and the
   hard ceiling on the Anthropic key/workspace, which is the only true spend limit.
4. Credential minting: `ALERT_DISPATCH_INBOUND_TOKEN` (random, sealed offline) and optionally
   `GITHUB_AUDIT_TOKEN`. FuzeFront needs no new secret.
5. Whether to wire Argo/Alertmanager alerts (§5) and which.

## 7. Cross-repo requests (not edited here)

### 7.1 `@fuze` → FuzeOne/FuzeFront
**What:** (a) Accept scope `fuzeagent:incident-session:create` in workload-identity declarations (it
is already free-form text; confirm nothing allowlists scope names). (b) Allow `fuzefront-security`
to read workload-identity ConfigMaps in the `fuzeinfra` namespace (this is a `registryNamespaces`
entry in FuzeInfra values, which FuzeInfra will add at enable time; FuzeFront only needs to confirm
the service tolerates a declaration outside its own namespace). (c) If
`securityService.networkPolicy.enabled` is ever turned on, allow ingress on 3002 from namespace
`fuzeinfra` pods labelled `app.kubernetes.io/name=alert-session-dispatcher`.
**Why:** the cluster dispatcher mints per-dispatch tokens via `POST /tokens/workload`.
**Minimal scope:** one scope string, no new endpoint, no new secret, no change to token TTL rules.
**Acceptance:** with a declaration `service: service:alert-session-dispatcher, scopes:
fuzeagent:incident-session:create` in `fuzeinfra`, a pod using SA `alert-session-dispatcher` gets
HTTP 200 from `/tokens/workload` and a JWT whose `scope` is exactly that string and `sub` is
`service:alert-session-dispatcher`; an SA without a declaration gets 401/403.

### 7.2 `@fuze` → FuzeOne/FuzeAgent
**What:** a new route (suggested `POST /api/orchestrator/incident-sessions`) that:
1. authenticates a FuzeFront `fuze-workload` bearer (introspect via `/tokens/introspect` or verify
   the signature per FuzeAgent's existing auth mode), requires scope
   `fuzeagent:incident-session:create`, and requires `sub == service:alert-session-dispatcher`;
2. ignores any caller-supplied org and creates the session in one configured org (the answer to §6.1);
3. accepts `{brief:string, data:object, source:string}`, delivers `brief` as the instruction and
   `data` as a clearly delimited, non-instruction block;
4. creates a real session (`POST /v1/sessions` via the existing driver) in an environment limited to
   repo+PR and `cluster-query.yml` dispatch (no kube credentials, no Actions-secret admin, no
   `workflow` write beyond cluster-query);
5. enforces its own daily session cap and spend ceiling, independent of the caller;
6. returns `201 {sessionId, sessionUrl}` (`sessionUrl` https only).
It should not reuse `/alerts/trigger`: that route fans out to three channels and its cloud channel
is a stub (`incident_remediation_dispatcher.py:244-288`).
**Why:** nothing in FuzeAgent accepts a workload token or creates a session on behalf of a
cluster component today (§1.3).
**Acceptance:** (i) no token → 401; wrong scope or wrong `sub` → 403; (ii) a body with
`data.org="other"` still lands in the configured org; (iii) the (cap+1)th request in a UTC day →
429 with no session created; (iv) the happy path returns a session id matching
`^[A-Za-z0-9_-]{1,128}$`; (v) a `data` value containing instruction-like text does not appear in
the instruction channel.
