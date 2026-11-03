"""One consumer's unparseable rule file must never reach Prometheus.

2026-10-13: fuzeagent/fuzeagent-incident-alerts (a `prometheus_rule: "1"` ConfigMap)
carried a LogQL expression. The rule sidecar copied it in, Prometheus refused it at
startup ("could not parse expression ... unexpected character: '|'") and
crash-looped 4161 times over 15 days. All metric alerting, for every product, was
off.

The first fix (#1512) checked the files once, at container start. It was measured
NOT to work on prod (2026-11-03): when a reload POST is refused (Prometheus still
replaying its WAL), the sidecar's watch loop throws and re-lists, writing every file
again, the bad one included, after the check had run. So the guard now sits
between the sidecar and Prometheus: the sidecar writes to rules-staging/,
rule-validator promotes only promtool-clean files into rules-discovered/, and
Prometheus reads only the latter.

These tests run the RENDERED validator script (one pass, VALIDATOR_ONCE=1) with
stub promtool/wget, so they test the shell that ships, not a copy of it.

Offline: helm template + /bin/sh.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "fuzeinfra"
STAGING = "/etc/prometheus/rules-staging"
DISCOVERED = "/etc/prometheus/rules-discovered"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(*args):
    out = subprocess.run(
        ["helm", "template", "fuzeinfra", str(CHART), "--namespace", "fuzeinfra",
         "-f", str(CHART / "values-contabo.yaml"), *args],
        capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _pod(docs):
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "fuzeinfra-prometheus")
    return dep["spec"]["template"]["spec"]


def _containers(docs):
    return {c["name"]: c for c in _pod(docs)["containers"]}


def _mounts(container):
    return {m["mountPath"]: m for m in container.get("volumeMounts", [])}


def _rule_globs(docs):
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "fuzeinfra-prometheus-config")
    return yaml.safe_load(cm["data"]["prometheus.yml"])["rule_files"]


@pytest.fixture(scope="module")
def prod():
    return _render()


def _run(tmp_path, prod, staging, discovered=None):
    """One validator pass with stubs. Returns (exit, stdout+stderr, staged, served, reloads)."""
    script = _containers(prod)["rule-validator"]["command"][2]
    src, dst, seen = tmp_path / "staging", tmp_path / "discovered", tmp_path / "seen"
    src.mkdir()
    dst.mkdir()
    for name, body in staging.items():
        (src / name).write_text(body)
    for name, body in (discovered or {}).items():
        (dst / name).write_text(body)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    # promtool stub: a file containing "BAD" fails, like a real parse error.
    (bindir / "promtool").write_text(
        '#!/bin/sh\nif grep -q BAD "$3"; then echo "$3: parse error: unexpected character: \'|\'"; exit 1; fi\n')
    reloads = tmp_path / "reloads"
    (bindir / "wget").write_text(f'#!/bin/sh\necho "$*" >> {reloads}\n')
    for f in bindir.iterdir():
        f.chmod(0o755)

    script = (script.replace(STAGING, str(src))
                    .replace(DISCOVERED, str(dst))
                    .replace("/tmp/rejected", str(seen))
                    .replace("/bin/promtool", str(bindir / "promtool")))
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "VALIDATOR_ONCE": "1"}
    proc = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    served = {p.name: p.read_text() for p in dst.iterdir()}
    calls = reloads.read_text().splitlines() if reloads.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, served, calls


def test_bad_file_is_never_served_and_good_files_are(tmp_path, prod):
    code, log, served, reloads = _run(tmp_path, prod, {
        "fuzefront-alerts.yaml": "groups: []\n",
        "incident-alerts.yaml": "groups: BAD\n",
        "plan.yml": "groups: []\n",
    })
    assert code == 0, log
    assert set(served) == {"fuzefront-alerts.yaml", "plan.yml"}
    # The log line the Loki PrometheusRulesRejected rule matches on.
    assert 'msg="Error loading rule file, quarantined" file=incident-alerts.yaml' in log
    assert len(reloads) == 1 and "localhost:9090/-/reload" in reloads[0]


def test_a_served_file_that_turns_bad_is_withdrawn(tmp_path, prod):
    _code, _log, served, reloads = _run(
        tmp_path, prod,
        staging={"a.yaml": "groups: BAD\n"},
        discovered={"a.yaml": "groups: []\n"})
    assert served == {}
    assert reloads, "withdrawing a served file must trigger a reload"


def test_a_deleted_configmap_is_unserved(tmp_path, prod):
    _code, _log, served, reloads = _run(tmp_path, prod, staging={}, discovered={"gone.yaml": "groups: []\n"})
    assert served == {} and reloads


def test_nothing_changed_means_no_reload(tmp_path, prod):
    _code, log, served, reloads = _run(
        tmp_path, prod, staging={"a.yaml": "groups: []\n"}, discovered={"a.yaml": "groups: []\n"})
    assert served == {"a.yaml": "groups: []\n"} and reloads == [] and log == ""


def test_prometheus_never_sees_the_sidecar_output(prod):
    """The whole fix: the sidecar's directory is not one Prometheus loads."""
    cs = _containers(prod)
    sidecar_dirs = {e["value"] for e in cs["rule-sidecar"]["env"] if e["name"] == "FOLDER"}
    assert sidecar_dirs == {STAGING}
    assert STAGING not in _mounts(cs["prometheus"])
    assert DISCOVERED in _mounts(cs["prometheus"])
    assert _mounts(cs["rule-validator"])[STAGING].get("readOnly") is True
    for g in _rule_globs(prod):
        assert not g.startswith(STAGING), g
    # The sidecar must not reload Prometheus itself: it would race the validator.
    assert "REQ_URL" not in {e["name"] for e in cs["rule-sidecar"]["env"]}
    # Every extension Prometheus loads from rules-discovered is one the validator checks.
    script = cs["rule-validator"]["command"][2]
    for g in _rule_globs(prod):
        if g.startswith(DISCOVERED):
            assert f'"$src"/*{g.rsplit("*", 1)[1]}' in script, g
    # Same image as Prometheus, so promtool matches the parser that loads the rules.
    assert cs["rule-validator"]["image"] == cs["prometheus"]["image"]


def test_no_validator_without_the_sidecar():
    cs = _containers(_render("--set", "prometheus.ruleSidecar.enabled=false"))
    assert set(cs) == {"prometheus"}
