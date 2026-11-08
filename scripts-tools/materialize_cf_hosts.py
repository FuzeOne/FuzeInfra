#!/usr/bin/env python3
"""
Consumer-declared CF host materializer.

Reads deploy/cf/hosts.yaml from a consumer repo (via GitHub API), validates
against FuzeInfra policy, and regenerates
terraform/contabo/materialized/consumers.tfvars deterministically.

Usage (called by cf-hosts-materialize.yml):
  python scripts-tools/materialize_cf_hosts.py \\
      --repo FuzeOne/FuzeKeys \\
      --ref abc1234 \\
      [--dry-run]

Environment:
  GH_TOKEN or GITHUB_TOKEN — GitHub PAT with repo:read scope (required)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required — pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
CONSUMERS_TFVARS = REPO_ROOT / "terraform" / "contabo" / "materialized" / "consumers.tfvars"

# Labels managed by bare *.tf — never allowed in the consumer registry.
# Labels that live OUTSIDE the chart and so cannot be derived from it: the public
# vanity hosts at the apex zone (cloudflare.tf `public_vanity_hosts`) plus argocd,
# which is served by its own tunnel ingress rule rather than a chart Ingress.
_NON_CHART_RESERVED: frozenset[str] = frozenset({
    "app", "auth", "plan", "fuzehub",  # public_vanity_hosts in cloudflare.tf
    "argocd",                          # admin tunnel rule
    "www",                             # cloudflare_record.website_www — canonical-host
                                       # redirect to the fuzefront.com apex. Owned by a
                                       # bare .tf resource, not by public_vanity_hosts,
                                       # so it is not derivable from either source.
})


def _reserved_labels() -> frozenset[str]:
    """Infra-owned labels, DERIVED rather than hand-listed.

    This used to be a literal frozenset of nine names, and it had drifted badly:
    it was missing elasticsearch, chromadb, airflow, flower, kafka-ui,
    mongo-express, rabbitmq, unleash, authentik, litellm, neo4j-bolt, relay,
    a2a-gateway and mcp-handoff — i.e. most of the platform, including hosts
    fronting datastores and an unauthenticated public WebSocket endpoint. A
    hand-maintained list of names that live somewhere else will always drift;
    the only stable version is one computed from those sources at check time.

    Falls back to the non-chart set alone if the derivation cannot run, so this
    module keeps working (more permissively, and loudly) rather than crashing —
    the dedicated CI guard, check_consumer_host_collisions.py, is the gate that
    must not be bypassed.
    """
    reserved = set(_NON_CHART_RESERVED)
    try:
        import importlib.util

        checker = Path(__file__).parent / "check_consumer_host_collisions.py"
        spec = importlib.util.spec_from_file_location("_cf_collision_guard", checker)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reserved |= set(module.derive_infra_owned_hosts())
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"WARNING: could not derive infra-owned labels ({exc}); "
            "falling back to the non-chart reserved set only. "
            "check_consumer_host_collisions.py remains the authoritative gate.",
            file=sys.stderr,
        )
    return frozenset(reserved)


RESERVED_LABELS: frozenset[str] = _reserved_labels()

# Only repos under this org are allowed to declare CF hosts.
ALLOWED_ORG = "izzywdev"

# Only 'bypass' access is accepted via this pipeline (admin gated apps go in cloudflare.tf).
ALLOWED_ACCESS = {"bypass"}

# Valid DNS label: lowercase a-z, 0-9, hyphens (not at start/end), dots for nesting.
_DNS_LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$')

TFVARS_HEADER = """\
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
"""


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _gh_raw(url: str, token: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return b""
        raise


def fetch_hosts_yaml(repo: str, ref: str, token: str) -> dict | None:
    content = _gh_raw(
        f"https://api.github.com/repos/{repo}/contents/deploy/cf/hosts.yaml?ref={ref}",
        token,
    )
    if not content:
        return None
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "content" in data:
            import base64
            raw = base64.b64decode("".join(data["content"].split()))
            return yaml.safe_load(raw) or {}
    except Exception:
        pass
    return yaml.safe_load(content) or {}


# ---------------------------------------------------------------------------
# consumers.tfvars parser
# ---------------------------------------------------------------------------

def parse_consumers_tfvars(path: Path) -> dict[str, str]:
    """Parse consumers.tfvars into {label: repo} mapping."""
    if not path.exists():
        return {}
    registry: dict[str, str] = {}
    content = path.read_text(encoding="utf-8")
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r'^"([^"]+)"\s*=\s*"([^"]+)"', line)
        if m:
            registry[m.group(1)] = m.group(2)
    return registry


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------

def validate(repo: str, manifest: dict, current_registry: dict[str, str]) -> list[str]:
    """Return list of policy violation messages (empty = valid)."""
    errors: list[str] = []

    org = repo.split("/")[0]
    if org != ALLOWED_ORG:
        errors.append(f"POLICY: declaring repo '{repo}' is not under org '{ALLOWED_ORG}'")
        return errors  # early out — don't trust remaining fields

    declared_app = manifest.get("app", "")
    if not declared_app:
        errors.append("POLICY: manifest missing required 'app' field")

    hosts = manifest.get("hosts", [])
    if not hosts:
        errors.append("POLICY: manifest declares no hosts")
        return errors

    for entry in hosts:
        label = entry.get("label", "")
        access = entry.get("access", "")

        if not label:
            errors.append("POLICY: entry missing required 'label' field")
            continue

        if "*" in label:
            errors.append(f"POLICY: wildcards not allowed — label '{label}' contains '*'")
            continue

        if not _DNS_LABEL_RE.match(label):
            errors.append(
                f"POLICY: label '{label}' is not a valid DNS label "
                "(lowercase alphanumeric + hyphens, dots for nesting, no leading/trailing hyphens)"
            )

        if label in RESERVED_LABELS:
            errors.append(f"POLICY: label '{label}' is reserved by FuzeInfra bare terraform")

        if access not in ALLOWED_ACCESS:
            errors.append(
                f"POLICY: access '{access}' for label '{label}' is not allowed "
                f"(only: {', '.join(sorted(ALLOWED_ACCESS))}). "
                "Admin UIs must be declared manually in cloudflare.tf."
            )

        existing_owner = current_registry.get(label)
        if existing_owner and existing_owner != repo:
            errors.append(
                f"POLICY: label '{label}' is already owned by '{existing_owner}' — "
                f"cannot be claimed by '{repo}'"
            )

    return errors


# ---------------------------------------------------------------------------
# consumers.tfvars generator
# ---------------------------------------------------------------------------

def build_registry(repo: str, manifest: dict, current_registry: dict[str, str]) -> dict[str, str]:
    """Rebuild registry: replace all entries for repo with new manifest declarations."""
    # Remove all existing entries for this repo.
    new_registry = {k: v for k, v in current_registry.items() if v != repo}
    # Add new entries.
    for entry in manifest.get("hosts", []):
        label = entry["label"]
        new_registry[label] = repo
    return new_registry


def render_tfvars(registry: dict[str, str]) -> str:
    """Render consumers.tfvars content from registry, grouped by repo."""
    if not registry:
        return TFVARS_HEADER + "\npublic_app_hosts = {}\n"

    # Group by owning repo for readability.
    by_repo: dict[str, list[str]] = {}
    for label, repo in sorted(registry.items()):
        by_repo.setdefault(repo, []).append(label)

    pad = max(len(f'"{label}"') for label in registry) + 2

    lines = [TFVARS_HEADER, "public_app_hosts = {"]
    for repo in sorted(by_repo):
        lines.append(f"  # {repo}")
        for label in sorted(by_repo[repo]):
            key = f'"{label}"'
            host = f"{label}.prod.fuzefront.com"
            lines.append(f"  {key:<{pad}}= \"{repo}\" # {host}")
        lines.append("")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    import os

    parser = argparse.ArgumentParser(description="FuzeInfra CF hosts materializer")
    parser.add_argument("--repo", required=True, help="Declaring repo (e.g. FuzeOne/FuzeKeys)")
    parser.add_argument("--ref", required=True, help="Git ref to fetch hosts.yaml from")
    parser.add_argument("--dry-run", action="store_true", help="Print diff but do not write")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set GH_TOKEN or GITHUB_TOKEN", file=sys.stderr)
        return 2

    repo = args.repo
    ref = args.ref

    print(f"Fetching deploy/cf/hosts.yaml from {repo}@{ref}...")
    manifest = fetch_hosts_yaml(repo, ref, token)
    if manifest is None:
        print(f"ERROR: {repo} has no deploy/cf/hosts.yaml at {ref} — nothing to do.")
        print("RESULT: NO_MANIFEST")
        return 0

    print(f"  Loaded: app={manifest.get('app')}, {len(manifest.get('hosts', []))} host(s)")

    current_registry = parse_consumers_tfvars(CONSUMERS_TFVARS)
    print(f"  Current registry: {len(current_registry)} entries across {len(set(current_registry.values()))} repo(s)")

    errors = validate(repo, manifest, current_registry)
    if errors:
        print("\nPOLICY VIOLATIONS:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print("\nREJECTED — fix violations before re-declaring.", file=sys.stderr)
        print("RESULT: REJECTED")
        return 1

    new_registry = build_registry(repo, manifest, current_registry)
    new_content = render_tfvars(new_registry)

    old_content = CONSUMERS_TFVARS.read_text(encoding="utf-8") if CONSUMERS_TFVARS.exists() else ""

    if new_content == old_content:
        print("\nNo change — consumers.tfvars already matches the declaration.")
        print("RESULT: NO_CHANGE")
        return 0

    if args.dry_run:
        print("\n--- consumers.tfvars would become ---")
        print(new_content)
        print("-------------------------------------")
        print("RESULT: WOULD_CHANGE (dry-run, not written)")
        return 0

    CONSUMERS_TFVARS.write_text(new_content, encoding="utf-8", newline="\n")
    added = set(new_registry) - set(current_registry)
    removed = set(current_registry) - set(new_registry)
    changed = {k for k in new_registry if k in current_registry and new_registry[k] != current_registry[k]}

    print(f"\nMaterialized {repo}:")
    if added:
        print(f"  + Added:   {sorted(added)}")
    if removed:
        print(f"  - Removed: {sorted(removed)}")
    if changed:
        print(f"  ~ Changed: {sorted(changed)}")
    if not added and not removed and not changed:
        print("  (no label changes, content reformatted)")
    print("RESULT: CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
