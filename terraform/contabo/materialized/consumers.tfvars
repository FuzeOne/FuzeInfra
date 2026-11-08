# =============================================================================
# GENERATED — materialized consumer declarations. DO NOT HAND-EDIT.
#
# This file is the ONLY place consumer-specific data enters FuzeInfra's
# terraform. The bare setup (../*.tf) is consumer-free by invariant; CI loads
# this file via -var-file. Source of truth is each consumer repo's own
# declaration (deploy/cf/hosts.yaml); entries are materialized here by the
# consumer-dispatch flow (repository_dispatch -> validate against policy ->
# auto-PR -> saved-plan gate -> deliberate merge applies).
#
# Policy reminders: labels must not collide, no wildcards, bypass only for
# hosts whose app owns its own auth — admin UIs get gated Access apps in
# cloudflare.tf instead.
# =============================================================================

public_app_hosts = {
  # FuzeOne/FuzeKeys (FuzeInfra#136 / #140)
  "keys"     = "FuzeOne/FuzeKeys" # frontend    — keys.prod.fuzefront.com
  "api.keys" = "FuzeOne/FuzeKeys" # API backend — api.keys.prod.fuzefront.com

  # MFE product apps — registered as FuzeFront micro-frontends
  "fuzesales"   = "FuzeOne/FuzeSales"   # fuzesales.prod.fuzefront.com
  "fuzecontact" = "FuzeOne/FuzeContact" # fuzecontact.prod.fuzefront.com
  "fuzeservice" = "FuzeOne/FuzeService" # fuzeservice.prod.fuzefront.com

  # FuzeOne/FuzePicker (FuzeInfra#242)
  "api.fuzepicker" = "FuzeOne/FuzePicker" # backend API (own JWT auth) — api.fuzepicker.prod.fuzefront.com
  "picker"         = "FuzeOne/FuzePicker" # static module-federation bundle — picker.prod.fuzefront.com
}
