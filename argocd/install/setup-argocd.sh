#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Install Argo CD into the current kube-context and bootstrap the FuzeInfra
# GitOps Application (App points at helm/fuzeinfra in this repo).
#
# Prereqs: kubectl, helm not required (Argo renders Helm itself).
# Usage:
#   ./argocd/install/setup-argocd.sh local   # bootstrap fuzeinfra-local (kind)
#   ./argocd/install/setup-argocd.sh aws     # bootstrap fuzeinfra-aws  (EKS)
#   ./argocd/install/setup-argocd.sh none    # install Argo CD only, no app
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARGOCD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENVIRONMENT="${1:-local}"
# Pin Argo CD for reproducibility; bump deliberately.
ARGOCD_VERSION="${ARGOCD_VERSION:-v2.13.3}"

command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }

echo "==> Installing Argo CD ${ARGOCD_VERSION} into namespace 'argocd'"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

echo "==> Waiting for Argo CD server to be ready"
kubectl -n argocd rollout status deploy/argocd-server --timeout=300s

# -----------------------------------------------------------------------------
# Tolerate the durable-node taint (fuzeinfra.io/durable=true:NoSchedule).
#
# Argo CD is installed from the upstream install.yaml, which carries no
# tolerations and exposes no values file, so this cannot be expressed
# declaratively the way the Helm-managed components do it -- it has to be a
# patch applied after install.
#
# WHY ARGO NEEDS IT. The durable nodes are tainted so application workloads stop
# piling onto the three control-plane/etcd nodes. Argo CD is not an application:
# it is the thing that reconciles every application. Without a toleration it can
# only run on autoscaled elastic nodes, which the reaper releases on billing
# boundaries -- so the GitOps engine would be evicted on a billing schedule, and
# while it is down nothing in the cluster reconciles, including whatever change
# was meant to repair the situation.
#
# Tolerating is not pinning -- the scheduler may still choose an elastic node.
# This only keeps the stable nodes eligible.
#
# Idempotent: re-running applies the same patch. Safe to run against an existing
# install (including prod) without reinstalling Argo.
echo "==> Patching Argo CD workloads to tolerate the durable-node taint"
DURABLE_TOLERATION='{"spec":{"template":{"spec":{"tolerations":[{"key":"fuzeinfra.io/durable","operator":"Equal","value":"true","effect":"NoSchedule"}]}}}}'
for kind in deployment statefulset; do
  for w in $(kubectl -n argocd get "$kind" -o name 2>/dev/null); do
    kubectl -n argocd patch "$w" --type=strategic -p "$DURABLE_TOLERATION" >/dev/null \
      && echo "     ok   $w" \
      || echo "     WARN could not patch $w" >&2
  done
done

echo "==> Applying AppProjects (FuzeInfra owns the destination/security boundary)"
kubectl apply -f "$ARGOCD_DIR/projects/fuzeinfra.yaml"
# Consumer projects held here are restricted (cannot deploy into the fuzeinfra
# namespace). A product that owns its own AppProject (#639) — FuzeFront, in
# FuzeOne/FuzeFront deploy/argocd/project.yaml — is NOT applied here: it is
# registered by argocd-register.yml, which validates it first. Applying a copy
# from this repo as well is the two-writer conflict that froze FuzeFront (#1278).
kubectl apply -f "$ARGOCD_DIR/projects/fuzeagent.yaml"

case "$ENVIRONMENT" in
  local)
    echo "==> Bootstrapping Application fuzeinfra-local"
    kubectl apply -f "$ARGOCD_DIR/applications/fuzeinfra-local.yaml"
    ;;
  aws)
    echo "==> Bootstrapping Application fuzeinfra-aws"
    kubectl apply -f "$ARGOCD_DIR/applications/fuzeinfra-aws.yaml"
    ;;
  none)
    echo "==> Skipping Application bootstrap (env=none)"
    ;;
  *)
    echo "Unknown environment '$ENVIRONMENT' (use: local | aws | none)"; exit 1
    ;;
esac

echo
echo "==> Argo CD admin password:"
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" 2>/dev/null | base64 -d || \
  echo "(secret not found - it may have been rotated/deleted after first login)"
echo
cat <<'EOF'

==> Open the Argo CD UI:
  kubectl -n argocd port-forward svc/argocd-server 8080:443
  then browse https://localhost:8080  (user: admin)

NOTE: if this repo is PRIVATE, register repo credentials so Argo CD can read it:
  argocd repo add https://github.com/FuzeOne/FuzeInfra.git \
    --username <user> --password <token>
  (or create a repository Secret labeled argocd.argoproj.io/secret-type=repository)
EOF

echo "==> Applying ArgoCD notifications config (OutOfSync -> GitHub dispatch)"
kubectl apply -f "$ARGOCD_DIR/notifications/argocd-notifications-cm.yaml"
# argocd-notifications-secret (github-token) is applied out-of-band (holds a token).
