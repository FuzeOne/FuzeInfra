import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("storage_provisioning", ROOT / "scripts/enable-service-object-storage-provisioning.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def copied_root(tmp_path):
    shutil.copytree(ROOT / "governance", tmp_path / "governance")
    return tmp_path


def request():
    return {"allocationId": "fuzex-artifact-storage", "consumerRepo": "FuzeOne/FuzeX"}


def write_sealed_source(root):
    source = root / "deploy/sealed-secrets"
    source.mkdir(parents=True)
    (source / "fuzex-object-storage.yaml").write_text("""apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: fuzex-object-storage
  namespace: fuzeinfra
spec:
  encryptedData:
    config: AgNotPlaintext
""")


def test_enables_only_matching_disabled_allocation_and_handoff(tmp_path):
    root = copied_root(tmp_path)
    write_sealed_source(root)
    MODULE.enable(root, request())
    allocations = json.loads((root / "governance/object-storage-allocations.json").read_text())
    handoffs = json.loads((root / "governance/credential-handoff.json").read_text())
    assert next(x for x in allocations["allocations"] if x["id"] == "fuzex-artifact-storage")["enabled"] is True
    assert next(x for x in handoffs["handoffs"] if x["id"] == "fuzex-artifact-storage")["enabled"] is True


def test_rejects_a_consumer_that_does_not_match_the_runtime_declaration(tmp_path):
    root = copied_root(tmp_path)
    write_sealed_source(root)
    with pytest.raises(ValueError, match="do not match"):
        MODULE.enable(root, {"allocationId": "fuzex-artifact-storage", "consumerRepo": "izzywdev/Other"})


def test_refuses_to_enable_before_fuzeinfra_has_sealed_the_provider_source(tmp_path):
    root = copied_root(tmp_path)
    with pytest.raises(ValueError, match="has not been sealed"):
        MODULE.enable(root, request())


def test_refuses_a_placeholder_or_plaintext_source_manifest(tmp_path):
    root = copied_root(tmp_path)
    source = root / "deploy/sealed-secrets"
    source.mkdir(parents=True)
    (source / "fuzex-object-storage.yaml").write_text("""apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: fuzex-object-storage
  namespace: fuzeinfra
spec:
  stringData:
    config: unsafe
""")
    with pytest.raises(ValueError, match="plaintext|encrypted config"):
        MODULE.enable(root, request())


def test_request_parser_is_exact(monkeypatch):
    monkeypatch.setenv("OBJECT_STORAGE_PROVISION_REQUEST", json.dumps(request()))
    assert MODULE.request_from_env() == request()
    monkeypatch.setenv("OBJECT_STORAGE_PROVISION_REQUEST", json.dumps({**request(), "extra": True}))
    with pytest.raises(ValueError, match="shape"):
        MODULE.request_from_env()
