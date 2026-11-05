# ---------------------------------------------------------------------------
# k3s + ArgoCD provisioning via SSH remote-exec
#
# Two resources, two scopes:
#
#   null_resource.provision   — full k3s + ArgoCD bootstrap.
#     Triggers ONLY on server_ip change (VPS replaced / first apply).
#     k3s install is idempotent; ArgoCD install is idempotent.
#     Does NOT trigger on ArgoCD manifest changes — ArgoCD auto-syncs from git.
#
#   null_resource.argocd_sync — lightweight manifest re-apply.
#     Triggers when ArgoCD project/app YAML changes.
#     Only pushes the updated YAML to the running cluster; never re-installs k3s.
#
# Separation prevents a routine commit to argocd/ from wiping cluster state
# by triggering a full re-provision cascade (which also re-runs secret patches).
# ---------------------------------------------------------------------------

locals {
  argocd_project_path        = "${path.root}/../../argocd/projects/fuzeinfra.yaml"
  argocd_app_path            = "${path.root}/../../argocd/applications/fuzeinfra-prod.yaml"
  argocd_sealed_secrets_path = "${path.root}/../../argocd/applications/sealed-secrets.yaml"
  # Reconciler for the committed SealedSecrets in deploy/sealed-secrets/ (issue
  # #228): keeps the per-service DB password secrets applied to `fuzeinfra` so a
  # namespace re-create never wedges the provisioning Job.
  argocd_infra_sealed_path = "${path.root}/../../argocd/applications/fuzeinfra-sealed-secrets.yaml"
  argocd_ingress_prod_path = "${path.root}/../../argocd/argocd-ingress-prod.yaml"
  values_path              = "${path.root}/../../helm/fuzeinfra/values-contabo.yaml"
}

resource "null_resource" "provision" {
  # Only re-run when the VPS is replaced (IP changes) or on first apply.
  # ArgoCD manifest changes are handled by null_resource.argocd_sync below.
  triggers = {
    server_ip = local.server_ip
  }

  connection {
    type        = "ssh"
    host        = local.server_ip
    user        = var.server_user
    private_key = file(var.ssh_private_key_path)
    timeout     = "5m"
  }

  # Upload ArgoCD manifests to the server
  provisioner "file" {
    source      = local.argocd_project_path
    destination = "/tmp/argocd-project.yaml"
  }

  provisioner "file" {
    source      = local.argocd_app_path
    destination = "/tmp/argocd-app.yaml"
  }

  provisioner "file" {
    source      = local.argocd_sealed_secrets_path
    destination = "/tmp/argocd-sealed-secrets.yaml"
  }

  provisioner "file" {
    source      = local.argocd_infra_sealed_path
    destination = "/tmp/argocd-infra-sealed-secrets.yaml"
  }

  provisioner "file" {
    source      = local.argocd_ingress_prod_path
    destination = "/tmp/argocd-ingress-prod.yaml"
  }

  provisioner "remote-exec" {
    inline = [
      # --- firewall ---
      # Ports 80/443 are intentionally CLOSED from the public internet.
      # All HTTP(S) traffic enters exclusively through the Cloudflare Named Tunnel,
      # which cloudflared initiates as outbound-only connections — no inbound port needed.
      # Blocking 80/443 externally prevents direct VPS access bypassing WAF + Access.
      #
      # 8472/udp = Flannel VXLAN overlay. REQUIRED once worker nodes join: without it
      # the control-plane DROPs inbound VXLAN from agents, so pods on worker nodes
      # can't reach control-plane services (CoreDNS/Postgres/Traefik) — symptom is
      # cross-node DNS timeouts / EAI_AGAIN. (The worker module already opens 8472;
      # the server never did, which broke the first worker that joined.)
      # NOTE: kubelet (10250) is intentionally NOT opened — k3s reaches agent kubelets
      # through the agent's outbound tunnel to 6443, so no inbound 10250 is needed,
      # and exposing the kubelet API is an unnecessary risk.
      # SECURITY: 8472 is opened Anywhere here only as a rebuild bootstrap default
      # (static cloud-init can't know future worker IPs). The RUNTIME posture is
      # scoped — the live rule allows 8472 only from the specific node IPs.
      # 51820/udp = flannel WireGuard-native overlay (switched 2026-07-22, FuzeInfra#318);
      # required in addition to 8472 so new nodes can join during/after the transition.
      "ufw allow 22/tcp && ufw allow 6443/tcp && ufw allow 8472/udp && ufw allow 51820/udp && ufw --force enable 2>/dev/null || true",
      # Scoped runtime 8472 rule for mendys-worker-1 (194.163.136.242) — added 2026-07-22.
      # Was missing before, which caused that node's overlay (and DNS) to be completely blocked.
      "ufw allow from 194.163.136.242 to any port 8472 proto udp comment 'flannel vxlan from mendys-worker-1' 2>/dev/null || true",
      "ufw allow from 194.163.136.242 to any port 51820 proto udp comment 'flannel wireguard from mendys-worker-1' 2>/dev/null || true",
      "ufw delete allow 80/tcp 2>/dev/null || true",
      "ufw delete allow 443/tcp 2>/dev/null || true",

      # --- k3s ---
      # --node-taint control-plane=:PreferNoSchedule biases tenant workloads onto
      # worker nodes, keeping the single control-plane node (apiserver + Traefik +
      # CoreDNS + Postgres) from saturating under tenant churn (FuzeInfra#92).
      # /etc/rancher/k3s/config.yaml is written first; it persists across reinstalls
      # and sets flannel-backend=wireguard-native (switched 2026-07-22, FuzeInfra#318).
      # All worker nodes must have 51820/udp open for the WireGuard peers to handshake.
      "mkdir -p /etc/rancher/k3s",
      "cat > /etc/rancher/k3s/config.yaml <<'KCFG'",
      "# Flannel WireGuard-native backend: encrypts pod overlay traffic, avoids",
      "# VXLAN fragmentation/loss on Contabo public IPs (FuzeInfra#318, 2026-07-22).",
      "# All nodes need UDP 51820 open. Switch from default VXLAN requires rolling",
      "# restart of all k3s-agent nodes after the server adopts this config.",
      "flannel-backend: wireguard-native",
      # --- Private networking (Contabo VPC) — INERT unless enabled ---
      # When local.private_net_enabled (var.enable_private_network AND a concrete
      # var.private_node_ip), route the k3s node identity + flannel overlay over
      # the private NIC (var.private_iface, eth1) instead of the public IP:
      #   flannel-iface     -> overlay peers over the private 10.0.0.0/22 net
      #   node-ip           -> the node's private address (internal cluster IP)
      #   node-external-ip  -> keeps the public IP as the advertised external IP
      # These emit as empty lines (valid YAML) when disabled, so prod is unchanged
      # until the VPC add-on is bought + the gate flipped. NOTE: the per-instance
      # Contabo VPC add-on is a MANUAL panel purchase (HTTP 402 otherwise) that
      # Terraform cannot order — see private-network.tf.
      "%{if local.private_net_enabled}flannel-iface: ${var.private_iface}%{endif}",
      "%{if local.private_net_enabled}node-ip: ${var.private_node_ip}%{endif}",
      "%{if local.private_net_enabled}node-external-ip: ${local.server_ip}%{endif}",
      # advertise-address -> the address the API server publishes in the
      # `kubernetes` Service (10.43.0.1) endpoints. MUST be the private IP.
      # k3s defaults advertise-address to node-external-ip when that is set, so
      # without this the Service advertises PUBLIC IPs while 6443 is firewalled
      # on the public interface of most nodes. Pods on elastic nodes then cannot
      # reach 10.43.0.1:443 at all (measured 0/5), which silently breaks every
      # workload that talks to the Kubernetes API — custom-hostname-api sat
      # Available=False for days on exactly this, reporting only
      # "Kubernetes API is unreachable" from its readiness probe.
      # With the private address advertised, the same probe is 8/8.
      "%{if local.private_net_enabled}advertise-address: ${var.private_node_ip}%{endif}",
      "tls-san:",
      "  - ${local.server_ip}",
      # Private tls-san so kubeconfigs/agents can reach the API over the private IP.
      "%{if local.private_net_enabled}  - ${var.private_node_ip}%{endif}",
      "node-taint:",
      "  - node-role.kubernetes.io/control-plane=:PreferNoSchedule",
      "KCFG",
      # Prefer IPv4 over IPv6 in glibc address selection (RFC 6724 §10.3).
      # Fixes GHCR pulls TCP-reset on Contabo IPv6 CDN paths (FuzeInfra#616).
      # Written before k3s so containerd inherits the preference from first boot.
      "printf 'precedence ::ffff:0:0/96  100\\n' > /etc/gai.conf",
      # Pin + verify installer before execution (avoid mutable curl|sh).
      "K3S_INSTALL_SCRIPT_URL='https://raw.githubusercontent.com/k3s-io/k3s/v1.36.2+k3s1/install.sh'",
      "K3S_INSTALL_SCRIPT_SHA256='46177d4c99440b4c0311b67233823a8e8a2fc09693f6c89af1a7161e152fbfad'",
      "curl -fsSL \"$K3S_INSTALL_SCRIPT_URL\" -o /tmp/k3s-install.sh",
      "echo \"$K3S_INSTALL_SCRIPT_SHA256  /tmp/k3s-install.sh\" | sha256sum -c -",
      "chmod 700 /tmp/k3s-install.sh",
      "if ! command -v k3s &>/dev/null; then",
      "  INSTALL_K3S_EXEC='--tls-san ${local.server_ip} --node-taint node-role.kubernetes.io/control-plane=:PreferNoSchedule' /tmp/k3s-install.sh",
      "else",
      "  echo 'k3s already installed, running upgrade check'",
      "  INSTALL_K3S_EXEC='--tls-san ${local.server_ip} --node-taint node-role.kubernetes.io/control-plane=:PreferNoSchedule' /tmp/k3s-install.sh || true",
      "fi",
      "sleep 15",
      "kubectl wait --for=condition=ready node --all --timeout=120s",

      # --- Lock Traefik to ClusterIP (no external LoadBalancer binding) + HA ---
      # All HTTP(S) must come through the Cloudflare tunnel; direct VPS access is blocked.
      # HelmChartConfig overrides k3s's bundled Traefik before ArgoCD even syncs.
      #
      # This MUST stay byte-identical to argocd/cluster-bootstrap/traefik-clusterip.yaml.
      # The two are applied by different paths — this literal runs ONCE from cloud-init at
      # first boot, while apply-cluster-config.yml re-applies the manifest on every push to
      # main — so if they diverge, a freshly provisioned node runs whatever THIS says until
      # the next push to main happens to reconcile it. Leaving the HA settings out here
      # would mean every new node comes up as a single-replica Traefik SPOF for an
      # unbounded window, which is exactly the condition the manifest exists to remove.
      # Same reason CoreDNS's 2-replica HA config is duplicated in the block below rather
      # than left to CI — that is the established precedent here, not an exception.
      "kubectl apply -f - <<'HELMCFG'",
      "apiVersion: helm.cattle.io/v1",
      "kind: HelmChartConfig",
      "metadata:",
      "  name: traefik",
      "  namespace: kube-system",
      "spec:",
      "  valuesContent: |",
      "    service:",
      "      type: ClusterIP",
      "    deployment:",
      "      replicas: 2",
      "    podDisruptionBudget:",
      "      enabled: true",
      "      minAvailable: 1",
      "    affinity:",
      "      podAntiAffinity:",
      "        preferredDuringSchedulingIgnoredDuringExecution:",
      "          - weight: 100",
      "            podAffinityTerm:",
      "              labelSelector:",
      "                matchLabels:",
      "                  app.kubernetes.io/name: traefik",
      "              topologyKey: kubernetes.io/hostname",
      "HELMCFG",
      # Wait for k3s to reconcile Traefik to ClusterIP (it polls ~every 15s)
      "for i in $(seq 1 20); do TYPE=$(kubectl get svc traefik -n kube-system -o jsonpath='{.spec.type}' 2>/dev/null); [ \"$TYPE\" = 'ClusterIP' ] && break; sleep 5; done",

      # --- CoreDNS HA: scale to 2 replicas with soft anti-affinity ---
      # Prevents a single-replica CoreDNS SPOF where every query crosses the WireGuard
      # overlay to one node, causing 5s/10s stalls on packet loss (FuzeInfra#375).
      # Soft (preferred) anti-affinity keeps it schedulable during node maintenance.
      "kubectl apply -f - <<'DNSCFG'",
      "apiVersion: helm.cattle.io/v1",
      "kind: HelmChartConfig",
      "metadata:",
      "  name: coredns",
      "  namespace: kube-system",
      "spec:",
      "  valuesContent: |",
      "    replicaCount: 2",
      "    affinity:",
      "      podAntiAffinity:",
      "        preferredDuringSchedulingIgnoredDuringExecution:",
      "          - weight: 100",
      "            podAffinityTerm:",
      "              labelSelector:",
      "                matchLabels:",
      "                  app.kubernetes.io/name: coredns",
      "              topologyKey: kubernetes.io/hostname",
      "DNSCFG",

      # --- ArgoCD ---
      "kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -",
      "ARGOCD_MANIFEST_URL='https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.3/manifests/install.yaml'",
      "ARGOCD_MANIFEST_SHA256='0940f3f92ee6b91141cefc5368b1379aed0aef898186879ea2ca5f2607ee2617'",
      "curl -fsSL \"$ARGOCD_MANIFEST_URL\" -o /tmp/argocd-install.yaml",
      "echo \"$ARGOCD_MANIFEST_SHA256  /tmp/argocd-install.yaml\" | sha256sum -c -",
      "kubectl apply -n argocd -f /tmp/argocd-install.yaml",
      "kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=300s",

      # --- ArgoCD AppProject + Applications ---
      "kubectl apply -f /tmp/argocd-project.yaml",
      "kubectl apply -f /tmp/argocd-app.yaml",
      # Sealed Secrets controller + published public-cert Ingress.
      "kubectl apply -f /tmp/argocd-sealed-secrets.yaml",
      # Reconciler for deploy/sealed-secrets/ (service-DB password secrets) so a
      # fresh `fuzeinfra` namespace always has them before the provisioning Job
      # runs — otherwise the Job wedges with CreateContainerConfigError (#228).
      "kubectl apply -f /tmp/argocd-infra-sealed-secrets.yaml",

      # --- ArgoCD Ingress via Traefik (insecure mode: Cloudflare handles TLS) ---
      "kubectl -n argocd patch configmap argocd-cmd-params-cm --type merge -p '{\"data\":{\"server.insecure\":\"true\"}}'",
      # External URL used by ArgoCD for CORS headers and OAuth callbacks.
      # Uses prod.fuzefront.com — the Cloudflare tunnel domain.
      "kubectl -n argocd patch configmap argocd-cm --type merge -p '{\"data\":{\"url\":\"https://argocd.prod.fuzefront.com\"}}'",
      # Traefik runs as ClusterIP so Ingress never gets a LB address; without this
      # custom health check ArgoCD marks every Ingress as Progressing forever.
      "kubectl -n argocd patch configmap argocd-cm --type merge -p '{\"data\":{\"resource.customizations.health.networking.k8s.io_Ingress\":\"hs = {}\\nhs.status = \\\"Healthy\\\"\\nhs.message = \\\"Ingress is healthy (ClusterIP mode)\\\"\\nreturn hs\\n\"}}'",
      "kubectl -n argocd rollout restart deployment/argocd-server",
      "kubectl -n argocd rollout status deployment/argocd-server --timeout=60s",
      # Ingress for argocd.prod.fuzefront.com (Cloudflare tunnel → Traefik → ArgoCD)
      "kubectl apply -f /tmp/argocd-ingress-prod.yaml",

      "echo 'Provisioning complete'",
    ]
  }
}

# ---------------------------------------------------------------------------
# Lightweight ArgoCD manifest re-apply
#
# Re-pushes project/app/ingress YAML when those files change. The cluster is
# already running; this never touches k3s. Depends on provision so it waits
# for a fresh cluster on first apply or after VPS replacement.
# ---------------------------------------------------------------------------
resource "null_resource" "argocd_sync" {
  # OFF by default — see var.enable_argocd_provisioner. It SSHes with a local key
  # file that doesn't exist on CI runners, so it must not run under the CD. With
  # count = 0 the connection block (and file(var.ssh_private_key_path)) is never
  # evaluated. Argo selfHeal + argocd-register.yml cover the work it did.
  count = var.enable_argocd_provisioner ? 1 : 0

  depends_on = [null_resource.provision]

  triggers = {
    server_ip          = local.server_ip
    app_sha            = filesha256(local.argocd_app_path)
    project_sha        = filesha256(local.argocd_project_path)
    sealed_secrets_sha = filesha256(local.argocd_sealed_secrets_path)
    infra_sealed_sha   = filesha256(local.argocd_infra_sealed_path)
  }

  connection {
    type        = "ssh"
    host        = local.server_ip
    user        = var.server_user
    private_key = file(var.ssh_private_key_path)
    timeout     = "2m"
  }

  provisioner "file" {
    source      = local.argocd_project_path
    destination = "/tmp/argocd-project.yaml"
  }

  provisioner "file" {
    source      = local.argocd_app_path
    destination = "/tmp/argocd-app.yaml"
  }

  provisioner "file" {
    source      = local.argocd_sealed_secrets_path
    destination = "/tmp/argocd-sealed-secrets.yaml"
  }

  provisioner "file" {
    source      = local.argocd_infra_sealed_path
    destination = "/tmp/argocd-infra-sealed-secrets.yaml"
  }

  provisioner "file" {
    source      = local.argocd_ingress_prod_path
    destination = "/tmp/argocd-ingress-prod.yaml"
  }

  provisioner "remote-exec" {
    inline = [
      "kubectl apply -f /tmp/argocd-project.yaml",
      "kubectl apply -f /tmp/argocd-app.yaml",
      "kubectl apply -f /tmp/argocd-sealed-secrets.yaml",
      "kubectl apply -f /tmp/argocd-infra-sealed-secrets.yaml",
      "kubectl apply -f /tmp/argocd-ingress-prod.yaml",
      "echo 'ArgoCD manifests synced'",
    ]
  }
}

# ---------------------------------------------------------------------------
# Extract kubeconfig from k3s, rewrite 127.0.0.1 → server IP, store locally
# Used by the github secret resource below
# ---------------------------------------------------------------------------
resource "null_resource" "extract_kubeconfig" {
  depends_on = [null_resource.provision]

  triggers = {
    server_ip    = local.server_ip
    provision_id = null_resource.provision.id
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      ssh -o StrictHostKeyChecking=no \
          -i "${var.ssh_private_key_path}" \
          ${var.server_user}@${local.server_ip} \
          "cat /etc/rancher/k3s/k3s.yaml" \
        | sed 's/127\.0\.0\.1/${local.server_ip}/g' \
        > "${path.root}/k3s-kubeconfig.yaml"
      chmod 600 "${path.root}/k3s-kubeconfig.yaml"
    EOT
  }
}
