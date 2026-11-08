# Infra-Request Dispatch (consumer → FuzeInfra)

Consumer repos (FuzeFront and future ones) **declare** their infrastructure
needs in git and **dispatch** to FuzeInfra to reconcile them. **FuzeInfra is the
sole credential holder** — consumers never see Contabo creds, the k3s node-token,
or the Terraform state backend.

The handler is **Terraform-only**: it reconciles node-provisioning requests and
nothing else. ArgoCD registration is a separate one-time `workflow_dispatch`
(`argocd-register.yml`); after it, ArgoCD self-syncs the consumer's argo
manifests and they never touch this handler.

```
┌────────────────────┐   repository_dispatch        ┌──────────────────────────┐
│ Consumer repo      │   (event_type:               │ FuzeInfra                │
│ (e.g. FuzeFront)   │    infra-request)            │                          │
│                    │ ───────────────────────────► │ infra-request-handler.yml│
│ deploy/terraform/  │   client_payload =           │  • no requests → skip    │
│   node-request.tf  │   { repo, ref, requests[] }  │  • whitelisted → apply   │
│   requests.json    │   (TF changes only)          │  • else → gated PR (plan)│
│ .github/workflows/ │                              │                          │
│   infra-dispatch.yml                              │ creds: FuzeInfra CI only │
└────────────────────┘                              └──────────────────────────┘

  deploy/argocd/  ──(one-time)──► argocd-register.yml ──► ArgoCD self-syncs after
                                  (workflow_dispatch)      registration; no handler
```

## Components in this repo

| Path | Role |
|------|------|
| [`modules/contabo-k3s-node`](../modules/contabo-k3s-node/) | TF module: VPS + cloud-init k3s agent join + labels (consumers reference this) |
| `.github/workflows/infra-request-handler.yml` | Terraform-only handler (skip / apply / gated-PR) |
| `.github/workflows/argocd-register.yml` | One-time ArgoCD registration of a repo (`workflow_dispatch`) |
| [`config/infra-request-whitelist.json`](../config/infra-request-whitelist.json) | Auto-apply rules |
| [`scripts-tools/validate_infra_request.py`](../scripts-tools/validate_infra_request.py) | Whitelist validator (returns skip / apply / gate) used by the handler |

## The auto-apply whitelist

A dispatched request is **auto-applied** only when it passes **every** rule in
[`config/infra-request-whitelist.json`](../config/infra-request-whitelist.json):

- `repo` ∈ `allowed_repos`
- each node's `product_id` ∈ `allowed_product_ids`
- each node's `region` ∈ `allowed_regions` (EU only by default)
- each node's `role` ∈ `allowed_roles` (`workload` only)
- node count ≤ `max_nodes_per_request`

Any violation → the handler opens a **gated PR carrying `terraform plan`**.
Merging the PR is the manual approval; nothing applies automatically.

Edit the JSON to widen/narrow what auto-applies — no workflow change needed.

## Scoped dispatch token — `FUZEINFRA_DISPATCH_TOKEN`

The consumer needs a credential that can do **exactly one thing**: trigger
`repository_dispatch` on FuzeInfra. It must **not** be able to read code, push,
or touch secrets. Two options:

### Option A — Fine-grained PAT (simplest)

1. GitHub → **Settings → Developer settings → Fine-grained personal access tokens → Generate new token**.
2. **Resource owner:** `izzywdev` (owner of FuzeInfra).
3. **Repository access:** *Only select repositories* → **FuzeInfra**.
4. **Permissions:** Repository permissions → **Contents: Read and write**
   *(the minimum that allows `POST /repos/{owner}/{repo}/dispatches`; no other
   scope is needed — leave everything else "No access")*.
5. Set a short expiry and a calendar reminder to rotate.
6. Copy the token.

### Option B — GitHub App (better for orgs / rotation)

1. Create a GitHub App owned by `izzywdev` with **Repository permissions →
   Contents: Read & write** only.
2. Install it on **FuzeInfra only**.
3. In the consumer workflow, mint a short-lived installation token (e.g.
   `actions/create-github-app-token`) and use it as the dispatch token.

### Store it in the consumer repo

Add the token to the **consumer** repo (e.g. FuzeFront) secrets as
**`FUZEINFRA_DISPATCH_TOKEN`**:

```bash
gh secret set FUZEINFRA_DISPATCH_TOKEN --repo FuzeOne/FuzeFront
```

> **Izzy:** create the token (Option A or B) and add it to FuzeFront's repo
> secrets. The handler in FuzeInfra uses FuzeInfra's *own* CI secrets for the
> Contabo/k3s/state credentials — the dispatch token only authorizes the
> *trigger*, never the apply.

### Consumer side — example `infra-dispatch.yml`

The consumer's workflow fires the dispatch (for reference; lives in the consumer repo).

**The handler is Terraform-only.** It reconciles infra (node) provisioning
requests and nothing else, so the dispatch must:

1. Trigger **only** on `deploy/terraform/**` — **never** on `deploy/argocd/**`.
   ArgoCD self-syncs argo manifests after the one-time registration (below), so
   argo-manifest edits are not infra-requests and must not dispatch.
2. Dispatch **only when there is a non-empty `requests` payload** to send. A push
   that changes terraform scaffolding but carries no node requests should send
   nothing (the handler would no-op anyway, but don't dispatch noise).

```yaml
name: infra-dispatch
on:
  push:
    branches: [main]
    paths: ["deploy/terraform/**"]      # NOT deploy/argocd/** — Argo owns those
jobs:
  dispatch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Dispatch infra-request to FuzeInfra
        env:
          GH_TOKEN: ${{ secrets.FUZEINFRA_DISPATCH_TOKEN }}
        run: |
          # Only dispatch if there is a non-empty requests[] payload to send.
          if [ ! -s deploy/terraform/requests.json ] \
             || [ "$(jq '(.requests // []) | length' deploy/terraform/requests.json)" -eq 0 ]; then
            echo "No non-empty 'requests' in deploy/terraform/requests.json — nothing to dispatch."
            exit 0
          fi
          gh api repos/FuzeOne/FuzeInfra/dispatches \
            -f event_type=infra-request \
            -F client_payload[repo]="${{ github.repository }}" \
            -F client_payload[ref]="${{ github.sha }}" \
            --input deploy/terraform/requests.json
```

> `requests.json` must have the shape `{ "requests": [ {name, product_id, region, role, labels}, ... ] }`.
> The consumer's `deploy/terraform/` root module must also **declare** the variables the
> handler injects (`requests`, `contabo_client_id`, `contabo_client_secret`,
> `contabo_api_user`, `contabo_api_password`, `k3s_server_url`, `k3s_node_token`,
> `image_id`, `ssh_public_key`) — otherwise `terraform plan` fails with
> "undeclared variable". Declare them and forward into the `contabo-k3s-node` module.

### One-time ArgoCD registration (separate from infra-requests)

Registering a repo into the cluster's ArgoCD is a **one-time, manual** action,
decoupled from the Terraform handler. Run FuzeInfra's `argocd-register.yml`:

```bash
gh workflow run argocd-register.yml --repo FuzeOne/FuzeInfra \
  -f repo=FuzeOne/FuzeFront -f ref=main -f path=deploy/argocd
```

After this, ArgoCD polls and self-syncs the consumer's `deploy/argocd` manifests.
Ongoing edits to those files are **not** infra-requests and trigger nothing.

## Required FuzeInfra CI secrets (handler side)

| Secret | Purpose |
|--------|---------|
| `CONTABO_CLIENT_ID` / `CONTABO_CLIENT_SECRET` / `CONTABO_API_USER` / `CONTABO_API_PASSWORD` | Contabo API |
| `K3S_SERVER_URL` / `K3S_NODE_TOKEN` | k3s agent join |
| `CONTABO_IMAGE_ID` | OS image for new nodes |
| `NODE_SSH_PUBLIC_KEY` | Break-glass SSH key injected via cloud-init |
| `KUBE_CONFIG` | base64 kubeconfig — node labeling (handler) + one-time Argo registration (`argocd-register.yml`) |
| `TF_STATE_BUCKET` / `TF_STATE_REGION` / `TF_STATE_ACCESS_KEY_ID` / `TF_STATE_SECRET_ACCESS_KEY` | FuzeInfra-owned remote TF state backend |

## Coexistence with cluster autoscaling

Nodes you dispatch through this workflow are **baseline** nodes as far as the cluster
autoscaler is concerned — and the autoscaler will **never scale them down**. This is
by construction, not configuration:

- The autoscaler manages **only** the nodes it creates itself, which carry the Contabo
  tag **`fuzeinfra-elastic`**. Anything **without** that tag — the control-plane and
  every node dispatched here — is **foreign** to it: counted for scheduling capacity,
  but never a scale-down (or scale-up) candidate.
- Dispatched nodes are **never** tagged `fuzeinfra-elastic` (the `contabo-k3s-node`
  module never sets that tag), so a dispatched node cannot be mistaken for an elastic one.
- The autoscaler's "floor" is therefore **implicit and floating**: it is whatever nodes
  exist right now (control-plane + everything dispatched), and it grows automatically
  when you dispatch more. FuzeInfra does **not** track or predict that count — preserving
  the platform's decoupling from consumer needs.

When dispatching: you do **not** need to coordinate node counts with the autoscaler
(dispatch what you need; the elastic pool floats above it), and you must **not** manually
apply the `fuzeinfra-elastic` tag to a dispatched node — that would hand it to the
autoscaler, which could then drain and delete it. See
[ADR 0001 — identity-scoped floating baseline](adr/0001-cluster-autoscaling-identity-scoped-baseline.md)
for the full rationale.

## Related docs

- [`consuming-repos/CLUSTER_QUERY.md`](consuming-repos/CLUSTER_QUERY.md) — the **read** side of this bridge: read-only `kubectl` against prod from a consumer repo. It accepts `repository_dispatch` (`event_type: cluster-query`), so **the same `FUZEINFRA_DISPATCH_TOKEN` works** — nothing extra to mint. A token that can trigger `infra-request` can already apply Terraform and create nodes, so a guarded read is strictly less powerful.
- [`TERRAFORM_CD.md`](TERRAFORM_CD.md) — the full merge-to-apply model (FuzeInfra-owned plane A + this dispatch bridge plane B), backend bootstrap, and the FuzeOne consumer-side workflows.
- [ADR 0001](adr/0001-cluster-autoscaling-identity-scoped-baseline.md) — why cluster autoscaling uses an identity-scoped floating baseline (decoupling from consumer-dispatched nodes).
- [`K3S_SECOND_NODE_RUNBOOK.md`](K3S_SECOND_NODE_RUNBOOK.md) — manual node-join procedure this automates.
- [`gitops.md`](gitops.md) — Argo CD delivery model.
- [`CONTRACT.md`](../CONTRACT.md) — FuzeInfra's stable service interface.
