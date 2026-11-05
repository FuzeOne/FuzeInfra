"""Prometheus' TSDB claim: one source of truth, retired claims kept, backup follows.

2026-10-14: fuzeinfra-prometheus-data had a corrupt ext4 that kubelet's mount-time
fsck could not repair, wedging the pod and Argo's sync. The fix is a fresh claim
(`prometheus.pvcName`) with the damaged one kept in `prometheus.retiredPvcs`.
Offline: helm template only.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "fuzeinfra"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(overlay):
    out = subprocess.run(
        ["helm", "template", "fuzeinfra", str(CHART), "--namespace", "fuzeinfra",
         "-f", str(CHART / overlay)],
        capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _by(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def test_deployment_mounts_the_active_claim():
    docs = _render("values-contabo.yaml")
    dep = _by(docs, "Deployment", "fuzeinfra-prometheus")
    vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    claim = vols["data"]["persistentVolumeClaim"]["claimName"]
    assert claim == "fuzeinfra-prometheus-data-v2"
    _by(docs, "PersistentVolumeClaim", claim)


def test_retired_claim_is_declared_kept_and_never_mounted():
    docs = _render("values-contabo.yaml")
    old = _by(docs, "PersistentVolumeClaim", "fuzeinfra-prometheus-data")
    ann = old["metadata"]["annotations"]
    assert ann["argocd.argoproj.io/sync-options"] == "Prune=false"
    assert ann["helm.sh/resource-policy"] == "keep"
    for d in docs:
        spec = (d.get("spec") or {}).get("template", {}).get("spec", {})
        for v in spec.get("volumes", []):
            claim = v.get("persistentVolumeClaim", {}).get("claimName")
            assert claim != "fuzeinfra-prometheus-data", f"{d['metadata']['name']} mounts the corrupt claim"


def test_backup_cronjob_follows_the_active_claim():
    docs = _render("values-contabo.yaml")
    job = _by(docs, "CronJob", "fuzeinfra-backup-prometheus")
    vols = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    claims = {v["persistentVolumeClaim"]["claimName"] for v in vols if "persistentVolumeClaim" in v}
    assert claims == {"fuzeinfra-prometheus-data-v2"}


def test_retired_claims_do_not_follow_the_active_size():
    """2026-11-05: raising prometheus.storage to 40Gi must not expand the retired claim."""
    docs = _render("values-contabo.yaml")
    active = _by(docs, "PersistentVolumeClaim", "fuzeinfra-prometheus-data-v2")
    retired = _by(docs, "PersistentVolumeClaim", "fuzeinfra-prometheus-data")
    assert active["spec"]["resources"]["requests"]["storage"] == "40Gi"
    assert retired["spec"]["resources"]["requests"]["storage"] == "20Gi"
