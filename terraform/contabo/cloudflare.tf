# ---------------------------------------------------------------------------
# Cloudflare Named Tunnel + Zero Trust Access
#
# All resources are conditional on cloudflare_api_token being set.
# When the token is provided, a single `terraform apply` in this directory:
#   1. Creates the Named Tunnel in Cloudflare Zero Trust
#   2. Configures routing rules (hostname → in-cluster service)
#   3. Creates DNS records: prod.fuzefront.com + *.prod.fuzefront.com
#   4. Creates a Cloudflare Access app protecting *.prod.fuzefront.com
#   5. Patches the cluster secret so cloudflared can connect immediately
#
# Set these in terraform.tfvars (never commit them):
#   cloudflare_api_token    = "..."
#   cloudflare_account_id   = "..."
#   cloudflare_zone_id      = "..."
# ---------------------------------------------------------------------------

locals {
  cloudflare_enabled = var.cloudflare_api_token != ""

  # Gate for the Google Access IdP and the two policies that reference it. Both
  # halves of the credential must be present: a half-wired IdP plans a DESTROY of
  # the IdP AND its policies, which silently drops the admin plane to email-OTP
  # only rather than failing loudly.
  google_idp_enabled = var.cloudflare_api_token != "" && var.google_access_client_id != "" && var.google_access_client_secret != ""
  prod_domain        = "${var.prod_subdomain}.${var.zone_name}"

  # Public vanity hosts served directly by the FuzeFront platform.
  # These live at the apex zone (e.g. app.fuzefront.com), OUTSIDE the
  # *.prod.fuzefront.com Access wildcard, so they are public by default —
  # Authentik handles platform auth, not Cloudflare Access. The FuzeFront
  # chart sets its Traefik Ingress host to match (className traefik, TLS off,
  # CF terminates edge TLS). Adding a future public host is a one-line edit.
  #
  # NOTE: `auth` (auth.fuzefront.com) is deliberately absent and must STAY absent.
  # The IdP is no longer published: Authentik is served at authentik.<prod domain>
  # INSIDE the *.prod Access wall (var.authentik_host), reachable only after a
  # Google login, with just its OIDC protocol paths bypassed — see
  # cloudflare_zero_trust_access_application.authentik_oidc_endpoints.
  #
  # Re-adding `auth` here would republish the IdP to the internet. It was possible
  # to remove only because Access stopped using Authentik as its identity provider;
  # putting that back would reintroduce the circular dependency.
  #
  # Caveat worth knowing: removing the record does not by itself make the host
  # unreachable. The zone has a wildcard, so auth.fuzefront.com continued to
  # resolve to the Cloudflare edge after cloudflare_record.vanity["auth"] was
  # destroyed (verified: an arbitrary name under the zone resolves to the same
  # anycast IPs). Reachability of the IdP is therefore governed by the Access apps
  # and by FuzeFront's Ingress hostnames, not by the presence of this record.
  #
  # `plan` = FuzePlan (plan.fuzefront.com). FuzeFront's portal loads FuzePlan's
  # module-federation remoteEntry.js from here, so it must be public (outside the
  # *.prod Access wildcard); FuzePlan declares its own Traefik Ingress for this
  # host in FuzeOne/FuzePlan. Routing to Traefik is via the catch-all ingress
  # rule below — no per-host tunnel rule is needed.
  public_vanity_hosts = ["app", "plan", "fuzehub"]
}

# 32-byte cryptographically random tunnel secret
resource "random_bytes" "tunnel_secret" {
  count  = local.cloudflare_enabled ? 1 : 0
  length = 32
}

# Named Tunnel (shows in Zero Trust dashboard as var.tunnel_name)
resource "cloudflare_zero_trust_tunnel_cloudflared" "fuzeinfra" {
  count      = local.cloudflare_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = var.tunnel_name
  secret     = random_bytes.tunnel_secret[0].base64

}

# Routing rules — managed on Cloudflare's side, fetched by cloudflared at startup.
# cloudflared runs as a pod inside k3s, so it can reach any k8s service by DNS.
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fuzeinfra" {
  count      = local.cloudflare_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].id

  config {
    # ArgoCD is in the 'argocd' namespace — route directly, bypassing Traefik.
    ingress_rule {
      hostname = "argocd.${local.prod_domain}"
      service  = "http://argocd-server.argocd:80"
    }
    # BREAK-GLASS k3s API route (var.api_breakglass_enabled). A raw-TCP tunnel to
    # the IN-CLUSTER apiserver ClusterIP (kubernetes.default.svc:443), which is
    # itself HA — kube-proxy load-balances it across all healthy apiservers, so
    # this survives the loss of any one control plane WITHOUT the floating VIP.
    # It is reached with a client-side `cloudflared access tcp` + the Access
    # service token below (Zero Trust, NOT Spectrum). MUST come before the
    # catch-all (first match wins; the catch-all has no hostname). See the runbook.
    dynamic "ingress_rule" {
      for_each = var.api_breakglass_enabled ? [1] : []
      content {
        hostname = "k8s-api.${local.prod_domain}"
        service  = "tcp://kubernetes.default.svc.cluster.local:443"
      }
    }
    # Generic catch-all: every other hostname �� Traefik, which host-routes by
    # Ingress. This is domain-agnostic: any product on its own domain (its own
    # apex/subdomains) just CNAMEs that host to THIS tunnel and declares a Traefik
    # Ingress in its OWN repo ��� no per-product hostname is enumerated here.
    # Traefik returns 404 for unconfigured hosts, so this exposes nothing new.
    # CF Access still gates *.prod.fuzefront.com at the edge (see below); other
    # domains are public by default (Authentik / the app owns their auth).
    ingress_rule {
      service = "http://traefik.kube-system:80"
    }
  }
}

# DNS: prod.fuzefront.com → tunnel CNAME (proxied through Cloudflare)
resource "cloudflare_record" "prod_apex" {
  count   = local.cloudflare_enabled ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = var.prod_subdomain
  value   = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type    = "CNAME"
  proxied = true
  ttl     = 1
}

# DNS: *.prod.fuzefront.com → same tunnel
resource "cloudflare_record" "prod_wildcard" {
  count   = local.cloudflare_enabled ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "*.${var.prod_subdomain}"
  value   = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type    = "CNAME"
  proxied = true
  ttl     = 1
}

# DNS: public vanity hosts (app.fuzefront.com) → same tunnel, proxied.
# Proxied so CF terminates TLS at the edge (Universal SSL covers the apex hosts)
# and the request reaches cloudflared → the matching ingress_rule above → Traefik.
# These hosts are NOT covered by the *.prod Access wildcard, so they are public.
resource "cloudflare_record" "vanity" {
  for_each = nonsensitive(local.cloudflare_enabled) ? toset(local.public_vanity_hosts) : toset([])
  zone_id  = var.cloudflare_zone_id
  name     = each.value
  value    = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type     = "CNAME"
  proxied  = true
  ttl      = 1
}

# DNS: fuzefront.com apex → tunnel CNAME (public marketing/product website).
# The vanity for_each above handles subdomains (app, plan, fuzehub); the apex
# needs its own resource because name = "@" cannot be expressed in that loop.
# Traffic reaches Traefik via the tunnel catch-all ingress_rule, then the
# fuzefront-website Ingress (host: fuzefront.com) in the FuzeFront Helm chart.
resource "cloudflare_record" "website_apex" {
  count           = local.cloudflare_enabled ? 1 : 0
  zone_id         = var.cloudflare_zone_id
  name            = "@"
  value           = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type            = "CNAME"
  proxied         = true
  ttl             = 1
  allow_overwrite = true
}

# DNS: www.fuzefront.com → the SAME tunnel as the apex (FuzeInfra#1278).
#
# Until this record existed, `www` was an UNPROXIED CNAME to
# d27hi5funhy3o7.cloudfront.net — an AWS CloudFront/S3 distribution defined in
# neither this repo nor FuzeFront — so fuzefront.com and www.fuzefront.com
# served two DIFFERENT BRANDS: the apex served the FuzeOne marketing site
# through the tunnel, while `www` served a stale FuzeHub landing page
# (last-modified 2026-07-02) that bypassed the tunnel and the cluster entirely.
# A visitor reached a different company depending on three letters.
#
# Pointing `www` at the tunnel sends it to Traefik via the catch-all
# ingress_rule above, where the fuzefront-website Ingress claims BOTH hosts and
# its nginx returns `301 https://fuzefront.com$request_uri` for the `www` Host —
# so the apex is the single canonical origin (FuzeOne/FuzeFront#1201).
#
# ORDERING — the host must be CLAIMED before it is ROUTED. Traefik answers 404
# for a host no Ingress claims, so flipping this record while FuzeFront's
# Ingress lacked the `www` rule would have turned a working host into a live
# 404. Verified present in the running cluster before this landed:
#   kubectl -n fuzefront get ingress fuzefront-website
#     -o custom-columns=NAME:.metadata.name,HOSTS:.spec.rules[*].host
#   -> fuzefront-website   fuzefront.com,www.fuzefront.com
#
# Not part of the vanity for_each above, for the same reason as the apex: this
# REPLACES a pre-existing record that Terraform does not own, so it needs
# `allow_overwrite = true`. Adding `www` to public_vanity_hosts instead would
# plan cleanly and then fail at apply on the duplicate CloudFront CNAME.
#
# The CloudFront distribution itself is deliberately untouched — it is owned by
# neither this repo nor FuzeFront. Once this record moves, `www.fuzefront.com`
# stops reaching it regardless of what it still serves; retiring or repointing
# it is a follow-up for whoever owns it.
resource "cloudflare_record" "website_www" {
  count           = local.cloudflare_enabled ? 1 : 0
  zone_id         = var.cloudflare_zone_id
  name            = "www"
  value           = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type            = "CNAME"
  proxied         = true
  ttl             = 1
  allow_overwrite = true
  comment         = "Canonical-host redirect to the fuzefront.com apex (FuzeInfra#1278)."
}

# ---------------------------------------------------------------------------
# Multi-tenant portal DNS + TLS (FuzeFront EPIC-16)
#
# Three addressing modes are served here:
#   (a) tenant subdomain  corpabc.fuzefront.com   -> the wildcard below
#   (b) path prefix       app.fuzefront.com/p/... -> no infra change (the `app`
#                                                    vanity host already exists)
#   (c) customer domain   app.corpabc.com         -> Cloudflare for SaaS, driven
#                                                    at runtime by the in-cluster
#                                                    custom-hostname API
#
# (a) WILDCARD DNS + TLS.
# `*.fuzefront.com` is a proxied CNAME to the same tunnel as everything else, so
# a tenant subdomain reaches cloudflared -> the catch-all ingress_rule -> Traefik
# -> whichever Ingress claims the host. Reserved hosts need no exclusion list:
# DNS resolves the most specific match first, so the explicit `app`, `auth`,
# `plan`, `prod` records and the `*.prod` wildcard all take precedence over `*`
# automatically. Adding a reserved host later means adding a record, not editing
# an exclusion.
#
# THAT IS A DNS-LAYER STATEMENT AND IT DOES NOT EXTEND TO INGRESS ROUTING.
# Cloudflare resolves plan.fuzefront.com to its own record, but the request still
# lands on Traefik, which sorts routers by RULE LENGTH, not host specificity — so
# a consumer's `*.fuzefront.com` Ingress rule outranks another product's exact
# `plan.fuzefront.com` rule and silently captures it. That took FuzePlan down
# once (FuzeFront#431, reverted #437). Any consumer adding a wildcard host must
# isolate it in its own Ingress with
# `traefik.ingress.kubernetes.io/router.priority: "1"`.
# See docs/consuming-repos/CUSTOM_DOMAINS.md §3.
#
# PRE-EXISTING RECORDS — verified by resolution on 2026-07-29:
#
#     connect.fuzefront.com        -> 104.21.14.243   (Cloudflare)
#     saas-origin.fuzefront.com    -> 104.21.14.243   (Cloudflare)
#     tenant-probe.fuzefront.com   -> 172.67.160.205  (served by the wildcard)
#
# All three multi-tenant records already resolve, so NONE of these resources
# gates whether the hostnames work. They already do; the consumer's Ingress rule
# is the only gate on SERVING (see docs/consuming-repos/CUSTOM_DOMAINS.md §1).
#
# Whether Terraform OWNS them is a separate question that only a plan answers.
# Read this PR's plan output before merging:
#
#   * "No changes"        -> state is in sync; nothing to do.
#   * "N to add"          -> the records exist OUTSIDE state. The apply will FAIL
#                            on duplicates rather than adopting them. Import each
#                            one first:
#         terraform import 'cloudflare_record.tenant_wildcard[0]' '<zone_id>/<record_id>'
#         terraform import 'cloudflare_record.saas_connect[0]'    '<zone_id>/<record_id>'
#         terraform import 'cloudflare_record.saas_origin[0]'     '<zone_id>/<record_id>'
#   * any DESTROY on these -> STOP. That would replace a record currently serving
#                            production traffic.
#
# The zone-level Cloudflare for SaaS fallback origin
# (cloudflare_custom_hostname_fallback_origin.saas) is NOT a DNS record, so its
# presence cannot be confirmed by resolution — the plan is the only evidence.
# Without it, Cloudflare refuses to activate any custom hostname, so a missing
# fallback origin fails in front of a customer rather than in CI.
#
# TLS is Cloudflare-terminated. Universal SSL already covers the apex and the
# first-level wildcard `*.fuzefront.com` on every plan, so tenant subdomains get
# a valid certificate with no cert-manager, no DNS-01 solver, and no new secret.
# That is also the only option that preserves the tunnel-only invariant: an ACME
# HTTP-01/TLS-ALPN solver needs a publicly reachable origin on :80/:443, which is
# exactly what pinning Traefik to ClusterIP exists to prevent. Note the one-level
# limit — `a.b.fuzefront.com` is NOT covered by Universal SSL and would need
# Advanced Certificate Manager.
# ---------------------------------------------------------------------------
resource "cloudflare_record" "tenant_wildcard" {
  count   = local.cloudflare_enabled && var.tenant_wildcard_enabled ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "*"
  value   = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type    = "CNAME"
  proxied = true
  ttl     = 1
  comment = "Multi-tenant portal subdomains (*.fuzefront.com). More-specific records win."
}

# ---------------------------------------------------------------------------
# (c) CUSTOM CUSTOMER DOMAINS — Cloudflare for SaaS
#
# Two records and one zone-level setting are all the STATIC infrastructure this
# needs. Individual customer domains are never enumerated here: they are created
# at runtime by the custom-hostname API (helm/fuzeinfra/templates/
# custom-hostname-api.yaml), which is the whole point — a Helm release per
# customer domain is not a thing we are willing to have.
#
#   connect.<zone>     the hostname customers CNAME their domain to. This is a
#                      published, customer-facing contract, so it is deliberately
#                      separate from the origin below — the origin can be
#                      repointed during a migration without asking every
#                      customer to change their DNS.
#   saas-origin.<zone> the Cloudflare for SaaS fallback origin: where the edge
#                      sends custom-hostname traffic once TLS is terminated.
#                      Proxied, so it resolves through Cloudflare to the tunnel.
#
# Traffic path once a hostname is active:
#   browser --TLS(app.corpabc.com)--> CF edge (custom hostname cert, Host kept)
#     --> saas-origin.<zone> --> cloudflared --> traefik.kube-system:80
#     --> the Ingress the custom-hostname API materialized --> consumer Service
#
# COST/QUOTA: Free/Pro/Business include 100 custom hostnames; each additional one
# is $0.10/month, to a ceiling of 50,000. The API enforces its own soft cap
# (customHostnameApi.maxCustomHostnames) so nobody walks into overage by accident.
# ---------------------------------------------------------------------------
resource "cloudflare_record" "saas_connect" {
  count   = local.cloudflare_enabled && var.saas_custom_hostnames_enabled ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "connect"
  value   = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type    = "CNAME"
  proxied = true
  ttl     = 1
  comment = "Published CNAME target for customer-owned domains (Cloudflare for SaaS)."
}

resource "cloudflare_record" "saas_origin" {
  count   = local.cloudflare_enabled && var.saas_custom_hostnames_enabled ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "saas-origin"
  value   = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type    = "CNAME"
  proxied = true
  ttl     = 1
  comment = "Cloudflare for SaaS fallback origin -> tunnel -> Traefik."
}

# Enabling the fallback origin activates Cloudflare for SaaS on the zone (SSL for SaaS must be enabled first).
# It must point at a record that already exists and is proxied, hence depends_on.
# Requires "Zone / Custom Hostnames: Edit" on the Cloudflare API token.
resource "cloudflare_custom_hostname_fallback_origin" "saas" {
  count      = local.cloudflare_enabled && var.saas_custom_hostnames_enabled ? 1 : 0
  zone_id    = var.cloudflare_zone_id
  origin     = "saas-origin.${var.zone_name}"
  depends_on = [cloudflare_record.saas_origin]
}

# ---------------------------------------------------------------------------
# Public consumer-app host registry (labels under ${var.prod_subdomain}).
#
# GOVERNANCE (docs/CONSUMER_ONBOARDING_SHARED_CLUSTER.md §1): FuzeInfra never
# hard-codes product-specific RESOURCES. Products with their own Cloudflare zone
# own their DNS via modules/cloudflare-dns from their own repo. But labels under
# the shared prod zone are different: they fall UNDER the *.prod wildcard OTP
# Access wall defined below, and a consumer must NOT be able to punch a bypass
# hole in FuzeInfra's wall from its own repo. So for shared-zone hosts FuzeInfra
# holds a DATA registry only (label => owning repo) — generic resources fan out
# from it. The registry data lives OUTSIDE the bare setup, in
# materialized/consumers.tfvars (generated from consumer-repo declarations);
# onboarding a product's public host adds an entry there, never in *.tf.
#
# Each entry gets: (a) a proxied CNAME to the shared tunnel (cloudflared
# catch-all → Traefik → the consumer's namespace Ingress; ClusterIP, HTTP-only,
# CF terminates edge TLS — tunnel-only invariant preserved), and (b) a
# more-specific bypass Access app exempting it from the OTP wildcard (same
# precedence trick as sealed_secrets_cert) because the app owns its own auth.
# Admin UIs must NOT go in this registry — they get gated Access apps below.
# ---------------------------------------------------------------------------
# BARE SETUP INVARIANT: this variable defaults to EMPTY and no consumer entry
# may ever be inlined here. Consumer hosts are materialized into
# materialized/consumers.tfvars (generated data, loaded via -var-file in CI) so
# the infra-authored *.tf tree stays consumer-free at all times.
variable "public_app_hosts" {
  description = "Public consumer-app host registry: label (relative to prod subdomain) => owning repo. Populated ONLY via materialized/consumers.tfvars."
  type        = map(string)
  default     = {}
}

resource "cloudflare_record" "public_app" {
  for_each = nonsensitive(local.cloudflare_enabled) ? var.public_app_hosts : {}
  zone_id  = var.cloudflare_zone_id
  name     = "${each.key}.${var.prod_subdomain}"
  value    = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type     = "CNAME"
  proxied  = true
  ttl      = 1
}

resource "cloudflare_zero_trust_access_application" "public_app" {
  for_each             = nonsensitive(local.cloudflare_enabled) ? var.public_app_hosts : {}
  account_id           = var.cloudflare_account_id
  name                 = "Public app ${each.key} (${each.value})"
  domain               = "${each.key}.${local.prod_domain}"
  type                 = "self_hosted"
  session_duration     = "0s"
  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "public_app_bypass" {
  for_each       = nonsensitive(local.cloudflare_enabled) ? var.public_app_hosts : {}
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.public_app[each.key].id
  name           = "Bypass — ${each.key} (${each.value} owns auth)"
  precedence     = 1
  decision       = "bypass"

  include {
    everyone = true
  }
}

# ---------------------------------------------------------------------------
# Google as the Cloudflare Access identity provider.
#
# Google is the ROOT credential for the entire admin plane. It authenticates you
# to Cloudflare Access (this resource), and SEPARATELY it authenticates you into
# Authentik, which in turn authenticates every internal service that speaks OIDC.
# One human credential; two independent consumers of it.
#
# WHY NOT AUTHENTIK HERE — it used to be, and it was backwards:
# Authentik runs INSIDE the cluster this wall protects, on FuzeInfra's own
# Postgres and Redis. Using it as the Access IdP made the network gate depend on
# the very thing it gates, and it forced auth.fuzefront.com to be published to
# the internet, because an IdP sitting behind the wall it authenticates cannot be
# reached in order to log in. Google is external, so the wall no longer depends
# on the cluster at all — and that is precisely what lets Authentik move BEHIND
# the wall (see local.public_vanity_hosts and var.authentik_host).
#
# TOKEN SCOPE: needs "Access: Organizations, Identity Providers, and Groups >
# Edit" on the Cloudflare API token, on top of every other scope this file needs.
# A missing Access scope plans CLEAN and fails only at apply, with a generic
# "Authentication error (10000)" — see docs/TERRAFORM_CD.md.
#
# The `!= ""` guard follows the crit_bridge_token convention: a count-gated
# resource whose secret is unwired plans a DESTROY, not a no-op. Wire both
# TF_VAR_google_access_client_id and _secret in the CD workflow BEFORE merging,
# or the apply silently drops the whole admin plane to email-OTP only.
resource "cloudflare_zero_trust_access_identity_provider" "google" {
  count      = local.google_idp_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Google"
  type       = "google"

  config {
    client_id     = var.google_access_client_id
    client_secret = var.google_access_client_secret
  }
}

# Cloudflare Access: protect *.prod.fuzefront.com.
# The apex prod.fuzefront.com is NOT matched by *.prod — it stays public.
resource "cloudflare_zero_trust_access_application" "admin_services" {
  count            = local.cloudflare_enabled ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "FuzeInfra Admin Services"
  domain           = "*.${local.prod_domain}"
  type             = "self_hosted"
  session_duration = var.access_session_duration

  app_launcher_visible = false
}

# Preferred login path: Google.
#
# THE TWO-BLOCK SHAPE IS LOAD-BEARING — do not collapse it into one block.
#   include { login_method } = HOW you authenticated (necessary, NOT sufficient)
#   require { email }        = WHO you are           (the actual allowlist)
#
# `include` is OR-semantics; `require` is AND-semantics. Moving the email list
# into `include` would mean "authenticated via Google OR on the list" — i.e.
# every Google account on the internet gets in. `email_domain = ["gmail.com"]`
# would likewise admit all of Gmail, and `gsuite {}` matches Workspace GROUP
# addresses, which a consumer gmail.com identity has none of. `email` (or
# `email_list`) is the only primitive that expresses "these specific people".
#
# This carries more weight than it appears to. For most hosts behind this wall —
# Prometheus, Alertmanager, Mongo Express, ChromaDB, LiteLLM — Cloudflare Access
# is the ENTIRE authorization layer; there is no second gate underneath. And
# Authentik itself does not restrict enrollment, so any Google identity that
# reaches it is provisioned as a real user. This block is what stands between
# that and the internet.
resource "cloudflare_zero_trust_access_policy" "admin_google" {
  count          = local.google_idp_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.admin_services[0].id
  name           = "Admin via Google"
  precedence     = 1
  decision       = "allow"

  include {
    login_method = [cloudflare_zero_trust_access_identity_provider.google[0].id]
  }

  require {
    email = var.allowed_admin_emails
  }
}

# BREAK-GLASS — do not remove.
#
# Email OTP is the only login path that depends on neither Google nor this
# cluster. Google is external so it survives a cluster outage, but it is still a
# single third party that can lock you out (account lockout, consent revoked, a
# billing or policy action). OTP is delivered by Cloudflare itself, so it is
# independent of both.
#
# Kept at lower precedence so Google is tried first, and `allowed_idps` is
# deliberately NOT set on the applications so both methods stay selectable on the
# Access login page.
resource "cloudflare_zero_trust_access_policy" "admin_email_otp" {
  count          = local.cloudflare_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.admin_services[0].id
  name           = "Admin email allowlist (OTP) — break-glass"
  precedence     = 2
  decision       = "allow"

  include {
    email = var.allowed_admin_emails
  }
}

# Cloudflare App Launcher — the portal itself at <team>.cloudflareaccess.com
# Needs its own access application + policy or CF shows "contact your admin".
resource "cloudflare_zero_trust_access_application" "app_launcher" {
  count            = local.cloudflare_enabled ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "App Launcher"
  type             = "app_launcher"
  session_duration = var.access_session_duration

  # NOTE: app_launcher_visible is INERT on a type=app_launcher resource —
  # Cloudflare's API always stores it false and ignores writes, so the provider's
  # computed default (true) vs the API (false) is a perpetual no-op diff. Ignore it
  # (the API owns this field). The portal's TILES are the `launcher_bookmark`
  # bookmark apps below — those carry app_launcher_visible=true and it sticks there.
  lifecycle {
    ignore_changes = [app_launcher_visible]
  }
}

resource "cloudflare_zero_trust_access_policy" "app_launcher_google" {
  count          = local.google_idp_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.app_launcher[0].id
  name           = "Admin via Google"
  precedence     = 1
  decision       = "allow"

  include {
    login_method = [cloudflare_zero_trust_access_identity_provider.google[0].id]
  }

  require {
    email = var.allowed_admin_emails
  }
}

# Break-glass for the portal itself — see admin_email_otp above.
resource "cloudflare_zero_trust_access_policy" "app_launcher" {
  count          = local.cloudflare_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.app_launcher[0].id
  name           = "Admin email allowlist (OTP) — break-glass"
  precedence     = 2
  decision       = "allow"

  include {
    email = var.allowed_admin_emails
  }
}

# ---------------------------------------------------------------------------
# REMOVED (deliberate, do not re-add): public CF Access bypasses for
# neo4j.<prod>/browser and grafana.<prod>/public/build.
#
# Both were `decision = bypass` + `everyone = true`, i.e. genuinely unauthenticated
# public access to those paths, punched through the *.prod OTP wall. They were
# deleted out-of-band in Cloudflare; removing them from config here makes the
# config match that reality so `terraform apply` stops recreating them. This is a
# config-only change — it does not alter prod, which already has no such bypass.
#
# The problems they originally solved, and where they stand now:
#
#   Grafana /public/build/* cache misses — SOLVED WITHOUT A BYPASS by the
#   `grafana_asset_serve` Worker below, which strips the CF_Authorization cookie
#   pre-cache so authenticated users share one warm edge cache entry. Access stays
#   enforced. No regression from removing the bypass.
#
#   Neo4j Browser blank page — NOT solved by removing this. The failure is a CORS
#   error: the SPA's `<script type="module" crossorigin>` imports send no cookies,
#   so CF Access 302s them to cloudflareaccess.com. `neo4j_browser_cache` below is
#   a cache rule and does not address it. Accept that the public Browser UI is
#   unavailable and reach it via `kubectl port-forward` / WARP, which is the
#   security-correct answer — do NOT restore a public bypass to fix it.
#
# The sealed-secrets cert bypass below is intentionally KEPT: it serves only the
# Sealed Secrets *public* encryption certificate, which is safe to expose and must
# be fetchable offline by `scripts/seal-secret.sh` without cluster access.
# ---------------------------------------------------------------------------

# Sealed Secrets public cert bypass.
#
# sealed-secrets.prod.fuzefront.com/v1/cert.pem serves the Sealed Secrets
# controller's PUBLIC key. Consumers fetch it to seal secrets OFFLINE — they
# have no cluster access and no Cloudflare Access account, so this endpoint must
# be reachable by anyone (CI, scripts, developers). The cert is a public key:
# it can encrypt but never decrypt, so exposing it is safe by design.
#
# A more-specific hostname Access app takes precedence over the wildcard
# *.prod.fuzefront.com OTP app, so this bypass exempts ONLY the cert endpoint;
# the controller's private key and the rest of the cluster stay protected.
# See docs/SECRETS_MANAGEMENT.md.
resource "cloudflare_zero_trust_access_application" "sealed_secrets_cert" {
  count                = local.cloudflare_enabled ? 1 : 0
  account_id           = var.cloudflare_account_id
  name                 = "Sealed Secrets public cert (public)"
  domain               = "sealed-secrets.${local.prod_domain}"
  type                 = "self_hosted"
  session_duration     = "0s"
  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "sealed_secrets_cert_bypass" {
  count          = local.cloudflare_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.sealed_secrets_cert[0].id
  name           = "Bypass — Sealed Secrets public cert"
  precedence     = 1
  decision       = "bypass"

  include {
    everyone = true
  }
}

# ---------------------------------------------------------------------------
# The OIDC PROTOCOL surface of Authentik, and only that, bypasses Access.
#
# Authentik now lives at authentik.<prod domain>, inside the *.prod wildcard
# Access app — so by default EVERY path on it is gated, which was verified
# against prod: /application/o/token/ returned a 302 to the Access login page
# instead of JSON. That breaks OIDC, because OIDC is two protocols, not one:
#
#   FRONT channel  /application/o/authorize/   the BROWSER is redirected here
#   BACK  channel  /application/o/token/       the SERVICE calls this itself
#                  /application/o/<app>/jwks/
#                  /application/o/userinfo/
#                  .../.well-known/openid-configuration
#
# Back-channel calls come from Grafana, ArgoCD and Kafka UI processes. They carry
# no Access cookie and cannot obtain one, so Access turns every token exchange
# into an HTML redirect and login fails after the user has already authenticated.
#
# WHY BYPASSING THIS IS NOT A HOLE:
# These are the standard, deliberately-public endpoints of every IdP on the
# internet — Google's, Okta's, Auth0's token endpoints are all unauthenticated.
# Each is protected by its own credential, not by network position:
#   /token/     requires client_id + client_secret
#   /userinfo/  requires a bearer token
#   /jwks/      serves PUBLIC keys; publishing them is the point
#   /authorize/ hands back nothing without an authenticated Authentik session
#
# What stays behind the wall is everything that matters: the admin interface
# (/if/admin/), the flow/login UI (/flows/, /if/flow/), and the whole rest of the
# host. An attacker who reaches /authorize/ is bounced to the login flow, which
# is NOT bypassed, so they cannot authenticate. A legitimate admin already holds
# an Access cookie for *.prod from the Google login, so they pass into the flow
# UI silently — one visible login, not two.
#
# Scoped by PATH PREFIX. Cloudflare matches the most specific app by domain+path,
# so this takes precedence over the *.prod wildcard for /application/o only.
# Do NOT widen the path: dropping the /application/o suffix would publish the
# admin UI.
resource "cloudflare_zero_trust_access_application" "authentik_oidc_endpoints" {
  count                = local.cloudflare_enabled ? 1 : 0
  account_id           = var.cloudflare_account_id
  name                 = "Authentik OIDC protocol endpoints (public)"
  domain               = "${var.authentik_host}/application/o"
  type                 = "self_hosted"
  session_duration     = "0s"
  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "authentik_oidc_endpoints_bypass" {
  count          = local.cloudflare_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.authentik_oidc_endpoints[0].id
  name           = "Bypass — Authentik OIDC protocol endpoints"
  precedence     = 1
  decision       = "bypass"

  include {
    everyone = true
  }
}

# Cache static build assets at the CF edge.
#
# One zone-level ruleset per phase is the CF limit, so Neo4j and Grafana rules
# live in the same ruleset. Both targets are content-hashed (filename = hash),
# so a 1-year TTL is safe — a file never changes under its hash-addressed name.
#
# Without caching: ~8–10 concurrent JS/CSS requests on every Grafana dashboard
# load all hit the CF tunnel simultaneously. CF coalesces duplicate-URL requests
# into one upstream fetch; if that fetch returns 503, all waiters see 503.
# The tablePanel CSS chunk is requested during the burst and reliably 503s,
# producing "Error loading: table" on every dashboard open.
#
# With caching: first request per file hits the tunnel (single fetch); all
# subsequent requests are served from the CF edge �� no tunnel involved.
resource "cloudflare_ruleset" "neo4j_browser_cache" {
  count   = local.cloudflare_enabled ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "Neo4j Browser Asset Cache"
  kind    = "zone"
  phase   = "http_request_cache_settings"

  rules {
    action = "set_cache_settings"
    action_parameters {
      cache = true
      edge_ttl {
        mode    = "override_origin"
        default = 3600
      }
      browser_ttl {
        mode    = "override_origin"
        default = 3600
      }
    }
    expression  = "(http.host eq \"neo4j.${local.prod_domain}\" and starts_with(http.request.uri.path, \"/browser/assets/\"))"
    description = "Cache Neo4j Browser static assets 1h — overrides origin no-store to prevent 503 on burst preload requests"
    enabled     = true
  }

  rules {
    action = "set_cache_settings"
    action_parameters {
      cache = true
      edge_ttl {
        mode    = "override_origin"
        default = 31536000
      }
      browser_ttl {
        mode    = "override_origin"
        default = 31536000
      }
    }
    expression  = "(http.host eq \"grafana.${local.prod_domain}\" and starts_with(http.request.uri.path, \"/public/build/\"))"
    description = "Cache Grafana content-hashed build assets 1yr at CF edge — prevents 503 on concurrent tablePanel CSS load"
    enabled     = true
  }
}

# CF Worker: strip CF_Authorization cookie for Grafana /public/build/* static assets.
#
# Problem: the CF_Authorization cookie (set domain-wide by CF Access) is included in the
# cache key for every browser request. Even with a cache rule that forces caching, CF
# treats each unique cookie value as a separate cache entry — so every authenticated user's
# first page load hits the origin tunnel cold, which 503s under the ~8-request burst.
#
# Fix: Workers intercept requests BEFORE CF's cache. Stripping the auth cookie makes CF
# compute a cookie-free cache key → matches the shared HIT already warm for unauthenticated
# requests → served from edge without touching the tunnel.
#
# http_request_transform only allows URL rewrites (not header removal at pre-cache time).
# http_request_late_transform allows header removal but runs after cache — too late.
# Workers are the only free-plan mechanism that runs pre-cache with header mutation.
resource "cloudflare_worker_script" "grafana_asset_serve" {
  count      = local.cloudflare_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "grafana-asset-serve"
  content    = file("${path.module}/grafana-asset-serve.js")
  module     = true
}

resource "cloudflare_worker_route" "grafana_build_assets" {
  count       = local.cloudflare_enabled ? 1 : 0
  zone_id     = var.cloudflare_zone_id
  pattern     = "grafana.${local.prod_domain}/public/build/*"
  script_name = cloudflare_worker_script.grafana_asset_serve[0].name
}

# ---------------------------------------------------------------------------
# Cloudflare App Launcher bookmarks
#
# One tile per service. type = "bookmark" creates a clickable shortcut in the
# CF Access App Launcher — no extra access policy needed because the wildcard
# self_hosted app above already enforces OTP on every *.prod.fuzefront.com URL.
# ---------------------------------------------------------------------------
locals {
  launcher_services = {
    "argocd"        = { name = "ArgoCD", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/argo-cd.png", path = "" }
    "grafana"       = { name = "Grafana", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/grafana.png", path = "" }
    "prometheus"    = { name = "Prometheus", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/prometheus.png", path = "" }
    "alertmanager"  = { name = "Alertmanager", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/alertmanager.png", path = "" }
    "airflow"       = { name = "Airflow", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/apache-airflow.png", path = "" }
    "flower"        = { name = "Flower", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/celery.png", path = "" }
    "kafka-ui"      = { name = "Kafka UI", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/kafka.png", path = "" }
    "mongo-express" = { name = "Mongo Express", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/mongodb.png", path = "" }
    "rabbitmq"      = { name = "RabbitMQ", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/rabbitmq.png", path = "" }
    "neo4j"         = { name = "Neo4j", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/neo4j.png", path = "" }
    # elasticsearch and chromadb are deliberately NOT here (M3-S3). They are raw
    # datastore HTTP APIs, not admin UIs — Neo4j is the kept exception because
    # Neo4j Browser IS an admin UI and the owner decision keeps it launcher-only.
    # Removing these tiles also drops their hosts from local.launcher_hosts
    # below, which is what lets the duplicate-tile reconciler and Ingress
    # removal (helm/fuzeinfra/templates/ingress.yaml) stay in sync: no tile, no
    # published host, no dangling Access-app coverage to reconcile.
    # FuzeFront admin UIs (FuzeOne/FuzeFront). Unleash lands UNDER the *.prod
    # wildcard, so the catch-all tunnel rule → Traefik, the *.prod CNAME, and the
    # *.prod Access app already cover routing/DNS/gating — no per-host tunnel
    # rule, CNAME, or Access app is needed. These bookmarks just add the launcher
    # tiles. Traefik host-routes each to the FuzeFront-owned Ingress:
    #   unleash   → svc fuzefront-unleash:4242  (Ingress live, commit a2d0af5)
    #   authentik → svc authentik-server:9000   (Ingress live in ns fuzefront)
    # Authentik is the exception: its tile is overridden below to the public
    # auth.<zone> host rather than authentik.<prod>, because it is the IdP that
    # now backs the *.prod wall. See launcher_url_overrides.
    "unleash"   = { name = "Unleash", logo = "https://avatars.githubusercontent.com/u/23053233?s=200&v=4", path = "" }
    "authentik" = { name = "Authentik", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/authentik.png", path = "" }
    # LiteLLM gateway admin UI (helm/litellm, Ingress → svc litellm:4000).
    # Under the *.prod wildcard like the rest, so this bookmark is all that is
    # needed — no per-host tunnel rule, CNAME or Access app.
    #
    # path = "/ui" because "/" redirects to the console anyway and landing
    # straight on it saves a hop. NOTE the Ingress is deliberately scoped to "/",
    # not "/ui": the console loads its bundle from /litellm-asset-prefix/... and
    # calls the API on the same origin, so a /ui-only route would render a blank
    # page (the Neo4j Browser failure mode).
    #
    # LiteLLM's own console auth is its master key and its SSO is an enterprise
    # feature, so the Authentik-backed Access wall in front of this host is the
    # only identity layer it gets. That is the whole reason the wall must keep
    # working — see the Access policies above.
    "litellm" = { name = "LiteLLM", logo = "https://avatars.githubusercontent.com/u/121462774?s=200&v=4", path = "/ui" }
    # Headlamp read-only Kubernetes UI (argocd/applications/headlamp.yaml,
    # Ingress → svc headlamp:80 in ns headlamp). Under the *.prod wildcard, so
    # this bookmark is all that is needed. Like LiteLLM, Access is its only
    # identity layer: every viewer acts as a read-only ServiceAccount
    # (unsafeUseServiceAccountToken), which is why that SA must stay read-only.
    "headlamp" = { name = "Headlamp", logo = "https://raw.githubusercontent.com/kubernetes-sigs/headlamp/v0.45.0/docs/images/icon.png", path = "" }
    # In-cluster Harbor container registry UI (argocd/applications/harbor.yaml,
    # Ingress → svc harbor-portal:80 in ns harbor). Under the *.prod wildcard,
    # so this bookmark is all that is needed for the App Launcher tile.
    "harbor"   = { name = "Harbor Registry", logo = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/harbor.png", path = "" }
  }

  # Tiles whose target is NOT https://<key>.<prod_domain><path>.
  #
  # The Authentik tile needs an override only for its PATH (/if/admin/), not its
  # host — var.authentik_host is now authentik.<prod domain>, which is what the
  # default would already produce.
  #
  # It used to point at the public auth.fuzefront.com, because while Cloudflare
  # Access authenticated AGAINST Authentik, reaching the IdP admin behind the wall
  # would have required an Access session only the IdP could issue. Access now
  # authenticates with Google, so that circularity is gone and the admin UI is
  # correctly gated by the *.prod wildcard like every other admin surface.
  launcher_url_overrides = {
    "authentik" = "https://${var.authentik_host}/if/admin/"
  }
}

resource "cloudflare_zero_trust_access_application" "launcher_bookmark" {
  for_each   = nonsensitive(local.cloudflare_enabled) ? local.launcher_services : {}
  account_id = var.cloudflare_account_id
  name       = each.value.name
  domain = lookup(
    local.launcher_url_overrides,
    each.key,
    "https://${each.key}.${local.prod_domain}${each.value.path}"
  )
  type                 = "bookmark"
  app_launcher_visible = true
  logo_url             = each.value.logo
}

# ---------------------------------------------------------------------------
# DUPLICATE-TILE RECONCILER
#
# The launcher showed Unleash twice and Authentik twice — once from the bookmark
# above (correct logo) and once from a `type = "self_hosted"` Access app a
# consumer repo created out-of-band with `app_launcher_visible: true` and no
# logo_url. FuzeOne/FuzeFront's runbook records doing exactly that for
# unleash.prod.fuzefront.com (app 514f8a21-3793-4726-858e-819556fbe346).
#
# Terraform cannot destroy what it never created and what is not in state, and
# importing each duplicate by hand needs an app id nobody has until they go
# looking. So the deletion is expressed as a reconciler that DISCOVERS them:
# any launcher-visible Access app on a host `local.launcher_services` already
# publishes a tile for, that Terraform does not own, is a duplicate by
# definition. The script documents its full safety envelope; the short version
# is that it only ever considers those exact hostnames, and never an id in
# `local.terraform_owned_access_app_ids` or var.launcher_tile_prune_keep_ids.
#
# Deleting a duplicate does NOT open the host up: it falls back to the wildcard
# *.prod.fuzefront.com email-OTP app that covered it before, whose allowlist the
# consumer's policy was written to mirror.
#
# The trigger set is deliberately CONTENT-hashed rather than time-based — a
# timestamp() trigger would make every plan non-empty and permanently trip the
# drift gate in terraform-plan-apply.yml. It re-runs when the tile set, the
# owned-id set, the keep-list, or the script itself changes.
# ---------------------------------------------------------------------------
variable "launcher_tile_prune_keep_ids" {
  description = "Cloudflare Access application ids the duplicate-tile reconciler must never delete. Escape hatch for a deliberate per-host self-hosted app that should keep its own launcher tile."
  type        = list(string)
  default     = []
}

locals {
  # The exact hostnames FuzeInfra publishes a tile for. Nothing outside this set
  # is ever a deletion candidate.
  launcher_hosts = [for key in keys(local.launcher_services) : "${key}.${local.prod_domain}"]

  # EVERY Access application this root module owns. The reconciler treats this
  # as the do-not-touch list, so adding a new Access app resource here is what
  # keeps it safe from a future prune — remember to extend this list with it.
  terraform_owned_access_app_ids = concat(
    [for app in cloudflare_zero_trust_access_application.launcher_bookmark : app.id],
    [for app in cloudflare_zero_trust_access_application.public_app : app.id],
    cloudflare_zero_trust_access_application.admin_services[*].id,
    cloudflare_zero_trust_access_application.app_launcher[*].id,
    cloudflare_zero_trust_access_application.sealed_secrets_cert[*].id,
    cloudflare_zero_trust_access_application.crit_alert_bridge[*].id,
    cloudflare_zero_trust_access_application.handoff_mcp[*].id,
    cloudflare_zero_trust_access_application.a2a_relay[*].id,
    cloudflare_zero_trust_access_application.a2a_gateway[*].id,
    # k8s-api.<prod> is NOT a launcher host, so the reconciler already never
    # targets it; listed here anyway per this local's own do-not-touch contract.
    cloudflare_zero_trust_access_application.k8s_api_breakglass[*].id,
    # Scoped to litellm.<prod>, which IS in local.launcher_hosts (the "litellm"
    # tile) — without this entry the reconciler would treat it as an unowned
    # duplicate of the wildcard admin_services coverage and delete it.
    cloudflare_zero_trust_access_application.litellm_service[*].id,
    # Path-scoped, launcher-invisible, but it lives on authentik.<prod> which IS
    # in local.launcher_hosts (the `authentik` tile) — so without this entry the
    # reconciler would treat it as a deletion candidate and silently re-break the
    # OIDC back channel.
    cloudflare_zero_trust_access_application.authentik_oidc_endpoints[*].id,
  )
}

resource "null_resource" "prune_duplicate_launcher_tiles" {
  count = local.cloudflare_enabled ? 1 : 0

  triggers = {
    hosts  = sha256(jsonencode(local.launcher_hosts))
    owned  = sha256(jsonencode(local.terraform_owned_access_app_ids))
    keep   = sha256(jsonencode(var.launcher_tile_prune_keep_ids))
    script = filesha256("${path.module}/prune-duplicate-launcher-tiles.py")
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = "python3 '${path.module}/prune-duplicate-launcher-tiles.py'"

    environment = {
      CF_API_TOKEN    = var.cloudflare_api_token
      CF_ACCOUNT_ID   = var.cloudflare_account_id
      LAUNCHER_HOSTS  = join(",", local.launcher_hosts)
      MANAGED_APP_IDS = join(",", local.terraform_owned_access_app_ids)
      KEEP_APP_IDS    = join(",", var.launcher_tile_prune_keep_ids)
    }
  }
}

# Construct the cloudflared token from known fields.
# Format: base64(JSON{ a: account_id, t: tunnel_id, s: base64_secret })
locals {
  tunnel_token = local.cloudflare_enabled ? base64encode(jsonencode({
    a = var.cloudflare_account_id
    t = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].id
    s = random_bytes.tunnel_secret[0].base64
  })) : ""
}

# ---------------------------------------------------------------------------
# Push token into the cluster secret so cloudflared connects on first sync.
# Runs after kubeconfig is available locally (extract_kubeconfig).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CRIT-Alert Bridge — Cloudflare Worker
#
# Grafana fires a webhook when severity=critical alert fires.
# This Worker validates the shared secret, drops "resolved" events, and
# calls GitHub repository_dispatch → triggers grafana-crit-fix.yml.
#
# Secret bindings keep credentials out of Worker env vars in cleartext.
# BRIDGE_TOKEN is also injected into fuzeinfra-secrets so Grafana can read it.
# ---------------------------------------------------------------------------
resource "cloudflare_worker_script" "crit_alert_bridge" {
  count      = local.cloudflare_enabled && var.crit_bridge_token != "" ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "crit-alert-bridge"
  content    = file("${path.module}/crit-alert-bridge.js")
  module     = true

  secret_text_binding {
    name = "GITHUB_TOKEN"
    text = var.github_token
  }

  plain_text_binding {
    name = "GITHUB_REPO"
    text = "${var.github_owner}/${var.github_repo}"
  }

  secret_text_binding {
    name = "BRIDGE_TOKEN"
    text = var.crit_bridge_token
  }
}

resource "cloudflare_worker_route" "crit_alert_bridge" {
  count       = local.cloudflare_enabled && var.crit_bridge_token != "" ? 1 : 0
  zone_id     = var.cloudflare_zone_id
  pattern     = "crit-alert.${local.prod_domain}/*"
  script_name = cloudflare_worker_script.crit_alert_bridge[0].name
}

# CF Access bypass — Grafana must POST without a browser OTP session.
# The wildcard *.prod.fuzefront.com Access app would block this endpoint.
# A more-specific hostname app takes precedence and lets the Worker handle auth
# itself (via BRIDGE_TOKEN).
resource "cloudflare_zero_trust_access_application" "crit_alert_bridge" {
  count                = local.cloudflare_enabled && var.crit_bridge_token != "" ? 1 : 0
  account_id           = var.cloudflare_account_id
  name                 = "CRIT Alert Bridge (public webhook)"
  domain               = "crit-alert.${local.prod_domain}"
  type                 = "self_hosted"
  session_duration     = "0s"
  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "crit_alert_bridge_bypass" {
  count          = local.cloudflare_enabled && var.crit_bridge_token != "" ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.crit_alert_bridge[0].id
  name           = "Bypass — CRIT alert webhook (Worker handles auth)"
  precedence     = 1
  decision       = "bypass"

  include {
    everyone = true
  }
}

# CF Access bypass — handoff MCP is a MACHINE endpoint: Anthropic Managed Agents
# connect server-to-server (no browser), so the wildcard *.prod.fuzefront.com
# email-OTP app would block them. This more-specific host app takes precedence and
# bypasses OTP; the handoff MCP server enforces its own bearer (HANDOFF_MCP_TOKEN),
# which agents present via a vault credential keyed to the URL. Gated off by default.
#
# TRAP, hit for real by #588: flipping the gate is NOT the same as applying it.
# handoff_mcp_access_enabled is set as a TF_VAR_* in
# .github/workflows/terraform-plan-apply.yml, and that workflow's APPLY job triggers
# only on push to main with `paths: ["terraform/**"]`. #588 changed the workflow file
# and nothing else, so it matched no path, no apply ran, and these two resources were
# never created — while every check reported green and mcp-handoff.<domain> quietly
# kept answering 302 to the OTP login. A variable flip that touches no file under
# terraform/ is inert. If you toggle one of these CI-side gates again, include a
# change under terraform/ in the same PR (editing this comment counts) or dispatch
# the workflow by hand — then verify at the edge, not in the diff:
#   curl -sS -o /dev/null -w '%{http_code}\n' https://mcp-handoff.<domain>/mcp
#   401 -> bypass is live and the app bearer is the gate.
#   302 -> still behind the *.prod email-OTP wall; a machine caller cannot pass it.
resource "cloudflare_zero_trust_access_application" "handoff_mcp" {
  count                = local.cloudflare_enabled && var.handoff_mcp_access_enabled ? 1 : 0
  account_id           = var.cloudflare_account_id
  name                 = "Handoff MCP (agent-to-agent, bearer-gated)"
  domain               = "mcp-handoff.${local.prod_domain}"
  type                 = "self_hosted"
  session_duration     = "0s"
  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "handoff_mcp_bypass" {
  count          = local.cloudflare_enabled && var.handoff_mcp_access_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.handoff_mcp[0].id
  name           = "Bypass — handoff MCP (app enforces HANDOFF_MCP_TOKEN bearer)"
  precedence     = 1
  decision       = "bypass"

  include {
    everyone = true
  }
}

# A2A WSS relay (agent-templates/orchestration/a2a_relay). Cloud Claude Code sessions
# open an OUTBOUND WebSocket to relay.<domain>; they are machine, non-interactive
# callers that cannot pass the *.prod email-OTP wall, so a more-specific bypass app is
# required. v0 relay is OPEN (capability = unguessable cse_ session-id); it enforces an
# optional FUZE_A2A_RELAY_TOKEN bearer when the a2a-relay-secret is landed. Same TRAP as
# handoff_mcp: flipping var.a2a_relay_access_enabled is inert unless the apply job runs —
# this file is under terraform/**, so a PR that edits it triggers the apply. Verify at the
# edge (WebSocket upgrade, not a browser):
#   curl -sS -o /dev/null -w '%{http_code}\n' https://relay.<domain>/healthz
#   200 -> bypass live (relay answers). 302 -> still behind the OTP wall.
resource "cloudflare_zero_trust_access_application" "a2a_relay" {
  count                = local.cloudflare_enabled && var.a2a_relay_access_enabled ? 1 : 0
  account_id           = var.cloudflare_account_id
  name                 = "A2A relay (cloud session WSS, capability-gated)"
  domain               = "relay.${local.prod_domain}"
  type                 = "self_hosted"
  session_duration     = "0s"
  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "a2a_relay_bypass" {
  count          = local.cloudflare_enabled && var.a2a_relay_access_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.a2a_relay[0].id
  name           = "Bypass — A2A relay (capability = cse_ session-id; optional bearer)"
  precedence     = 1
  decision       = "bypass"

  include {
    everyone = true
  }
}

# A2A delivery gateway (agent-templates/orchestration/a2a_gateway). Cloud Claude Code
# sessions POST to a2a-gateway.<domain>; the gateway runs `claude -p --cloud <id>` to
# WAKE + deliver to an idle peer. Machine, non-interactive callers → need a bypass past
# the *.prod email-OTP wall. The gateway enforces its own FUZE_A2A_GATEWAY_TOKEN bearer.
# Same TRAP as handoff_mcp/a2a_relay: flipping the var is inert unless the apply job runs;
# this file is under terraform/**, so a PR editing it triggers the apply. Verify:
#   curl -sS -o /dev/null -w '%{http_code}\n' https://a2a-gateway.<domain>/healthz
#   200 -> bypass live. 302 -> still behind the OTP wall.
resource "cloudflare_zero_trust_access_application" "a2a_gateway" {
  count                = local.cloudflare_enabled && var.a2a_gateway_access_enabled ? 1 : 0
  account_id           = var.cloudflare_account_id
  name                 = "A2A delivery gateway (cloud session HTTPS, bearer-gated)"
  domain               = "a2a-gateway.${local.prod_domain}"
  type                 = "self_hosted"
  session_duration     = "0s"
  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "a2a_gateway_bypass" {
  count          = local.cloudflare_enabled && var.a2a_gateway_access_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.a2a_gateway[0].id
  name           = "Bypass — A2A gateway (app enforces FUZE_A2A_GATEWAY_TOKEN bearer)"
  precedence     = 1
  decision       = "bypass"

  include {
    everyone = true
  }
}

# ---------------------------------------------------------------------------
# LiteLLM CI service token (fuze.yml's hosted-runner LLM routing)
#
# UNLIKE handoff_mcp/a2a_relay/a2a_gateway above, this is NOT a bypass app.
# Those three are FuzeInfra's own components with no real human-facing UI, so
# removing Cloudflare Access entirely and trusting the app's own bearer token
# is a fair trade. LiteLLM is different: it has a real admin console at
# litellm.<prod>/ui (helm/litellm, see the launcher_services comment above),
# LiteLLM's own auth there is only a master key, and its SSO is an enterprise
# feature it does not have — meaning the Cloudflare Access wall in front of it
# is the ONLY identity layer that console gets. A bypass app would remove that
# entirely, for every caller, human or not. So this carves out a
# litellm.<prod>-scoped Access application (more specific than the
# *.prod wildcard `admin_services` covers it under today, so Cloudflare
# prefers this one for that exact host) that keeps the SAME Google + email-OTP
# human policies `admin_services` already has, and ADDS a third policy that
# lets a service token in alongside them — human access to /ui is unchanged,
# and a CI job presenting the token's client id/secret as
# CF-Access-Client-Id/CF-Access-Client-Secret headers now also gets through.
#
# THE TRAP: this application's domain is inside local.launcher_hosts (the
# "litellm" launcher tile), so it MUST be added to
# local.terraform_owned_access_app_ids below (see that local's own comment) or
# the duplicate-tile reconciler will treat it as an unowned duplicate and
# delete it on the next apply.
resource "cloudflare_zero_trust_access_application" "litellm_service" {
  count            = local.cloudflare_enabled ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "LiteLLM gateway (human console + CI service token)"
  domain           = "litellm.${local.prod_domain}"
  type             = "self_hosted"
  session_duration = var.access_session_duration

  app_launcher_visible = false
}

resource "cloudflare_zero_trust_access_policy" "litellm_service_google" {
  count          = local.google_idp_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.litellm_service[0].id
  name           = "Admin via Google"
  precedence     = 1
  decision       = "allow"

  include {
    login_method = [cloudflare_zero_trust_access_identity_provider.google[0].id]
  }

  require {
    email = var.allowed_admin_emails
  }
}

resource "cloudflare_zero_trust_access_policy" "litellm_service_email_otp" {
  count          = local.cloudflare_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.litellm_service[0].id
  name           = "Admin email allowlist (OTP) — break-glass"
  precedence     = 2
  decision       = "allow"

  include {
    email = var.allowed_admin_emails
  }
}

# The CI credential itself. `terraform apply` mints this against the
# Cloudflare API; the client_secret is returned exactly once by that API call
# and is captured only in Terraform state (see backend.tf's S3 backend) —
# it is never in this file, never in a PR diff, and never seen by whatever
# applies this (see docs/runbooks — extraction is a deliberate, separate,
# human/local-terminal step via `terraform output`, same reasoning as every
# other secret this repo hands to `scripts/provision_secrets.py` rather than
# ever typing a value into a PR, a chat, or a commit).
#
# REQUIRED TOKEN SCOPE — this resource needs the CLOUDFLARE_API_TOKEN to carry
# the **Account → Access: Service Tokens → Edit** permission group. It is a
# SEPARATE group from "Access: Apps and Policies", so the token can create the
# Access application + policies below and still fail HERE at apply with an empty
# `error creating access service token:  (1010)` — the plan cannot catch it,
# because Cloudflare only checks the permission on write. The permission group
# was granted on the CD token (2026-09-14), so this resource applies on the
# next merge; if it 1010s again, re-check the token still carries the group.
# See docs/TERRAFORM_CD.md → "CLOUDFLARE_API_TOKEN scope".
#
# THE SECRET IS NOT WHAT BREAKS HERE — the POLICY ACTION is. Read the `decision`
# comment on litellm_service_ci_token below before touching anything in this
# pair. A token whose secret is perfectly valid still 302s to the IdP login page
# if its policy action is `allow` instead of `non_identity`, and the dashboard's
# "Last Seen: Not Seen Yet" on this token is the SYMPTOM of the policy never
# admitting it — not evidence of a bad secret.
#
# 2026-09-16: that "Not Seen Yet" was misread as a stale secret, and four applies
# (#1030 taint-replace, #1032/#1034/#1037 rotations) were burned chasing it. The
# rotation recorded below did eventually succeed and is harmless, but it fixed
# nothing: the 302 persisted identically afterwards, from both a laptop and a CI
# runner, with a provably fresh secret. If this endpoint 302s, check the policy
# `decision` FIRST — a service token's client_secret is write-once, so terraform
# genuinely cannot validate it, which makes "bad secret" a seductive and very
# expensive wrong answer.
#
# Rotation mechanics, kept only so the attributes below are legible (each was a
# separate failed apply before the constraint was known): Cloudflare refuses to
# DELETE a token still referenced by a policy (12139 — rotate, don't replace);
# `client_secret_version` may only be incremented if
# `previous_client_secret_expires_at` is also set (12130); and it may only be
# incremented BY ONE from the current live value (12130). Both attributes are
# optional+computed, so they can be dropped from config without a diff once this
# rotation has settled — leaving them pinned risks a future apply trying to
# re-drive a rotation Cloudflare will not legally repeat.
#
# Rotation keeps the resource id/client_id untouched, so the paired Access
# policy's `service_token = [...]` reference (by id) needs no change. When this
# token IS legitimately rotated, re-provision the new values to GitHub secrets
# via `terraform output -raw ... | gh secret set ...` — note that
# FuzeSDLC's provision-secrets.yml only FILLS MISSING secrets, it never
# overwrites an existing one, so a rotated value must be pushed to each
# consuming repo directly.
resource "cloudflare_zero_trust_access_service_token" "litellm_ci" {
  count                             = local.cloudflare_enabled ? 1 : 0
  account_id                        = var.cloudflare_account_id
  name                              = "fuze.yml LLM routing (hosted-runner CI)"
  client_secret_version             = 2
  previous_client_secret_expires_at = "2026-09-16T08:00:00Z"
}

resource "cloudflare_zero_trust_access_policy" "litellm_service_ci_token" {
  count          = local.cloudflare_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.litellm_service[0].id
  name           = "CI service token (fuze.yml LLM routing)"
  precedence     = 3

  # MUST be `non_identity` ("Service Auth" in the dashboard), NOT `allow`.
  # `allow` is an IDENTITY decision: it requires a human IdP login on top of
  # whatever `include` matches, so a request carrying only CF-Access-Client-Id
  # / CF-Access-Client-Secret is redirected (302) to the Access login page
  # instead of being admitted — exactly what this app did from 2026-09-14 until
  # 2026-09-16. Cloudflare states it plainly: "Make sure to set the policy
  # action to Service Auth; otherwise, Access will prompt for an identity
  # provider login."
  # (developers.cloudflare.com/cloudflare-one/identity/service-tokens/)
  #
  # The failure is SILENT everywhere it would normally be caught: `include`
  # still references the right token id, the app/aud/domain still match, plan
  # reports "No changes", and the redirect's own JWT just says
  # service_token_status:false / auth_status:NONE — which reads like a bad
  # credential, and is why this was misdiagnosed as a stale secret and "fixed"
  # with four pointless token rotations. `non_identity` is the only difference
  # that mattered.
  decision = "non_identity"

  include {
    service_token = [cloudflare_zero_trust_access_service_token.litellm_ci[0].id]
  }
}

# ---------------------------------------------------------------------------
# BREAK-GLASS k3s API endpoint over the Cloudflare Tunnel.
#
# The SECOND, independent external path to the API, for when the Contabo floating
# VIP or the Contabo network itself is the fault (the VIP is the primary path —
# api-floating-vip.tf + helm/fuzeinfra/templates/api-vip-keepalived.yaml). The
# tunnel ingress rule (tcp:// to the in-cluster apiserver ClusterIP) is added in
# cloudflare_zero_trust_tunnel_cloudflared_config above, gated on the same var.
#
# Reached with a client-side `cloudflared access tcp` presenting the service token
# below as CF-Access-Client-Id / CF-Access-Client-Secret — this is a Cloudflare
# Zero Trust flow on the SAME free tier as the admin-UI email-OTP wall, NOT
# Spectrum (Spectrum is only needed for a raw public TCP listener with no
# client-side cloudflared). kubectl still does its own TLS/mTLS to the apiserver
# through the local proxy, end to end. See docs/runbooks/api-floating-vip.md.
#
# ENABLED 2026-09-22 via TF_VAR_api_breakglass_enabled=true in
# terraform-plan-apply.yml. Like handoff_mcp/a2a_*, the flip is only effective when
# the apply job runs — this file is under terraform/**, so a PR editing it triggers
# the apply. Verify at the edge after (needs the service token, extracted once via
# `terraform output -raw` — never committed):
#   cloudflared access tcp --hostname k8s-api.<domain> --url 127.0.0.1:6443 \
#     --service-token-id "$CF_ID" --service-token-secret "$CF_SECRET" &
#   kubectl --server https://127.0.0.1:6443 get --raw /livez
# ---------------------------------------------------------------------------
variable "api_breakglass_enabled" {
  description = "Expose the k3s API over the CF tunnel (tcp://) behind an Access service token, as a break-glass path independent of the floating VIP."
  type        = bool
  default     = false
}

resource "cloudflare_zero_trust_access_application" "k8s_api_breakglass" {
  count                = local.cloudflare_enabled && var.api_breakglass_enabled ? 1 : 0
  account_id           = var.cloudflare_account_id
  name                 = "k3s API (break-glass, service-token + admin)"
  domain               = "k8s-api.${local.prod_domain}"
  type                 = "self_hosted"
  session_duration     = var.access_session_duration
  app_launcher_visible = false
}

# The machine credential used by `cloudflared access tcp`. client_secret is
# returned once by the Cloudflare API and captured only in Terraform state; it is
# never in this file, a PR diff, or seen by whatever applies this. Provision it to
# GitHub Actions / operator kubeconfigs out of band via `terraform output -raw`,
# exactly like litellm_ci above. Needs the Account → Access: Service Tokens → Edit
# permission group on CLOUDFLARE_API_TOKEN (granted 2026-09-14).
resource "cloudflare_zero_trust_access_service_token" "k8s_api_breakglass" {
  count      = local.cloudflare_enabled && var.api_breakglass_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "k3s API break-glass (cloudflared access tcp)"
}

# MUST be non_identity ("Service Auth"), not allow — an `allow` decision demands a
# human IdP login on top and 302s a token-only request to the login page. This is
# the exact trap that cost four pointless litellm_ci token rotations (see that
# resource). This is the primary break-glass path (machine kubectl).
resource "cloudflare_zero_trust_access_policy" "k8s_api_breakglass_service_token" {
  count          = local.cloudflare_enabled && var.api_breakglass_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.k8s_api_breakglass[0].id
  name           = "Service token (cloudflared access tcp)"
  precedence     = 1
  decision       = "non_identity"

  include {
    service_token = [cloudflare_zero_trust_access_service_token.k8s_api_breakglass[0].id]
  }
}

# Human break-glass (email OTP) — cluster-independent, like admin_email_otp.
resource "cloudflare_zero_trust_access_policy" "k8s_api_breakglass_email_otp" {
  count          = local.cloudflare_enabled && var.api_breakglass_enabled ? 1 : 0
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.k8s_api_breakglass[0].id
  name           = "Admin email allowlist (OTP) — break-glass"
  precedence     = 2
  decision       = "allow"

  include {
    email = var.allowed_admin_emails
  }
}

# ---------------------------------------------------------------------------
# MendysRobotics.com subdomain routing (issue #120)
#
# MendysRobotics has its own apex domain (mendysrobotics.com) managed in its
# own Cloudflare zone. FuzeInfra owns the DNS + Access for the three product
# subdomains because they route through the shared FuzeInfra CF Tunnel and
# would otherwise require MendysRobotics to hold FuzeInfra cluster credentials.
#
# Enable by setting mendysrobotics_zone_id in terraform.tfvars (or as a GH
# Actions secret MENDYSROBOTICS_ZONE_ID). All three resources are gated on
# the variable being non-empty so a bare `terraform apply` with no variable
# remains byte-identical.
#
# MENDYSROBOTICS_ZONE_ID is now set (#120) — live.mendysrobotics.com,
# marketplace.mendysrobotics.com, and wp.mendysrobotics.com are active.
#
# INVARIANT: never touch the mendysrobotics.com apex or www record — those are
# managed in the MendysRobotics landing repo.
#
# Trigger: retrigger plan after stale-plan failure on PR #519 (run 31439208312).
# ---------------------------------------------------------------------------
locals {
  mendysrobotics_enabled = local.cloudflare_enabled && var.mendysrobotics_zone_id != ""
}

# DNS: live.mendysrobotics.com → shared FuzeInfra tunnel CNAME.
# Proxied so Cloudflare terminates TLS at the edge (Universal SSL).
resource "cloudflare_record" "mendys_live" {
  count           = local.mendysrobotics_enabled ? 1 : 0
  zone_id         = var.mendysrobotics_zone_id
  name            = "live"
  value           = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type            = "CNAME"
  proxied         = true
  ttl             = 1
  allow_overwrite = true
}

# DNS: marketplace.mendysrobotics.com → same tunnel (public-facing, no Access gate).
resource "cloudflare_record" "mendys_marketplace" {
  count           = local.mendysrobotics_enabled ? 1 : 0
  zone_id         = var.mendysrobotics_zone_id
  name            = "marketplace"
  value           = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type            = "CNAME"
  proxied         = true
  ttl             = 1
  allow_overwrite = true
}

# DNS: wp.mendysrobotics.com → same tunnel (public-facing WordPress, no Access gate).
resource "cloudflare_record" "mendys_wp" {
  count           = local.mendysrobotics_enabled ? 1 : 0
  zone_id         = var.mendysrobotics_zone_id
  name            = "wp"
  value           = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].cname
  type            = "CNAME"
  proxied         = true
  ttl             = 1
  allow_overwrite = true
}

# REMOVED (deliberate, do not re-add): CF Access app gating live.mendysrobotics.com.
#
# The management portal authenticates at the APPLICATION level — brokered SSO
# through the wrapped Security API (fuzefront-security) against the dedicated
# Mendys Authentik instance — exactly like marketplace.mendysrobotics.com,
# which has never been behind Access. The email-OTP Access app that used to
# live here stacked a second, unrelated login wall in front of that flow and
# 302'd every request (including /api/*) to fuzefront.cloudflareaccess.com, so
# no traffic ever reached the Mendys stack: prod sign-in was fully down
# (issue #1115 / Jira MR-37). All three mendysrobotics.com subdomains are
# app-auth-only by design; none of them may carry an Access app.

# ---------------------------------------------------------------------------
# fuzeinfra-tunnel-secrets — Terraform-owned Secret (ArgoCD never touches it)
#
# Separate from fuzeinfra-secrets (Helm/ArgoCD-owned) so that ArgoCD resyncs
# and full cluster wipes never wipe these keys. Any `terraform apply` after a
# fresh provision recreates this secret automatically via provision_id trigger.
#
# Keys:
#   CLOUDFLARE_TUNNEL_TOKEN       — bearer token for cloudflared token mode
#   CLOUDFLARE_TUNNEL_CREDENTIALS — JSON credentials for local-config mode
#   CRIT_BRIDGE_TOKEN             — shared secret for the CF Worker webhook bridge
# ---------------------------------------------------------------------------
locals {
  tunnel_credentials_json = local.cloudflare_enabled ? jsonencode({
    AccountTag   = var.cloudflare_account_id
    TunnelID     = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].id
    TunnelSecret = random_bytes.tunnel_secret[0].base64
  }) : ""
}

resource "null_resource" "tunnel_secrets" {
  count      = local.cloudflare_enabled ? 1 : 0
  depends_on = [null_resource.extract_kubeconfig]

  triggers = {
    # Re-run on: new provision, tunnel rotation, or crit-bridge token change.
    provision_id = null_resource.provision.id
    tunnel_id    = cloudflare_zero_trust_tunnel_cloudflared.fuzeinfra[0].id
    token_hash   = sha256(local.tunnel_token)
    crit_hash    = sha256(var.crit_bridge_token)
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      export KUBECONFIG="${path.root}/k3s-kubeconfig.yaml"

      # Wait for the namespace to exist (ArgoCD PreSync must have run).
      for i in $(seq 1 30); do
        kubectl get namespace fuzeinfra &>/dev/null && break
        echo "  Waiting for fuzeinfra namespace ($i/30)..."
        sleep 10
      done

      TOKEN_B64=$(printf '%s' '${local.tunnel_token}' | base64 -w0 2>/dev/null || printf '%s' '${local.tunnel_token}' | base64)
      CREDS_B64=$(printf '%s' '${local.tunnel_credentials_json}' | base64 -w0 2>/dev/null || printf '%s' '${local.tunnel_credentials_json}' | base64)
      CRIT_B64=$(printf '%s' '${var.crit_bridge_token}' | base64 -w0 2>/dev/null || printf '%s' '${var.crit_bridge_token}' | base64)

      kubectl create secret generic fuzeinfra-tunnel-secrets \
        -n fuzeinfra \
        --from-literal=CLOUDFLARE_TUNNEL_TOKEN=placeholder \
        --from-literal=CLOUDFLARE_TUNNEL_CREDENTIALS=placeholder \
        --from-literal=CRIT_BRIDGE_TOKEN=placeholder \
        --dry-run=client -o yaml \
      | kubectl apply -f -

      kubectl patch secret fuzeinfra-tunnel-secrets -n fuzeinfra \
        --type=merge \
        -p "{\"data\":{\"CLOUDFLARE_TUNNEL_TOKEN\":\"$${TOKEN_B64}\",\"CLOUDFLARE_TUNNEL_CREDENTIALS\":\"$${CREDS_B64}\",\"CRIT_BRIDGE_TOKEN\":\"$${CRIT_B64}\"}}"

      echo "fuzeinfra-tunnel-secrets created/updated."
    EOT
  }
}
