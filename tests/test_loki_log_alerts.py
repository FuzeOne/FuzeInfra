"""Loki log-alert rules are loaded, routed, and select on streams that really exist.

These rules (helm/fuzeinfra/loki-rules/) are the only alerts that keep working
while Prometheus is down. On 2026-10-05..10-14 the Prometheus volume had a corrupt
ext4, every Prometheus-evaluated rule went dark, and a stuck Argo sync froze prod
deploys for 9 days with nothing alerting.

A Loki rule fails SILENTLY in three ways this file guards against:

  * Not loaded. The ruler reads <storage dir>/<tenant>/; with auth_enabled: false the
    tenant is "fake". Mount the ConfigMap anywhere else and Loki starts clean and
    evaluates nothing (the state before this change: an alertmanager_url and no rules).
  * Selects a stream nobody produces. `{job="systemd-journal"}` matched nothing for as
    long as the journal job existed without a journal mounted into Promtail. An
    absent stream is not an error to LogQL; the rule just never fires.
  * Not routable. alertmanager-fuze.yml keys dedup + owner routing on (alertname,
    namespace); a critical alert with no namespace lands on the wrong repo.

LogQL itself is NOT executed here (no Loki binary offline): this checks wiring and
label provenance, not query semantics.

Offline: helm template only.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "fuzeinfra"
RULES_DIR = CHART / "loki-rules"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

# A stream selector: `{key="v", key=~"v"}` (no nested braces in our exprs).
_SELECTOR = re.compile(r"\{([^{}]*)\}")
_MATCHER = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"([^"]*)"')


def _render(*args):
    out = subprocess.run(
        ["helm", "template", "fuzeinfra", str(CHART), "--namespace", "fuzeinfra", *args],
        capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _by(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def _maybe(docs, kind, name):
    return next((d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name), None)


def _rules():
    out = []
    for path in sorted(RULES_DIR.glob("*.yaml")):
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            out.extend(group["rules"])
    return out


@pytest.fixture(scope="module")
def prod():
    return _render("-f", str(CHART / "values-contabo.yaml"))


def test_rules_exist():
    names = {r["alert"] for r in _rules()}
    assert {"VolumeFilesystemCorrupt", "VolumeMountFailing", "ArgoSyncWaitingOnHealth",
            "ArgoControllerSilent", "PrometheusSilent"} <= names


def test_ruler_loads_rules_from_the_tenant_directory(prod):
    cfg = yaml.safe_load(_by(prod, "ConfigMap", "fuzeinfra-loki-config")["data"]["loki-config.yaml"])
    # The tenant directory below is only "fake" because auth is off.
    assert cfg["auth_enabled"] is False
    storage = cfg["ruler"]["storage"]
    assert storage["type"] == "local"
    rules_dir = storage["local"]["directory"]

    loki = _by(prod, "Deployment", "fuzeinfra-loki")["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for c in loki["containers"] for m in c["volumeMounts"]}
    assert mounts["rules"] == f"{rules_dir}/fake"
    vols = {v["name"]: v for v in loki["volumes"]}
    assert vols["rules"]["configMap"]["name"] == "fuzeinfra-loki-rules"
    # rule_path is written by the ruler, so it must be on the writable data volume.
    assert cfg["ruler"]["rule_path"].startswith(mounts["data"] + "/")
    assert cfg["ruler"]["alertmanager_url"].startswith("http://fuzeinfra-alertmanager:")


def test_every_rule_file_ships_verbatim(prod):
    """`.Files.Get`, never `tpl`: `{{ $labels.x }}` must reach Loki untouched."""
    data = _by(prod, "ConfigMap", "fuzeinfra-loki-rules")["data"]
    files = sorted(RULES_DIR.glob("*.yaml"))
    assert files
    for path in files:
        assert data[path.name].strip() == path.read_text(encoding="utf-8").strip()
    assert "{{ $labels." in "".join(data.values())


def test_critical_rules_are_routable():
    for rule in _rules():
        labels = rule.get("labels", {})
        assert labels.get("severity") in {"critical", "warning"}, rule["alert"]
        assert rule.get("annotations", {}).get("summary"), rule["alert"]
        if labels["severity"] != "critical":
            continue
        by = re.search(r"sum by \(([^)]*)\)", rule["expr"])
        grouped = {s.strip() for s in by.group(1).split(",")} if by else set()
        assert "namespace" in labels or "namespace" in grouped, (
            f"{rule['alert']}: critical alert without a namespace label; "
            "alertmanager-fuze.yml routes and dedups on (alertname, namespace)")


def _stream_sources(docs):
    """Map each stream source to the label NAMES it emits (and fixed values)."""
    promtail = yaml.safe_load(_by(docs, "ConfigMap", "fuzeinfra-promtail-config")["data"]["config.yml"])
    jobs = {j["job_name"]: j for j in promtail["scrape_configs"]}

    def targets(job):
        return {r["target_label"] for r in job.get("relabel_configs", [])
                if not r.get("target_label", "_").startswith("__")}

    sources = {"pods": {"labels": targets(jobs["kubernetes-pods"]), "job": None}}
    if "journal" in jobs:
        static = jobs["journal"]["journal"]["labels"]
        sources["systemd-journal"] = {"labels": targets(jobs["journal"]) | set(static),
                                      "job": static["job"]}
    exporter = _maybe(docs, "ConfigMap", "fuzeinfra-kube-event-exporter-config")
    if exporter:
        cfg = yaml.safe_load(exporter["data"]["config.yaml"])
        stream = next(r["loki"]["streamLabels"] for r in cfg["receivers"] if "loki" in r)
        sources["kube-events"] = {"labels": set(stream), "job": stream["job"]}
    return sources


def _selectors():
    for rule in _rules():
        for body in _SELECTOR.findall(rule["expr"]):
            yield rule["alert"], dict((k, v) for k, _op, v in _MATCHER.findall(body))


def test_every_selected_stream_is_produced_by_the_chart(prod):
    sources = _stream_sources(prod)
    for alert, matchers in _selectors():
        job = matchers.get("job")
        if job is None:
            src = sources["pods"]
        else:
            src = next((s for s in sources.values() if s["job"] == job), None)
            assert src, f"{alert}: selects job={job!r}, which nothing in the chart emits"
        missing = set(matchers) - src["labels"]
        assert not missing, f"{alert}: selects {sorted(missing)}, which that stream never carries"


def test_own_namespace_selectors_name_real_workloads(prod):
    """`app`/`container` on a fuzeinfra stream must match a rendered pod, or a dead-man
    rule like PrometheusSilent fires forever (or never) for a typo."""
    pods = []
    for d in prod:
        tpl = (d.get("spec") or {}).get("template")
        if d["kind"] in {"Deployment", "StatefulSet", "DaemonSet"} and tpl:
            pods.append((tpl["metadata"].get("labels", {}).get("app.kubernetes.io/name"),
                         {c["name"] for c in tpl["spec"]["containers"]}))
    for alert, m in _selectors():
        if m.get("namespace") != "fuzeinfra" or "app" not in m:
            continue
        hits = [names for app, names in pods if app == m["app"]]
        assert hits, f"{alert}: no fuzeinfra workload has app.kubernetes.io/name={m['app']!r}"
        if "container" in m:
            assert any(m["container"] in names for names in hits), (
                f"{alert}: {m['app']!r} has no container {m['container']!r}")


def test_journal_job_and_mounts_move_together(prod):
    promtail = _by(prod, "DaemonSet", "fuzeinfra-promtail")["spec"]["template"]["spec"]
    mounts = {m["mountPath"] for c in promtail["containers"] for m in c["volumeMounts"]}
    assert {"/var/log/journal", "/run/log/journal", "/etc/machine-id"} <= mounts

    off = _render("-f", str(CHART / "values-contabo.yaml"), "--set", "promtail.journal.enabled=false")
    cfg = yaml.safe_load(_by(off, "ConfigMap", "fuzeinfra-promtail-config")["data"]["config.yml"])
    assert "journal" not in {j["job_name"] for j in cfg["scrape_configs"]}
    ds = _by(off, "DaemonSet", "fuzeinfra-promtail")["spec"]["template"]["spec"]
    assert "/var/log/journal" not in {m["mountPath"] for c in ds["containers"] for m in c["volumeMounts"]}


def test_ruler_gate_removes_rules_and_mount():
    off = _render("-f", str(CHART / "values-contabo.yaml"), "--set", "loki.ruler.enabled=false")
    assert _maybe(off, "ConfigMap", "fuzeinfra-loki-rules") is None
    loki = _by(off, "Deployment", "fuzeinfra-loki")["spec"]["template"]["spec"]
    assert "rules" not in {v["name"] for v in loki["volumes"]}
    cfg = yaml.safe_load(_by(off, "ConfigMap", "fuzeinfra-loki-config")["data"]["loki-config.yaml"])
    assert "storage" not in cfg["ruler"]


def test_event_exporter_cannot_read_secrets(prod):
    """Event lines land in Loki, which far more people can read than the API."""
    role = _by(prod, "ClusterRole", "fuzeinfra-fuzeinfra-kube-event-exporter")
    for rule in role["rules"]:
        assert not {"secrets", "configmaps", "*"} & set(rule["resources"]), rule
        assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
    dep = _by(prod, "Deployment", "fuzeinfra-kube-event-exporter")
    assert dep["spec"]["replicas"] == 1  # two replicas would export every Event twice
