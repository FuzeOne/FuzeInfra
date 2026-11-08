#!/usr/bin/env bash
# =============================================================================
# generate-claude-handler-kubeconfig.sh  (issue #78)
#
# Mints a kubeconfig for the scoped `claude-handler` ServiceAccount and prints
# it to stdout. Store the result as the GitHub Actions `KUBE_CONFIG` secret used
# by .github/workflows/claude.yml — REPLACING the cluster-admin k3s kubeconfig.
#
# The handler's blast radius is then bounded by RBAC (k8s/claude-handler/rbac.yaml),
# not by the OS guard shims in claude.yml.
#
# Prerequisites:
#   * kubectl configured with an ADMIN context against the target cluster
#     (you run this once, as a human/CD; the output is what the handler uses).
#   * RBAC applied:  kubectl apply -f k8s/claude-handler/rbac.yaml
#
# Usage:
#   scripts/generate-claude-handler-kubeconfig.sh                 # short-lived token (default)
#   TOKEN_TTL=24h scripts/generate-claude-handler-kubeconfig.sh   # custom TTL
#   MODE=longlived scripts/generate-claude-handler-kubeconfig.sh  # non-expiring SA-token Secret
#   APISERVER=https://1.2.3.4:6443 scripts/generate-claude-handler-kubeconfig.sh
#
# Then:
#   scripts/generate-claude-handler-kubeconfig.sh | gh secret set KUBE_CONFIG --repo FuzeOne/FuzeInfra
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-fuzeinfra-automation}"
SA_NAME="${SA_NAME:-claude-handler}"
CONTEXT_NAME="${CONTEXT_NAME:-claude-handler}"
CLUSTER_NAME="${CLUSTER_NAME:-fuzeinfra}"
TOKEN_TTL="${TOKEN_TTL:-2h}"
MODE="${MODE:-shortlived}"   # shortlived | longlived

err() { echo "error: $*" >&2; exit 1; }

command -v kubectl >/dev/null 2>&1 || err "kubectl not found on PATH"

# Confirm the SA exists (RBAC must be applied first).
kubectl -n "$NAMESPACE" get serviceaccount "$SA_NAME" >/dev/null 2>&1 \
  || err "ServiceAccount $NAMESPACE/$SA_NAME not found — apply k8s/claude-handler/rbac.yaml first"

# ---- Discover API server + CA from the current (admin) context ---------------
CURRENT_CONTEXT="$(kubectl config current-context)"
CURRENT_CLUSTER="$(kubectl config view -o jsonpath="{.contexts[?(@.name=='${CURRENT_CONTEXT}')].context.cluster}")"
APISERVER="${APISERVER:-$(kubectl config view -o jsonpath="{.clusters[?(@.name=='${CURRENT_CLUSTER}')].cluster.server}")}"
[ -n "$APISERVER" ] || err "could not determine API server URL; set APISERVER=..."

CA_DATA="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name=='${CURRENT_CLUSTER}')].cluster.certificate-authority-data}")"
if [ -z "$CA_DATA" ]; then
  CA_FILE="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name=='${CURRENT_CLUSTER}')].cluster.certificate-authority}")"
  [ -n "$CA_FILE" ] || err "no CA data/file found for cluster ${CURRENT_CLUSTER}"
  CA_DATA="$(base64 -w0 < "$CA_FILE" 2>/dev/null || base64 < "$CA_FILE" | tr -d '\n')"
fi

# ---- Obtain a token for the ServiceAccount -----------------------------------
if [ "$MODE" = "longlived" ]; then
  # Non-expiring SA-token Secret. Scoped by RBAC; use if your runner cannot
  # refresh short-lived tokens. Rotate by deleting the Secret and re-running.
  SECRET_NAME="${SA_NAME}-token"
  kubectl -n "$NAMESPACE" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${SECRET_NAME}
  namespace: ${NAMESPACE}
  annotations:
    kubernetes.io/service-account.name: ${SA_NAME}
type: kubernetes.io/service-account-token
EOF
  # Wait for the token controller to populate the Secret.
  for _ in $(seq 1 30); do
    TOKEN="$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || true)"
    [ -n "$TOKEN" ] && break
    sleep 1
  done
  [ -n "${TOKEN:-}" ] || err "token Secret ${SECRET_NAME} was not populated"
else
  # Short-lived bound token via the TokenRequest API (recommended).
  TOKEN="$(kubectl -n "$NAMESPACE" create token "$SA_NAME" --duration="$TOKEN_TTL")" \
    || err "kubectl create token failed (needs k8s >= 1.24)"
fi

# ---- Emit kubeconfig to stdout ----------------------------------------------
cat <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER_NAME}
    cluster:
      server: ${APISERVER}
      certificate-authority-data: ${CA_DATA}
contexts:
  - name: ${CONTEXT_NAME}
    context:
      cluster: ${CLUSTER_NAME}
      namespace: fuzeinfra
      user: ${SA_NAME}
current-context: ${CONTEXT_NAME}
users:
  - name: ${SA_NAME}
    user:
      token: ${TOKEN}
EOF

echo "# kubeconfig for ${NAMESPACE}/${SA_NAME} (mode=${MODE}, ttl=${TOKEN_TTL})" >&2
echo "# store as the KUBE_CONFIG GitHub Actions secret" >&2
