"""Executable invariants for the deployment-freeze watchdog.

The watchdog (scripts-tools/deployment_watchdog.py, run by
.github/workflows/deployment-watchdog.yml) exists because of the
2026-08-30..09-01 fleet-deployment freeze: a full Loki PVC crash-looped Loki 694
times over 2d11h, the permanently-unhealthy StatefulSet wedged one Argo sync
operation in phase=Running for 37h, and every prod change queued silently behind
it. Every component reported "running" and nothing alerted.

The properties that actually matter are the ones a passing-but-vacuous watchdog
would also satisfy, so they are pinned here explicitly:

  1. A Running op BELOW the threshold is NOT flagged (or the alert is noise
     within a week and gets muted, which is the same as not existing) and one
     ABOVE it IS.
  2. A healthy pod is never flagged.
  3. An existing open issue is not re-filed.
  4. "Cannot reach the cluster" FAILS. It must never render as "all clear" —
     that is the vacuous gate in its most dangerous form, and it is precisely
     what would have made this watchdog useless during the incident it is
     named after.
  5. It never reads a Secret. FuzeInfra's job logs are PUBLIC, so a read whose
     OUTPUT is a credential leaks it (tests/test_cluster_query_guard.py encodes
     the same rule for cluster-query.yml).

Offline: no cluster, no network, no gh. Every cluster interaction is a fixture.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts-tools" / "deployment_watchdog.py"
CONFIG = ROOT / "governance" / "watchdog-thresholds.json"
WORKFLOW = ROOT / ".github" / "workflows" / "deployment-watchdog.yml"


def _load_module():
    # scripts-tools is not an importable package name (hyphen), so load by path.
    spec = importlib.util.spec_from_file_location("deployment_watchdog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec: @dataclass resolves its own module out of sys.modules
    # and raises AttributeError on None if the module is not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wd = _load_module()
CFG = json.loads(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_live_github(monkeypatch):
    """Fail any test that reaches the real `gh` CLI.

    Every GitHub side effect in the watchdog funnels through `wd.gh`, and the
    issue helpers act on the LIVE repo passed via --repo. A test that forgets to
    stub one of them silently lists — and could comment on or close — real
    issues in FuzeOne/FuzeInfra. That nearly happened when the clean-run path
    gained an auto-close: test_no_findings_files_nothing_and_exits_zero made a
    real `gh issue list` call and passed only because no stuck-argo-op issue
    happened to be open. Tests that need GitHub behaviour stub the specific
    helper (create_issue, comment_issue, ...) above this layer.
    """
    def _refuse(args, timeout=60):
        pytest.fail(f"test reached the real gh CLI: gh {' '.join(args)}")
    monkeypatch.setattr(wd, "gh", _refuse)


NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# fixtures shaped like the real objects
# ---------------------------------------------------------------------------

def argo_app(name: str, phase: str, started_minutes_ago: float | None,
             message: str = "") -> dict:
    state: dict = {"phase": phase, "message": message}
    if started_minutes_ago is not None:
        state["startedAt"] = _ts(started_minutes_ago)
    return {
        "metadata": {"name": name, "namespace": "argocd"},
        "status": {
            "operationState": state,
            "sync": {"status": "OutOfSync"},
            "health": {"status": "Degraded"},
        },
    }


def pod(name, namespace="fuzeinfra", *, container="app", restarts=0, reason=None,
        ready=True, start_minutes_ago=5.0, phase="Running", message=None,
        owner=None) -> dict:
    """`owner`, if given, is (kind, name) and becomes the pod's sole ownerReference
    — how a real Deployment/CronJob-owned pod carries its stable controller
    identity (see _stable_workload_name)."""
    state = {"running": {"startedAt": _ts(start_minutes_ago)}} if reason is None else {
        "waiting": {"reason": reason, "message": message}
    }
    meta = {"name": name, "namespace": namespace}
    if owner is not None:
        owner_kind, owner_name = owner
        meta["ownerReferences"] = [{"kind": owner_kind, "name": owner_name}]
    return {
        "metadata": meta,
        "spec": {"nodeName": "vmi3396106", "containers": [{"name": container}]},
        "status": {
            "phase": phase,
            "startTime": _ts(start_minutes_ago),
            "containerStatuses": [
                {
                    "name": container,
                    "ready": ready,
                    "restartCount": restarts,
                    "state": state,
                    "lastState": {
                        "terminated": {
                            "exitCode": 1,
                            "reason": "Error",
                            "finishedAt": _ts(1),
                        }
                    },
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# 1. stuck Argo operation — the highest-value signal
# ---------------------------------------------------------------------------

def test_running_op_below_threshold_is_not_flagged():
    """A real fuzeinfra-prod sync takes ~5 minutes; hooks and rollouts run.

    Flagging those is how an alert gets muted, which ends in exactly the silence
    this watchdog was written to break.
    """
    apps = {"items": [argo_app("fuzeinfra-prod", "Running", 44)]}
    assert wd.detect_stuck_argo_ops(apps, CFG, NOW) == []


def test_running_op_above_threshold_is_flagged_with_the_blocking_resource():
    """The incident itself: 37h in phase=Running, blocked on the Loki StatefulSet."""
    message = "waiting for healthy state of apps/StatefulSet/fuzeinfra-loki and 1 more resources"
    apps = {"items": [argo_app("fuzeinfra-prod", "Running", 37 * 60, message)]}

    findings = wd.detect_stuck_argo_ops(apps, CFG, NOW)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == wd.KIND_ARGO
    assert finding.subject == "fuzeinfra-prod"
    # The diagnostics a human needs must be IN the finding, not left in a log:
    # app name, phase, startedAt and the blocking resource.
    assert finding.facts["phase"] == "Running"
    assert finding.facts["startedAt"] == _ts(37 * 60)
    assert finding.facts["message"] == message
    assert finding.facts["running_minutes"] == pytest.approx(2220.0)


def test_threshold_is_read_from_config_not_hardcoded():
    apps = {"items": [argo_app("fuzeinfra-prod", "Running", 50)]}
    assert len(wd.detect_stuck_argo_ops(apps, CFG, NOW)) == 1
    relaxed = {**CFG, "argo_stuck_op": {**CFG["argo_stuck_op"], "running_minutes": 120}}
    assert wd.detect_stuck_argo_ops(apps, relaxed, NOW) == []


@pytest.mark.parametrize("phase", ["Succeeded", "Failed", "Terminating", "Error"])
def test_non_running_phases_are_never_flagged(phase):
    apps = {"items": [argo_app("fuzeinfra-prod", phase, 5 * 24 * 60)]}
    assert wd.detect_stuck_argo_ops(apps, CFG, NOW) == []


def test_app_with_no_operation_state_is_not_flagged():
    apps = {"items": [{"metadata": {"name": "litellm"}, "status": {}}]}
    assert wd.detect_stuck_argo_ops(apps, CFG, NOW) == []


def test_default_threshold_matches_the_governance_file():
    assert CFG["argo_stuck_op"]["running_minutes"] == 45


def node(name, *, ready_status="True", unknown_minutes_ago=None, control_plane=False,
         etcd_voter=None, internal_ip="10.0.0.1", external_ip="203.0.113.1") -> dict:
    """A Node fixture. `unknown_minutes_ago` set means Ready=Unknown since then;
    otherwise Ready has whatever `ready_status` says (default healthy True)."""
    if unknown_minutes_ago is not None:
        ready = {"type": "Ready", "status": "Unknown",
                  "lastTransitionTime": _ts(unknown_minutes_ago),
                  "lastHeartbeatTime": _ts(5)}
    else:
        ready = {"type": "Ready", "status": ready_status,
                  "lastTransitionTime": _ts(0), "lastHeartbeatTime": _ts(0)}
    conditions = [ready]
    if etcd_voter is not None:
        conditions.append({"type": "EtcdIsVoter",
                           "status": "True" if etcd_voter else "False"})
    labels = {}
    if control_plane:
        labels["node-role.kubernetes.io/control-plane"] = "true"
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {
            "conditions": conditions,
            "addresses": [
                {"type": "InternalIP", "address": internal_ip},
                {"type": "ExternalIP", "address": external_ip},
            ],
        },
    }


# ---------------------------------------------------------------------------
# 1b. dark node (kubelet stopped posting status)
# ---------------------------------------------------------------------------

def test_healthy_node_is_never_flagged_regardless_of_age():
    nodes = {"items": [node("fuze-core-1", ready_status="True")]}
    assert wd.detect_dark_nodes(nodes, CFG, NOW) == []


def test_recently_unknown_node_below_threshold_is_not_flagged():
    """A kubelet restart or a brief API-server blip legitimately reports Unknown briefly."""
    nodes = {"items": [node("fuze-core-3", unknown_minutes_ago=10)]}
    assert wd.detect_dark_nodes(nodes, CFG, NOW) == []


def test_dark_control_plane_node_is_flagged_with_quorum_facts():
    """The real incident: fuze-core-3 dark 4.5 days, still an etcd voter."""
    nodes = {"items": [
        node("fuze-core-1", control_plane=True, etcd_voter=True),
        node("fuze-core-2", control_plane=True, etcd_voter=True),
        node("fuze-core-3", control_plane=True, etcd_voter=True,
             unknown_minutes_ago=4.5 * 24 * 60, external_ip="194.163.136.242"),
    ]}
    findings = wd.detect_dark_nodes(nodes, CFG, NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == wd.KIND_DARK_NODE
    assert f.subject == "fuze-core-3"
    assert f.facts["is_control_plane"] is True
    assert f.facts["is_etcd_voter"] is True
    # The other two control-plane nodes are Ready — not the last vote standing.
    assert f.facts["other_ready_control_plane_nodes"] == 2
    assert f.facts["external_ip"] == "194.163.136.242"
    assert f.facts["unknown_minutes"] == pytest.approx(4.5 * 24 * 60)


def test_dark_worker_node_is_flagged_and_is_not_marked_control_plane():
    nodes = {"items": [node("fuzeinfra-prod-elastic-v2-c056a22a", unknown_minutes_ago=60)]}
    findings = wd.detect_dark_nodes(nodes, CFG, NOW)
    assert len(findings) == 1
    assert findings[0].facts["is_control_plane"] is False
    assert findings[0].facts["is_etcd_voter"] is False


def test_dark_node_threshold_is_read_from_config_not_hardcoded():
    nodes = {"items": [node("fuze-core-3", unknown_minutes_ago=40)]}
    assert len(wd.detect_dark_nodes(nodes, CFG, NOW)) == 1
    relaxed = {**CFG, "dark_node": {**CFG["dark_node"], "unknown_minutes": 120}}
    assert wd.detect_dark_nodes(nodes, relaxed, NOW) == []


def test_dark_node_ignore_list_excludes_a_node():
    nodes = {"items": [node("known-flapping-node", unknown_minutes_ago=60)]}
    cfg = {**CFG, "dark_node": {**CFG["dark_node"], "ignore_nodes": ["known-flapping-node"]}}
    assert wd.detect_dark_nodes(nodes, cfg, NOW) == []


def test_dark_node_default_threshold_matches_the_governance_file():
    assert CFG["dark_node"]["unknown_minutes"] == 30


def test_dark_node_detection_itself_never_dispatches():
    """detect_dark_nodes stays a PURE detector regardless of the auto_restart /
    auto_reinstall flags below — it returns Finding objects only. Every mutation
    the escalation ladder performs lives in decide_dark_node_escalation (a pure
    decision, tested separately below) plus the dispatch_*/comment_issue/
    close_issue side-effecting wrappers main() calls with its result — never in
    the detector."""
    import inspect
    source = inspect.getsource(wd.detect_dark_nodes)
    assert "dispatch_terminate_op" not in source
    assert "dispatch_reboot" not in source
    assert "dispatch_cp_reinstall" not in source
    assert "subprocess" not in source
    assert "gh(" not in source


def test_dark_node_facts_point_at_the_existing_reboot_workflow():
    assert (ROOT / ".github" / "workflows" / "contabo-instance-reboot.yml").is_file()
    finding = wd.detect_dark_nodes(
        {"items": [node("fuze-core-3", unknown_minutes_ago=60)]}, CFG, NOW
    )[0]
    assert "contabo-instance-reboot.yml" in finding.facts["next_step"]


# ---------------------------------------------------------------------------
# 1c. dark-node escalation ladder — the FIVE-DAY incident this replaces
# ---------------------------------------------------------------------------
#
# fuze-core-3 went dark 2026-09-10, was detected within 30 minutes, and was not
# actually fixed until 2026-09-15/16 — five days of a human needing to read an
# issue, decide, dispatch, and re-check. decide_dark_node_escalation is the
# bounded state machine that now makes that decision instead. It is PURE (no
# gh/subprocess/kubectl), so every case below is exercised with no network.

def _cp_finding(unknown_minutes=45.0, other_ready=2, is_etcd_voter=True, is_control_plane=True):
    return wd.Finding(
        kind=wd.KIND_DARK_NODE, subject="fuze-core-3",
        summary="dark",
        facts={
            "node": "fuze-core-3",
            "unknown_minutes": unknown_minutes,
            "external_ip": "194.163.136.242",
            "is_control_plane": is_control_plane,
            "is_etcd_voter": is_etcd_voter,
            "other_ready_control_plane_nodes": other_ready,
        },
    )


def _elastic_finding(unknown_minutes=45.0):
    return wd.Finding(
        kind=wd.KIND_DARK_NODE, subject="fuzeinfra-prod-elastic-v2-c056a22a",
        summary="dark",
        facts={
            "node": "fuzeinfra-prod-elastic-v2-c056a22a",
            "unknown_minutes": unknown_minutes,
            "external_ip": "203.0.113.9",
            "is_control_plane": False,
            "is_etcd_voter": False,
            "other_ready_control_plane_nodes": 3,
        },
    )


DARK_CFG = CFG["dark_node"]


def test_first_reboot_fires_immediately_with_default_state():
    action, state, message = wd.decide_dark_node_escalation(
        _cp_finding(), dict(wd.DEFAULT_DARK_STATE), DARK_CFG, NOW
    )
    assert action == "reboot"
    assert state["reboot_attempts"] == 1
    assert state["last_reboot_at"] == NOW.isoformat()
    assert "1/3" in message


def test_second_reboot_waits_out_the_cooldown():
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 1, "last_reboot_at": _ts(10)}
    action, new_state, message = wd.decide_dark_node_escalation(_cp_finding(), state, DARK_CFG, NOW)
    assert action == "none"
    assert new_state["reboot_attempts"] == 1  # unchanged — still cooling down
    assert "cooling down" in message


def test_second_reboot_fires_once_cooldown_elapses():
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 1, "last_reboot_at": _ts(31)}
    action, new_state, _ = wd.decide_dark_node_escalation(_cp_finding(), state, DARK_CFG, NOW)
    assert action == "reboot"
    assert new_state["reboot_attempts"] == 2


def test_third_reboot_is_the_last_attempt():
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 2, "last_reboot_at": _ts(31)}
    action, new_state, message = wd.decide_dark_node_escalation(_cp_finding(), state, DARK_CFG, NOW)
    assert action == "reboot"
    assert new_state["reboot_attempts"] == 3
    assert "3/3" in message


def test_no_fourth_reboot_ever_fires():
    """After 3 attempts, decide_dark_node_escalation NEVER returns 'reboot' again —
    it either waits out escalate_after_minutes or moves to reinstall/refusal."""
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action, _, _ = wd.decide_dark_node_escalation(_cp_finding(unknown_minutes=45), state, DARK_CFG, NOW)
    assert action == "none"  # still short of escalate_after_minutes


def test_reinstall_requires_both_attempts_exhausted_and_time_elapsed():
    """3 attempts alone, with the node barely dark, is not enough — both gates apply."""
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action, _, message = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=100), state, DARK_CFG, NOW
    )
    assert action == "none"
    assert "waiting for escalate_after_minutes" in message


def test_reinstall_fires_once_both_gates_clear():
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action, new_state, message = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=125, other_ready=2), state, DARK_CFG, NOW
    )
    assert action == "reinstall"
    assert new_state["escalated"] is True
    assert new_state["escalated_at"] == NOW.isoformat()
    assert "escalating to reinstall" in message


def test_reinstall_refused_when_it_would_risk_etcd_quorum():
    """The exact fact a human was handed on the issue (other_ready_control_plane_nodes)
    is now also a hard gate on the automated path. Only 1 other Ready CP node — below
    reinstall_min_other_ready_control_plane=2 — must refuse, not proceed."""
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action, new_state, message = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=125, other_ready=1), state, DARK_CFG, NOW
    )
    assert action == "refuse-quorum"
    assert new_state["escalated"] is False
    assert "quorum" in message


def test_quorum_refusal_reports_once_not_every_cycle():
    """A durable refusal must not spam the issue every 15-minute run forever."""
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action1, state1, _ = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=125, other_ready=1), state, DARK_CFG, NOW
    )
    assert action1 == "refuse-quorum"
    action2, _, _ = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=140, other_ready=1), state1, DARK_CFG, NOW
    )
    assert action2 == "none"  # same reason already reported — no repeat comment


def test_quorum_refusal_re_reports_if_the_situation_changes():
    """A DIFFERENT refusal reason (e.g. quorum got worse) should still surface."""
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200),
             "refused_reason": "refusing auto-reinstall: only 1 other ready control-plane node(s), need >= 2 for etcd quorum safety"}
    action, _, _ = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=140, other_ready=0), state, DARK_CFG, NOW
    )
    assert action == "refuse-quorum"


def test_reinstall_is_scoped_to_control_plane_durable_nodes_only():
    """Elastic nodes are OUT OF SCOPE — they have their own autoscaler-driven
    replacement lifecycle; this ladder must never touch them."""
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action, new_state, message = wd.decide_dark_node_escalation(
        _elastic_finding(unknown_minutes=125), state, DARK_CFG, NOW
    )
    assert action == "refuse-elastic"
    assert new_state["escalated"] is False
    assert "control-plane/durable" in message


def test_non_voter_control_plane_node_skips_the_quorum_check():
    """A durable/control-plane node that ISN'T an etcd voter has no quorum to
    protect, so it proceeds straight to reinstall once both gates clear."""
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action, _, _ = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=125, other_ready=0, is_etcd_voter=False), state, DARK_CFG, NOW
    )
    assert action == "reinstall"


def test_already_escalated_node_takes_no_further_automatic_action():
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "escalated": True,
             "escalated_at": _ts(30)}
    action, new_state, message = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=300), state, DARK_CFG, NOW
    )
    assert action == "none"
    assert new_state == state
    assert "already escalated" in message


def test_auto_restart_disabled_takes_no_action_at_all():
    cfg = {**DARK_CFG, "auto_restart": False}
    action, state, message = wd.decide_dark_node_escalation(
        _cp_finding(), dict(wd.DEFAULT_DARK_STATE), cfg, NOW
    )
    assert action == "none"
    assert state == wd.DEFAULT_DARK_STATE
    assert "disabled" in message


def test_auto_reinstall_disabled_stops_after_reboots_exhausted():
    cfg = {**DARK_CFG, "auto_reinstall": False}
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 3, "last_reboot_at": _ts(200)}
    action, _, message = wd.decide_dark_node_escalation(
        _cp_finding(unknown_minutes=125), state, cfg, NOW
    )
    assert action == "none"
    assert "auto_reinstall disabled" in message


def test_decide_dark_node_escalation_never_mutates_its_input_state():
    original = {**wd.DEFAULT_DARK_STATE}
    frozen = dict(original)
    wd.decide_dark_node_escalation(_cp_finding(), original, DARK_CFG, NOW)
    assert original == frozen


def test_dark_state_marker_round_trips_through_comments():
    state = {**wd.DEFAULT_DARK_STATE, "reboot_attempts": 2, "last_reboot_at": _ts(5)}
    marker = wd.render_dark_state_marker(state)
    comments = [
        {"body": "a human said hi"},
        {"body": f"🔁 reboot attempt 1/3.\n\n{wd.render_dark_state_marker({**wd.DEFAULT_DARK_STATE, 'reboot_attempts': 1})}"},
        {"body": f"🔁 reboot attempt 2/3.\n\n{marker}"},
    ]
    assert wd.parse_dark_node_state(comments) == state


def test_dark_state_marker_defaults_when_no_comments_match():
    assert wd.parse_dark_node_state([{"body": "just chatter, no marker"}]) == wd.DEFAULT_DARK_STATE
    assert wd.parse_dark_node_state([]) == wd.DEFAULT_DARK_STATE


def test_dark_node_name_extracted_from_issue_body_marker():
    finding = _cp_finding()
    body = wd.build_issue_body(finding, CFG)
    issue = {"body": body}
    assert wd._dark_node_name_from_issue(issue) == "fuze-core-3"


def test_currently_unknown_node_names_ignores_ready_nodes():
    nodes = {"items": [
        node("fuze-core-1", ready_status="True"),
        node("fuze-core-3", unknown_minutes_ago=5),
    ]}
    assert wd._currently_unknown_node_names(nodes) == {"fuze-core-3"}


def test_reboot_dispatch_validates_the_node_name():
    with pytest.raises(wd.UnsafeCommand):
        wd.dispatch_reboot("owner/repo", "; rm -rf /", "10.0.0.1", "reason", "1")


def test_reinstall_dispatch_validates_the_node_name():
    with pytest.raises(wd.UnsafeCommand):
        wd.dispatch_cp_reinstall("owner/repo", "$(whoami)", "10.0.0.1", "template", "1")


def test_escalation_config_defaults_match_the_governance_file():
    assert DARK_CFG["auto_restart"] is True
    assert DARK_CFG["max_reboot_attempts"] == 3
    assert DARK_CFG["escalate_after_minutes"] == 120
    assert DARK_CFG["auto_reinstall"] is True
    assert DARK_CFG["reinstall_min_other_ready_control_plane"] == 2


# ---------------------------------------------------------------------------
# 2. chronic CrashLoopBackOff
# ---------------------------------------------------------------------------

def test_healthy_pod_is_not_flagged_by_any_pod_detection():
    pods = {"items": [pod("litellm-69d54765ff-sc9gz")]}
    assert wd.detect_chronic_crashloop(pods, CFG, NOW) == []
    assert wd.detect_stuck_container_creating(pods, CFG, NOW) == []


def test_a_few_restarts_are_not_chronic():
    """A rollout, an OOM burst or a dependency flap is not a self-sealing loop."""
    pods = {"items": [pod("api-1", restarts=7, reason="CrashLoopBackOff", ready=False,
                          start_minutes_ago=10)]}
    assert wd.detect_chronic_crashloop(pods, CFG, NOW) == []


def test_loki_restart_storm_is_flagged():
    """694 restarts over 2d11h — the real number from the incident."""
    pods = {"items": [pod("fuzeinfra-loki-0", container="loki", restarts=694,
                          reason="CrashLoopBackOff", ready=False,
                          start_minutes_ago=59 * 60)]}
    findings = wd.detect_chronic_crashloop(pods, CFG, NOW)
    assert len(findings) == 1
    assert findings[0].facts["restartCount"] == 694
    assert findings[0].subject == "fuzeinfra/fuzeinfra-loki-0:loki"


def test_long_crashloop_is_flagged_even_below_the_restart_count():
    pods = {"items": [pod("api-1", restarts=6, reason="CrashLoopBackOff", ready=False,
                          start_minutes_ago=180)]}
    assert len(wd.detect_chronic_crashloop(pods, CFG, NOW)) == 1


def test_short_crashloop_below_both_thresholds_is_not_flagged():
    pods = {"items": [pod("api-1", restarts=3, reason="CrashLoopBackOff", ready=False,
                          start_minutes_ago=30)]}
    assert wd.detect_chronic_crashloop(pods, CFG, NOW) == []


# ---------------------------------------------------------------------------
# 3. stuck ContainerCreating (the stale-mount class)
# ---------------------------------------------------------------------------

def test_stuck_container_creating_is_flagged_with_the_mount_error():
    """'already mounted or mount point busy' kept Loki down 14h AFTER the disk was fixed."""
    message = ("MountVolume.SetUp failed for volume \"pvc-loki\": already mounted or "
               "mount point busy")
    pods = {"items": [pod("fuzeinfra-loki-0", container="loki", reason="ContainerCreating",
                          ready=False, start_minutes_ago=14 * 60, phase="Pending",
                          message=message)]}
    findings = wd.detect_stuck_container_creating(pods, CFG, NOW)
    assert len(findings) == 1
    assert findings[0].kind == wd.KIND_CREATING
    assert findings[0].facts["waiting_message"] == message


def test_recent_container_creating_is_not_flagged():
    """Multi-GB image pulls on a cold node legitimately take minutes."""
    pods = {"items": [pod("api-1", reason="ContainerCreating", ready=False,
                          start_minutes_ago=4, phase="Pending")]}
    assert wd.detect_stuck_container_creating(pods, CFG, NOW) == []


# ---------------------------------------------------------------------------
# 4. PVC nearing full — the rotate-by-volume alarm
# ---------------------------------------------------------------------------

def test_pvc_below_threshold_is_not_flagged():
    usage = [{"namespace": "fuzeinfra", "claim": "storage-fuzeinfra-loki-0",
              "ratio": 0.62, "source": "prometheus"}]
    assert wd.detect_pvc_pressure(usage, CFG) == []


def test_pvc_above_threshold_is_flagged_before_it_fills():
    """Loki has no size-based retention: an alarm before full is the only mechanism."""
    usage = [{"namespace": "fuzeinfra", "claim": "storage-fuzeinfra-loki-0",
              "ratio": 0.87, "used_bytes": 4_670_000_000,
              "capacity_bytes": 5_368_709_120, "source": "prometheus"}]
    findings = wd.detect_pvc_pressure(usage, CFG)
    assert len(findings) == 1
    assert findings[0].subject == "fuzeinfra/storage-fuzeinfra-loki-0"
    assert findings[0].facts["used_ratio"] == 0.87
    assert findings[0].facts["source"] == "prometheus"


def test_prometheus_vector_is_parsed_into_usage_records():
    payload = {
        "status": "success",
        "data": {"result": [
            {"metric": {"namespace": "fuzeinfra",
                        "persistentvolumeclaim": "storage-fuzeinfra-loki-0"},
             "value": [1756732800, "0.94"]},
        ]},
    }
    records = wd.parse_prometheus_vector(payload)
    assert records == [{"namespace": "fuzeinfra",
                        "claim": "storage-fuzeinfra-loki-0", "ratio": 0.94}]


def test_prometheus_with_zero_samples_raises_instead_of_reporting_clear():
    """'Prometheus knows about no volume at all' is blindness, not an all-clear."""
    with pytest.raises(wd.ClusterUnreachable):
        wd.parse_prometheus_vector({"status": "success", "data": {"result": []}})


def test_prometheus_error_status_raises():
    with pytest.raises(wd.ClusterUnreachable):
        wd.parse_prometheus_vector({"status": "error", "error": "query timeout"})


def test_df_fallback_output_is_parsed():
    out = (
        "Filesystem                1B-blocks       Used  Available Capacity Mounted on\n"
        "/dev/longhorn/pvc-loki   5368709120 5368705024          0     100% /loki\n"
    )
    assert wd.parse_df_output(out) == (5368705024, 5368709120)


def test_pvc_source_falls_back_to_df_when_prometheus_is_unreachable(monkeypatch):
    """The fallback exists so a Prometheus outage cannot silently disable this check."""
    monkeypatch.setattr(wd, "pvc_usage_via_prometheus",
                        lambda cfg: (_ for _ in ()).throw(wd.ClusterUnreachable("prom down")))
    monkeypatch.setattr(
        wd, "kubectl_exec_df",
        lambda ns, p, c, path, timeout=60: (
            "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
            "/dev/x 100 90 10 90% /loki\n"
        ),
    )
    pods = {"items": [{
        "metadata": {"name": "fuzeinfra-loki-0", "namespace": "fuzeinfra"},
        "status": {"phase": "Running"},
        "spec": {
            "volumes": [{"name": "storage",
                         "persistentVolumeClaim": {"claimName": "storage-fuzeinfra-loki-0"}}],
            "containers": [{"name": "loki",
                            "volumeMounts": [{"name": "storage", "mountPath": "/loki"}]}],
        },
    }]}
    usage, source = wd.collect_pvc_usage(pods, CFG)
    assert source == "kubectl-exec-df"
    assert usage[0]["ratio"] == pytest.approx(0.9)
    assert wd.detect_pvc_pressure(usage, CFG)[0].facts["source"] == "kubectl-exec-df"


def test_both_pvc_sources_failing_raises_rather_than_reporting_clear(monkeypatch):
    monkeypatch.setattr(wd, "pvc_usage_via_prometheus",
                        lambda cfg: (_ for _ in ()).throw(wd.ClusterUnreachable("prom down")))
    with pytest.raises(wd.ClusterUnreachable):
        wd.collect_pvc_usage({"items": []}, CFG)


# ---------------------------------------------------------------------------
# 5. dedup — a watchdog that files 40 duplicates gets muted
# ---------------------------------------------------------------------------

def _stuck_finding():
    apps = {"items": [argo_app("fuzeinfra-prod", "Running", 37 * 60, "waiting for healthy state")]}
    return wd.detect_stuck_argo_ops(apps, CFG, NOW)[0]


def test_existing_open_issue_is_not_refiled():
    finding = _stuck_finding()
    existing = [{"number": 744, "url": "https://github.com/FuzeOne/FuzeInfra/issues/744",
                 "title": "anything a human renamed it to",
                 "body": f"{finding.marker}\n\n@fuze ..."}]
    new, duplicates = wd.filter_new_findings([finding], existing)
    assert new == []
    assert duplicates and duplicates[0][1]["number"] == 744


def test_matching_title_also_dedupes_when_the_body_marker_was_edited_away():
    finding = _stuck_finding()
    existing = [{"number": 745, "url": "u", "title": finding.title(), "body": "human rewrote this"}]
    new, _ = wd.filter_new_findings([finding], existing)
    assert new == []


def test_a_different_condition_still_gets_its_own_issue():
    """Dedup is per condition, not per run — one issue per DISTINCT condition."""
    stuck = _stuck_finding()
    other = wd.detect_chronic_crashloop(
        {"items": [pod("fuzeinfra-loki-0", container="loki", restarts=694,
                       reason="CrashLoopBackOff", ready=False, start_minutes_ago=600)]},
        CFG, NOW,
    )[0]
    existing = [{"number": 744, "url": "u", "title": stuck.title(),
                 "body": stuck.marker}]
    new, duplicates = wd.filter_new_findings([stuck, other], existing)
    assert [f.kind for f in new] == [wd.KIND_CRASHLOOP]
    assert len(duplicates) == 1


def test_issue_title_is_stable_across_runs():
    """Ages/counts in the title would defeat the title half of the dedup."""
    early = wd.detect_stuck_argo_ops(
        {"items": [argo_app("fuzeinfra-prod", "Running", 46)]}, CFG, NOW)[0]
    late = wd.detect_stuck_argo_ops(
        {"items": [argo_app("fuzeinfra-prod", "Running", 3000)]}, CFG, NOW)[0]
    assert early.title() == late.title() == "[watchdog] stuck-argo-op: fuzeinfra-prod"


def test_issue_body_mentions_fuze_and_embeds_the_real_diagnostics():
    finding = _stuck_finding()
    body = wd.build_issue_body(finding, CFG, "https://example/run/1",
                               ["Dispatched `argo-terminate-op.yml` with `app=fuzeinfra-prod`"])
    assert "@fuze" in body
    assert finding.marker in body
    assert "fuzeinfra-prod" in body
    assert "waiting for healthy state" in body
    # The auto-terminate MUST be disclosed in the issue that reports the condition.
    assert "argo-terminate-op.yml" in body


# ---------------------------------------------------------------------------
# 6. blindness FAILS, and the cluster access stays read-only + secret-free
# ---------------------------------------------------------------------------

def test_unreachable_cluster_exits_nonzero_and_never_reports_clear(monkeypatch, capsys):
    """The property that makes this watchdog worth having.

    If `kubectl` cannot reach the API server, main() must exit non-zero. Silently
    treating an unreachable cluster as "nothing wrong" is exactly the failure mode
    the 2d11h freeze was: a report of health from something that never looked.
    """
    def refuse(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, "", "Unable to connect to the server: dial tcp i/o timeout"
        )

    monkeypatch.setattr(wd.subprocess, "run", refuse)

    # main() propagates; run() is the process entrypoint and maps it to exit 2.
    with pytest.raises(wd.ClusterUnreachable):
        wd.main(["--repo", "FuzeOne/FuzeInfra", "--dry-run"])
    assert wd.run(["--repo", "FuzeOne/FuzeInfra", "--dry-run"]) == 2

    out = capsys.readouterr().out
    assert "::error::" in out
    assert "no stuck argo operation" not in out.lower()


def test_kubectl_nonzero_exit_raises_cluster_unreachable(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "connection refused"),
    )
    with pytest.raises(wd.ClusterUnreachable):
        wd.kubectl(["get", "pods", "-A", "-o", "json"])


@pytest.mark.parametrize("args", [
    ["-n", "fuzeinfra", "get", "secret", "litellm-secret", "-o", "yaml"],
    ["get", "secrets", "-A", "-o", "yaml"],
    ["-n", "fuzeinfra", "get", "secret/litellm-secret", "-o", "yaml"],
    ["-n", "fuzeinfra", "get", "pods,secrets"],
    ["-n", "fuzeinfra", "get", "secrets.v1.", "-o", "yaml"],
    ["-n", "fuzeinfra", "get", "Secret", "litellm-secret"],
])
def test_secret_reads_are_refused(args):
    """This repo's job logs are PUBLIC: a read whose output is a credential leaks it."""
    with pytest.raises(wd.UnsafeCommand):
        wd.assert_safe_kubectl(args)


@pytest.mark.parametrize("args", [
    ["-n", "fuzeinfra", "delete", "pod", "fuzeinfra-loki-0"],
    ["-n", "fuzeinfra", "patch", "statefulset", "fuzeinfra-loki"],
    ["-n", "argocd", "edit", "application", "fuzeinfra-prod"],
    ["-n", "fuzeinfra", "exec", "fuzeinfra-loki-0", "--", "rm", "-rf", "/loki"],
    ["-n", "fuzeinfra", "scale", "sts", "fuzeinfra-loki", "--replicas=0"],
])
def test_mutating_kubectl_is_refused(args):
    """Prod is GitOps under Argo selfHeal; an out-of-band write is reverted anyway."""
    with pytest.raises(wd.UnsafeCommand):
        wd.assert_safe_kubectl(args)


def test_config_view_raw_is_refused():
    """`kubectl config view --raw` prints this runner's cluster-admin kubeconfig."""
    with pytest.raises(wd.UnsafeCommand):
        wd.assert_safe_kubectl(["config", "view", "--raw"])


def test_raw_is_only_allowed_for_the_service_proxy():
    wd.assert_safe_kubectl([
        "get", "--raw",
        "/api/v1/namespaces/fuzeinfra/services/fuzeinfra-prometheus:9090/proxy/api/v1/query?query=up",
    ])
    with pytest.raises(wd.UnsafeCommand):
        wd.assert_safe_kubectl(["get", "--raw", "/metrics"])


@pytest.mark.parametrize("args", [
    ["get", "pods", "-A", "-o", "json"],
    ["-n", "argocd", "get", "applications", "-o", "json"],
    ["-n", "fuzeinfra", "get", "sealedsecret", "litellm-secret", "-o", "yaml"],
    ["-n", "fuzeinfra", "describe", "deployment", "litellm-secret-reader"],
])
def test_legitimate_reads_still_work(args):
    wd.assert_safe_kubectl(args)


def test_exec_df_fallback_refuses_injected_names():
    with pytest.raises(wd.UnsafeCommand):
        wd.kubectl_exec_df("fuzeinfra", "loki-0; rm -rf /", "loki", "/loki")
    with pytest.raises(wd.UnsafeCommand):
        wd.kubectl_exec_df("fuzeinfra", "loki-0", "loki", "/loki; cat /etc/shadow")


def test_terminate_op_dispatch_validates_the_app_name():
    with pytest.raises(wd.UnsafeCommand):
        wd.dispatch_terminate_op("FuzeOne/FuzeInfra", "argo-terminate-op.yml",
                                 "fuzeinfra-prod; curl evil")


def test_auto_terminate_targets_the_existing_purpose_built_workflow():
    workflow = CFG["argo_stuck_op"]["auto_terminate_workflow"]
    assert (ROOT / ".github" / "workflows" / workflow).is_file()


# ---------------------------------------------------------------------------
# 7. the workflow itself
# ---------------------------------------------------------------------------

def test_workflow_runs_on_a_hosted_runner_not_the_cluster_it_watches():
    """A watchdog hosted by its own subject cannot start when the subject is broken.

    Every other cluster-touching workflow here uses `runs-on: staging`, an ARC
    runner INSIDE the prod cluster. This one must not.
    """
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert spec["jobs"]["watch"]["runs-on"] == "ubuntu-latest"


def test_workflow_is_scheduled():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = spec.get("on", spec.get(True))
    assert "schedule" in triggers and triggers["schedule"]


def test_workflow_has_no_failure_swallowing():
    """No continue-on-error / || true / exit 0. Blind must never look green."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    assert "continue-on-error" not in body
    assert "|| true" not in body
    assert "exit 0" not in body


# ---------------------------------------------------------------------------
# 8. per-run issue cap — measured against the real cluster, not invented
# ---------------------------------------------------------------------------

def test_stuck_argo_op_files_first_under_the_cap():
    """The op that blocks every other deploy must never lose a slot to a crash loop."""
    crashloops = wd.detect_chronic_crashloop(
        {"items": [pod(f"api-{i}", restarts=600, reason="CrashLoopBackOff", ready=False,
                       start_minutes_ago=900) for i in range(20)]},
        CFG, NOW,
    )
    stuck = _stuck_finding()
    ordered = wd.prioritize(crashloops + [stuck], CFG["issues"]["priority"])
    assert ordered[0] is stuck


def test_end_to_end_caps_issues_filed_reports_everything_and_dispatches_terminate(monkeypatch):
    """Detection is never capped; only issue CREATION is.

    The first live dry-run against prod returned 55 distinct conditions. Filing
    55 issues at once is the muting failure in a different shape, so the run
    files the worst `max_per_run` and lists the rest in the summary.
    """
    apps = {"items": [argo_app("fuzeinfra-prod", "Running", 37 * 60,
                               "waiting for healthy state of apps/StatefulSet/fuzeinfra-loki")]}
    pods = {"items": [pod(f"api-{i}", restarts=600, reason="CrashLoopBackOff", ready=False,
                          start_minutes_ago=900) for i in range(20)]}
    filed: list[tuple[str, str]] = []
    dispatched: list[str] = []

    monkeypatch.setattr(wd, "collect_cluster_state", lambda cfg: (apps, pods, {"items": []}))
    monkeypatch.setattr(wd, "collect_pvc_usage", lambda p, cfg: ([], "prometheus"))
    monkeypatch.setattr(wd, "list_open_watchdog_issues", lambda repo, label: [])
    monkeypatch.setattr(wd, "ensure_label", lambda *a, **k: None)
    monkeypatch.setattr(wd, "create_issue",
                        lambda repo, label, title, body: filed.append((title, body)) or "url")
    monkeypatch.setattr(wd, "dispatch_terminate_op",
                        lambda repo, wf, app, ref="main": dispatched.append(app))

    exit_code = wd.main(["--repo", "FuzeOne/FuzeInfra"])

    assert exit_code == 1  # findings -> red run, so the freeze is visible in Actions
    assert len(filed) == CFG["issues"]["max_per_run"] < 21
    # The stuck op took the first slot and triggered the sanctioned terminate.
    assert filed[0][0] == "[watchdog] stuck-argo-op: fuzeinfra-prod"
    assert dispatched == ["fuzeinfra-prod"]
    assert "argo-terminate-op.yml" in filed[0][1]


def test_no_findings_files_nothing_and_exits_zero(monkeypatch):
    monkeypatch.setattr(wd, "collect_cluster_state", lambda cfg: ({"items": []}, {"items": []}, {"items": []}))
    monkeypatch.setattr(wd, "collect_pvc_usage", lambda p, cfg: ([], "prometheus"))
    monkeypatch.setattr(wd, "list_open_watchdog_issues", lambda repo, label: [])
    monkeypatch.setattr(wd, "create_issue", lambda *a, **k: pytest.fail("filed an issue with no findings"))
    assert wd.main(["--repo", "FuzeOne/FuzeInfra"]) == 0


# ---------------------------------------------------------------------------
# 9. noise containment learned from the first live runs
# ---------------------------------------------------------------------------

def _multi_container_stuck_pod():
    """A pod wedged on a volume attach: every container reports waiting."""
    return {
        "metadata": {"name": "fuzeinfra-kafka-0", "namespace": "fuzeinfra"},
        "spec": {"nodeName": "vmi3396106"},
        "status": {
            "phase": "Pending",
            "startTime": _ts(22 * 60),
            "initContainerStatuses": [{
                "name": "init-chown-data", "ready": False, "restartCount": 0,
                "state": {"waiting": {"reason": "PodInitializing"}},
            }],
            "containerStatuses": [{
                "name": "kafka", "ready": False, "restartCount": 0,
                "state": {"waiting": {"reason": "PodInitializing",
                                      "message": "already mounted or mount point busy"}},
            }],
        },
    }


def test_one_containercreating_finding_per_pod_not_per_container():
    """One stuck mount is one condition.

    The per-container shape filed `fuzeinfra-kafka-0:kafka` and
    `fuzeinfra-kafka-0:init-chown-data` as two issues for one wedged pod on
    2026-09-01 — noise that dilutes exactly the tracker it is trying to fill.
    """
    findings = wd.detect_stuck_container_creating(
        {"items": [_multi_container_stuck_pod()]}, CFG, NOW)
    assert len(findings) == 1
    assert findings[0].subject == "fuzeinfra/fuzeinfra-kafka-0"
    assert findings[0].facts["containers"] == ["kafka", "init-chown-data"]
    # The diagnostic message is still carried, from whichever container has one.
    assert findings[0].facts["waiting_message"] == "already mounted or mount point busy"


def _crashloop_pods(n):
    return {"items": [pod(f"api-{i}", restarts=600, reason="CrashLoopBackOff", ready=False,
                          start_minutes_ago=900) for i in range(n)]}


def _full_ceiling():
    return [{"number": n, "url": f"u{n}", "title": f"[watchdog] x: {n}", "body": ""}
            for n in range(CFG["issues"]["max_open"])]


def test_open_issue_ceiling_still_holds_back_noise(monkeypatch):
    """max_per_run alone only spreads a backlog out; the ceiling bounds it.

    At 10 per run every 15 minutes, the 55 conditions the first live run found
    would all have been filed within ~1.5h regardless. Nothing NEW of the noisy
    kinds is filed while `max_open` issues are already open.
    """
    filed = []
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, _crashloop_pods(5), {"items": []}))
    monkeypatch.setattr(wd, "collect_pvc_usage", lambda p, cfg: ([], "prometheus"))
    monkeypatch.setattr(wd, "list_open_watchdog_issues", lambda repo, label: _full_ceiling())
    monkeypatch.setattr(wd, "ensure_label", lambda *a, **k: pytest.fail("touched labels with no slot"))
    monkeypatch.setattr(wd, "create_issue", lambda *a, **k: filed.append(a) or "url")

    exit_code = wd.main(["--repo", "FuzeOne/FuzeInfra"])

    assert filed == []
    # Detection is NOT capped: the condition is still reported and the run is red.
    assert exit_code == 1


def test_open_issue_ceiling_never_starves_a_stuck_argo_op(monkeypatch):
    """REGRESSION (2026-09-15..20 prod freeze): a full ceiling must not mute the
    one condition that freezes every prod deploy.

    This test used to assert the OPPOSITE — that with max_open issues open, a
    37-hour wedged fuzeinfra-prod sync files nothing. That was the freeze,
    encoded as correct: 20 stuck-backup-pod issues held every slot, and because
    auto-terminate rides on filing, the wedge was neither reported nor acted on.
    """
    apps = {"items": [argo_app("fuzeinfra-prod", "Running", 37 * 60, "waiting for healthy state")]}
    filed, dispatched = [], []
    monkeypatch.setattr(wd, "collect_cluster_state", lambda cfg: (apps, {"items": []}, {"items": []}))
    monkeypatch.setattr(wd, "collect_pvc_usage", lambda p, cfg: ([], "prometheus"))
    monkeypatch.setattr(wd, "list_open_watchdog_issues", lambda repo, label: _full_ceiling())
    monkeypatch.setattr(wd, "ensure_label", lambda *a, **k: None)
    monkeypatch.setattr(wd, "create_issue", lambda repo, label, title, body: filed.append(title) or "url")
    monkeypatch.setattr(wd, "dispatch_terminate_op",
                        lambda repo, wf, app, ref="main": dispatched.append(app))

    wd.main(["--repo", "FuzeOne/FuzeInfra"])

    assert filed == ["[watchdog] stuck-argo-op: fuzeinfra-prod"]
    assert dispatched == ["fuzeinfra-prod"]


def test_ceiling_leaves_room_for_a_partial_batch(monkeypatch):
    filed = []
    already_open = [{"number": n, "url": "u", "title": "t", "body": ""}
                    for n in range(CFG["issues"]["max_open"] - 3)]

    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, _crashloop_pods(9), {"items": []}))
    monkeypatch.setattr(wd, "collect_pvc_usage", lambda p, cfg: ([], "prometheus"))
    monkeypatch.setattr(wd, "list_open_watchdog_issues", lambda repo, label: already_open)
    monkeypatch.setattr(wd, "ensure_label", lambda *a, **k: None)
    monkeypatch.setattr(wd, "create_issue", lambda *a, **k: filed.append(a) or "url")

    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert len(filed) == 3


# ---------------------------------------------------------------------------
# 10. the 2026-09-15..20 freeze: detected 42 times, reported and acted on 0
# ---------------------------------------------------------------------------
#
# The watchdog saw fuzeinfra-prod wedged at 47.3h and logged "already tracked by
# #776 (not re-filed)". #776 was the PREVIOUS incident's issue, never closed.
# Three compounding bugs; one test group each.

ARGO_KEY = "<!-- watchdog-key: stuck-argo-op:fuzeinfra-prod -->"
WEDGED = {"items": [argo_app("fuzeinfra-prod", "Running", 47 * 60,
                             "waiting for healthy state of apps/Deployment/fuzeinfra-overprovisioning")]}


def _stale_argo_issue(body_extra=""):
    return {"number": 776, "url": "https://github.com/FuzeOne/FuzeInfra/issues/776",
            "title": "[watchdog] stuck-argo-op: fuzeinfra-prod", "body": ARGO_KEY + body_extra}


def _wire(monkeypatch, apps, open_issues, comments=()):
    calls = {"dispatched": [], "commented": [], "closed": [], "filed": []}
    monkeypatch.setattr(wd, "collect_cluster_state", lambda cfg: (apps, {"items": []}, {"items": []}))
    monkeypatch.setattr(wd, "collect_pvc_usage", lambda p, cfg: ([], "prometheus"))
    monkeypatch.setattr(wd, "list_open_watchdog_issues", lambda repo, label: list(open_issues))
    monkeypatch.setattr(wd, "list_issue_comments", lambda repo, n: list(comments))
    monkeypatch.setattr(wd, "ensure_label", lambda *a, **k: None)
    monkeypatch.setattr(wd, "create_issue", lambda repo, label, t, b: calls["filed"].append(t) or "url")
    monkeypatch.setattr(wd, "comment_issue", lambda repo, n, b: calls["commented"].append((n, b)))
    monkeypatch.setattr(wd, "close_issue", lambda repo, n: calls["closed"].append(n))
    monkeypatch.setattr(wd, "dispatch_terminate_op",
                        lambda repo, wf, app, ref="main": calls["dispatched"].append(app))
    return calls


# --- bug 1: auto-terminate only fired for NEWLY filed findings ---------------

def test_already_tracked_argo_wedge_is_still_terminated(monkeypatch):
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue()])
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["dispatched"] == ["fuzeinfra-prod"]
    assert calls["filed"] == []  # still deduplicated — no second issue
    (number, body), = calls["commented"]
    assert number == 776
    assert "fuzeinfra-overprovisioning" in body  # names the blocking resource
    assert wd.argo_terminated_marker("fuzeinfra-prod", _ts(47 * 60)) in body


def test_already_terminated_operation_is_not_re_dispatched(monkeypatch):
    """Bounded: one terminate per operation, never every 15 minutes."""
    marker = wd.argo_terminated_marker("fuzeinfra-prod", _ts(47 * 60))
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue()], comments=[{"body": marker}])
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["dispatched"] == []


def test_run_that_filed_the_issue_is_not_followed_by_a_second_terminate(monkeypatch):
    """A newly filed issue carries the marker in its body, so the next run skips it."""
    marker = wd.argo_terminated_marker("fuzeinfra-prod", _ts(47 * 60))
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue(body_extra=marker)])
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["dispatched"] == []


def test_a_new_operation_on_the_same_app_is_terminated_again(monkeypatch):
    """A marker for an OLDER op must not suppress action on a new wedge."""
    old = wd.argo_terminated_marker("fuzeinfra-prod", _ts(20 * 24 * 60))
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue()], comments=[{"body": old}])
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["dispatched"] == ["fuzeinfra-prod"]


def test_newly_filed_argo_issue_records_the_terminated_operation(monkeypatch):
    bodies = []
    calls = _wire(monkeypatch, WEDGED, [])
    monkeypatch.setattr(wd, "create_issue", lambda repo, label, t, b: bodies.append(b) or "url")
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["dispatched"] == ["fuzeinfra-prod"]
    assert wd.argo_terminated_marker("fuzeinfra-prod", _ts(47 * 60)) in bodies[0]


# --- bug 2: resolved Argo issues were never closed ---------------------------

def test_resolved_argo_issue_is_closed_on_a_clean_run(monkeypatch):
    """The early return on zero findings used to skip ALL issue handling."""
    calls = _wire(monkeypatch, {"items": []}, [_stale_argo_issue()])
    assert wd.main(["--repo", "FuzeOne/FuzeInfra"]) == 0
    assert calls["closed"] == [776]


def test_resolved_argo_issue_is_closed_alongside_other_findings(monkeypatch):
    calls = _wire(monkeypatch, {"items": []}, [_stale_argo_issue()])
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, _crashloop_pods(1), {"items": []}))
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["closed"] == [776]


def test_still_wedged_argo_issue_is_not_closed(monkeypatch):
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue()])
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["closed"] == []


def test_unreadable_application_list_never_mass_closes(monkeypatch):
    """No 'items' key is not evidence of recovery."""
    calls = _wire(monkeypatch, {}, [_stale_argo_issue()])
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["closed"] == []


def test_dry_run_closes_nothing(monkeypatch):
    calls = _wire(monkeypatch, {"items": []}, [_stale_argo_issue()])
    wd.main(["--repo", "FuzeOne/FuzeInfra", "--dry-run"])
    assert calls["closed"] == []


def test_closing_frees_the_slot_for_the_same_run(monkeypatch):
    """A closed issue must not still count against max_open in the run that closed it."""
    open_issues = [_stale_argo_issue()] + [
        {"number": n, "url": "u", "title": f"[watchdog] x: {n}", "body": ""}
        for n in range(CFG["issues"]["max_open"] - 1)
    ]
    calls = _wire(monkeypatch, {"items": []}, open_issues)
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, _crashloop_pods(1), {"items": []}))
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["closed"] == [776]
    assert len(calls["filed"]) == 1


# ---------------------------------------------------------------------------
# 11. the point-in-time ISSUE FLOOD: `[watchdog] stuck-containercreating: <pod>`
#     filed a new issue every run because the dedupe SUBJECT was the raw pod
#     name, which churns on every new pod instance
#     (`fuzeinfra-backup-mongodb-29820980-8mcg8` -> a new `-<epoch>-<hash>`
#     suffix every CronJob run; `<deploy>-<rs-hash>-<pod-hash>` every rollout).
#     _stable_workload_name/_stable_pod_subject fix the KEY; close_resolved_
#     findings gives the resulting issue a bounded lifecycle.
# ---------------------------------------------------------------------------

def test_stable_workload_name_strips_replicaset_pod_template_hash():
    """Deployment-owned pod: subject is the Deployment, not `<deploy>-<rs>-<pod>`."""
    p = pod("api-7c9b6d4f8-x2vqk", owner=("ReplicaSet", "api-7c9b6d4f8"))
    assert wd._stable_workload_name(p) == "api"


def test_stable_workload_name_strips_cronjob_job_and_pod_suffix():
    """The exact case named in the flood: a CronJob-spawned backup pod."""
    p = pod("fuzeinfra-backup-mongodb-29820980-8mcg8",
            owner=("Job", "fuzeinfra-backup-mongodb-29820980"))
    assert wd._stable_workload_name(p) == "fuzeinfra-backup-mongodb"


def test_stable_workload_name_keeps_statefulset_pod_ordinal():
    """StatefulSet pod names (`name-0`) are already stable — must not be mangled."""
    p = pod("fuzeinfra-loki-0", owner=("StatefulSet", "fuzeinfra-loki"))
    assert wd._stable_workload_name(p) == "fuzeinfra-loki"


def test_stable_workload_name_falls_back_to_suffix_stripping_without_owner():
    """No ownerReferences at all (e.g. a bare pod): strip suffix-shaped tails."""
    p = pod("fuzeinfra-backup-mongodb-29820980-8mcg8")
    assert wd._stable_workload_name(p) == "fuzeinfra-backup-mongodb"


def test_stable_workload_name_is_unchanged_for_a_plain_name():
    """No random-looking suffix to strip: the name passes through unchanged."""
    p = pod("fuzeinfra-loki-0")
    assert wd._stable_workload_name(p) == "fuzeinfra-loki-0"


def test_stuck_container_creating_dedupe_key_is_stable_across_pod_churn():
    """Two separate detection runs, two different pod instances of the same
    CronJob, must produce the SAME Finding.key — the property the flood
    violated."""
    def _finding_for(pod_name, job_name):
        p = pod(pod_name, container="mongodb", reason="ContainerCreating",
                ready=False, start_minutes_ago=45, phase="Pending",
                owner=("Job", job_name))
        findings = wd.detect_stuck_container_creating({"items": [p]}, CFG, NOW)
        assert len(findings) == 1
        return findings[0]

    first = _finding_for("fuzeinfra-backup-mongodb-29820980-8mcg8",
                          "fuzeinfra-backup-mongodb-29820980")
    second = _finding_for("fuzeinfra-backup-mongodb-30015550-p4x9z",
                          "fuzeinfra-backup-mongodb-30015550")
    assert first.key == second.key == "stuck-containercreating:fuzeinfra/fuzeinfra-backup-mongodb"
    # The exact pod instance is still preserved for diagnostics.
    assert first.facts["pod"] == "fuzeinfra-backup-mongodb-29820980-8mcg8"
    assert second.facts["pod"] == "fuzeinfra-backup-mongodb-30015550-p4x9z"


def _stuck_creating_pod(pod_name, job_name, minutes=45):
    return pod(pod_name, container="mongodb", reason="ContainerCreating",
               ready=False, start_minutes_ago=minutes, phase="Pending",
               owner=("Job", job_name))


def _churned_backup_issue(pod_name="fuzeinfra-backup-mongodb-29820980-8mcg8"):
    key = "<!-- watchdog-key: stuck-containercreating:fuzeinfra/fuzeinfra-backup-mongodb -->"
    instance = f"<!-- watchdog-instance: {pod_name} -->"
    return {
        "number": 1062,
        "url": "https://github.com/FuzeOne/FuzeInfra/issues/1062",
        "title": "[watchdog] stuck-containercreating: fuzeinfra/fuzeinfra-backup-mongodb",
        "body": f"{key}\n{instance}\n",
    }


def test_new_pod_instance_of_an_already_tracked_workload_is_not_refiled(monkeypatch):
    """The flood, exactly: a fresh CronJob pod (new hash) must dedupe against
    the already-open issue for this workload instead of filing #1063."""
    new_pod = _stuck_creating_pod("fuzeinfra-backup-mongodb-30015550-p4x9z",
                                  "fuzeinfra-backup-mongodb-30015550")
    calls = _wire(monkeypatch, {"items": []}, [_churned_backup_issue()])
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, {"items": [new_pod]}, {"items": []}))
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["filed"] == []


def test_new_pod_instance_updates_the_tracked_issue_with_a_comment(monkeypatch):
    """Not re-filed, but not silently swallowed either: the thread is updated."""
    new_pod = _stuck_creating_pod("fuzeinfra-backup-mongodb-30015550-p4x9z",
                                  "fuzeinfra-backup-mongodb-30015550")
    calls = _wire(monkeypatch, {"items": []}, [_churned_backup_issue()])
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, {"items": [new_pod]}, {"items": []}))
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    (number, body), = calls["commented"]
    assert number == 1062
    assert "fuzeinfra-backup-mongodb-30015550-p4x9z" in body


def test_same_pod_instance_duplicate_is_a_true_noop(monkeypatch):
    """Re-detecting the SAME still-stuck pod must not comment every 15 minutes."""
    same_pod = _stuck_creating_pod("fuzeinfra-backup-mongodb-29820980-8mcg8",
                                   "fuzeinfra-backup-mongodb-29820980")
    calls = _wire(monkeypatch, {"items": []}, [_churned_backup_issue()])
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, {"items": [same_pod]}, {"items": []}))
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["filed"] == []
    assert calls["commented"] == []


def test_close_resolved_findings_closes_an_issue_no_longer_detected(monkeypatch):
    commented, closed = [], []
    monkeypatch.setattr(wd, "comment_issue", lambda repo, n, b: commented.append(n))
    monkeypatch.setattr(wd, "close_issue", lambda repo, n: closed.append(n))
    still_open, closed_lines = wd.close_resolved_findings(
        "FuzeOne/FuzeInfra", [_churned_backup_issue()], wd.KIND_CREATING, active_keys=set(),
    )
    assert still_open == []
    assert closed == [1062]
    assert commented == [1062]
    assert closed_lines and "no longer detected" in closed_lines[0]


def test_close_resolved_findings_leaves_a_still_active_issue_open():
    key = "stuck-containercreating:fuzeinfra/fuzeinfra-backup-mongodb"
    still_open, closed_lines = wd.close_resolved_findings(
        "FuzeOne/FuzeInfra", [_churned_backup_issue()], wd.KIND_CREATING, active_keys={key},
    )
    assert len(still_open) == 1
    assert closed_lines == []


def test_stuck_creating_issue_closes_when_the_workload_recovers(monkeypatch):
    """Bounded lifecycle end-to-end: once the workload stops appearing in the
    findings, its tracked issue closes instead of sitting open forever."""
    calls = _wire(monkeypatch, {"items": []}, [_churned_backup_issue()])
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, {"items": []}, {"items": []}))
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["closed"] == [1062]


# ---------------------------------------------------------------------------
# 12. the 2026-10-14 freeze: a comment-capped tracking issue silenced EVERYTHING
# ---------------------------------------------------------------------------
#
# Tracking issue #1238 reached GitHub's 2,500-comment cap. The "still occurring on
# a new pod instance" comment then raised GitHubError, run() returned 2, and every
# statement after it was skipped: the auto-terminate of a stuck Argo operation and
# the dark-node reboot ladder. fuzeinfra-prod sat Running for NINE DAYS, detected
# every ~20 minutes and acted on zero times. The alerting path was the thing that
# crashed, so nothing alerted. Same outcome as 2026-09-15..20 (42 runs, 0 actions),
# a different cause — which is why this is pinned here beside it.

CAP_MESSAGE = ("gh issue comment 1062 --repo FuzeOne/FuzeInfra --body-file x.md exited 1: "
               "GraphQL: Commenting is disabled on issues with more than 2500 comments "
               "(addComment)")


def _wire_capped(monkeypatch, capped_numbers, close_fails=False):
    """Argo wedge ALREADY tracked (#776) + a churned backup pod tracked by #1062,
    where commenting on `capped_numbers` raises exactly what GitHub raises."""
    new_pod = _stuck_creating_pod("fuzeinfra-backup-mongodb-30015550-p4x9z",
                                  "fuzeinfra-backup-mongodb-30015550")
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue(), _churned_backup_issue()])
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: (WEDGED, {"items": [new_pod]}, {"items": []}))

    def comment(repo, number, body):
        if number in capped_numbers:
            raise wd.GitHubError(CAP_MESSAGE)
        calls["commented"].append((number, body))

    def close(repo, number):
        if close_fails:
            raise wd.GitHubError("gh issue close exited 1: boom")
        calls["closed"].append(number)

    monkeypatch.setattr(wd, "comment_issue", comment)
    monkeypatch.setattr(wd, "close_issue", close)
    return calls


def test_a_comment_capped_issue_does_not_abort_the_run(monkeypatch):
    _wire_capped(monkeypatch, {1062})
    # Findings are present, so the run is red by design (1) — but NOT the
    # "could not complete" exit (2) that meant it had gone blind.
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 1


def test_a_stuck_argo_operation_is_still_terminated_after_a_capped_comment(monkeypatch):
    """The point of the whole fix: the code AFTER the failing comment must still run."""
    calls = _wire_capped(monkeypatch, {1062})
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["dispatched"] == ["fuzeinfra-prod"]
    (number, body), = calls["commented"]
    assert number == 776
    assert wd.argo_terminated_marker("fuzeinfra-prod", _ts(47 * 60)) in body


def test_a_capped_issue_is_closed_so_the_finding_refiles_fresh(monkeypatch):
    """Otherwise it dedupes every future occurrence against itself, forever."""
    calls = _wire_capped(monkeypatch, {1062})
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert calls["closed"] == [1062]


def test_the_capped_issue_is_reported_in_the_summary(monkeypatch, capsys):
    _wire_capped(monkeypatch, {1062})
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    out = capsys.readouterr().out
    assert "comment cap" in out
    assert "::warning::" in out


def test_failing_to_close_a_capped_issue_is_reported_not_fatal(monkeypatch, capsys):
    calls = _wire_capped(monkeypatch, {1062}, close_fails=True)
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 1
    assert calls["dispatched"] == ["fuzeinfra-prod"]
    assert "could NOT be closed" in capsys.readouterr().out


def test_a_non_cap_comment_failure_is_also_non_fatal_but_does_not_close(monkeypatch):
    """A rate limit or a 403 must not skip the Argo terminate either, and must not
    close an issue that is perfectly healthy."""
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue(), _churned_backup_issue()])
    new_pod = _stuck_creating_pod("fuzeinfra-backup-mongodb-30015550-p4x9z",
                                  "fuzeinfra-backup-mongodb-30015550")
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: (WEDGED, {"items": [new_pod]}, {"items": []}))

    def comment(repo, number, body):
        if number == 1062:
            raise wd.GitHubError("gh issue comment exited 1: HTTP 403 rate limit exceeded")
        calls["commented"].append((number, body))

    monkeypatch.setattr(wd, "comment_issue", comment)
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 1
    assert calls["dispatched"] == ["fuzeinfra-prod"]
    assert calls["closed"] == []


def test_the_cap_detector_matches_githubs_real_message_and_nothing_looser():
    assert wd.is_comment_cap_error(wd.GitHubError(CAP_MESSAGE))
    assert not wd.is_comment_cap_error(wd.GitHubError("HTTP 403 rate limit exceeded"))
    assert not wd.is_comment_cap_error(wd.GitHubError("Commenting failed"))


def test_an_unreadable_cluster_is_still_fatal(monkeypatch):
    """The blind-fails-loudly property must survive: only the INFORMATIONAL comment
    became non-fatal, not 'could not look at the cluster'."""
    def boom(cfg):
        raise wd.ClusterUnreachable("kubectl failed")
    monkeypatch.setattr(wd, "collect_cluster_state", boom)
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 2


# --- the call that ACTUALLY killed the runs (the first fix guarded the wrong one) --
#
# Reproduced from the failing log, not guessed: #1238 is the backup-neo4j thread, its
# workload is no longer stuck, so the watchdog tries to CLOSE it as resolved. Closing
# posts a courtesy "no longer detected" comment FIRST. At the cap that comment raised,
# the run died before close_issue, and the issue stayed open to fail again every 20
# minutes. The earlier fix (section 12) guarded the "still occurring" comment, a
# different site that this incident never reached — which is why it did not help.

def _capped_resolved_issue():
    issue = _churned_backup_issue()
    issue.update({"number": 1238,
                  "url": "https://github.com/FuzeOne/FuzeInfra/issues/1238",
                  "title": "[watchdog] stuck-containercreating: fuzeinfra/fuzeinfra-backup-neo4j"})
    issue["body"] = "<!-- watchdog-key: stuck-containercreating:fuzeinfra/fuzeinfra-backup-neo4j -->\n"
    return issue


def _wire_resolved_and_capped(monkeypatch):
    # backup-neo4j is no longer detected; something else IS (so the run is not the
    # early-return "no findings" path), and the Argo wedge is still tracked.
    calls = _wire(monkeypatch, WEDGED, [_stale_argo_issue(), _capped_resolved_issue()])

    def comment(repo, number, body):
        if number == 1238:
            raise wd.GitHubError(CAP_MESSAGE.replace("1062", "1238"))
        calls["commented"].append((number, body))

    monkeypatch.setattr(wd, "comment_issue", comment)
    return calls


def test_a_resolved_but_comment_capped_issue_is_still_closed(monkeypatch):
    """The courtesy comment may be refused; the CLOSE must still happen."""
    calls = _wire_resolved_and_capped(monkeypatch)
    wd.main(["--repo", "FuzeOne/FuzeInfra"])
    assert 1238 in calls["closed"]


def test_the_run_survives_and_still_terminates_the_argo_wedge(monkeypatch):
    calls = _wire_resolved_and_capped(monkeypatch)
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 1   # findings present, not blind
    assert calls["dispatched"] == ["fuzeinfra-prod"]


def test_the_same_holds_on_a_clean_run_with_no_findings(monkeypatch):
    """The early-return path also closes resolved issues, and must survive the cap."""
    calls = _wire(monkeypatch, {"items": []}, [_capped_resolved_issue()])

    def comment(repo, number, body):
        raise wd.GitHubError(CAP_MESSAGE)

    monkeypatch.setattr(wd, "comment_issue", comment)
    monkeypatch.setattr(wd, "collect_cluster_state",
                        lambda cfg: ({"items": []}, {"items": []}, {"items": []}))
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 0
    assert calls["closed"] == [1238]


def test_a_resolved_argo_issue_at_the_cap_is_still_closed(monkeypatch):
    stale = _stale_argo_issue()

    calls = _wire(monkeypatch, {"items": []}, [stale])

    def comment(repo, number, body):
        raise wd.GitHubError(CAP_MESSAGE)

    monkeypatch.setattr(wd, "comment_issue", comment)
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 0
    assert calls["closed"] == [776]


def test_state_carrying_comments_are_still_strict(monkeypatch):
    """The argo-terminated marker is read back to stop a terminate re-dispatch loop.
    If posting it fails the run must abort, not shrug: a lost marker would re-dispatch
    a terminate against the same operation every run."""
    _wire(monkeypatch, WEDGED, [_stale_argo_issue()])

    def comment(repo, number, body):
        raise wd.GitHubError(CAP_MESSAGE)

    monkeypatch.setattr(wd, "comment_issue", comment)
    assert wd.run(["--repo", "FuzeOne/FuzeInfra"]) == 2


def test_comment_best_effort_reports_success_and_failure(monkeypatch, capsys):
    monkeypatch.setattr(wd, "comment_issue", lambda r, n, b: None)
    assert wd.comment_best_effort("r", "1", "b") is True
    def boom(r, n, b):
        raise wd.GitHubError("nope")
    monkeypatch.setattr(wd, "comment_issue", boom)
    assert wd.comment_best_effort("r", "1", "b") is False
    assert "::warning::" in capsys.readouterr().out
