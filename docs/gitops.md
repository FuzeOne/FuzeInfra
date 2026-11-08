# GitOps with Argo CD

FuzeInfra is delivered to Kubernetes via **Argo CD**: Argo CD watches this repo
and continuously syncs the `helm/fuzeinfra` chart into the cluster. Push to
`main` → Argo CD reconciles the cluster to match. The same model works on a
local `kind` cluster and on EKS — only the values overlay differs.

## Layout

```
argocd/
  projects/fuzeinfra.yaml            # AppProject: allowed repo + destinations
  applications/fuzeinfra-local.yaml  # App -> helm/fuzeinfra (values-local.yaml)
  applications/fuzeinfra-aws.yaml    # App -> helm/fuzeinfra (values-aws.yaml)
  install/setup-argocd.sh            # install Argo CD + bootstrap an App
```

Argo CD itself is **bootstrapped separately** from the app it manages (it must
not be part of the chart it deploys). Once installed, the `Application` is the
only thing you apply by hand; everything else flows through git.

## Quick start (local / kind)

```bash
# 0) bring up the local cluster (ingress etc.)
make kind-up                      # or ./k8s/kind/setup-kind.sh

# 1) install Argo CD and bootstrap the local Application
make argocd-install ENV=local     # or ./argocd/install/setup-argocd.sh local

# 2) get the admin password and open the UI
make argocd-password
make argocd-ui                    # port-forward https://localhost:8080 (user: admin)
```

Argo CD will sync `helm/fuzeinfra` (values-local.yaml) into the `fuzeinfra`
namespace and self-heal it. Check status:

```bash
kubectl -n argocd get applications
kubectl -n fuzeinfra get pods
```

> If you used `make kind-up`, the chart is already installed via Helm. Let Argo
> CD adopt it (it will reconcile the same resources), or skip the Helm install
> and let Argo CD own it from the start — pick one owner to avoid drift.

## AWS (EKS)

```bash
aws eks update-kubeconfig --region us-east-1 --name fuzeinfra
kubectl apply -f k8s/aws/gp3-storageclass.yaml      # default storage
# install ingress-nginx (see docs/kubernetes-migration.md)
make argocd-install ENV=aws                          # bootstraps fuzeinfra-aws
```

## Private repository

If `FuzeOne/FuzeInfra` is private, Argo CD needs read credentials:

```bash
argocd repo add https://github.com/FuzeOne/FuzeInfra.git \
  --username <user> --password <personal-access-token>
```

or create a `Secret` in `argocd` labeled
`argocd.argoproj.io/secret-type=repository`.

## How Helm hooks behave

Argo CD renders the chart with `helm template` and converts Helm hooks to Argo
resource hooks. The chart's `airflow-init` Job (`helm.sh/hook: post-install,
post-upgrade`) runs as an Argo **PostSync** hook — DB migration/bootstrap still
happens automatically on each sync.

## Sync policy

Both Applications use `automated` sync with `prune` + `selfHeal`, plus
`CreateNamespace=true` and `ServerSideApply=true`. For a more conservative
production posture on EKS, drop the `automated` block in
`applications/fuzeinfra-aws.yaml` and sync deliberately (`argocd app sync
fuzeinfra-aws`).

---

# Branch protection for `main`

To make CI a real gate (and to let GitHub **auto-merge** queue PRs until checks
pass), protect `main` and require the CI check. `test-infrastructure` runs on
every PR to `main`, so it's the right required check. (Don't require
`Lint & schema-validate chart` as mandatory — it only runs on `helm/**` changes,
so requiring it would block PRs that don't touch the chart, since the check would
never report.)

## Option A — GitHub UI

**Settings → Branches → Add branch ruleset (or Add rule)** for `main`:

- **Require a pull request before merging**: on
  - Required approvals: `0` (or `1` if you want review gating)
- **Require status checks to pass before merging**: on
  - **Require branches to be up to date before merging**: on
  - Search and add the check: **`test-infrastructure`**
- **Do not allow bypassing the above settings**: optional
- Leave force-pushes and deletions disabled.

Then **Settings → General → Pull Requests → Allow auto-merge** (already enabled).

## Option B — `gh` CLI (exact config)

```bash
gh api -X PUT repos/FuzeOne/FuzeInfra/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["test-infrastructure"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
```

Notes:
- `strict: true` = PRs must be up to date with `main` before merging.
- `required_approving_review_count: 0` keeps a solo workflow unblocked; set to
  `1` to require a review.
- To also gate on the Helm chart, change `.github/workflows/helm-validate.yml` to
  run on all PRs (remove the `paths:` filter) and add
  `"Lint & schema-validate chart"` to `contexts`.

With this in place, enabling auto-merge on a PR makes GitHub merge it
automatically once `test-infrastructure` is green.
