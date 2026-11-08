# ---------------------------------------------------------------------------
# Contabo API credentials
# Get these from: https://new.contabo.com/account/api-credentials
# ---------------------------------------------------------------------------
variable "contabo_client_id" {
  description = "Contabo OAuth2 client ID (from customer portal API credentials)"
  type        = string
  sensitive   = true
}

variable "contabo_client_secret" {
  description = "Contabo OAuth2 client secret"
  type        = string
  sensitive   = true
}

variable "contabo_api_user" {
  description = "Contabo account email address"
  type        = string
}

variable "contabo_api_password" {
  description = "Contabo account password"
  type        = string
  sensitive   = true
}

# ---------------------------------------------------------------------------
# VPS configuration
# Find image/product IDs at: https://api.contabo.com/#tag/Images/operation/retrieveImage
# or run: curl -s -H "Authorization: Bearer <token>" https://api.contabo.com/v1/compute/images
# ---------------------------------------------------------------------------
variable "instance_display_name" {
  description = "Display name for the VPS in Contabo dashboard"
  type        = string
  default     = "fuzeinfra-prod"
}

variable "image_id" {
  description = "Contabo OS image UUID. Ubuntu 24.04 LTS image ID (find via Contabo API or dashboard)"
  type        = string
  # Example: look up with `contabo images list` or the Contabo API
}

variable "product_id" {
  description = "Contabo product/plan UUID (e.g. VPS S, M, L). Find in Contabo dashboard URLs or API."
  type        = string
  # Example VPS S has 4 vCPU / 8 GB RAM / 200 GB SSD
}

variable "ssh_public_key" {
  description = "SSH public key to inject into the VPS via cloud-init (contents of ~/.ssh/id_ed25519.pub)"
  type        = string
}

# ---------------------------------------------------------------------------
# SSH access (to provision k3s and ArgoCD)
# ---------------------------------------------------------------------------
variable "ssh_private_key_path" {
  description = "Path to the SSH private key matching the key registered in Contabo"
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "server_user" {
  description = "SSH user on the VPS"
  type        = string
  default     = "root"
}

# ---------------------------------------------------------------------------
# Production domain
# Default uses nip.io (free wildcard DNS for any IP).
# Replace with your real domain if you have one pointed at the server.
# E.g. "infra.yourdomain.com" requires a wildcard *.infra.yourdomain.com DNS record.
# ---------------------------------------------------------------------------
variable "domain" {
  description = "Base domain for Ingress hostnames (e.g. grafana.<domain>)"
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# GitHub (for setting the KUBE_CONFIG secret)
# ---------------------------------------------------------------------------
variable "github_token" {
  description = "GitHub personal access token with repo + secrets write permission"
  type        = string
  sensitive   = true
}

variable "github_owner" {
  description = "GitHub owner (org or user)"
  type        = string
  default     = "izzywdev"
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "FuzeInfra"
}

# ---------------------------------------------------------------------------
# Cloudflare Zero Trust (optional — leave api_token empty to skip)
#
# When cloudflare_api_token is set, a single `terraform apply` in this
# directory creates the Named Tunnel, DNS records, and Access policies
# AND injects the computed token into the cluster — no manual steps.
#
# Obtain a Cloudflare API token with these permissions:
#   Zone    > DNS                                          > Edit
#   Zone    > Zone                                          > Read
#   Zone    > SSL and Certificates                          > Edit
#   Account > Cloudflare Tunnel                             > Edit
#   Account > Access: Apps and Policies                     > Edit
#   Account > Access: Organizations, Identity Providers, and Groups > Edit
#   Account > Workers Scripts                               > Edit
# Create at: https://dash.cloudflare.com/profile/api-tokens
#
# The identity-provider scope is required by
# cloudflare_zero_trust_access_identity_provider.authentik. A missing Access
# scope plans CLEAN and fails only at apply, with a generic
# "Authentication error (10000)" naming the resource but not the scope.
# Keep this list in sync with docs/TERRAFORM_CD.md and docs/cloudflare-zero-trust.md.
# ---------------------------------------------------------------------------
variable "cloudflare_api_token" {
  description = "Cloudflare API token. Leave empty to skip all Cloudflare resources."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID (from https://dash.cloudflare.com → account settings)"
  type        = string
  default     = ""
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for the domain (from the zone overview page)"
  type        = string
  default     = ""
}

variable "tunnel_name" {
  description = "Name of the Named Tunnel in Cloudflare Zero Trust dashboard"
  type        = string
  # MUST match the deployed tunnel name. The production tunnel is named
  # "FuzeInfra" (confirmed in state); a mismatch makes terraform rename — and if
  # `name` is ForceNew, recreate — the tunnel, which would break all prod access.
  default = "FuzeInfra"
}

variable "prod_subdomain" {
  description = "Subdomain under zone_name that points to this cluster (e.g. 'prod' → prod.fuzefront.com)"
  type        = string
  default     = "prod"
}

variable "zone_name" {
  description = "Root DNS zone managed in Cloudflare (e.g. fuzefront.com)"
  type        = string
  default     = "fuzefront.com"
}

# ---------------------------------------------------------------------------
# Multi-tenant portal DNS/TLS (FuzeFront EPIC-16). Both default OFF so a bare
# `terraform apply` stays byte-identical to today's cluster; flip them in the
# same change that enables helm/fuzeinfra customHostnameApi.
# ---------------------------------------------------------------------------
variable "tenant_wildcard_enabled" {
  description = <<-EOT
    Let Terraform MANAGE the proxied wildcard CNAME *.<zone_name> pointing at the
    tunnel, so tenant subdomains (corpabc.fuzefront.com) reach Traefik.

    This does NOT gate whether *.<zone_name> resolves: a wildcard record already
    exists in the zone, created outside this state, so tenant hosts resolve
    regardless. The consumer's Ingress rule is the only gate on SERVING. Applying
    this on a zone that already has the record fails on a duplicate unless the
    record is imported first — see the comment in cloudflare.tf.

    At the DNS layer, reserved hosts need no exclusion list: explicit records and
    the *.<prod_subdomain> wildcard are more specific and win automatically. That
    does NOT extend to Traefik, which sorts routers by rule length — a consumer's
    wildcard Ingress rule will capture other products' exact hosts unless it is
    isolated in its own Ingress at router.priority 1.

    TLS comes from Cloudflare Universal SSL, which covers the apex and the FIRST
    wildcard level only.
  EOT
  type        = bool
  default     = true
}

variable "saas_custom_hostnames_enabled" {
  description = <<-EOT
    Enable Cloudflare for SaaS on the zone: creates connect.<zone_name> (the
    CNAME target published to customers) and saas-origin.<zone_name> (the
    fallback origin), and points the zone's fallback origin at the latter.
    Individual customer domains are NOT managed here — they are created at
    runtime by the in-cluster custom-hostname API.

    Cost: 100 custom hostnames included on Free/Pro/Business, $0.10/month each
    beyond that (ceiling 50,000).

    Requires the Terraform API token to have "Zone / Custom Hostnames: Edit"
    permission. Set to true only after that permission is added to the
    "FuzeInfra Terraform" token in the Cloudflare dashboard.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# MendysRobotics.com subdomain routing (issue #120)
# ---------------------------------------------------------------------------
variable "mendysrobotics_zone_id" {
  description = "Cloudflare Zone ID for the mendysrobotics.com zone. Leave empty to skip all mendysrobotics.com DNS + Access resources."
  type        = string
  default     = ""
  sensitive   = true
}

variable "allowed_admin_emails" {
  description = <<-EOT
    Email addresses allowed through Cloudflare Access. Applies to BOTH login
    methods: it is the `include` for the break-glass email-OTP policy and the
    `require` for the Google policy.

    THIS IS THE PLATFORM'S PRIMARY AUTHORIZATION CONTROL. Not one layer of
    several — for most hosts behind the *.prod wall (Prometheus, Alertmanager,
    Mongo Express, ChromaDB, LiteLLM) it is the only one, because those products
    have no identity layer of their own. Authentik does not restrict enrollment
    either, so any Google identity that gets past this list is provisioned as a
    real Authentik user.

    Widening it to a domain, or moving it out of the policy's `require` block
    into `include`, opens the admin plane to every Google account on the internet.
  EOT
  type        = list(string)
  default     = ["izzy.weinberg@gmail.com"]
}

variable "authentik_host" {
  description = <<-EOT
    Hostname of the Authentik IdP, used for the Authentik App Launcher tile and
    as the browser-facing OIDC host for the internal services it authenticates.

    This is now a host INSIDE the *.prod Access wall, and that is a deliberate
    reversal. It could not be, while Authentik was itself the Access IdP: an IdP
    behind the wall it authenticates cannot be reached in order to log in. Now
    that Cloudflare Access authenticates with Google instead, that circularity is
    gone and Authentik is reachable only after passing Access — which is the
    stated requirement that nothing, including the IdP, is exposed publicly.

    Authentik itself is deployed by FuzeOne/FuzeFront, which serves this host
    from its own Traefik Ingress.
  EOT
  type        = string
  default     = "authentik.prod.fuzefront.com"
}

variable "google_access_client_id" {
  description = <<-EOT
    OAuth client ID for the Google identity provider used by Cloudflare Access.
    Created in Google Cloud Console as a "Web application" OAuth client, with
    authorized redirect URI:

      https://<team>.cloudflareaccess.com/cdn-cgi/access/callback

    Empty (or an empty client secret) disables the Google identity provider AND
    both of its Access policies, leaving only the break-glass email-OTP path.
    This is a count gate: if the CD workflow stops passing it, the plan is a
    DESTROY of the IdP and its policies, not a no-op — the same failure mode
    already documented for crit_bridge_token.
  EOT
  type        = string
  default     = ""
}

variable "google_access_client_secret" {
  description = <<-EOT
    OAuth client secret paired with google_access_client_id. Wire as
    TF_VAR_google_access_client_secret from the GitHub secret
    GOOGLE_ACCESS_CLIENT_SECRET.

    NOTE this is a DIFFERENT credential from the GOOGLE_CLIENT_ID/SECRET that
    FuzeFront's Authentik and security-service use for Google sign-in. Both let
    you log in with the same Google account, but they are separate OAuth clients
    with different redirect URIs and must be rotated independently. Reusing one
    for the other fails with redirect_uri_mismatch.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "access_session_duration" {
  description = "How long a Cloudflare Access session lasts before re-auth is required"
  type        = string
  default     = "24h"
}

variable "crit_bridge_token" {
  description = "Shared secret between Grafana and the crit-alert CF Worker (BRIDGE_TOKEN). Set in terraform.tfvars. Also injected into fuzeinfra-secrets as CRIT_BRIDGE_TOKEN so Grafana can read it."
  type        = string
  default     = ""
  sensitive   = true
}

variable "handoff_mcp_access_enabled" {
  description = "Create the more-specific CF Access 'bypass' app for mcp-handoff.<domain> so Anthropic Managed Agents (machine, non-interactive) skip the *.prod email-OTP wildcard; the handoff MCP server enforces its own HANDOFF_MCP_TOKEN bearer. Flip to true when the handoff MCP is deployed."
  type        = bool
  default     = false
}

variable "a2a_relay_access_enabled" {
  description = "Create the more-specific CF Access 'bypass' app for relay.<domain> so cloud Claude Code sessions (machine, non-interactive WebSocket) skip the *.prod email-OTP wildcard. The A2A relay is reachable over WSS; v0 is open (capability = unguessable cse_ session-id), and it enforces an optional FUZE_A2A_RELAY_TOKEN bearer when set. Flip to true when the a2a-relay is deployed (a2aRelay.enabled)."
  type        = bool
  default     = false
}

variable "a2a_gateway_access_enabled" {
  description = "Create the more-specific CF Access 'bypass' app for a2a-gateway.<domain> so cloud Claude Code sessions (machine, non-interactive) can POST to the A2A delivery gateway without the *.prod email-OTP wildcard. The gateway enforces its own optional FUZE_A2A_GATEWAY_TOKEN bearer. Flip to true when the a2a-gateway is deployed (a2aGateway.enabled)."
  type        = bool
  default     = false
}

variable "ci_worker_count" {
  description = "Number of TF-managed CI runner nodes to provision. DEFAULT 2 (raised from 1 in FuzeInfra#586: fuzeinfra-ci-runner-1 alone was saturated by ~22 ARC scale sets) → spins up fuzeinfra-ci-runner-1..2. The CI env pins TF_VAR_ci_worker_count to the same value. CI nodes are tainted fuzeinfra.io/ci=true:NoSchedule and labeled fuzeinfra.io/pool=ci at k3s agent registration (module cloud-init), so only ARC runner pods (nodeSelector fuzeinfra.io/pool=ci + matching toleration, see runners/arc/runner-scale-set-values.yaml) land there — every scale set schedules onto ANY node with that label, so no per-scale-set change is needed when this count grows."
  type        = number
  default     = 2

  validation {
    condition     = var.ci_worker_count >= 0
    error_message = "ci_worker_count must be >= 0."
  }
}

variable "ci_worker_product_id" {
  description = "Contabo product/plan SKU for ADDITIONAL CI runner nodes (index >= 1 in ci-workers.tf); the first node (fuzeinfra-ci-runner-1) stays on var.product_id so growing the pool never replaces it. Default V153 = the current 'Cloud VPS 4' catalog SKU (4 vCPU/8 GiB); the older V92 is retired and fails ordering with 'No offer was found for product ID V92'. Keep this in sync with helm/fuzeinfra/values-contabo.yaml's autoscaler productId."
  type        = string
  default     = "V153"
}

variable "ci_worker_region" {
  description = "Contabo region for CI runner nodes."
  type        = string
  default     = "EU"
}

variable "k3s_node_token" {
  description = "k3s node-token from the running server (/var/lib/rancher/k3s/server/node-token), used to join baseline worker nodes as k3s agents. Same secret already used by the infra-request-handler workflow (K3S_NODE_TOKEN) and modules/contabo-k3s-node — sourced from CI secrets / terraform.tfvars, never hardcoded."
  type        = string
  sensitive   = true
  default     = ""
}

variable "k3s_channel" {
  description = "k3s release channel/version pin for baseline worker nodes (INSTALL_K3S_CHANNEL). Pinned to v1.36 to match the running control-plane and prevent skew (FuzeInfra#318)."
  type        = string
  default     = "v1.36"
}

# ---------------------------------------------------------------------------
# Private networking (Contabo VPC) — control-plane attachment
#
# All OFF by default: enabling requires the per-instance VPC add-on to be
# bought manually in the Contabo panel first (HTTP 402 otherwise — see
# private-network.tf). Nothing here mutates prod until a human flips the gate
# AND supplies a concrete private IP, so k3s never gets `node-ip:`/`flannel-iface`
# pointed at an interface that isn't up.
# ---------------------------------------------------------------------------
variable "enable_private_network" {
  description = "Codify + attach the Contabo private network (net 60932, 10.0.0.0/22) to the control-plane VPS, and route k3s node/overlay traffic over eth1. OFF by default; requires the per-VPS VPC add-on purchased in the Contabo panel first (Terraform cannot buy it)."
  type        = bool
  default     = false
}

variable "private_network_name" {
  description = "Name of the Contabo private network to codify/import (the live net 60932 is named this). Used as the resource name; import with `terraform import contabo_private_network.prod[0] 60932`."
  type        = string
  default     = "FuzeInfra-prod"
}

variable "private_network_region" {
  description = "Contabo region locator for the private network. The live net 60932 lives in data center 'European Union 2' (region EU). CIDR (10.0.0.0/22) and data_center are Contabo-assigned read-only attributes and cannot be set here."
  type        = string
  default     = "EU"
}

# ---------------------------------------------------------------------------
# The numeric id of the live private network (60932).
#
# enable_private_network / private_network_name above govern the CONTROL-PLANE
# resource in private-network.tf. This variable is what worker/CI node sets are
# placed on: it is handed to modules/contabo-k3s-node as private_network_id,
# which turns on the eth1 / --node-ip / --flannel-iface / off-VLAN-quarantine
# half of that module's cloud-init WITHOUT declaring a network resource, so a
# plan can never detach a member Terraform did not author.
#
# Membership itself (and the paid per-instance add-on) is ordered out-of-band by
# the ca-private-net workflow: action=upgrade then action=assign. The assign may
# land after the node has booted and may reboot it; the node join is a systemd
# oneshot that re-runs every boot until it succeeds on the VLAN, so a late
# assign repairs the node with no further human step.
# ---------------------------------------------------------------------------
variable "node_private_network_id" {
  description = "Contabo private-network id (60932) that worker/CI nodes are placed on. 0 disables private networking for those node sets. Membership + the paid per-instance add-on are ordered out-of-band via the ca-private-net workflow."
  type        = number
  default     = 60932
}

variable "private_iface" {
  description = "Private NIC device name inside the VPS that the Contabo VPC attaches as (eth1 on a 2-NIC Ubuntu 24.04 image). Used for netplan bring-up and k3s --flannel-iface."
  type        = string
  default     = "eth1"
}

variable "private_node_ip" {
  description = "Static private IPv4 of the control-plane node within the private network CIDR (10.0.0.0/22), e.g. 10.0.0.10. Required when enable_private_network is true — used for k3s node-ip and the private tls-san. Empty leaves the k3s private-network config inert even if enable_private_network is true."
  type        = string
  default     = ""
}

variable "enable_argocd_provisioner" {
  description = <<-EOT
    Run null_resource.argocd_sync, which SSHes to the server (using
    ssh_private_key_path) to re-apply the ArgoCD Application/Project/SealedSecrets
    manifests. OFF by default: it requires a local SSH private key FILE that does
    not exist on CI runners (the merge-to-apply CD), so it breaks CI applies.
    Ongoing reconciliation is handled by ArgoCD selfHeal, and one-time argo
    registration by the argocd-register.yml workflow. Enable locally (in
    terraform.tfvars) only if you want terraform to push argo manifests via SSH.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Contabo Object Storage (S3) — see object-storage.tf and
# docs/design/s3-and-private-networking.md.
#
# PAID: enabling this PURCHASES storage (~EUR 6.99/mo per 1 TB). Default OFF so
# a routine apply never buys storage. The S3 access key / secret are NOT
# produced by any resource here — they are account-level credentials fetched
# once from the Contabo panel and delivered as an offline-sealed SealedSecret
# (deploy/sealed-secrets/loki-s3-credentials.yaml.template). Never commit real
# keys or put them in tfvars.
# ---------------------------------------------------------------------------
variable "enable_object_storage" {
  description = "Provision Contabo Object Storage + buckets (PAID). Enabled 2026-07-29 so DB backups can be offloaded OFF-cluster: the in-cluster PVC sink was destroyed when the durable nodes' Longhorn disks were re-initialised, taking every stored dump with it. Backups kept on the same storage they protect against are not backups."
  type        = bool
  default     = true
}

variable "object_storage_region" {
  description = "Contabo Object Storage region. 'EU' -> eu2.contabostorage.com (co-located with the EU2 prod node). Other values: 'US-central', 'SIN'."
  type        = string
  default     = "EU"

  validation {
    condition     = contains(["EU", "US-central", "SIN"], var.object_storage_region)
    error_message = "object_storage_region must be one of: EU, US-central, SIN."
  }
}

variable "object_storage_purchased_tb" {
  description = "Purchased quota in TB. Smallest tier is ~0.25 TB (250 GB); confirm the provider/account accepts sub-1TB in your region."
  type        = number
  default     = 0.25

  validation {
    condition     = var.object_storage_purchased_tb > 0
    error_message = "object_storage_purchased_tb must be greater than 0."
  }
}

variable "object_storage_autoscaling_limit_tb" {
  description = "If > 0, enable auto-scaling of purchased quota up to this hard ceiling (TB) so a log/backup spike never fails writes while capping the bill. 0 disables auto-scaling."
  type        = number
  default     = 0

  validation {
    condition     = var.object_storage_autoscaling_limit_tb >= 0
    error_message = "object_storage_autoscaling_limit_tb must be >= 0 (0 disables auto-scaling)."
  }
}

variable "object_storage_display_name" {
  description = "Display name for the Object Storage tenant in the Contabo panel."
  type        = string
  default     = "fuzeinfra-storage"
}

variable "object_storage_bucket_loki" {
  description = "Bucket name for Loki log chunks (native S3 object-store backend)."
  type        = string
  default     = "fuzeinfra-loki"
}

variable "object_storage_bucket_backups" {
  description = "Bucket name for scheduled DB dump/snapshot backups (CronJob offload)."
  type        = string
  default     = "fuzeinfra-backups"
}

variable "object_storage_bucket_blobs" {
  description = "Bucket name for application blob/artifact storage."
  type        = string
  default     = "fuzeinfra-blobs"
}


# ---------------------------------------------------------------------------
# Durable storage nodes (durable-storage-nodes.tf)
#
# PAID. Each node is a billed Contabo VPS. Default OFF (count=0) so a
# routine apply never purchases a node. Set durable_node_count ≥ 1 only
# after verifying that durable_node_product_id names a SKU with ≥300 GiB
# root disk — the whole point is additional Longhorn disk capacity.
#
# Motivation: fuze-core-3 was at 139% Longhorn overcommit (201 GiB
# scheduled / 154.9 GiB raw disk) as of 2026-10-13.
# ---------------------------------------------------------------------------
variable "durable_node_count" {
  description = "Number of dedicated durable storage nodes to provision (fuze-durable-storage-N). Default 0 = none. Set to ≥1 to relieve Longhorn disk overcommitment on the control-plane nodes. Each node is a billed Contabo VPS — confirm durable_node_product_id has ≥300 GiB disk first."
  type        = number
  default     = 0

  validation {
    condition     = var.durable_node_count >= 0
    error_message = "durable_node_count must be >= 0."
  }
}

variable "durable_node_product_id" {
  description = <<-EOT
    Contabo product/plan SKU for durable storage nodes. MUST be a SKU with
    ≥300 GiB root disk to meaningfully expand Longhorn's capacity. Verify via
    the Contabo panel or GET /v1/compute/instances/product-pricing before applying.

    Approximate guidance for EU region (confirm against live catalog — Contabo
    retires and re-IDs SKUs frequently):
      V45  / V46  — older Cloud VPS M/L family, typically 200–400 GiB
      V153        — Cloud VPS 4, 4 vCPU/8 GiB (used for CI + elastic nodes;
                    verify disk size before using here)
    A Contabo "Cloud VPS" with 400 GiB NVMe is the recommended floor.

    Empty string is valid only when durable_node_count = 0.
  EOT
  type        = string
  default     = ""
}

variable "durable_node_region" {
  description = "Contabo region for durable storage nodes. Should match the control-plane region (EU = European Union 2 data-centre, co-located with VLAN 60932)."
  type        = string
  default     = "EU"
}

variable "manage_control_plane_config" {
  description = "Manage /etc/rancher/k3s/config.yaml on ALL control planes (control-planes.tf). DEFAULT FALSE: applying it rewrites the config and RESTARTS k3s on each control plane in turn — a live HA operation. Flip only for a deliberate, supervised run. Requires the SSH private key, so it runs from a workstation, not CD."
  type        = bool
  default     = false
}
