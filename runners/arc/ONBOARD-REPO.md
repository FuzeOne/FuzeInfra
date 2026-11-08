# ARC Runner Onboarding Prompt

Copy the block below as the opening message in a new Claude Code session
**inside the target repo**.  Fill in the `<PLACEHOLDERS>` before sending.

---

```
I need you to install a GitHub Actions self-hosted runner for this repo using
the shared ARC (Actions Runner Controller) that already runs on the FuzeInfra
Contabo k3s cluster.

## Context

FuzeInfra hosts a shared ARC controller in the `arc-systems` namespace.
It watches the `arc-runners` namespace.  Each repo gets its own
AutoscalingRunnerSet (Helm release) in that namespace.  The controller then
spins up ephemeral runner pods on the `fuzeinfra-ci-runner-1` node
(label: fuzeinfra.io/pool=ci, taint: fuzeinfra.io/ci=true:NoSchedule).

The tooling for this lives in FuzeInfra:
  - Script:    runners/arc/register-repo.sh  (idempotent, helm upgrade --install)
  - Workflow template: runners/arc/workflow-template/arc-register.yml

## What I need you to do

1. Add a `.github/workflows/arc-register.yml` to THIS repo by copying the
   template from FuzeInfra (https://github.com/FuzeOne/FuzeInfra, path
   runners/arc/workflow-template/arc-register.yml) and filling in:
     SCALE_SET_NAME: "<REPO_SLUG>"   # e.g. "fuzefront" — this becomes runs-on:
     USE_EXISTING_SECRET: "arc-runner-github-app"
   Leave GH_APP_* secrets empty (we share the existing k8s secret).

2. The workflow needs these repo secrets (add them if missing):
     KUBE_CONFIG  — base64-encoded kubeconfig for the FuzeInfra cluster.
                    Get it from: kubectl config view --raw | base64 -w0
                    (run on a machine that has access to the Contabo cluster)

   Since we're using USE_EXISTING_SECRET, no GH_APP_* secrets are needed
   provided the existing arc-runner-github-app k8s secret already covers
   this repo.  If it doesn't, you'll need to:
     a. Add the repo to the GitHub App's repository access in GitHub Settings →
        Developer settings → GitHub Apps → [app name] → Repository access
     b. Then re-run the workflow.

3. Commit the workflow file, push, and trigger it (workflow_dispatch, action=install).

4. Verify success:
   - `kubectl -n arc-runners get autoscalingrunnersets` shows a new entry for
     SCALE_SET_NAME
   - The GitHub repo's Settings → Actions → Runners shows the scale set listed

5. Update one existing workflow in this repo to test it — add a minimal job:
   ```yaml
   jobs:
     smoke:
       runs-on: <SCALE_SET_NAME>
       steps:
         - run: echo "runner works — $(hostname)"
   ```
   Confirm it runs to completion (not stuck in queued).

## Key constraints

- Scale set name must be unique across all repos sharing the arc-runners namespace.
  Existing names in use: "staging" (FuzeInfra).  Pick something repo-specific.
- All runners land on fuzeinfra-ci-runner-1 (4 CPU / 7.75 GB RAM, Contabo).
- The controller watchSingleNamespace is "arc-runners" — do NOT deploy to any
  other namespace or the controller will ignore it.
- Do NOT hand-deploy prod resources.  The arc-runners Argo CD app manages the
  FuzeInfra scale set via GitOps; the workflow-dispatched Helm install for new
  repos is the correct mechanism (Argo does not manage other repos' scale sets).

## Done criteria

- arc-register.yml workflow present in .github/workflows/ and merged to main
- Workflow ran successfully (action=install)
- kubectl confirms the AutoscalingRunnerSet exists
- Smoke test job ran on the self-hosted runner (not ubuntu-latest)
```

---

## Notes for the human

| Placeholder | What to fill in |
|---|---|
| `<REPO_SLUG>` | Short unique name for the runner, e.g. `fuzefront`, `fuzeops`. Becomes the `runs-on:` label. |
| `KUBE_CONFIG` secret | Run `kubectl config view --raw | base64 -w0` on a machine with Contabo cluster access and paste into the repo's secrets. |
| GitHub App access | If the smoke test job stays queued, check that the GitHub App (whose credentials are in `arc-runner-github-app`) has been granted access to this repo under GitHub Settings → Developer settings → GitHub Apps. |

The register-repo.sh script is idempotent — re-running the workflow is safe and
will upgrade the scale set in place.

## Docker / Docker-in-Docker (DinD)

Every scale set registered via `register-repo.sh` runs with **`containerMode: dind`**,
so runner pods get a **real Docker daemon** (a privileged `docker:dind` sidecar +
`init-dind-externals` initContainer, with `DOCKER_HOST` wired for the runner). This
means these work out of the box on `runs-on: <SCALE_SET_NAME>` — no separate
`kind-host` runner is needed for image builds:

- `docker build` / `docker buildx build --push` (GHCR image builds) ✅
- `docker run` / `docker ps` (daemon reachable via `DOCKER_HOST`) ✅

**`docker compose` works by default now.** `register-repo.sh` defaults the runner
image to the **FuzeInfra CI-capable image** (`ghcr.io/izzywdev/fuzeinfra-arc-runner`,
built from `runners/arc/Dockerfile`), which bakes in the **compose-v2 & buildx CLI
plugins**, `jq`/`curl`, a warm Python/Node toolcache, and **Playwright browser OS
deps**. So `docker compose -f …`, `docker buildx …`, and `npx playwright install
<browser>` all work on `runs-on: <SCALE_SET_NAME>` out of the box.

> **Prerequisite:** that image must be **published + PUBLIC** on GHCR (or an
> `imagePullSecret` wired into `arc-runners`) or runner pods `ImagePullBackOff`.
> Publish it once via the `build-runner-image` workflow
> (`runners/arc/workflows-to-install/build-runner-image.yml` → move into
> `.github/workflows/`) or `runners/arc/build-and-push-runner-image.sh`.

To fall back to the stock image (no compose), pass `--runner-image
ghcr.io/actions/actions-runner:latest`. DinD alone does **not** add compose.

**Re-register existing scale sets to pick up DinD.** The dind sidecar only appears
on pods created *after* the Helm values change. Any scale set registered before DinD
was enabled must be re-registered once:

- Consumer repos: re-run your `arc-register.yml` (workflow_dispatch, action=install).
- FuzeInfra's own `staging` set: run `arc-reinstall-scaleset.yml` (or let the
  `arc-runners-staging` Argo CD app sync the updated `runner-scale-set-values.yaml`).

**Capacity note.** DinD raises per-runner limits to cpu:4 / mem:4Gi (requests stay
modest at cpu:500m / mem:1Gi so scheduling isn't blocked). The CI node
`fuzeinfra-ci-runner-1` is 4 CPU / 7.75 GB, so heavy parallel image builds across
many scale sets can contend — scale the CI pool (see the node-autoscaling design
doc) if builds start queuing on capacity.

---

## Troubleshooting

Real-world root causes and fixes confirmed in production.

### A) Control plane UFW must be updated for every new runner node

**Symptom:** Runner pods show `Running` in Kubernetes but appear offline on GitHub; DNS
resolution fails inside pods (can `curl 8.8.8.8` but not `api.github.com`).

**Root cause:** UFW on the control plane has no INPUT rule for UDP port 8472 from the
new node's IP, making flannel VXLAN one-directional. Packets go out but never come back.

**Diagnosis:** On the runner node:
```bash
ip -s link show flannel.1
```
If `RX packets: 0`, the return path is broken.

**Fix (run on control plane):**
```bash
ufw allow from <NEW_NODE_IP> to any port 8472 proto udp comment 'flannel vxlan from <node-name>'
```

This step is mandatory after every new node joins the cluster. See also
`docs/runbooks/node-provisioning.md` for the full post-join checklist.

---

### B) harden-gate.yml needs `actions/setup-python@v6` before pip steps

**Symptom:** `gate-sast` or `gate-authz` jobs fail with `pip: command not found` on
ARC runners (the same jobs succeed on `ubuntu-latest`).

**Root cause:** The ARC runner image does not pre-install Python. The `harden-gate`
workflow template invokes `pip` without first setting up the Python environment.

**Fix:** Add a `setup-python` step before any `pip` invocation in both `gate-sast` and
`gate-authz` jobs:
```yaml
- uses: actions/setup-python@v6
  with:
    python-version: '3.12'
```

---

### C) Entrypoint wrapper required in Helm values

**Symptom:** Runner pods start and complete in 2-3 seconds without picking up any jobs.

**Root cause:** Without an explicit entrypoint, the container's default `CMD` (`/bin/bash`)
runs and exits immediately. ARC only injects `run.sh` on the non-dind path.

**Fix:** Ensure `command` and `args` are set in the runner scale set Helm values:
```yaml
command: ["/bin/bash", "-c"]
args: ["exec /entrypoint.sh"]
```
Or, for the dind path: `exec /home/runner/run.sh`.

---

### D) ARC namespace reference table

| What you're looking for | Namespace | Command |
|---|---|---|
| Scale set listener (per repo) | `arc-systems` | `kubectl get pods -n arc-systems \| grep <slug>-listener` |
| Runner pods (ephemeral) | `arc-runners` | `kubectl get pods -n arc-runners -l app.kubernetes.io/name=<slug>` |
| Controller | `arc-systems` | `kubectl get pods -n arc-systems \| grep controller` |

---

### F) "Queued forever" can be slot starvation, NOT an offline runner

**Symptom:** a repo's `runs-on: <slug>` jobs sit `queued` for hours (or get
`cancelled` by `cancel-in-progress` on the next push) and never run — but the
scale set is registered, the listener is up, and *other* repos' CI is fine.

**This is not the offline-runner failure in (A)/(C).** The scale set *is*
picking up jobs; there just aren't enough free slots. Two compounding causes,
both seen on fuzeplan + fuzesdlc (2026-08-27):

1. **Over-subscription.** A single push fans out more `runs-on: <slug>` jobs
   than the set's `maxRunners`. FuzePlan alone emits ~17 (harden-gate's 9 +
   ci-cd's 8) against `maxRunners: 5`. A Dependabot/PR burst multiplies that.
2. **Hung jobs → zombie runners.** ARC runners are ephemeral (one job per pod).
   A job step with **no `timeout-minutes`** that hangs (a Playwright/`docker
   compose up --wait` waiting on a server that never comes up) pins its runner
   slot — and shared CI-node CPU/RAM — for up to GitHub's **6-hour** job cap.
   `maxRunners` such zombies wedge the set at zero free slots.

**Confirm it:** the runner *did* run recently (`gh api repos/<o>/<r>/actions/runs`
shows past `success`), and a stuck job's pod is still `Running`, renewing its
job lease long after any real job would finish. GitHub-hosted jobs in the same
repo (e.g. CodeQL on `ubuntu-latest`) still pass — only the `<slug>` jobs stick.

**Fixes (in order of durability):**

- **Job timeouts (root cause).** Add `timeout-minutes` to every `runs-on:
  <slug>` job so a hung job self-terminates instead of squatting a slot.
  Route by ownership: **canonical** workflows (`harden-gate.yml`,
  `nightly-integration.yml`, and the rest of `governance_sync.py`'s
  `STANDARD_STACK`) are reconciled from FuzeSDLC — fix them in
  **`workflow-templates/`** there or the nightly governance sweep reverts the
  edit; **repo-owned** workflows (`ci-cd.yml`, `release.yml`, anything not in
  STANDARD_STACK) are fixed in the repo directly.
- **The self-heal watchdog reaps zombies automatically.** `controller-selfheal.yaml`
  check 4 deletes any EphemeralRunner Running past `RUNNER_STUCK_SECS` (2h),
  freeing the slot — a backstop for jobs that still lack a timeout.
- **Capacity.** Raising a set's `maxRunners` does **not** help on its own: all
  sets share one 4-vCPU CI node, and by pod *requests* only ~7 runners fit
  cluster-wide, so extra `maxRunners` just makes pods `Pending`. The real lever
  is adding a CI node (`terraform/contabo/ci-workers.tf`; see the cluster
  scalability backlog), not a values bump.

### E) Diagnostic checklist

Work through these in order when runners are not picking up jobs:

1. **Listener running?**
   ```bash
   kubectl get pods -n arc-systems | grep <slug>-listener
   ```
2. **Runner pods being created?**
   ```bash
   kubectl get pods -n arc-runners -l app.kubernetes.io/name=<slug>
   ```
3. **Runners online on GitHub?**
   ```bash
   gh api repos/<org>/<repo>/actions/runners --jq '.runners[] | "\(.name) \(.status)"'
   ```
4. **If offline — check VXLAN return path:**
   ```bash
   ip -s link show flannel.1   # on the runner node
   ```
   `RX packets: 0` → add UFW rule on control plane (see A above).
5. **gate-sast failing with `pip not found`?** Add `actions/setup-python@v6` before any
   pip step (see B above).
