"""Prometheus' TSDB volume must leave headroom above retention.size.

2026-11-05: fuzeinfra-prometheus-data-v2 (20Gi) hit "no space left on device" with
retention.size at 15GB. retention.size bounds blocks + WAL but not <tsdb>/snapshots
(the nightly backup), the flushed head block a snapshot writes, or a compaction's
temporary output, so 5Gi of slack was not enough and every scrape, rule append and
head compaction failed. The volume must be at least twice the retention cap.

Offline: helm template only.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "fuzeinfra"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

_UNITS = {"": 1, "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
          "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40,
          "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def _bytes(q):
    m = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]*)", str(q).strip())
    assert m and m.group(2) in _UNITS, q
    return float(m.group(1)) * _UNITS[m.group(2)]


@pytest.mark.parametrize("overlay", ["values-contabo.yaml", "values-aws.yaml", "values-local.yaml"])
def test_tsdb_volume_is_at_least_twice_the_retention_cap(overlay):
    out = subprocess.run(
        ["helm", "template", "fuzeinfra", str(CHART), "--namespace", "fuzeinfra",
         "-f", str(CHART / overlay)],
        capture_output=True, text=True, check=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    dep = next((d for d in docs if d["kind"] == "Deployment"
                and d["metadata"]["name"] == "fuzeinfra-prometheus"), None)
    if dep is None:
        pytest.skip("prometheus disabled on this overlay")
    vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    claim = vols["data"].get("persistentVolumeClaim", {}).get("claimName")
    if not claim:
        pytest.skip("no PVC-backed TSDB on this overlay")
    pvc = next(d for d in docs if d["kind"] == "PersistentVolumeClaim"
               and d["metadata"]["name"] == claim)
    size = _bytes(pvc["spec"]["resources"]["requests"]["storage"])
    args = next(c for c in dep["spec"]["template"]["spec"]["containers"]
                if c["name"] == "prometheus")["args"]
    cap = next(a.split("=", 1)[1] for a in args if a.startswith("--storage.tsdb.retention.size="))
    assert size >= 2 * _bytes(cap), f"{claim}: {size/2**30:.0f}Gi volume vs retention.size={cap}"
