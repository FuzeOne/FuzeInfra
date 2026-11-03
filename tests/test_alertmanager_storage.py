"""Alertmanager must not depend on a StatefulSet volumeClaimTemplate.

2026-10-13..11-03: data-fuzeinfra-alertmanager-0 (Longhorn pvc-d16da7af) lost 2 of
3 replicas, Longhorn could not schedule new ones ("disks are unavailable"), and the
pod sat in ContainerCreating for 15 days. No alert could be delivered, and Argo's
fuzeinfra-prod sync waited on the StatefulSet's health the whole time. A StatefulSet
cannot drop its volumeClaimTemplate, so the chart runs Alertmanager as a Deployment:
emptyDir by default, or an optional standalone PVC.

Offline: helm template only.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "fuzeinfra"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(*args):
    out = subprocess.run(
        ["helm", "template", "fuzeinfra", str(CHART), "--namespace", "fuzeinfra",
         "-f", str(CHART / "values-contabo.yaml"), *args],
        capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _named(docs, name):
    return [d for d in docs if d["metadata"]["name"] == name]


def test_alertmanager_is_a_deployment_without_claim_templates():
    docs = _named(_render(), "fuzeinfra-alertmanager")
    kinds = {d["kind"] for d in docs}
    assert "StatefulSet" not in kinds
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert dep["spec"]["strategy"]["type"] == "Recreate"
    assert dep["spec"]["replicas"] == 1
    vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    assert "emptyDir" in vols["data"], "default must not depend on a Longhorn volume"
    mounts = {m["name"]: m["mountPath"] for c in dep["spec"]["template"]["spec"]["containers"]
              for m in c["volumeMounts"]}
    assert mounts["data"] == "/alertmanager"


def test_optional_persistence_is_a_standalone_kept_pvc():
    docs = _render("--set", "alertmanager.persistence.enabled=true")
    pvc = next(d for d in docs if d["kind"] == "PersistentVolumeClaim"
               and d["metadata"]["name"] == "fuzeinfra-alertmanager-data")
    assert pvc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == "Prune=false"
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    dep = next(d for d in _named(docs, "fuzeinfra-alertmanager") if d["kind"] == "Deployment")
    vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    assert vols["data"]["persistentVolumeClaim"]["claimName"] == "fuzeinfra-alertmanager-data"


def test_service_still_routes_to_the_pod():
    """Prometheus and the Loki ruler address Alertmanager by Service name."""
    docs = _render()
    svc = next(d for d in _named(docs, "fuzeinfra-alertmanager") if d["kind"] == "Service")
    dep = next(d for d in _named(docs, "fuzeinfra-alertmanager") if d["kind"] == "Deployment")
    labels = dep["spec"]["template"]["metadata"]["labels"]
    assert all(labels.get(k) == v for k, v in svc["spec"]["selector"].items())
    # The admin-plane NetworkPolicy opt-in must survive the kind change.
    assert labels.get("fuzeinfra.io/ingress-profile") == "admin"
