# ArgoCD self-heal → GitHub autofix loop

Part of the **ADLC** (agentic development life cycle): when a deployed app on the
shared prod cluster goes unhealthy, the platform notices and drives a fix through
GitOps **without a human polling `kubectl`**.

## The loop

```
ArgoCD app unhealthy
  │  (argocd-notifications-cm trigger)
  ▼
repository_dispatch  event_type=argo-out-of-sync  ──►  FuzeOne/FuzeInfra
  │
  ▼
.github/workflows/argo-outofsync-autofix.yml
  ├─ Telegram 🔴 alert (link to the Argo app)
  ├─ app sourced from FuzeInfra  ─► devops-engineer Claude run → fix PR (auto-merge → Argo re-syncs)
  └─ app sourced from a CONSUMER ─► idempotent @fuze issue in the OWNING repo
```

Config lives in `argocd/notifications/argocd-notifications-cm.yaml` (triggers +
dispatch payload) and `.github/workflows/argo-outofsync-autofix.yml` (the reaction).

## Failure-mode coverage (the important part)

An Argo app has **two** independent status axes, and a naïve setup that only
watches one silently misses the others:

| Axis | Value | Meaning | Typical cause |
|---|---|---|---|
| `sync.status` | `OutOfSync` | live drifted from a **renderable** desired state | manual change, chart edit not yet applied |
| `sync.status` | `Unknown` | **ComparisonError** — manifests won't render at all | malformed values/kustomize; Helm template error |
| `health.status` | `Degraded` | rendered + applied, workload unhealthy | ImagePullBackOff, CrashLoop, failed hook, unready pods |

The loop triggers on **all three** (`on-out-of-sync`, `on-sync-unknown`,
`on-health-degraded`), each `oncePer` state transition so a stuck app doesn't spam.

> **Why this matters — the miss that motivated it.** For an early version that only
> fired on `OutOfSync`, a consumer app (`mendys-platform`) sat broken for ~2h and
> nothing alerted. The cause was a malformed `values-contabo.yaml` (a stray `t>`
> on line 1 made Helm parse the whole file as a string), so the app went straight
> to `Unknown`/ComparisonError — it **never reached `OutOfSync`**, and Argo never
> generated a ReplicaSet. Separately, its pods were `Degraded` (ImagePullBackOff)
> with sync never OutOfSync. Both are now covered.

## Multi-repo routing (scale to N consumer repos)

FuzeInfra runs the Argo instance, but it **does not edit a consumer's chart** —
that's the cross-repo delegation boundary. So the reaction routes by the app's
`spec.source.repoURL` (carried in the dispatch payload):

- **App sourced from `FuzeOne/FuzeInfra`** → in-repo `devops-engineer` Claude
  run edits `helm/fuzeinfra/**` and opens a fix PR.
- **App sourced from a consumer repo** (e.g. `izzywdev/MendysRobotics`) → an
  **idempotent `@fuze` issue** is opened in that repo (label `argo-autofix`,
  one per app) carrying the app name, sync/health, and the real `status.conditions`
  error, so the consumer's own agents fix it from their own manifests.

The dispatch payload therefore includes `app`, `sync`, `health`, `repo`, and
`conditions` (the actual error, `toJson`-encoded so quotes/newlines can't corrupt
the JSON body). The app name is validated to a strict k8s-name charset before it
reaches any agent holding cluster creds.

## Operational requirements

- **argocd-notifications controller** running in the `argocd` namespace.
- **Secret `argocd-notifications-secret`** with `github-token` (repo scope, to POST
  `/dispatches`).
- **GitHub Actions secrets:** `ANTHROPIC_API_KEY`, `KUBE_CONFIG`, `TELEGRAM_*`
  (optional alert), and `GH_TOKEN` — a PAT with access to consumer repos so the
  cross-repo `@fuze` issue can be opened. Without `GH_TOKEN`, consumer-app
  failures still alert but the issue is skipped (logged as a warning).

## Related self-heal arms

- `docs/crit-log-autofix.md` — critical-log → autofix.
- Nightly govern-loop (FuzeSDLC baseline §7) — nightly integration + reconciliation.
