# Consumer CF Host Declaration

Consuming repos declare their public `*.prod.fuzefront.com` hostnames in their
own codebase. FuzeInfra validates, materializes, and applies via the saved-plan
Terraform gate. No human hand-edits `materialized/consumers.tfvars`, and the
bare `*.tf` setup stays consumer-free.

> **Own-zone products** (e.g. mendysrobotics.com) use the `modules/cloudflare-dns`
> Terraform module directly — this declaration flow is for **shared-zone**
> `*.prod.fuzefront.com` hosts only.

---

## 1. Consumer-side contract

Create `deploy/cf/hosts.yaml` in your repo:

```yaml
app: myapp           # must match your k8s namespace
hosts:
  - label: myapp     # → myapp.prod.fuzefront.com
    access: bypass   # your app handles its own auth
  - label: api.myapp # → api.myapp.prod.fuzefront.com
    access: bypass
```

### Field reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `app` | string | yes | Your k8s namespace name (lowercase alphanumeric + hyphens) |
| `hosts[].label` | string | yes | DNS label relative to `prod.fuzefront.com`; dots allowed for nesting (e.g. `api.myapp`) |
| `hosts[].access` | `"bypass"` | yes | Only `bypass` is accepted; admin UIs must be declared manually in `cloudflare.tf` |

---

## 2. Triggering the dispatch

Add this job to your repo's CI (fires on changes to `deploy/cf/hosts.yaml`):

```yaml
# .github/workflows/declare-cf-hosts.yml
name: Declare CF hosts

on:
  push:
    branches: [main]
    paths: [deploy/cf/hosts.yaml]
  workflow_dispatch:

jobs:
  dispatch:
    runs-on: ubuntu-latest
    steps:
      - name: Dispatch to FuzeInfra
        run: |
          curl -X POST https://api.github.com/repos/FuzeOne/FuzeInfra/dispatches \
            -H "Authorization: Bearer ${{ secrets.FUZEINFRA_DISPATCH_TOKEN }}" \
            -H "Content-Type: application/json" \
            -d '{
              "event_type": "cf-hosts-declare",
              "client_payload": {
                "repo": "${{ github.repository }}",
                "ref":  "${{ github.sha }}"
              }
            }'
```

The `FUZEINFRA_DISPATCH_TOKEN` is the existing `FUZEINFRA_DISPATCH_TOKEN` secret
(same one used for `cluster-query`). No new token is needed.

---

## 3. What FuzeInfra does on receipt

```
repository_dispatch (cf-hosts-declare)
  │
  ▼
cf-hosts-materialize.yml
  │
  ├─ Validate payload (repo field + format)
  │
  ├─ Fetch deploy/cf/hosts.yaml at the declared ref
  │
  ├─ Policy gate (scripts-tools/materialize_cf_hosts.py)
  │   ├─ Repo must be under izzywdev/ org
  │   ├─ Labels: valid DNS, no wildcards, no reserved labels
  │   ├─ Access: bypass only (admin UIs → cloudflare.tf manually)
  │   └─ No collision with labels owned by a different repo
  │
  ├─ [REJECTED] → file a policy-violation issue with details
  │
  ├─ [NO_CHANGE] → silent no-op (registry already matches)
  │
  └─ [CHANGED] → regenerate consumers.tfvars
                 → open / force-update PR on `materialize/cf-hosts-<app>`
                 → terraform-plan-apply.yml runs the plan gate
                 → human reviews plan + merges
                 → Terraform applies: CNAME record + bypass Access app created
```

---

## 4. Policy rules

| Rule | Details |
|------|---------|
| Org allowlist | Repo must be under `izzywdev/` |
| DNS labels | Lowercase alphanumeric, hyphens (not at start/end), dots for nesting; no wildcards (`*`) |
| Reserved labels | `app`, `auth`, `plan`, `fuzehub`, `argocd` (and all FuzeInfra admin UIs) are blocked |
| Collision guard | A label already claimed by repo A cannot be claimed by repo B |
| Access mode | Only `bypass` — admin UIs with email-OTP Access must be declared in `cloudflare.tf` |

---

## 5. What each entry provisions

For every `{label}` in the registry FuzeInfra Terraform creates:

1. **CNAME record** — `{label}.prod.fuzefront.com` → cloudflared tunnel (proxied)
2. **Cloudflare Access app** — `{label}.prod.fuzefront.com`, `session_duration: 0s`, not shown in App Launcher
3. **Bypass policy** — `everyone` bypass (your app controls auth, CF doesn't gate it)

Your service still needs a Traefik `Ingress` in your own chart:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp
  namespace: myapp
  annotations:
    kubernetes.io/ingress.class: traefik
spec:
  rules:
    - host: myapp.prod.fuzefront.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: myapp
                port:
                  number: 80
```

---

## 6. Removing a host

Delete the entry from `deploy/cf/hosts.yaml` (or remove the whole file) and
re-trigger the dispatch. The materializer rewrites the registry; the resulting
plan will show `destroy` for the CNAME and Access app. Merge the PR to apply.

---

## 7. Idempotency

Re-declaring the same hosts produces a `NO_CHANGE` result — no PR is opened and
no Terraform plan runs. Force-pushing the same `hosts.yaml` is safe.

---

## 8. Materialized file location

`terraform/contabo/materialized/consumers.tfvars` — generated, do not hand-edit.
Loaded in CI via `-var-file=materialized/consumers.tfvars` in `terraform-plan-apply.yml`.
