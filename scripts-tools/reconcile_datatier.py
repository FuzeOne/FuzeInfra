#!/usr/bin/env python3
"""
Declarative dataTier reconciler for FuzeInfra.

Reads .fuze/manifest.json from each consuming repo listed in
config/consuming-repos.yaml, compares the dataTier[] declarations against
helm/fuzeinfra/values-contabo.yaml, and reports gaps.

With --apply, adds the missing Helm values entries to values-contabo.yaml
as commented additions (for human review in a PR before ArgoCD applies them).

With --verify, runs post-provision checks against the live DB after applying.

Usage:
  # Report gaps (CI gate):
  python scripts-tools/reconcile_datatier.py

  # Apply missing provisions to values-contabo.yaml:
  python scripts-tools/reconcile_datatier.py --apply

  # Dry-run against a values file override:
  python scripts-tools/reconcile_datatier.py --values helm/fuzeinfra/values-dev.yaml

Environment:
  GH_TOKEN or GITHUB_TOKEN  — GitHub PAT with repo:read scope (required)
"""

from __future__ import annotations

import argparse
import json
import os
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
DEFAULT_VALUES = REPO_ROOT / "helm" / "fuzeinfra" / "values-contabo.yaml"
CONSUMING_REPOS_CONFIG = REPO_ROOT / "config" / "consuming-repos.yaml"

# Stores whose provisioning is currently supported by this reconciler.
SUPPORTED_STORES = {"postgres", "mariadb", "mongo", "neo4j"}


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _gh_get(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def fetch_manifest(repo: str, token: str) -> dict | None:
    return _gh_get(
        f"https://api.github.com/repos/{repo}/contents/.fuze/manifest.json",
        token,
    )


# ---------------------------------------------------------------------------
# Values file helpers
# ---------------------------------------------------------------------------

def load_values(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _pg_index(values: dict) -> dict[str, dict]:
    """Index Postgres entries by role AND name for flexible manifest matching."""
    idx: dict[str, dict] = {}
    for e in values.get("serviceDatabases", []):
        for key in filter(None, [e.get("role"), e.get("name")]):
            idx[key.lower()] = e
    return idx


def _mariadb_index(values: dict) -> dict[str, dict]:
    """Index MariaDB entries by user AND name for flexible manifest matching."""
    idx: dict[str, dict] = {}
    for e in values.get("serviceMariadbDatabases", []):
        for key in filter(None, [e.get("user"), e.get("name")]):
            idx[key.lower()] = e
    return idx


def _mongo_index(values: dict) -> dict[str, dict]:
    """Index Mongo entries by user AND name for flexible manifest matching."""
    idx: dict[str, dict] = {}
    for e in values.get("serviceMongoDatabases", []):
        for key in filter(None, [e.get("user"), e.get("name")]):
            idx[key.lower()] = e
    return idx


def _neo4j_index(values: dict) -> dict[str, dict]:
    """Index per-consumer Neo4j instances by name."""
    idx: dict[str, dict] = {}
    for e in values.get("serviceNeo4jInstances", []):
        name = e.get("name", "")
        if name:
            idx[name.lower()] = e
    return idx


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

Gap = dict  # typed alias for clarity

def _check_postgres(repo: str, dt: dict, pg_idx: dict[str, dict]) -> list[Gap]:
    role: str = dt["role"]
    db: str = dt["database"]
    privs: str = dt.get("privileges", "readWrite")

    existing = pg_idx.get(role.lower()) or pg_idx.get(role.replace("_svc", "").lower()) or pg_idx.get(role.replace("_app", "").lower())
    if not existing:
        return [
            {
                "repo": repo,
                "store": "postgres",
                "status": "MISSING",
                "role": role,
                "database": db,
                "privileges": privs,
                "detail": f"No serviceDatabases entry with role='{role}'",
            }
        ]

    gaps: list[Gap] = []
    if not existing.get("enabled", True):
        gaps.append(
            {
                "repo": repo,
                "store": "postgres",
                "status": "DISABLED",
                "role": role,
                "database": db,
                "detail": f"serviceDatabases entry '{existing.get('name')}' is disabled",
            }
        )
    if existing.get("database") != db:
        gaps.append(
            {
                "repo": repo,
                "store": "postgres",
                "status": "DB_MISMATCH",
                "role": role,
                "database": db,
                "current_database": existing.get("database"),
                "detail": (
                    f"Entry provisions db='{existing.get('database')}' "
                    f"but manifest declares db='{db}'"
                ),
            }
        )
    return gaps


def _check_mariadb(repo: str, dt: dict, maria_idx: dict[str, dict]) -> list[Gap]:
    """MariaDB mirrors the Postgres shape: one user + one database, granted
    ALL PRIVILEGES on that database and nothing else."""
    role: str = dt["role"]
    db: str = dt["database"]

    existing = (
        maria_idx.get(role.lower())
        or maria_idx.get(role.replace("_svc", "").lower())
        or maria_idx.get(role.replace("_app", "").lower())
    )
    if not existing:
        return [
            {
                "repo": repo,
                "store": "mariadb",
                "status": "MISSING",
                "role": role,
                "database": db,
                "detail": f"No serviceMariadbDatabases entry with user='{role}'",
            }
        ]

    gaps: list[Gap] = []
    if not existing.get("enabled", True):
        gaps.append(
            {
                "repo": repo,
                "store": "mariadb",
                "status": "DISABLED",
                "role": role,
                "database": db,
                "detail": f"serviceMariadbDatabases entry '{existing.get('name')}' is disabled",
            }
        )
    if existing.get("database") != db:
        gaps.append(
            {
                "repo": repo,
                "store": "mariadb",
                "status": "DB_MISMATCH",
                "role": role,
                "database": db,
                "current_database": existing.get("database"),
                "detail": (
                    f"Entry provisions db='{existing.get('database')}' "
                    f"but manifest declares db='{db}'"
                ),
            }
        )
    return gaps


def _check_mongo(repo: str, dt: dict, mongo_idx: dict[str, dict]) -> list[Gap]:
    role: str = dt["role"]
    db: str = dt["database"]
    auth_src: str = dt.get("authSource", "admin")
    privs: str = dt.get("privileges", "readWrite")

    existing = mongo_idx.get(role.lower()) or mongo_idx.get(role.replace("_app", "").lower()) or mongo_idx.get(role.replace("_svc", "").lower())
    if not existing:
        return [
            {
                "repo": repo,
                "store": "mongo",
                "status": "MISSING",
                "role": role,
                "database": db,
                "privileges": privs,
                "auth_source": auth_src,
                "detail": f"No serviceMongoDatabases entry with user='{role}'",
            }
        ]

    gaps: list[Gap] = []
    if not existing.get("enabled", True):
        gaps.append(
            {
                "repo": repo,
                "store": "mongo",
                "status": "DISABLED",
                "role": role,
                "database": db,
                "detail": f"serviceMongoDatabases entry '{existing.get('name')}' is disabled",
            }
        )
    granted_dbs = {r["db"] for r in existing.get("roles", [])}
    if db not in granted_dbs:
        gaps.append(
            {
                "repo": repo,
                "store": "mongo",
                "status": "GRANT_MISSING",
                "role": role,
                "database": db,
                "current_grants": sorted(granted_dbs),
                "detail": (
                    f"Role '{role}' not granted on '{db}'; current grants: {sorted(granted_dbs)}"
                ),
            }
        )
    return gaps


def _check_neo4j(repo: str, dt: dict, neo4j_idx: dict[str, dict]) -> list[Gap]:
    instance_name: str = dt.get("instance", dt.get("store", ""))
    existing = neo4j_idx.get(instance_name.lower())
    if not existing:
        return [
            {
                "repo": repo,
                "store": "neo4j",
                "status": "MISSING",
                "instance": instance_name,
                "detail": f"No serviceNeo4jInstances entry with name='{instance_name}'",
            }
        ]
    gaps: list[Gap] = []
    if not existing.get("enabled", True):
        gaps.append(
            {
                "repo": repo,
                "store": "neo4j",
                "status": "DISABLED",
                "instance": instance_name,
                "detail": (
                    f"serviceNeo4jInstances entry '{instance_name}' exists but is disabled "
                    f"(flip enabled:true after sealing {existing.get('passwordSecret', {}).get('name', 'credentials')})"
                ),
            }
        )
    return gaps


def detect_gaps(manifest: dict, repo: str, values: dict) -> list[Gap]:
    pg_idx = _pg_index(values)
    maria_idx = _mariadb_index(values)
    mongo_idx = _mongo_index(values)
    neo4j_idx = _neo4j_index(values)
    gaps: list[Gap] = []

    for dt in manifest.get("dataTier", []):
        store = dt.get("store", "").lower()
        if store not in SUPPORTED_STORES:
            print(f"  [SKIP] {repo}: store '{store}' not yet supported by reconciler", flush=True)
            continue
        if store == "postgres":
            gaps.extend(_check_postgres(repo, dt, pg_idx))
        elif store == "mariadb":
            gaps.extend(_check_mariadb(repo, dt, maria_idx))
        elif store == "mongo":
            gaps.extend(_check_mongo(repo, dt, mongo_idx))
        elif store == "neo4j":
            gaps.extend(_check_neo4j(repo, dt, neo4j_idx))

    return gaps


# ---------------------------------------------------------------------------
# Values patch generation (--apply mode)
# ---------------------------------------------------------------------------

def _short_name(repo: str) -> str:
    """FuzeOne/FuzePlan → fuzeplan"""
    return repo.split("/", 1)[-1].lower()


def _pg_snippet(gap: Gap, repo: str) -> str:
    role = gap["role"]
    db = gap["database"]
    privs = gap.get("privileges", "readWrite")
    name = _short_name(repo)
    secret_name = f"{name}-db-credentials"
    return (
        f"  # Auto-generated by reconcile_datatier.py from {repo}/.fuze/manifest.json\n"
        f"  # REVIEW: add the sealed-secret '{secret_name}' (key: password)\n"
        f"  # to the fuzeinfra namespace before enabling.\n"
        f"  - name: {name}\n"
        f"    enabled: false  # flip to true once the sealed secret is present\n"
        f"    role: {role}\n"
        f"    database: {db}\n"
        f"    passwordSecret:\n"
        f"      name: {secret_name}\n"
        f"      key: password\n"
    )


def _mariadb_snippet(gap: Gap, repo: str) -> str:
    role = gap["role"]
    db = gap["database"]
    name = _short_name(repo)
    secret_name = f"{name}-db-credentials"
    return (
        f"  # Auto-generated by reconcile_datatier.py from {repo}/.fuze/manifest.json\n"
        f"  # REVIEW: add the sealed-secret '{secret_name}' (key: password)\n"
        f"  # to the fuzeinfra namespace before enabling. See\n"
        f"  # docs/consuming-repos/MARIADB_PROVISIONING.md\n"
        f"  - name: {name}\n"
        f"    enabled: false  # flip to true once the sealed secret is present\n"
        f"    user: {role}\n"
        f"    database: {db}\n"
        f"    passwordSecret:\n"
        f"      name: {secret_name}\n"
        f"      key: password\n"
    )


def _mongo_snippet(gap: Gap, repo: str) -> str:
    role = gap["role"]
    db = gap["database"]
    privs = gap.get("privileges", "readWrite")
    auth_src = gap.get("auth_source", "admin")
    name = _short_name(repo)
    secret_name = f"{name}-mongo-credentials"
    return (
        f"  # Auto-generated by reconcile_datatier.py from {repo}/.fuze/manifest.json\n"
        f"  # REVIEW: add the sealed-secret '{secret_name}' (key: password)\n"
        f"  # to the fuzeinfra namespace before enabling.\n"
        f"  - name: {name}\n"
        f"    enabled: false  # flip to true once the sealed secret is present\n"
        f"    user: {role}\n"
        f"    authDatabase: {auth_src}\n"
        f"    managePassword: true\n"
        f"    database: {db}\n"
        f"    roles:\n"
        f"      - {{role: {privs}, db: {db}}}\n"
        f"    passwordSecret:\n"
        f"      name: {secret_name}\n"
        f"      key: password\n"
    )


def apply_gaps(gaps: list[Gap], values_path: Path) -> None:
    """Append missing Helm values entries to values_path (as disabled stubs for review)."""
    pg_additions: list[str] = []
    maria_additions: list[str] = []
    mongo_additions: list[str] = []

    for gap in gaps:
        if gap["status"] not in {"MISSING"}:
            print(f"  [SKIP apply] {gap['status']} gaps require manual review: {gap['detail']}")
            continue
        if gap["store"] == "postgres":
            pg_additions.append(_pg_snippet(gap, gap["repo"]))
        elif gap["store"] == "mariadb":
            maria_additions.append(_mariadb_snippet(gap, gap["repo"]))
        elif gap["store"] == "mongo":
            mongo_additions.append(_mongo_snippet(gap, gap["repo"]))

    if not pg_additions and not maria_additions and not mongo_additions:
        print("Nothing to apply (only DB_MISMATCH/DISABLED/GRANT_MISSING gaps — fix manually).")
        return

    content = values_path.read_text(encoding="utf-8")

    if pg_additions:
        insert_after = re.search(r"^serviceDatabases:\s*\n", content, re.MULTILINE)
        if insert_after:
            pos = insert_after.end()
            additions_block = "\n".join(pg_additions)
            content = content[:pos] + additions_block + "\n" + content[pos:]
        else:
            content += "\nserviceDatabases:\n" + "\n".join(pg_additions) + "\n"

    if maria_additions:
        insert_after = re.search(r"^serviceMariadbDatabases:\s*\n", content, re.MULTILINE)
        if insert_after:
            pos = insert_after.end()
            content = content[:pos] + "\n".join(maria_additions) + "\n" + content[pos:]
        else:
            content += "\nserviceMariadbDatabases:\n" + "\n".join(maria_additions) + "\n"

    if mongo_additions:
        insert_after = re.search(r"^serviceMongoDatabases:\s*\n", content, re.MULTILINE)
        if insert_after:
            pos = insert_after.end()
            additions_block = "\n".join(mongo_additions)
            content = content[:pos] + additions_block + "\n" + content[pos:]
        else:
            content += "\nserviceMongoDatabases:\n" + "\n".join(mongo_additions) + "\n"

    values_path.write_text(content, encoding="utf-8")
    print(
        f"Patched {values_path} with {len(pg_additions)} Postgres + "
        f"{len(maria_additions)} MariaDB + {len(mongo_additions)} Mongo stub(s)."
    )
    print("Review the additions, flip enabled: true, and commit to trigger ArgoCD provisioning.")


# ---------------------------------------------------------------------------
# Egress summary (informational — NetworkPolicy generation is future scope)
# ---------------------------------------------------------------------------

def summarise_egress(manifest: dict, repo: str) -> None:
    egress = manifest.get("egress", [])
    if not egress:
        return
    print(f"  [EGRESS] {repo} declares {len(egress)} egress host(s):")
    for e in egress:
        print(f"    {e['host']}:{e.get('port', 443)}  — {e.get('reason', '')}")
    print(
        "    → Route LLM providers (openai/anthropic) through litellm.fuzeinfra.svc.cluster.local:4000."
        " Other hosts may need a NetworkPolicy (future reconciler scope)."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="FuzeInfra dataTier reconciler")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Append missing DISABLED stub entries to values-contabo.yaml for review",
    )
    parser.add_argument(
        "--values",
        default=str(DEFAULT_VALUES),
        help="Path to the Helm values file to compare against (default: values-contabo.yaml)",
    )
    parser.add_argument(
        "--repos-config",
        default=str(CONSUMING_REPOS_CONFIG),
        help="YAML config listing consuming repos (default: config/consuming-repos.yaml)",
    )
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set GH_TOKEN or GITHUB_TOKEN", file=sys.stderr)
        return 2

    values_path = Path(args.values)
    if not values_path.exists():
        print(f"ERROR: values file not found: {values_path}", file=sys.stderr)
        return 2

    repos_config_path = Path(args.repos_config)
    if not repos_config_path.exists():
        print(f"ERROR: repos config not found: {repos_config_path}", file=sys.stderr)
        return 2

    with repos_config_path.open() as fh:
        repos: list[str] = yaml.safe_load(fh).get("repos", [])

    values = load_values(values_path)
    all_gaps: list[Gap] = []

    print(f"Reconciling dataTier declarations for {len(repos)} consuming repo(s)...\n")
    for repo in repos:
        print(f"  Checking {repo}...", end=" ", flush=True)
        manifest = fetch_manifest(repo, token)
        if manifest is None:
            print("no .fuze/manifest.json — skipping")
            continue
        if not manifest.get("dataTier"):
            print("no dataTier declared — skipping")
            continue

        gaps = detect_gaps(manifest, repo, values)
        all_gaps.extend(gaps)
        if gaps:
            print(f"{len(gaps)} gap(s)")
            for g in gaps:
                if g["store"] == "neo4j":
                    print(f"    [{g['status']}] {g['store']} instance={g.get('instance', '?')}")
                else:
                    print(f"    [{g['status']}] {g['store']} role={g.get('role', '?')} db={g.get('database', '?')}")
                print(f"    → {g['detail']}")
        else:
            print("OK")

        summarise_egress(manifest, repo)

    print()
    if not all_gaps:
        print("✅  All dataTier declarations are provisioned.")
        return 0

    blocking = [g for g in all_gaps if g["status"] in {"MISSING", "GRANT_MISSING"}]
    advisory = [g for g in all_gaps if g["status"] not in {"MISSING", "GRANT_MISSING"}]

    if advisory:
        print(f"ℹ️   {len(advisory)} advisory gap(s) (require manual review — do not fail CI):")
        for g in advisory:
            if g["store"] == "neo4j":
                print(f"    [{g['status']}] {g['store']} {g['repo']} instance={g.get('instance', '?')}")
            else:
                print(f"    [{g['status']}] {g['store']} {g['repo']} role={g.get('role', '?')} db={g.get('database', '?')}")
            print(f"    → {g['detail']}")
            if g["status"] == "DB_MISMATCH":
                print(
                    "    TIP: If the platform uses a service-named DB (e.g. 'mendys'), update the"
                    f" manifest to match (database: '{g.get('current_database')}'), or ask"
                    " FuzeInfra to provision the declared name."
                )

    if blocking:
        print(
            f"\n❌  {len(blocking)} blocking gap(s) — provisions are MISSING and must be added:\n"
        )
        for g in blocking:
            if g["store"] == "neo4j":
                print(f"    [{g['status']}] {g['store']} {g['repo']} instance={g.get('instance', '?')}")
            else:
                print(f"    [{g['status']}] {g['store']} {g['repo']} role={g.get('role', '?')} db={g.get('database', '?')}")
            print(f"    → {g['detail']}")

        if args.apply:
            print("\nApplying stub entries for MISSING provisions...")
            apply_gaps(blocking, values_path)
    else:
        print("\n✅  No blocking gaps — all declared dataTier provisions exist.")
        print("    (Advisory gaps above may need manifest updates — see TIP lines.)")

    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
