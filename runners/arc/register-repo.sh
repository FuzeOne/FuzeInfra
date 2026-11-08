#!/usr/bin/env bash
# =============================================================================
# register-repo.sh — register a GitHub repo as an ARC self-hosted runner
#
# Installs a gha-runner-scale-set Helm release on the shared FuzeInfra ARC
# controller.  The controller already runs in arc-systems; this script only
# adds a new AutoscalingRunnerSet pointing at your repo.
#
# Prerequisites
#   - kubectl pointing at the FuzeInfra cluster (kubeconfig set up)
#   - helm >= 3.10
#   - A GitHub App or PAT with the repo in scope (see --help)
#
# Usage
#   ./runners/arc/register-repo.sh \
#       --repo-url   https://github.com/FuzeOne/FuzeFront \
#       --name       fuzefront \
#       --app-id     123456 \
#       --app-install-id  789012 \
#       --app-private-key /path/to/private-key.pem
#
#   # or: supply an existing k8s secret that already holds the GitHub App creds
#   ./runners/arc/register-repo.sh \
#       --repo-url  https://github.com/FuzeOne/FuzeFront \
#       --name      fuzefront \
#       --secret    arc-runner-github-app   # existing secret in arc-runners
#
#   # The runner pod spec (image, containerMode/dind, resources, node pinning)
#   # is NOT settable here -- it comes from runner-scale-set-values.yaml, the
#   # same file Argo renders. Change it THERE so both paths stay identical.
#
#   # uninstall a repo's scale set
#   ./runners/arc/register-repo.sh --name fuzefront --uninstall
#
# After registration, add to your repo's workflows:
#   jobs:
#     build:
#       runs-on: fuzefront   # matches --name
# =============================================================================

set -euo pipefail

# ---- defaults ---------------------------------------------------------------
RUNNER_NS="arc-systems-runners"
RUNNER_NS="arc-runners"          # must match controller watchSingleNamespace
CONTROLLER_NS="arc-systems"
CONTROLLER_SA="arc-controller-gha-rs-controller"
ARC_RUNNER_CHART="oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set"
ARC_VERSION="0.14.2"
# DinD sidecars are unbounded and share the CI node (fuzeinfra-ci-runner-1,
# 4 CPU / ~7.75 GB). Cap concurrency so parallel compose/build stacks don't OOM
# the node; raise per-repo with --max-runners once the CI pool is scaled out.
MAX_RUNNERS=3
MIN_RUNNERS=0
# NOTE: the runner image, containerMode/dind wiring, resources, node pinning and
# tolerations all live in runner-scale-set-values.yaml (shared with Argo). They
# are deliberately NOT overridable from this script -- see the incident note in
# the "Per-repo values" section below for what happened the last time this
# script rendered its own divergent pod spec.

REPO_URL=""
SCALE_SET_NAME=""
GITHUB_SECRET_NAME=""
APP_ID=""
APP_INSTALL_ID=""
APP_PRIVATE_KEY_FILE=""
UNINSTALL=false

# ---- arg parsing ------------------------------------------------------------
usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '/Usage/,/^===/{ /^===/d; p }'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)          REPO_URL="$2"; shift 2 ;;
    --name)              SCALE_SET_NAME="$2"; shift 2 ;;
    --secret)            GITHUB_SECRET_NAME="$2"; shift 2 ;;
    --app-id)            APP_ID="$2"; shift 2 ;;
    --app-install-id)    APP_INSTALL_ID="$2"; shift 2 ;;
    --app-private-key)   APP_PRIVATE_KEY_FILE="$2"; shift 2 ;;
    --max-runners)       MAX_RUNNERS="$2"; shift 2 ;;
    --runner-image|--container-mode)
      # Previously these rendered into an inline values block. They now have no
      # effect, and silently ignoring them is how the pod spec diverged from
      # Argo's in the first place -- so refuse instead of pretending.
      echo "ERROR: $1 is no longer supported." >&2
      echo "       The runner pod spec (image, containerMode/dind, resources) is owned by" >&2
      echo "       runners/arc/runner-scale-set-values.yaml, which BOTH this script and the" >&2
      echo "       Argo Applications render. Edit that file so every scale set changes" >&2
      echo "       together; a per-invocation override here would re-create the split" >&2
      echo "       field-manager drift that broke 8 scale sets on 2026-08-31." >&2
      exit 1 ;;
    --uninstall)         UNINSTALL=true; shift ;;
    --help|-h)           usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

# ---- validate ---------------------------------------------------------------
if [[ -z "$SCALE_SET_NAME" ]]; then
  echo "ERROR: --name is required (e.g. --name fuzefront)" >&2
  exit 1
fi

if $UNINSTALL; then
  echo "==> Uninstalling scale set '$SCALE_SET_NAME' from $RUNNER_NS …"
  helm uninstall "$SCALE_SET_NAME" --namespace "$RUNNER_NS" --wait 2>/dev/null \
    || echo "(not installed — nothing to do)"
  SECRET_NAME="arc-runner-${SCALE_SET_NAME}-github-app"
  kubectl -n "$RUNNER_NS" delete secret "$SECRET_NAME" --ignore-not-found
  echo "Done."
  exit 0
fi

if [[ -z "$REPO_URL" ]]; then
  echo "ERROR: --repo-url is required (e.g. --repo-url https://github.com/FuzeOne/FuzeFront)" >&2
  exit 1
fi

# ---- GitHub App secret ------------------------------------------------------
if [[ -n "$GITHUB_SECRET_NAME" ]]; then
  # Caller supplied an existing secret name — verify it exists
  if ! kubectl -n "$RUNNER_NS" get secret "$GITHUB_SECRET_NAME" &>/dev/null; then
    echo "ERROR: secret '$GITHUB_SECRET_NAME' not found in namespace $RUNNER_NS" >&2
    exit 1
  fi
  K8S_SECRET_NAME="$GITHUB_SECRET_NAME"
  echo "==> Using existing secret: $K8S_SECRET_NAME"
else
  # Create a new secret from GitHub App credentials
  if [[ -z "$APP_ID" || -z "$APP_INSTALL_ID" || -z "$APP_PRIVATE_KEY_FILE" ]]; then
    echo "ERROR: supply --secret <existing> OR all three of --app-id / --app-install-id / --app-private-key" >&2
    exit 1
  fi
  if [[ ! -f "$APP_PRIVATE_KEY_FILE" ]]; then
    echo "ERROR: private key file not found: $APP_PRIVATE_KEY_FILE" >&2
    exit 1
  fi

  K8S_SECRET_NAME="arc-runner-${SCALE_SET_NAME}-github-app"
  echo "==> Creating GitHub App secret '$K8S_SECRET_NAME' …"
  kubectl -n "$RUNNER_NS" create secret generic "$K8S_SECRET_NAME" \
    --from-literal=github_app_id="$APP_ID" \
    --from-literal=github_app_installation_id="$APP_INSTALL_ID" \
    --from-file=github_app_private_key="$APP_PRIVATE_KEY_FILE" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

# ---- Per-repo values --------------------------------------------------------
# SINGLE SOURCE OF TRUTH: the pod spec comes from runner-scale-set-values.yaml,
# the SAME file the Argo Applications in argocd/applications/arc-runners.yaml
# render with. This script only supplies the per-repo parameters on top of it.
#
# WHY THIS IS NO LONGER AN INLINE VALUES BLOCK (2026-08-31 incident):
# this script used to build its own values using the `containerMode: dind`
# SHORTHAND. On chart 0.14.2 that shorthand renders dind as a native sidecar
# *initContainer*:
#     initContainers: [init-dind-externals, dind]   containers: [runner]
# while the shared values file -- since #675, which bounded dind's resources --
# declares dind explicitly as an ordinary container:
#     initContainers: [init-dind-externals]         containers: [runner, dind]
#
# Either form is valid alone. The damage came from BOTH managers owning the one
# live AutoscalingRunnerSet: helm (this script) wrote the initContainer form,
# Argo later applied the container form server-side, and ServerSideApply CANNOT
# prune a field owned by a DIFFERENT field manager ("helm"). The stale `dind`
# initContainer therefore survived indefinitely, leaving the live object with
# dind in BOTH lists. Kubernetes requires container names to be unique across
# initContainers+containers, so the API server rejected EVERY runner pod with:
#     Failed to create the pod: ... spec.initContainers[1].name: Duplicate value: "dind"
# Each EphemeralRunner went Failed with no pod; ARC never garbage-collects
# Failed runners, so they accumulated to maxRunners and the listener then looped
# forever re-patching a set that could never produce a runner. Eight scale sets
# (fuzecontact, fuzehub, fuzemarket, fuzepicker, fuzesales, fuzeservice,
# fuzesocial, mendysrobotics) queued ALL their CI silently for up to 30h while
# Argo reported them Synced the entire time -- Git and Argo's own applied config
# were both correct; the drift lived in a field Argo did not own.
#
# Rendering from the shared file keeps the imperative path and the GitOps path
# byte-identical, so the two managers can no longer disagree.
VALUES_FILE="$(dirname "$0")/runner-scale-set-values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "ERROR: shared values file not found: $VALUES_FILE" >&2
  echo "       Callers using a sparse checkout must include" >&2
  echo "       runners/arc/runner-scale-set-values.yaml alongside this script." >&2
  exit 1
fi

# ---- Ensure runner SA exists in the namespace (idempotent) ------------------
echo "==> Ensuring runner ServiceAccount in $RUNNER_NS …"
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: arc-runner-sa
  namespace: ${RUNNER_NS}
EOF

# ---- Helm install/upgrade ---------------------------------------------------
echo "==> Registering scale set '$SCALE_SET_NAME' for $REPO_URL …"
echo "    values file  : $VALUES_FILE (shared with Argo -- pod spec lives there)"
echo "    maxRunners   : $MAX_RUNNERS"
helm upgrade --install "$SCALE_SET_NAME" \
  "$ARC_RUNNER_CHART" \
  --version "$ARC_VERSION" \
  --namespace "$RUNNER_NS" \
  --create-namespace \
  --values "$VALUES_FILE" \
  --set githubConfigUrl="$REPO_URL" \
  --set githubConfigSecret="$K8S_SECRET_NAME" \
  --set runnerScaleSetName="$SCALE_SET_NAME" \
  --set maxRunners="$MAX_RUNNERS" \
  --set minRunners="$MIN_RUNNERS" \
  --wait --timeout 3m

echo ""
echo "✓ Scale set '${SCALE_SET_NAME}' registered for ${REPO_URL}"
echo ""
echo "Add to your repo's workflows:"
echo "  jobs:"
echo "    build:"
echo "      runs-on: ${SCALE_SET_NAME}"
echo ""
echo "Verify in GitHub:"
echo "  ${REPO_URL}/settings/actions/runners"
