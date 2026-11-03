"""Offline guards for .github/workflows/argo-terminate-op.yml (the deployment-watchdog's one write).

2026-10-14: Step 3 used `kubectl patch --subresource=status`, but Argo CD's Application
CRD has no status subresource (`subresources: {}`), so it always failed with NotFound and the
watchdog could never clear a stale `operationState.phase: Running` whose `.operation` was empty.
That wedge held a prod sync (and a Kafka fix) for hours.

2026-11-03: Step 1 terminated a stale op, Argo started the FRESH op one second later,
and Steps 2-3 saw phase=Running and killed the fresh op (the #1513 fix sync). Argo never
auto-retries a failed revision, so prod stayed wedged. The behavioural tests below run
the shipped script against a stub kubectl/argocd that reproduces that race.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WF = (REPO / ".github" / "workflows" / "argo-terminate-op.yml").read_text()


def code_lines() -> list[str]:
    return [l for l in WF.splitlines() if l.strip() and not l.strip().startswith("#")]


def test_never_patches_a_status_subresource_that_does_not_exist():
    assert not any("--subresource" in l for l in code_lines())


def test_every_kubectl_call_names_the_argocd_namespace():
    for l in code_lines():
        if re.search(r"\bkubectl\s+(get|patch|annotate|delete|apply)\b", l) and "config set-context" not in l:
            assert "-n argocd" in l or "--namespace" in l, l


def test_status_patch_touches_only_phase_and_message_and_is_pinned_to_the_op():
    assert 'if [ "$phase" != "Running" ]' in WF  # does nothing unless an op is actually stuck
    script = _script()
    step3 = script[script.index("=== Step 3"):]
    m = re.search(r'guarded_patch "(\[.*?\])"', step3)
    assert m, "step 3 must go through guarded_patch"
    ops = json.loads(m.group(1).replace('\\"', '"').replace("${started}", "T"))
    assert ops[0] == {"op": "test", "path": "/status/operationState/startedAt", "value": "T"}
    assert {o["path"] for o in ops[1:]} == {"/status/operationState/phase", "/status/operationState/message"}


# --------------------------------------------------------------------------- behaviour

STUB_KUBECTL = r"""#!/usr/bin/env python3
import json, sys, os
st = os.environ["STATE"]
state = json.load(open(st))
a = sys.argv[1:]
if a[:2] == ["config", "set-context"]:
    sys.exit(0)
if "get" in a:
    jp = next((x for x in a if x.startswith("jsonpath=")), "")[len("jsonpath="):]
    os_ = state["status"]["operationState"]
    out = {"{.status.operationState.phase}": os_.get("phase", ""),
           "{.status.operationState.startedAt}": os_.get("startedAt", ""),
           "{.operation}": json.dumps(state["operation"], separators=(",", ":")) if state.get("operation") else ""}
    print(out.get(jp, "(summary)"), end="")
    sys.exit(0)
if "patch" in a:
    ops = json.loads(a[a.index("-p") + 1])
    with open(st + ".patches", "a") as f:
        f.write(json.dumps(ops) + "\n")
    def get(path):
        cur = state
        for k in path.strip("/").split("/"):
            if not isinstance(cur, dict) or k not in cur:
                raise KeyError(path)
            cur = cur[k]
        return cur
    try:
        for o in ops:
            if o["op"] == "test" and get(o["path"]) != o["value"]:
                print("the server rejected our request: test operation failed", file=sys.stderr)
                sys.exit(1)
    except KeyError:
        sys.exit(1)
    for o in ops:
        keys = o["path"].strip("/").split("/")
        cur = state
        for k in keys[:-1]:
            cur = cur[k]
        if o["op"] == "remove":
            del cur[keys[-1]]
        elif o["op"] in ("add", "replace"):
            cur[keys[-1]] = o["value"]
    json.dump(state, open(st, "w"))
    sys.exit(0)
sys.exit(2)
"""

# argocd stub: what terminate-op does to the cluster is the scenario under test.
STUB_ARGOCD = r"""#!/usr/bin/env python3
import json, sys, os
if sys.argv[1:2] == ["login"]:
    sys.exit(0)
st = os.environ["STATE"]
state = json.load(open(st))
effect = os.environ["TERMINATE_EFFECT"]
if effect == "race":       # stale op dies, auto-sync starts the fresh op immediately
    state["status"]["operationState"] = {"phase": "Running", "startedAt": "NEW"}
    state["operation"] = {"sync": {"revision": "fresh"}}
elif effect == "works":
    state["status"]["operationState"]["phase"] = "Failed"
    state.pop("operation", None)
json.dump(state, open(st, "w"))   # "noop": the wedge Step 1 cannot clear
"""


def _script() -> str:
    wf = yaml.safe_load(WF)
    steps = wf["jobs"]["terminate"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "Terminate stale operation")


def _run(tmp_path, effect, operation):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"kubectl": STUB_KUBECTL, "argocd": STUB_ARGOCD, "sleep": "#!/bin/sh\n"}.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    state = tmp_path / "state.json"
    doc = {"status": {"operationState": {"phase": "Running", "startedAt": "OLD"}}}
    if operation:
        doc["operation"] = operation
    state.write_text(json.dumps(doc))
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STATE": str(state),
           "TERMINATE_EFFECT": effect, "APP": "fuzeinfra-prod", "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", "-c", _script()], env=env, capture_output=True, text=True, timeout=30)
    patches = Path(str(state) + ".patches")
    applied = patches.read_text().splitlines() if patches.exists() else []
    return proc, json.loads(state.read_text()), applied


def test_a_fresh_op_started_after_terminate_is_never_touched(tmp_path):
    """The 2026-11-03 race, exactly."""
    proc, state, patches = _run(tmp_path, "race", {"sync": {"revision": "stale"}})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["status"]["operationState"] == {"phase": "Running", "startedAt": "NEW"}
    assert state["operation"] == {"sync": {"revision": "fresh"}}
    assert patches == [], "no write may be attempted against the fresh op"


def test_wedge_with_empty_operation_is_cleared_by_step3(tmp_path):
    """The 2026-10-14 wedge: Running, no .operation, terminate-op is a no-op."""
    proc, state, _ = _run(tmp_path, "noop", None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state["status"]["operationState"]["phase"] == "Failed"
    assert state["status"]["operationState"]["startedAt"] == "OLD"


def test_stuck_operation_is_removed_by_step2_then_marked_failed(tmp_path):
    proc, state, patches = _run(tmp_path, "noop", {"sync": {"revision": "stale"}})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "operation" not in state
    assert state["status"]["operationState"]["phase"] == "Failed"
    assert json.loads(patches[0])[0] == {"op": "test", "path": "/operation", "value": {"sync": {"revision": "stale"}}}


def test_successful_terminate_writes_nothing(tmp_path):
    proc, state, patches = _run(tmp_path, "works", {"sync": {"revision": "stale"}})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert patches == []
