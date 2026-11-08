#!/usr/bin/env python3
"""Unit tests for validate_infra_request.validate().

validate() returns (decision, reasons) where decision is one of:
  "skip"  -> not an infra-request (no requests); handler no-ops
  "apply" -> whitelisted; handler auto-applies
  "gate"  -> valid request, not whitelisted; handler opens a gated PR
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from validate_infra_request import validate  # noqa: E402

WHITELIST = {
    "allowed_product_ids": ["V45", "V46"],
    "allowed_regions": ["EU"],
    "allowed_roles": ["workload"],
    "max_nodes_per_request": 3,
    "allowed_repos": ["FuzeOne/FuzeFront"],
}


class ValidateTests(unittest.TestCase):
    def test_valid_request_auto_applies(self):
        payload = {"requests": [{"name": "w1", "product_id": "V45", "region": "EU", "role": "workload"}]}
        decision, reasons = validate(WHITELIST, payload, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "apply", reasons)

    def test_defaults_region_and_role(self):
        payload = {"requests": [{"name": "w1", "product_id": "V46"}]}
        decision, _ = validate(WHITELIST, payload, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "apply")

    def test_bad_product_region_role_gated(self):
        payload = {"requests": [{"name": "w1", "product_id": "V99", "region": "US", "role": "server"}]}
        decision, reasons = validate(WHITELIST, payload, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "gate")
        self.assertEqual(len(reasons), 3)

    def test_unknown_repo_gated(self):
        payload = {"requests": [{"name": "w1", "product_id": "V45"}]}
        decision, _ = validate(WHITELIST, payload, "stranger/Repo")
        self.assertEqual(decision, "gate")

    def test_too_many_nodes_gated(self):
        payload = {"requests": [{"name": f"w{i}", "product_id": "V45"} for i in range(4)]}
        decision, _ = validate(WHITELIST, payload, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "gate")

    def test_empty_requests_skipped(self):
        # No infra requests -> not an infra-request -> skip (NOT a gated PR).
        decision, _ = validate(WHITELIST, {"requests": []}, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "skip")

    def test_missing_requests_key_skipped(self):
        # An argo-only / unrelated dispatch carries no `requests` key at all.
        decision, _ = validate(WHITELIST, {"changed": "deploy/argocd/applications/x.yaml"}, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "skip")

    def test_approved_bypasses_whitelist(self):
        # A human merged the gated PR → re-dispatch carries approved=true → apply
        # even though the product_id is NOT whitelisted.
        payload = {"approved": True, "requests": [{"name": "w1", "product_id": "V92", "region": "EU", "role": "workload"}]}
        decision, _ = validate(WHITELIST, payload, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "apply")

    def test_approved_still_skips_when_no_requests(self):
        # approved is irrelevant if there's nothing to provision.
        decision, _ = validate(WHITELIST, {"approved": True, "requests": []}, "FuzeOne/FuzeFront")
        self.assertEqual(decision, "skip")


if __name__ == "__main__":
    unittest.main()
