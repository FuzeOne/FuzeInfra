import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "service_provisioning", ROOT / "scripts/enable-service-postgres-provisioning.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

REQUEST = {
    "service": "fuzex", "role": "fuzex_svc", "database": "fuzex_design_frames",
    "consumerRepo": "FuzeOne/FuzeX", "handoffId": "fuzex-postgres",
    "target": {"namespace": "fuzex", "secretName": "fuzex-design-frames-db",
               "manifestPath": "deploy/helm/fuzex/files/secrets/design-frames-db-sealed.yaml", "branch": "master"},
}


@pytest.fixture
def checkout(tmp_path):
    for name in ("helm/fuzeinfra/values-contabo.yaml", "governance/credential-handoff.json"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, target)
    sealed = tmp_path / "deploy/sealed-secrets/fuzex-db-credentials.yaml"
    sealed.parent.mkdir(parents=True)
    sealed.write_text(yaml.safe_dump({
        "kind": "SealedSecret", "metadata": {"namespace": "fuzeinfra", "name": "fuzex-db-credentials"},
        "spec": {"encryptedData": {"password": "fixture-ciphertext"}},
    }))
    return tmp_path


def test_enables_only_matching_predeclared_runtime_allocation(checkout):
    module.enable(checkout, REQUEST)
    values = yaml.safe_load((checkout / "helm/fuzeinfra/values-contabo.yaml").read_text())
    registry = json.loads((checkout / "governance/credential-handoff.json").read_text())
    assert next(item for item in values["serviceDatabases"] if item["name"] == "fuzex")["enabled"] is True
    assert next(item for item in registry["handoffs"] if item["id"] == "fuzex-postgres")["enabled"] is True
    with pytest.raises(ValueError, match="disabled Postgres allocation"):
        module.enable(checkout, REQUEST)


@pytest.mark.parametrize("mutation", ["wrong-repo", "unsafe-path", "wrong-role", "broad-scope"])
def test_rejects_bad_request_or_ciphertext_without_mutating_gates(checkout, mutation):
    request = json.loads(json.dumps(REQUEST))
    if mutation == "wrong-repo":
        request["consumerRepo"] = "other-org/FuzeX"
        with pytest.raises(ValueError, match="credential handoff"):
            module.enable(checkout, request)
        return
    if mutation == "unsafe-path":
        request["target"]["manifestPath"] = "deploy/../escape.yaml"
        with pytest.raises(ValueError, match="invalid identifier"):
            os.environ["POSTGRES_PROVISION_REQUEST"] = json.dumps(request)
            module.request_from_env()
        return
    if mutation == "wrong-role":
        request["role"] = "different_role"
    if mutation == "broad-scope":
        secret_path = checkout / "deploy/sealed-secrets/fuzex-db-credentials.yaml"
        secret = yaml.safe_load(secret_path.read_text())
        secret["metadata"]["annotations"] = {"sealedsecrets.bitnami.com/cluster-wide": "true"}
        secret_path.write_text(yaml.safe_dump(secret))
    before = ((checkout / "helm/fuzeinfra/values-contabo.yaml").read_text(),
              (checkout / "governance/credential-handoff.json").read_text())
    with pytest.raises(ValueError):
        module.enable(checkout, request)
    assert before == ((checkout / "helm/fuzeinfra/values-contabo.yaml").read_text(),
                      (checkout / "governance/credential-handoff.json").read_text())


def test_workflow_is_generic_and_has_no_direct_cluster_access():
    workflow = yaml.safe_load((ROOT / ".github/workflows/provision-service-postgres.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {"repository_dispatch": {"types": ["provision-service-postgres"]}}
    assert "fuzex" not in (ROOT / ".github/workflows/provision-service-postgres.yml").read_text().lower()
    for step in workflow["jobs"]["provision"]["steps"]:
        assert "kubectl" not in step.get("run", "")
