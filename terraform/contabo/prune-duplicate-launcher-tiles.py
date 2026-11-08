#!/usr/bin/env python3
"""Delete DUPLICATE Cloudflare Access App Launcher tiles.

WHY THIS EXISTS
---------------
FuzeInfra owns the App Launcher. Every tile is a `type = "bookmark"` Access
application declared in `local.launcher_services` (cloudflare.tf) — a pure link
carrying a `logo_url`, with authentication supplied by the single wildcard
self-hosted app on `*.prod.fuzefront.com`.

Consumer repos do not have that context. Twice now a consumer created its OWN
`type = "self_hosted"` Access application for a host FuzeInfra already publishes
a tile for, with `app_launcher_visible: true`, out-of-band via the Cloudflare
API — see FuzeOne/FuzeFront `docs/runbooks/unleash-launcher-and-developer-flags.md`,
which records creating app `514f8a21-3793-4726-858e-819556fbe346` for
`unleash.prod.fuzefront.com`. The result is TWO tiles for one service: the
Terraform bookmark (correct logo) and the consumer's self-hosted app (no
`logo_url`, so the launcher renders a blank/generic icon).

Terraform alone cannot fix this. The duplicates are not in state, and a resource
that does not exist in configuration cannot be destroyed — importing each one by
hand needs an app id nobody has until they look. So this reconciler runs at apply
time and deletes, by discovery, any launcher-visible Access application for a
host FuzeInfra already owns a tile for.

WHAT IT WILL NOT TOUCH — the safety envelope, in order:
  1. Anything Terraform owns (MANAGED_APP_IDS is every Access application id in
     this root module) — so the bookmarks themselves can never be pruned.
  2. Anything in KEEP_APP_IDS (var.launcher_tile_prune_keep_ids), the operator
     escape hatch for a deliberate per-host self-hosted app.
  3. Apps that are not launcher-visible. An invisible app carries policy but no
     tile, so it is not a duplicate of anything and deleting it would change
     access, not appearance.
  4. Account-level app types that are not per-host tiles: app_launcher, warp,
     biso, dash_sso.
  5. Any host outside LAUNCHER_HOSTS — the exact `<key>.prod.fuzefront.com` set
     derived from `local.launcher_services`. Nothing else in the account is even
     considered.

EFFECT ON ACCESS when a duplicate IS deleted: the host falls back to the
wildcard `*.prod.fuzefront.com` email-OTP app that already covered it before the
consumer's app existed. FuzeFront's runbook states its policy was written to
mirror that wildcard's allowlist exactly, so effective access is unchanged — the
tile goes away, the gate does not.

Environment (all supplied by the null_resource in cloudflare.tf):
  CF_API_TOKEN     Cloudflare API token — needs Account > Access: Apps and
                   Policies > Edit (the same token that creates the bookmarks).
  CF_ACCOUNT_ID    Cloudflare account id.
  LAUNCHER_HOSTS   Comma-separated hostnames FuzeInfra publishes tiles for.
  MANAGED_APP_IDS  Comma-separated Terraform-owned Access application ids.
  KEEP_APP_IDS     Comma-separated ids to leave alone regardless.
  PRUNE_DRY_RUN    Set to 1 to report what would be deleted without deleting.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

API = "https://api.cloudflare.com/client/v4"

# Account-scoped app types that are not per-host launcher tiles. Deleting one of
# these would break the launcher portal itself (app_launcher) or a device
# posture integration, so they are never candidates however they are configured.
NEVER_DELETE_TYPES = {"app_launcher", "warp", "biso", "dash_sso"}


def env_set(name):
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}


def api(method, path, token):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"success": False, "errors": [{"message": f"HTTP {exc.code}: {body}"}]}


def hostname_of(domain):
    """Access `domain` may be a bare host or a full URL with a path.

    Bookmarks store `https://litellm.prod.fuzefront.com/ui`; self-hosted apps
    store `unleash.prod.fuzefront.com`. Compare on the hostname alone, so a tile
    is matched regardless of which shape the duplicate happens to use.
    """
    if not domain:
        return ""
    if "://" not in domain:
        domain = f"//{domain}"
    return (urlsplit(domain).hostname or "").lower()


def list_apps(account_id, token):
    apps, page = [], 1
    while True:
        result = api("GET", f"/accounts/{account_id}/access/apps?per_page=50&page={page}", token)
        if not result.get("success"):
            raise SystemExit(f"ERROR: listing Access apps failed: {result.get('errors')}")
        batch = result.get("result") or []
        apps.extend(batch)
        info = result.get("result_info") or {}
        # total_pages is absent on single-page accounts; a short batch also ends it.
        if page >= info.get("total_pages", page) or not batch:
            return apps
        page += 1


def main():
    token = os.environ.get("CF_API_TOKEN", "")
    account_id = os.environ.get("CF_ACCOUNT_ID", "")
    if not token or not account_id:
        raise SystemExit("ERROR: CF_API_TOKEN and CF_ACCOUNT_ID are required")

    hosts = {h.lower() for h in env_set("LAUNCHER_HOSTS")}
    protected = env_set("MANAGED_APP_IDS") | env_set("KEEP_APP_IDS")
    dry_run = os.environ.get("PRUNE_DRY_RUN", "") == "1"

    if not hosts:
        print("No LAUNCHER_HOSTS supplied — nothing to reconcile.")
        return 0

    # Refuse to run without the Terraform-owned id list. An empty set here is
    # indistinguishable from "Terraform owns nothing", and would make the
    # bookmarks themselves candidates for deletion — the launcher would prune
    # itself. Better to fail the apply than to erase every tile.
    if not env_set("MANAGED_APP_IDS"):
        raise SystemExit("ERROR: MANAGED_APP_IDS is empty — refusing to run (would prune FuzeInfra's own tiles)")

    apps = list_apps(account_id, token)
    print(f"Scanned {len(apps)} Access application(s) across {len(hosts)} launcher host(s).")

    failures = 0
    deleted = 0
    for app in apps:
        app_id = app.get("id", "")
        if app_id in protected:
            continue
        if not app.get("app_launcher_visible"):
            continue
        if app.get("type") in NEVER_DELETE_TYPES:
            continue
        host = hostname_of(app.get("domain", ""))
        if host not in hosts:
            continue

        label = f"{app.get('name', '?')!r} (id {app_id}, type {app.get('type')}, host {host})"
        if dry_run:
            print(f"  WOULD DELETE duplicate tile {label}")
            continue

        result = api("DELETE", f"/accounts/{account_id}/access/apps/{app_id}", token)
        if result.get("success"):
            print(f"  deleted duplicate tile {label}")
            deleted += 1
        else:
            # A 404 means someone already removed it — that is the desired end
            # state, not an error worth failing the apply over.
            errors = result.get("errors") or []
            if any(e.get("code") == 12109 or "not found" in str(e.get("message", "")).lower() for e in errors):
                print(f"  already gone {label}")
                continue
            print(f"  FAILED to delete {label}: {errors}", file=sys.stderr)
            failures += 1

    if not deleted and not failures:
        print("No duplicate launcher tiles found — nothing to do.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
