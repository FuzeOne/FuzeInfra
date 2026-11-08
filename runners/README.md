> **ACCOUNT REALITY (2026-07-24): `izzywdev` is a GitHub _User_ account, NOT an
> organization.** So the "org-level runner group" design below does **not** apply
> as written — there is no `github.com/organizations/izzywdev`. Until the repos
> move to the new **`fuzeone` org**, self-hosted runners register at **repo level**
> (the ARC scale sets are per-repo; the `kind-host` runner registered against
> `FuzeOne/FuzeInfra`). Re-do this org-level once repos are under `fuzeone`.
>
> **Two runners exist:** (1) **`kind-host`** — a classic runner **process on the
> developer Windows host** (not a container) that drives Docker Desktop + kind;
> (2) **`staging`** — the shared **ARC scale-set in the Contabo k3s cluster**
> (`arc-systems`/`arc-runners`). Target ARC scale sets with **`runs-on: staging`**
> (the scale-set NAME) — NOT `[self-hosted, staging]`, which never matches.
>
> **Job-fetch gotcha (NOT MTU):** if `staging` jobs queue forever, the usual cause
> is the **ARC controller stuck after transient `generatejitconfig` TLS timeouts**
> (it stops creating runner pods). Fix: `kubectl -n arc-systems delete pod <controller>`
> to force a retry; clear any stale queued-run backlog. Verified working via
> `staging-runner-smoke.yml`.

# Shared Staging Runner — org-level GitHub Actions Self-Hosted Runner

This directory provisions a **shared, org-level GitHub Actions self-hosted runner** for the
`izzywdev` organization.  It lives in the same `fuzeinfra` kind/EKS cluster that hosts the
shared platform stack, and it is **consumer-agnostic** — no application-specific names,
namespaces, or logic appear here.

## Contents

```
runners/
├── arc/
│   ├── controller-values.yaml       # Helm values for ARC controller (arc-systems ns)
│   ├── runner-scale-set-values.yaml # Helm values for the runner scale set (arc-runners ns)
│   ├── github-secret.yaml           # Secret TEMPLATE — fill before applying
│   └── install.sh                   # Bootstrap: install controller + scale set
└── rbac/
    ├── deployer-role.yaml           # Namespaced Role for deploy operations
    └── deployer-rolebinding.yaml    # RoleBinding: runner SA → deployer Role
```

ArgoCD application (optional GitOps management of the scale set):

```
argocd/applications/arc-runners.yaml
```

---

## Design decisions

### 1. Registration scope: org-level runner group

The runner registers at the **`izzywdev` organization level**, not at any individual repo.
It is placed in a **runner group named `staging-runners`** (create it manually in
[GitHub Settings → Actions → Runner groups](https://github.com/organizations/izzywdev/settings/actions/runner-groups)).

Set that group's "Repository access" to an **explicit repo allowlist**, not "All repositories".
Every repo that should access this runner must be explicitly added to the group.  This is the
primary access-control boundary for which repos can schedule jobs on the runner.

### 2. Mechanism: Actions Runner Controller (ARC) scale sets — recommended

ARC v0.9+ "scale set" mode is used (not the legacy `HorizontalRunnerAutoscaler` approach).

| Feature | ARC scale sets | Long-lived Deployment |
|---|---|---|
| Freshness | Fresh pod per job (ephemeral) | Shared persistent pod |
| Security | No cross-job contamination | State may leak between jobs |
| Idle cost | Scales to 0 when idle | Always-on pod |
| Complexity | Helm + CRD | Single Deployment |

**ARC is strongly recommended** for a shared privileged runner.  The ephemeral model
(one pod per job, terminated after) prevents workspace contamination and credential leakage
between unrelated jobs from different repos.

The lighter long-lived Deployment approach is documented below for reference but should only
be used for non-sensitive, single-repo scenarios.

### 3. GitHub authentication: GitHub App (recommended) vs PAT

| | GitHub App | Personal Access Token (PAT) |
|---|---|---|
| Scope | Org-level, scoped permissions | Often user-level, broad |
| Rotation | Key rotation without invalidating other tokens | Token must be rotated manually |
| Audit | Actions logged under App identity | Actions logged under user identity |
| Setup | Slightly more steps | Simpler |

**Recommendation: GitHub App.**  See [Creating the credential](#creating-the-credential).

### 4. Cluster permissions: least-privilege deployer ServiceAccount

The runner pod runs as **`arc-runner-sa`** in the **`arc-runners`** namespace.

At install time, this SA has **zero permissions** in the cluster.  It only gains access to a
staging namespace when the platform team applies the `runners/rbac/` templates there — a
deliberate onboarding step.  The runner can only operate in namespaces where a RoleBinding
was explicitly created.

The `deployer` Role grants only:
- `apps`: Deployments, ReplicaSets, StatefulSets, DaemonSets (CRUD)
- `core`: Services, ConfigMaps, Pods (read-only), Secrets (write-only, no read)
- `networking.k8s.io`: Ingresses (CRUD)
- `autoscaling`: HPAs (CRUD)

No cluster-scoped resources.  No exec, no port-forward, no log access.

### 5. Consumer-agnostic design

Zero references to any specific application, team, or namespace appear in this directory.
Consumer repos are identified only by their presence in the runner-group allowlist (managed
in GitHub UI) and by the RoleBinding the platform team applies in their namespace.

---

## Security boundary

> **Read this section before deploying.**

A shared self-hosted runner executes **arbitrary workflow YAML from any allowlisted repo**
on a Kubernetes identity with in-cluster access.  The mitigations below together define the
trust boundary:

| Threat | Mitigation |
|---|---|
| Cross-job contamination | Ephemeral ARC pods — each job gets a clean pod, terminated after |
| Privilege escalation via runner | `arc-runner-sa` has zero cluster permissions except explicit RoleBindings; runner container runs non-root with dropped capabilities |
| Rogue repo in the org gaining runner access | Runner group with explicit allowlist — repos must be added manually |
| Fork PRs executing privileged jobs | Do NOT set `pull_request_target` without careful review; use `pull_request` (no write token) for untrusted forks |
| Long-lived credentials in runner environment | GitHub App private key lives in a Kubernetes Secret in `arc-runners`; not exposed to job environment |
| Lateral movement to other namespaces | Namespaced RoleBindings only — the runner cannot reach any namespace without an explicit grant |

**Trust boundary:** any workflow in an allowlisted repo that runs on `[self-hosted, staging]`
executes code with the ability to deploy workloads into staging namespaces where the RBAC
has been granted.  Treat the runner-group allowlist as a security boundary equivalent to
org membership for the staging cluster.

---

## Prerequisites

- `kubectl` configured against the target cluster
- `helm` >= 3.10
- ArgoCD running in the cluster (for GitOps mode; optional otherwise)
- The `fuzeinfra` kind cluster (`k8s/kind/kind-cluster.yaml`) or the EKS cluster

---

## Creating the credential

### Option A — GitHub App (recommended)

1. Go to [New GitHub App](https://github.com/organizations/izzywdev/settings/apps/new)
2. Fill in:
   - **Name**: `izzywdev-arc-runner`
   - **Homepage URL**: `https://github.com/izzywdev`
   - **Webhook**: disabled
3. Under **Permissions → Organization permissions**, set:
   - **Self-hosted runners**: Read & Write
4. Under **Where can this GitHub App be installed?**: select **Only on this account**
5. Click **Create GitHub App**; note the **App ID**
6. Scroll to **Private keys** → **Generate a private key**; download the `.pem` file
7. Click **Install App** → Install on `izzywdev`; note the **Installation ID** from the URL
   (`/installations/<INSTALLATION_ID>`)
8. Create the Kubernetes Secret:

```bash
kubectl -n arc-runners create secret generic arc-runner-github-app \
  --from-literal=github_app_id=<APP_ID> \
  --from-literal=github_app_installation_id=<INSTALLATION_ID> \
  --from-literal=github_app_private_key="$(cat /path/to/private-key.pem)"
```

### Option B — PAT (simpler, not recommended for shared runners)

1. Create a Classic PAT with scopes: `repo` (full), `admin:org` → `manage_runners:org`
2. Create the Secret:

```bash
kubectl -n arc-runners create secret generic arc-runner-github-app \
  --from-literal=github_token=<YOUR_PAT>
```

---

## Installing the runner

```bash
# 1. Create the GitHub credential Secret FIRST (see above)

# 2. Create the org runner group in GitHub UI (if it doesn't exist):
#    https://github.com/organizations/izzywdev/settings/actions/runner-groups
#    Name: staging-runners
#    Access: selected repositories only (add your allowlist)

# 3. Bootstrap ARC controller + scale set
cd /path/to/FuzeInfra
chmod +x runners/arc/install.sh
./runners/arc/install.sh

# Verify
kubectl -n arc-runners get pods
kubectl -n arc-systems get pods

# 4. (Optional) Enable GitOps management for the scale set via ArgoCD:
kubectl apply -f argocd/applications/arc-runners.yaml
```

To upgrade after values changes:

```bash
./runners/arc/install.sh --upgrade
```

To remove everything:

```bash
./runners/arc/install.sh --uninstall
```

---

## Consumer onboarding

A consumer repo needs two things to use the shared runner:

### Step 1 — Add the repo to the runner group allowlist

In GitHub: **Organization Settings → Actions → Runner groups → staging-runners → Repository access**  
Add the consumer repo.  This is the platform team's gate.

### Step 2 — Grant deployer RBAC in the staging namespace

The platform team applies the Role + RoleBinding in the consumer's staging namespace:

```bash
NAMESPACE=<consumer-staging-namespace>   # e.g. myapp-staging

# Apply the deployer Role
kubectl apply -f runners/rbac/deployer-role.yaml -n "$NAMESPACE"

# Apply the RoleBinding (binds arc-runner-sa in arc-runners to the deployer Role)
kubectl apply -f runners/rbac/deployer-rolebinding.yaml -n "$NAMESPACE"
```

That's the entire onboarding.  The runner can now deploy to `$NAMESPACE`.

### Step 3 — Target the runner in workflows

```yaml
# .github/workflows/deploy-staging.yml (in the consumer repo)
jobs:
  deploy:
    runs-on: [self-hosted, staging]
    steps:
      - uses: actions/checkout@v4

      # Install tooling (kubectl/helm are not pre-baked in the base runner image).
      - uses: azure/setup-kubectl@v4
      - uses: azure/setup-helm@v4

      # The runner pod's SA (arc-runner-sa) has in-cluster access; no kubeconfig needed.
      - name: Deploy to staging
        run: |
          helm upgrade --install my-app ./chart \
            --namespace ${{ vars.STAGING_NAMESPACE }} \
            --set image.tag=${{ github.sha }} \
            --atomic --timeout 5m
```

> The runner pod uses **in-cluster service account auth** — consumers need neither a kubeconfig
> nor any cluster credentials in their workflow secrets.

---

## Alternative: long-lived runner Deployment

For lightweight, non-sensitive use cases a simple Deployment can host a runner:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: staging-runner
  namespace: arc-runners
spec:
  replicas: 1
  selector:
    matchLabels: { app: staging-runner }
  template:
    metadata:
      labels: { app: staging-runner }
    spec:
      serviceAccountName: arc-runner-sa
      containers:
        - name: runner
          image: ghcr.io/actions/actions-runner:latest
          env:
            - name: GITHUB_RUNNER_LABELS
              value: "self-hosted,staging"
            - name: GITHUB_RUNNER_GROUP
              value: "staging-runners"
            - name: GITHUB_URL
              value: "https://github.com/izzywdev"
            - name: GITHUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: arc-runner-github-app
                  key: github_token
```

**Not recommended for shared use** — the pod is not ephemeral, so jobs from different repos
share the same environment.

---

## Host-level kind runner — RETIRED, no longer required

> **`kind-validate.yml` no longer uses a self-hosted runner.** It runs on
> `ubuntu-latest`. Nothing below is needed; the `kind-host` runner can be
> deregistered. This section is kept only so the history is legible.

**Why it was retired.** The `kind-host` runner was a classic Actions runner
process on a developer's machine. It stopped picking up jobs on 2026-07-24, and
every `kind-validate` run afterwards either timed out in the queue (`cancelled`)
or failed — so the gate did not run *for days* while merely looking "pending" on
PRs. A gate whose liveness depends on somebody's laptop being switched on is a
gate that will keep going quiet, and a quiet gate is worse than no gate because
it still shows up in the checks list.

GitHub-hosted `ubuntu-latest` has docker, 4 vCPU / 16 GB RAM, and enough free
disk after the workflow's cleanup step to run a 3-node kind cluster with the full
stack. Two things also got *better* in the move:

- **Ephemeral.** The old job executed PR-supplied scripts on a persistent host,
  which is RCE-on-a-developer-machine if a fork PR ever slipped through — hence
  the fork-PR exclusion it had to carry. A throwaway hosted VM removes both the
  risk and the exclusion.
- **Same command as a developer.** The job now runs
  `python3 scripts-tools/devenv.py up --fresh --profile full`, which is exactly
  `make dev-fresh`. The gate and the local experience cannot drift.

The ARC `staging` pods still cannot run kind (they drop all capabilities, run
non-root, and are capped at 1Gi) — that remains true and out of scope by design.
It simply no longer matters, because kind runs on hosted infrastructure instead.

**Don't want to host a runner?** The same gate runs locally as a pre-push hook —
zero runner infrastructure:

```bash
# .git/hooks/pre-push   (chmod +x)
make kind-up && make kind-validate && make kind-test && make kind-down
```

The PR gate is simply the automated version of these commands.

> **Security (public repo):** a self-hosted runner executes PR-supplied code.
> `kind-validate.yml` is gated to skip **fork** PRs (`pull_request.head.repo.fork`),
> so only same-repo branches and pushes to `main` run on the host. Also keep the
> org setting **Settings → Actions → "Require approval for all outside
> collaborators"** enabled so a fork PR can never auto-launch any workflow on
> self-hosted infra. For untrusted contributions, prefer ephemeral runners.

---

## Cluster-level bootstrap note (ArgoCD project)

The `arc-runners` and `arc-systems` namespaces are created by `install.sh`.

`arc-systems` **is** GitOps-managed: `argocd/projects/fuzeinfra.yaml` lists it as an
allowed destination and `argocd/applications/arc-selfheal.yaml` syncs
`runners/arc/gitops/` into it (registered on every prod deploy by
`deploy-prod.yml`). Only the watchdog is managed there — the ARC controller and
the scale sets are still bootstrapped by `install.sh`.

`arc-runners` **is** now an allowed destination as of PR #415, which also added
`- ghcr.io/actions/actions-runner-controller-charts` to `spec.sourceRepos`. Before
that, `arc-runners-staging` had sat at `InvalidSpecError` since 2026-07-08 and had
never synced once — which is why the live scale sets drifted from
`runner-scale-set-values.yaml` indefinitely (nothing was reading it):

```
application destination ... namespace 'arc-runners' do not match any of the
allowed destinations in project 'fuzeinfra'
application repo ghcr.io/actions/actions-runner-controller-charts is not permitted
```

That single Application has been replaced by one per scale set in
`argocd/applications/arc-runners.yaml`. They carry `selfHeal: true` but
**`prune: false`**, deliberately: the sets already exist as imperative
`helm install` releases, and Argo adopts the live objects via ServerSideApply —
pruning on that first reconcile could delete every runner set at once. Flip to
`prune: true` only after a clean sync has been observed for all 8.

Note those Applications are **not applied automatically**: `argocd/applications/`
is applied by hand (`kubectl apply -f ...`), so merging that PR changed Git
without changing the cluster. The self-heal Application below is different — it
is registered on every prod deploy by `deploy-prod.yml`.

---

## The self-heal / CI-capacity watchdog

`runners/arc/gitops/controller-selfheal.yaml` runs a CronJob every 15 min in
`arc-systems` covering the two ways CI goes quiet:

| Failure | Detection | Action |
|---|---|---|
| Controller wedged, stops creating runner pods (2026-07-24) | ephemeral runners Pending with zero Running | `rollout restart deployment/arc-controller-gha-rs-controller` |
| CI node lost `fuzeinfra.io/pool=ci` or is cordoned (2026-07-27) | no schedulable node carries the label | logs `CRIT`, exits non-zero, and **skips** the controller restart |

The second case is not self-healable from inside the cluster: k3s applies
`--node-label` only at agent registration
(`modules/contabo-k3s-node/cloud-init.tftpl`), so a manual `kubectl label` is
permanent and Terraform never reconciles it. Fix it by hand:

```bash
kubectl label node <ci-node> fuzeinfra.io/pool=ci --overwrite
```

Two independent alarms cover it: the `CRIT` line routes to a GitHub issue via
the Loki bridge (`docs/crit-log-autofix.md`, keyed off the
`fuzeinfra.io/owner-repo` annotation the Argo Application stamps on
`arc-systems`), and `CiNodePoolLabelMissing` / `CiNodePoolUnschedulable` in
`helm/fuzeinfra/rules/nodes.yml` fire from Prometheus.

> The watchdog image **must contain a shell**. It shipped as `rancher/kubectl`
> with `command: ["sh","-c"]`, which has no `/bin/sh`, and StartError'd on every
> single run from creation until 2026-07-27.

---

## Checklist before going live

- [ ] GitHub App created, private key stored in `arc-runner-github-app` Secret
- [ ] `staging-runners` runner group created in GitHub org, repos allowlisted
- [ ] `arc-runner-sa` ServiceAccount exists in `arc-runners`
- [ ] ARC controller running in `arc-systems`
- [ ] Runner scale set `staging` registered (visible in GitHub org runner settings)
- [ ] Deployer Role + RoleBinding applied in each consumer staging namespace
- [ ] First consumer workflow tested end-to-end
