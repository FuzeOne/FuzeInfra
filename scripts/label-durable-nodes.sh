#!/usr/bin/env bash
# Label the DURABLE nodes so Longhorn places replicas only on them, and verify
# the cluster is in the state the platform expects.
#
# WHY THIS EXISTS
# Longhorn runs with `createDefaultDiskLabeledNodes: true`
# (argocd/applications/longhorn.yaml), which means a node contributes a replica
# disk ONLY if it carries `node.longhorn.io/create-default-disk=true`. That
# label is node-local state — it is NOT in Git and it does NOT survive a node
# reinstall. Every time a durable node was reinstalled during the private-VLAN
# cutover the label was silently lost, and Longhorn then had too few schedulable
# disks to place 3 replicas ("ReplicaSchedulingFailure ... insufficient
# storage" / "disks are unavailable"), which stalls every affected volume.
#
# Run this after: initial cluster bring-up, ANY node reinstall, or adding a
# durable node. It is idempotent and safe to re-run.
#
#   ./scripts/label-durable-nodes.sh                 # label + verify (default nodes)
#   DURABLE_NODES="a b c" ./scripts/label-durable-nodes.sh
#   ./scripts/label-durable-nodes.sh --verify-only
#
# NEVER label an ephemeral/autoscaled elastic node durable: its Longhorn engine
# is short-lived, and a promoted elastic hung a live Postgres volume in prod
# (2026-07-24) — see docs/runbooks/k3s-ha-etcd-migration.md.
set -euo pipefail

LABEL="node.longhorn.io/create-default-disk=true"

# The durable (replica-hosting) nodes are DISCOVERED, not hardcoded.
#
# WHY NOT A NAME LIST. This defaulted to "vmi3383846 vmi3396106 mendys-worker-1"
# and every one of those names is now wrong or on its way out: mendys-worker-1
# was reinstalled as fuze-core-3 on 2026-09-07, and vmi3383846/vmi3396106 are
# being renamed to fuze-core-1/fuze-core-2. A hardcoded list does not fail
# loudly when it goes stale -- it prints "SKIP <name> (not in cluster)" and
# exits 0, leaving that node without the label. Longhorn then sees fewer
# schedulable disks than it needs, and because replicaSoftAntiAffinity=false a
# 3-replica volume becomes unsatisfiable. That is the same class of silent
# starvation this script was written to prevent, so the script must not itself
# be a source of it.
#
# The control-plane role label is the right key: in this cluster durable ==
# control-plane/etcd (the three nodes that own Longhorn disks), it is set by k3s
# at registration so it survives every rename, and it can never match an elastic
# node -- which preserves the "NEVER label an elastic node durable" invariant
# above structurally rather than by convention.
discover_durable_nodes() {
  kubectl get nodes -l node-role.kubernetes.io/control-plane=true \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}' 2>/dev/null || true
}
DURABLE_NODES="${DURABLE_NODES:-$(discover_durable_nodes)}"
if [ -z "${DURABLE_NODES// /}" ]; then
  # Fail closed. Labelling nothing would look like success while leaving
  # Longhorn with no disks at all.
  echo "ERROR: no control-plane nodes discovered and DURABLE_NODES not set." >&2
  echo "       Refusing to run: labelling zero nodes is indistinguishable from success." >&2
  exit 1
fi

# The durable node that carries the heavy-I/O monitoring stack (Prometheus TSDB +
# Loki), and which is therefore designated the LOWEST-priority holder of the API
# floating VIP -- it should be the last node to serve API traffic, never the first.
#
# WHY: on 2026-09-06 Loki sat on vmi3383846, the same node serving the API. Its
# chunk flushes plus Prometheus TSDB compaction starved etcd off the disk ("apply
# request took too long ... read-only range"), the apiserver's own readiness then
# failed, and it refused connections cluster-wide. Separating "the node that serves
# the API" from "the node that does bulk disk I/O" is the structural fix;
# nodeConfigurator.ioPriority (iocost) is the belt to this braces.
#
# Monitoring cannot simply leave the durable pool -- Longhorn disks exist ONLY on
# durable nodes, so an elastic node can never attach these volumes.
#
# This MUST stay consistent with the keepalived VRRP priorities for the API
# floating VIP: this node gets the LOWEST priority. That mechanism now exists —
# helm/fuzeinfra/templates/api-vip-keepalived.yaml, configured by the
# `apiVip.nodes` priority table in values-contabo.yaml (monitoring node last),
# documented in docs/runbooks/api-floating-vip.md. If the monitoring node moves,
# update that table's priorities to keep this node last.
MONITORING_LABEL="fuzeinfra.io/role=monitoring"
# Resolved rather than hardcoded, for the same reason as DURABLE_NODES: this node
# is mid-rename (vmi3396106 -> fuze-core-2). Order of preference:
#   1. an explicit MONITORING_NODE from the environment,
#   2. whichever node already carries the label (survives a rename),
#   3. the first candidate name that actually exists (survives a reinstall,
#      which drops the label).
resolve_monitoring_node() {
  [ -n "${MONITORING_NODE:-}" ] && { printf '%s' "$MONITORING_NODE"; return; }
  local cur
  cur=$(kubectl get nodes -l "$MONITORING_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [ -n "$cur" ] && { printf '%s' "$cur"; return; }
  for c in fuze-core-2 vmi3396106; do
    kubectl get node "$c" >/dev/null 2>&1 && { printf '%s' "$c"; return; }
  done
}
MONITORING_NODE="$(resolve_monitoring_node)"
VERIFY_ONLY="${1:-}"

# ---------------------------------------------------------------------------
# DURABLE-NODE TAINT -- DEFAULT OFF, deliberately.
#
# `fuzeinfra.io/durable=true:NoSchedule` is what actually enforces "only core
# infra and platform frontend run on the durable nodes". The existing
# control-plane taint is only PreferNoSchedule, which is advisory -- the
# scheduler ignores it under pressure, which is exactly what happened:
# 70 and 57 pods on two durable nodes on 2026-09-07, 20,585 container restarts,
# load average 59, and etcd losing heartbeats on the node that was also the
# raft leader.
#
# WHY IT IS NOT ON BY DEFAULT YET. NoSchedule does not evict running pods, so
# applying it looks harmless and then breaks things at the NEXT restart -- a
# delayed failure that is easy to misattribute. Everything that must keep
# running on these nodes has to tolerate it FIRST:
#   * FuzeInfra chart workloads      -- done (#910)
#   * longhorn / coredns / ARC / Argo -- done (#921)
#   * FuzeFront                       -- NOT DONE (FuzeOne/FuzeFront#913)
#   * the LIVE Longhorn taint-toleration Setting -- NOT DONE; defaultSettings
#     is not reapplied on upgrade, so the running cluster needs an explicit
#     patch, and Longhorn requires no volume be attached when it changes.
#
# Flip the default to true in the same PR that closes those two, so the gate
# and the readiness move together instead of drifting apart.
#
#   APPLY_DURABLE_TAINT=true ./scripts/label-durable-nodes.sh
#
# Removing it is the exact inverse and is safe to run any time:
#   kubectl taint node <n> fuzeinfra.io/durable=true:NoSchedule-
APPLY_DURABLE_TAINT="${APPLY_DURABLE_TAINT:-false}"
DURABLE_TAINT_KEY="fuzeinfra.io/durable"
DURABLE_TAINT="${DURABLE_TAINT_KEY}=true:NoSchedule"

kubectl version --request-timeout=10s >/dev/null 2>&1 || {
  echo "ERROR: kubectl cannot reach a cluster" >&2; exit 1; }

if [ "$VERIFY_ONLY" != "--verify-only" ]; then
  echo "== labelling durable nodes =="
  for n in $DURABLE_NODES; do
    if kubectl get node "$n" >/dev/null 2>&1; then
      kubectl label node "$n" "$LABEL" --overwrite >/dev/null
      echo "  ok   $n"
    else
      echo "  SKIP $n (not in cluster)"
    fi
  done

  echo "== labelling the monitoring / lowest-VIP-priority node =="
  if kubectl get node "$MONITORING_NODE" >/dev/null 2>&1; then
    kubectl label node "$MONITORING_NODE" "$MONITORING_LABEL" --overwrite >/dev/null
    echo "  ok   $MONITORING_NODE ($MONITORING_LABEL)"
  else
    # Loud, because Prometheus and Loki nodeSelect on this label: without it they
    # are unschedulable rather than merely misplaced.
    echo "  WARN $MONITORING_NODE not in cluster - Prometheus/Loki will stay Pending" >&2
  fi

  if [ "$APPLY_DURABLE_TAINT" = "true" ]; then
    echo "== applying the durable taint ($DURABLE_TAINT) =="
    # Refuse if the cluster is not actually ready for it. Checking the two
    # components whose failure is worst and least obvious: Longhorn loses the
    # manager on every node that holds storage, and Argo stops reconciling the
    # very thing that would undo the mistake. NoSchedule does not evict, so
    # neither breaks until the next restart -- which is precisely why this has
    # to be a pre-flight check and not a "watch and see".
    unready=""
    for sel in "app=longhorn-manager:longhorn-system" "app.kubernetes.io/name=argocd-application-controller:argocd"; do
      s="${sel%%:*}"; ns="${sel##*:}"
      if ! kubectl -n "$ns" get pods -l "$s" \
           -o jsonpath="{range .items[*]}{range .spec.tolerations[*]}{.key}{'\n'}{end}{end}" 2>/dev/null \
           | grep -qx "$DURABLE_TAINT_KEY"; then
        unready="$unready $ns/$s"
      fi
    done
    if [ -n "$unready" ]; then
      echo "  REFUSING: these do not tolerate $DURABLE_TAINT_KEY yet:$unready" >&2
      echo "  Tainting now would strand them at their next restart, not immediately," >&2
      echo "  which makes the breakage look unrelated. Land the tolerations first." >&2
      exit 1
    fi
    for n in $DURABLE_NODES; do
      if kubectl get node "$n" >/dev/null 2>&1; then
        kubectl taint node "$n" "$DURABLE_TAINT" --overwrite >/dev/null
        echo "  ok   $n tainted"
      else
        echo "  SKIP $n (not in cluster)"
      fi
    done
  else
    echo "== durable taint NOT applied (APPLY_DURABLE_TAINT=false) =="
    echo "   Placement is advisory until this is enabled -- PreferNoSchedule is"
    echo "   a hint the scheduler drops under pressure. See the header comment."
  fi
fi

echo "== verification =="
labelled=$(kubectl get nodes -l node.longhorn.io/create-default-disk=true \
             --no-headers 2>/dev/null | wc -l | tr -d ' ')
echo "  nodes labelled durable: $labelled"

# Report the taint state explicitly. It is invisible in `kubectl get nodes` and
# the difference between "placement is enforced" and "placement is a suggestion"
# is not something to have to go looking for.
tainted=$(kubectl get nodes -o jsonpath="{range .items[*]}{range .spec.taints[*]}{.key}{'\n'}{end}{end}" 2>/dev/null \
            | grep -cx "$DURABLE_TAINT_KEY" || true)
echo "  nodes carrying $DURABLE_TAINT_KEY: $tainted"
if [ "$tainted" -eq 0 ]; then
  echo "  NOTE: placement on durable nodes is ADVISORY only (control-plane taint is"
  echo "        PreferNoSchedule). Application pods can and do land here anyway."
fi
[ "$labelled" -ge 3 ] || {
  echo "  WARN: Longhorn needs >=3 durable nodes for 3-replica volumes." >&2; }

# Cordoned nodes are excluded from Longhorn replica scheduling. Leftover
# troubleshooting cordons silently starve placement — surface them.
cordoned=$(kubectl get nodes --no-headers 2>/dev/null | grep -c SchedulingDisabled || true)
[ "$cordoned" -eq 0 ] && echo "  cordoned nodes: 0" \
  || echo "  WARN: $cordoned cordoned node(s) — Longhorn will not schedule replicas there:
$(kubectl get nodes --no-headers | awk '/SchedulingDisabled/{print "    "$1}')"

if kubectl get nodes.longhorn.io -n longhorn-system >/dev/null 2>&1; then
  echo "  Longhorn disk scheduling status:"
  # NOTE: no backslash-escaped quotes inside the f-string expression below.
  # The previous revision used f"...{n[\"metadata\"][\"name\"]}..." and died on
  # EVERY run with:
  #     SyntaxError: unexpected character after line continuation character
  # Reproduced directly on a prod node (python3 3.12.3) -- so this is NOT an old
  # -interpreter problem; the escaped quote is simply invalid there. The `%`
  # formatting below sidesteps the question entirely.
  #
  # Why it went unnoticed for so long: this is the LAST thing the script does and
  # it is followed by `|| true`, so the traceback scrolled past under a cheerful
  # "done." and the exit code stayed 0. The single line that reports whether
  # Longhorn can actually place a replica has therefore never once printed.
  #
  # Also now reports UNSCHEDULABLE disks. Only ever printing the healthy ones
  # made a starved cluster look identical to a healthy one: on 2026-09-07 two of
  # three durable disks were Schedulable=False and this section would have shown
  # a single node and no hint that anything was wrong.
  kubectl -n longhorn-system get nodes.longhorn.io -o json 2>/dev/null | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except Exception as e:
    print("    (could not parse Longhorn node list: %s)" % e)
    sys.exit(0)
good = bad = 0
for n in items:
    name = n.get("metadata", {}).get("name", "?")
    disks = n.get("status", {}).get("diskStatus", {}) or {}
    for dname, d in disks.items():
        conds = {c.get("type"): c.get("status") for c in (d.get("conditions") or [])}
        sched = conds.get("Schedulable")
        avail = d.get("storageAvailable") or 0
        mx = d.get("storageMaximum") or 0
        if sched == "True":
            good += 1
            print("    OK   %-16s avail=%.1fG max=%.1fG" % (name, avail/1e9, mx/1e9))
        else:
            bad += 1
            print("    FULL %-16s avail=%.1fG max=%.1fG  Schedulable=%s" % (name, avail/1e9, mx/1e9, sched))
print("    -> %d schedulable disk(s), %d unschedulable" % (good, bad))
if good < 3:
    print("    WARN: fewer than 3 schedulable disks; replicaSoftAntiAffinity=false")
    print("          means a 3-replica volume cannot be satisfied.")
' || true
fi
echo "done."
