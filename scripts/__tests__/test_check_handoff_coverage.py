"""Tests for check_handoff_coverage.

ASSERTS THE CHECK FAILS ON A GAP, not merely that it passes on a clean registry.
That distinction is the whole point: the defect this check exists to catch --
seven repos reading a Secret with no delivery path -- was invisible precisely
because absence produces no failure. A check that has only ever been observed
passing would reproduce that.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import check_handoff_coverage as chc  # noqa: E402


def registry(*namespaces, disabled=(), source="fuzefront"):
    src = {"namespace": source, "secretName": "fuzefront-registration", "secretKey": "token"}
    return {"handoffs": (
        [{"id": f"{ns}-registration", "enabled": True, "source": src,
          "target": {"namespace": ns}} for ns in namespaces]
        + [{"id": f"{ns}-registration", "enabled": False, "source": src,
            "target": {"namespace": ns}} for ns in disabled]
    )}


class CoverageTest(unittest.TestCase):
    def _run(self, reg, repos, manifests, consumers=None):
        """consumers defaults to "every repo consumes it", which is the strict case."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(reg, fh)
            path = fh.name
        argv = ["check_handoff_coverage.py", "--registry", path]
        try:
            # Shape these like the REAL `gh search code --json repository` payload.
            # The first version of this test passed bare name lists, which is why it
            # missed the crash CI found: the stub was easier to satisfy than gh is.
            names = repos if consumers is None else consumers
            cons = [{"repository": {"name": n}} for n in names]
            calls = {"n": 0}

            def fake_gh(*a):
                # first call lists repos, second is the code search for consumers
                calls["n"] += 1
                return repos if calls["n"] == 1 else cons

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(chc, "gh_json", side_effect=fake_gh), \
                 mock.patch.object(chc, "manifest_for", side_effect=lambda org, r: manifests.get(r)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = chc.main()
                return rc, buf.getvalue()
        finally:
            os.unlink(path)

    def test_fails_when_a_portal_product_has_no_entry(self):
        rc, out = self._run(registry("fuzebi"), ["FuzeBI", "FuzeMarket"],
                            {"FuzeBI": {"tier": "product"}, "FuzeMarket": {"tier": "product"}})
        self.assertEqual(rc, 1, "a portal product with no entry MUST fail the check")
        self.assertIn("FuzeMarket", out)
        self.assertIn("no entry", out)

    def test_passes_when_every_product_is_covered(self):
        rc, out = self._run(registry("fuzebi", "fuzemarket"), ["FuzeBI", "FuzeMarket"],
                            {"FuzeBI": {"tier": "product"}, "FuzeMarket": {"tier": "product"}})
        self.assertEqual(rc, 0)
        self.assertIn("has an enabled hand-off entry", out)

    def test_a_disabled_entry_is_not_coverage(self):
        # fuzequality is disabled because its repo cannot be resolved. If it returns,
        # this check must say so rather than counting it as listed.
        rc, out = self._run(registry("fuzebi", disabled=("fuzequality",)),
                            ["FuzeBI", "FuzeQuality"],
                            {"FuzeBI": {"tier": "product"}, "FuzeQuality": {"tier": "product"}})
        self.assertEqual(rc, 1)
        self.assertIn("DISABLED", out)

    def test_opt_out_and_non_product_tiers_are_skipped(self):
        # Mirrors sdlc-bootstrap's portal_registration.declared(); if these diverge the
        # check nags repos that legitimately opted out.
        rc, out = self._run(registry(), ["FuzeX", "FuzeSDLC", "FuzeInfra"], {
            "FuzeX": {"tier": "product", "portal": {"registers": False}},
            "FuzeSDLC": {"tier": "governance"},
            "FuzeInfra": {"tier": "infra"},
        })
        self.assertEqual(rc, 0, "opted-out and non-product repos must not be demanded")
        self.assertIn("portal.registers is false", out)
        self.assertIn("tier=governance", out)

    def test_repo_without_a_manifest_is_ignored(self):
        rc, _ = self._run(registry(), ["SomeRandomRepo"], {})
        self.assertEqual(rc, 0, "a repo not onboarded to the SDLC is not a portal product")


    def test_a_product_not_yet_consuming_the_secret_only_warns(self):
        # Enforcement ahead of adoption is the failure mode that made gate-identifier
        # block every rollout PR. A product that has not wired registration yet is a
        # future task, not a live outage, and must not fail the gate.
        rc, out = self._run(registry(), ["FuzeAgent"], {"FuzeAgent": {"tier": "product"}},
                            consumers=["SomeOtherRepo"])
        self.assertEqual(rc, 0, "a product not consuming the Secret must not fail the gate")
        self.assertIn("not consuming the Secret yet", out)

    def test_a_consumer_without_an_entry_still_fails(self):
        rc, out = self._run(registry(), ["FuzeMarket"], {"FuzeMarket": {"tier": "product"}},
                            consumers=["FuzeMarket"])
        self.assertEqual(rc, 1, "a repo consuming the Secret with no entry is a live gap")
        self.assertIn("CONSUMING the Secret", out)

    def test_empty_code_search_is_an_error_not_an_all_clear(self):
        # An empty search would silently reclassify every live gap as a warning.
        with self.assertRaises(SystemExit) as cm:
            self._run(registry(), ["FuzeMarket"], {"FuzeMarket": {"tier": "product"}}, consumers=[])
        self.assertEqual(cm.exception.code, 1)


    def test_malformed_search_payload_does_not_crash(self):
        # CI hit AttributeError: 'NoneType' has no attribute 'lower' because a --jq
        # projection produced nulls. Entries of the wrong shape must be skipped, not
        # dereferenced.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(registry("fuzemarket"), fh)
            path = fh.name
        junk = [None, {}, {"repository": None}, "nonsense",
                {"repository": {"nameWithOwner": "FuzeOne/FuzeMarket"}}]
        calls = {"n": 0}

        def fake_gh(*a):
            calls["n"] += 1
            return ["FuzeMarket"] if calls["n"] == 1 else junk

        try:
            with mock.patch.object(sys, "argv", ["x", "--registry", path]), \
                 mock.patch.object(chc, "gh_json", side_effect=fake_gh), \
                 mock.patch.object(chc, "manifest_for",
                                   side_effect=lambda o, r: {"tier": "product"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = chc.main()
        finally:
            os.unlink(path)
        # nameWithOwner fallback resolves FuzeMarket, which IS covered -> pass
        self.assertEqual(rc, 0)
        self.assertIn("has an enabled hand-off entry", buf.getvalue())


    def test_the_publisher_repo_is_not_reported_as_a_consumer(self):
        # FuzeFront's repo references fuzefront-registration because it MINTS it.
        # Code search cannot distinguish minting from consuming; the registry's
        # source.namespace can.
        rc, out = self._run(registry("fuzebi", source="fuzefront"),
                            ["FuzeFront", "FuzeBI"],
                            {"FuzeFront": {"tier": "product"}, "FuzeBI": {"tier": "product"}},
                            consumers=["FuzeFront", "FuzeBI"])
        self.assertEqual(rc, 0, "the publisher must not be demanded to have a delivery path")
        self.assertIn("publisher of the Secret", out)


if __name__ == "__main__":
    unittest.main()
