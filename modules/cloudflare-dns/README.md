# `cloudflare-dns` module

Generic, consumer-agnostic Cloudflare DNS for products on their **own** domain
(a separate CF zone from `fuzefront.com`). A product declares its hosts in its
own `deploy/terraform/`; **FuzeInfra applies it** via the same dispatch path as
node provisioning (`infra-dispatch.yml` → `infra-request-handler.yml`). FuzeInfra
holds the Cloudflare token + Terraform state; the consumer holds neither.

Each host becomes a **proxied CNAME** to the shared Cloudflare Tunnel entrypoint,
so traffic flows: Cloudflare edge (TLS) → tunnel → Traefik → Ingress (host-routed).
No product hostnames are hardcoded in FuzeInfra — the caller supplies everything.

## Usage (from a consumer's `deploy/terraform/`)

```hcl
variable "cloudflare_dns_token" {
  type      = string
  sensitive = true
  default   = "" # injected at apply time by FuzeInfra's handler
}

provider "cloudflare" {
  api_token = var.cloudflare_dns_token
}

module "dns" {
  source          = "git::https://github.com/FuzeOne/FuzeInfra.git//modules/cloudflare-dns?ref=main"
  domain          = "mendysrobotics.com"
  zone_id         = "3784bf88809ac135888856ab6183db00" # public identifier
  tunnel_hostname = "prod.fuzefront.com"
  hosts           = ["live", "marketplace", "wp", "api.live"]
}
```

The consumer's `versions.tf` must also require the `cloudflare` provider (so the
provider block above resolves). FuzeInfra's handler injects `cloudflare_dns_token`
from its `CLOUDFLARE_API_TOKEN` secret, which must have `DNS:Edit` on every
family zone it applies (add the product's zone before first apply).

## Requirements
- `CLOUDFLARE_API_TOKEN` (FuzeInfra secret) scoped `DNS:Edit` on the product zone.
- `zone_id` is a public identifier — safe to commit. Apex/www stay out of `hosts`.
