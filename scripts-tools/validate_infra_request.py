#!/usr/bin/env python3
"""Validate a dispatched infra-request against the auto-apply whitelist.

Used by the infra-request-handler workflow to decide what to do with a dispatch.
The handler is Terraform-only: it reconciles infra (node) provisioning requests
and nothing else. ArgoCD owns the consumer's argo manifests after the one-time
initial registration, so a dispatch that carries no infra `requests` (e.g. an
argo-only or unrelated change that wrongly fired the consumer's dispatch) is NOT
an infra-request at all and must be a clean no-op — never a gated PR.

There are three possible decisions:
  * "skip"  -> payload has no non-empty `requests` list. Not an infra-request;
               the handler does nothing (no terraform, no PR).
  * "apply" -> a real request from an allowed repo where every node satisfies the
               whitelist (product_id, region, role, bounded count) -> auto-apply.
  * "gate"  -> a real request that violates the whitelist -> manual-approval PR
               carrying `terraform plan`.

Usage:
    validate_infra_request.py --whitelist config/infra-request-whitelist.json \
        --request request.json --repo FuzeOne/FuzeFront [--github-output "$GITHUB_OUTPUT"]

request.json is the dispatch client_payload, expected to contain a top-level
"requests" list of {name, product_id, region, role, labels} objects.

Exit code is always 0 (the decision is conveyed via output, not failure).
Writes `decision=skip|apply|gate`, `whitelisted=true|false` (true iff apply) and
`reasons=<text>` to --github-output when given.
"""
import argparse
import json
import sys


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate(whitelist, payload, repo):
    """Return (decision, reasons) where decision is 'skip' | 'apply' | 'gate'."""
    reasons = []

    requests = payload.get("requests")
    if not isinstance(requests, list) or not requests:
        # Not an infra-request: nothing to provision. The handler no-ops rather
        # than opening a gated PR (argo-only/unrelated changes land here).
        return "skip", ["no infra 'requests' in payload — nothing to reconcile (not an infra-request)"]

    # Manually approved: a human merged the gated PR, which re-dispatches the
    # request with approved=true. That merge IS the approval, so apply regardless
    # of the whitelist (the whitelist only governs UNATTENDED auto-apply).
    if payload.get("approved") is True:
        return "apply", ["manually approved via gated-PR merge — whitelist bypassed"]

    allowed_repos = whitelist.get("allowed_repos")
    if allowed_repos and repo not in allowed_repos:
        reasons.append(f"repo '{repo}' is not in allowed_repos")

    max_nodes = whitelist.get("max_nodes_per_request")
    if isinstance(max_nodes, int) and len(requests) > max_nodes:
        reasons.append(f"{len(requests)} nodes requested, max allowed is {max_nodes}")

    allowed_products = whitelist.get("allowed_product_ids", [])
    allowed_regions = whitelist.get("allowed_regions", [])
    allowed_roles = whitelist.get("allowed_roles", [])

    for i, req in enumerate(requests):
        name = req.get("name", f"#{i}")
        product_id = req.get("product_id")
        region = req.get("region", "EU")
        role = req.get("role", "workload")

        if allowed_products and product_id not in allowed_products:
            reasons.append(f"node '{name}': product_id '{product_id}' not in {allowed_products}")
        if allowed_regions and region not in allowed_regions:
            reasons.append(f"node '{name}': region '{region}' not in {allowed_regions}")
        if allowed_roles and role not in allowed_roles:
            reasons.append(f"node '{name}': role '{role}' not in {allowed_roles}")

    return ("apply" if not reasons else "gate"), reasons


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--whitelist", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--repo", required=True, help="owner/name of the requesting repo")
    parser.add_argument("--github-output", default=None, help="path to $GITHUB_OUTPUT to append to")
    args = parser.parse_args(argv)

    whitelist = load_json(args.whitelist)
    payload = load_json(args.request)

    decision, reasons = validate(whitelist, payload, args.repo)
    reason_text = "; ".join(reasons) if reasons else "all nodes satisfy the whitelist"
    whitelisted = "true" if decision == "apply" else "false"

    print(f"decision={decision}")
    print(f"whitelisted={whitelisted}")
    print(f"reasons={reason_text}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"decision={decision}\n")
            fh.write(f"whitelisted={whitelisted}\n")
            fh.write(f"reasons={reason_text}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
