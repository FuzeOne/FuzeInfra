"""One consumer's unparseable rule file must not stop Prometheus from starting.

2026-10-13: fuzeagent/fuzeagent-incident-alerts (a `prometheus_rule: "1"` ConfigMap)
carried a LogQL expression. The rule sidecar copied it into rules-discovered/,
Prometheus refused it at startup ("could not parse expression ... unexpected
character: '|'") and crash-looped 4161 times over 15 days. All metric alerting, for
every product, was off.

The Prometheus container now runs `promtool check rules` on each discovered file
before exec'ing Prometheus and renames failures out of the rule_files glob. These
tests run the RENDERED script with stub promtool/prometheus binaries, so they test
the shell that ships, not a copy of it.

Offline: helm template + /bin/sh.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "fuzeinfra"
DISCOVERED = "/etc/prometheus/rules-discovered"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(*args):
    out = subprocess.run(
        ["helm", "template", "fuzeinfra", str(CHART), "--namespace", "fuzeinfra",
         "-f", str(CHART / "values-contabo.yaml"), *args],
        capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _container(docs):
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "fuzeinfra-prometheus")
    return next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "prometheus")


def _rule_globs(docs):
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "fuzeinfra-prometheus-config")
    return yaml.safe_load(cm["data"]["prometheus.yml"])["rule_files"]


def _run(tmp_path, files):
    """Run the shipped script with stubs. Returns (exit code, stderr, prometheus argv, dir listing)."""
    container = _container(_render())
    assert container["command"][:2] == ["/bin/sh", "-c"]
    script = container["command"][2]

    rules = tmp_path / "rules-discovered"
    rules.mkdir()
    for name, body in files.items():
        (rules / name).write_text(body)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Stub promtool: a file containing "BAD" fails, like a real parse error.
    (bindir / "promtool").write_text(
        '#!/bin/sh\nif grep -q BAD "$3"; then echo "$3: parse error: unexpected character: \'|\'"; exit 1; fi\n')
    (bindir / "prometheus").write_text('#!/bin/sh\necho "argv:$*"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)

    script = (script.replace(DISCOVERED, str(rules))
                    .replace("/bin/promtool", str(bindir / "promtool"))
                    .replace("/bin/prometheus", str(bindir / "prometheus")))
    proc = subprocess.run(["/bin/sh", "-c", script, container["command"][3], *container["args"]],
                          capture_output=True, text=True, env={**os.environ, "PATH": "/usr/bin:/bin"})
    return proc.returncode, proc.stderr, proc.stdout, sorted(p.name for p in rules.iterdir())


def test_bad_file_is_quarantined_and_prometheus_still_starts(tmp_path):
    code, err, out, listing = _run(tmp_path, {
        "fuzefront-alerts.yaml": "groups: []\n",
        "incident-alerts.yaml": "groups: BAD\n",
        "plan.yml": "groups: []\n",
    })
    assert code == 0
    assert listing == ["fuzefront-alerts.yaml", "incident-alerts.yaml.rejected", "plan.yml"]
    # Prometheus is exec'd with its real flags.
    assert out.startswith("argv:--config.file=/etc/prometheus/prometheus.yml")
    # The log line the Loki PrometheusRulesRejected rule matches on.
    assert 'msg="Error loading rule file, quarantined"' in err
    assert "incident-alerts.yaml" in err


def test_no_discovered_files_is_a_clean_start(tmp_path):
    code, err, out, listing = _run(tmp_path, {})
    assert code == 0 and listing == [] and err == ""
    assert out.startswith("argv:")


def test_quarantined_name_leaves_every_rule_files_glob(tmp_path):
    """The rename only helps if `.rejected` falls outside what Prometheus loads."""
    globs = [g for g in _rule_globs(_render()) if g.startswith(DISCOVERED)]
    assert globs, "rule sidecar is on in prod but no rules-discovered glob is configured"
    exts = {g.rsplit("*", 1)[1] for g in globs}
    assert exts == {".yml", ".yaml"}, exts
    # The script must check every extension Prometheus loads, or a bad .yml slips through.
    script = _container(_render())["command"][2]
    for ext in exts:
        assert f"{DISCOVERED}/*{ext}" in script


def test_no_wrapper_without_the_sidecar():
    container = _container(_render("--set", "prometheus.ruleSidecar.enabled=false"))
    assert "command" not in container
