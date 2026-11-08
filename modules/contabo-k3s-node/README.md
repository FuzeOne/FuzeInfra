# Module: `contabo-k3s-node`

Provisions one or more **Contabo VPS** instances and joins each to the existing
FuzeInfra **k3s** cluster as an **agent** (worker) node via cloud-init — no
manual SSH, no post-apply provisioner. It is the FuzeInfra-owned module that
consumer repos reference from `deploy/terraform/node-request.tf` and that the
[`infra-request-handler`](../../docs/INFRA_REQUEST_DISPATCH.md) workflow applies
with FuzeInfra's credentials.

> **Credentials live only in FuzeInfra CI.** A consumer repo references this
> module and declares *what* it wants (`requests`); it never holds the Contabo
> creds or the k3s node-token. Those are injected as variables by the handler.

## What it does, per request

1. Creates a `contabo_instance` (`product_id`, `region`, `image_id`).
2. Injects the SSH public key via cloud-init for break-glass access.
3. cloud-init runs the k3s installer in **agent** mode against
   `k3s_server_url` / `k3s_node_token` and labels the node
   `node-role=<role>` (plus `fuzeinfra.io/role=<role>` and any extra `labels`).
4. Optionally attaches every node to a `contabo_private_network`.

## Usage (consumer `deploy/terraform/node-request.tf`)

```hcl
module "nodes" {
  source = "git::https://github.com/FuzeOne/FuzeInfra.git//modules/contabo-k3s-node?ref=main"

  # Declared by the consumer — the "what":
  requests = [
    {
      name       = "fuzefront-worker-1"
      product_id = "V45"          # Contabo plan; must be on the handler whitelist
      region     = "EU"
      role       = "workload"
      labels     = { "app" = "fuzefront" }
    },
  ]

  # Injected by the FuzeInfra handler from CI secrets — the "how" (do NOT set
  # these in the consumer repo; leave them to the handler / a *.auto.tfvars it writes):
  contabo_client_id     = var.contabo_client_id
  contabo_client_secret = var.contabo_client_secret
  contabo_api_user      = var.contabo_api_user
  contabo_api_password  = var.contabo_api_password
  k3s_server_url        = var.k3s_server_url
  k3s_node_token        = var.k3s_node_token
  image_id              = var.image_id
  ssh_public_key        = var.ssh_public_key
}
```

## Inputs

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `requests` | `list(object)` | — | Nodes to reconcile: `{ name, product_id, region?, role?, labels? }` |
| `contabo_client_id` | `string` (sensitive) | — | Contabo OAuth2 client ID |
| `contabo_client_secret` | `string` (sensitive) | — | Contabo OAuth2 client secret |
| `contabo_api_user` | `string` (sensitive) | — | Contabo account email |
| `contabo_api_password` | `string` (sensitive) | — | Contabo account password |
| `k3s_server_url` | `string` | — | Existing k3s server URL, e.g. `https://<ip>:6443` |
| `k3s_node_token` | `string` (sensitive) | — | k3s node-token from the server |
| `image_id` | `string` | — | Contabo OS image UUID (Ubuntu 24.04 LTS) |
| `ssh_public_key` | `string` | — | SSH public key injected via cloud-init |
| `k3s_channel` | `string` | `stable` | k3s channel/version pin (`INSTALL_K3S_CHANNEL`) |
| `private_network_name` | `string` | `""` | Private network to attach nodes to (empty = disabled) |
| `private_network_region` | `string` | `EU` | Region for the private network |

## Outputs

| Name | Description |
|------|-------------|
| `nodes` | Map of `name → { instance_id, ipv4 }` |
| `node_ids` | List of Contabo instance IDs |
| `private_network_id` | Private network ID, or `null` when disabled |

## Notes

- **Firewall:** cloud-init opens `22/tcp`, `10250/tcp`, and `8472/udp` (Flannel
  VXLAN overlay). For nodes communicating over public IPs, prefer the WireGuard
  Flannel backend — see [`K3S_SECOND_NODE_RUNBOOK.md`](../../docs/K3S_SECOND_NODE_RUNBOOK.md).
- **Stateful workloads** must be pinned to the node holding their `local-path`
  data; new agent nodes are for stateless capacity (same runbook, §4).
- **`ignore_changes = [user_data]`** prevents a node from being recreated when
  the cloud-init template is re-rendered — cloud-init only runs on first boot.
- **k3s version skew:** pin `k3s_channel` to the server's version; an agent more
  than one minor ahead/behind the server is unsupported.
