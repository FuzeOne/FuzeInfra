"""The consumer AppProject policy gate (scripts/validate_consumer_appproject.py).

#639 let products own their AppProject; it did not relax the policy (#99: never
a `fuzeinfra` destination; #629: Namespace is the only cluster-scoped kind).
Once ownership moved nothing enforced either, and #1278 is what that cost:
FuzeFront#1198 whitelisted cluster RBAC in its own project, and the same project
existed in two repos, so FuzeFront deploys flapped and then froze for days.

These tests pin every rule in both directions, replay the exact FuzeFront#1198
project, hold the projects FuzeInfra still keeps to the same boundary, and check
that argocd-register runs the gate before it applies anything.

Offline: no cluster, no network.
"""

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
PROJECTS = REPO / "argocd" / "projects"
APPLICATIONS = REPO / "argocd" / "applications"
WORKFLOW = REPO / ".github" / "workflows" / "argocd-register.yml"
OWNERSHIP = REPO / "governance" / "consumer-owned-appprojects.json"

_spec = importlib.util.spec_from_file_location(
    "validate_consumer_appproject", REPO / "scripts" / "validate_consumer_appproject.py"
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

IN_CLUSTER = "https://kubernetes.default.svc"

# The least-privilege shape: FuzeFront's project as it should be.
COMPLIANT = {
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "AppProject",
    "metadata": {"name": "fuzefront", "namespace": "argocd"},
    "spec": {
        "sourceRepos": ["https://github.com/FuzeOne/FuzeFront.git"],
        "destinations": [
            {"namespace": "fuzefront", "server": IN_CLUSTER},
            {"namespace": "fuzequality", "server": IN_CLUSTER},
            {"namespace": "argocd", "server": IN_CLUSTER},
        ],
        "clusterResourceWhitelist": [{"group": "", "kind": "Namespace"}],
        "namespaceResourceWhitelist": [{"group": "*", "kind": "*"}],
    },
}


def _with(**spec_changes):
    doc = copy.deepcopy(COMPLIANT)
    for key, value in spec_changes.items():
        if key == "name":
            doc["metadata"]["name"] = value
        else:
            doc["spec"][key] = value
    return doc


def _rules(errors):
    return sorted({re.search(r"(R\d) ", e).group(1) for e in errors})


def test_compliant_project_passes():
    assert gate.check_project(COMPLIANT, infra_held=set()) == []


def test_the_fuzefront_1198_project_is_rejected():
    """The project that froze FuzeFront deploys, replayed verbatim (its whitelist)."""
    doc = _with(clusterResourceWhitelist=[
        {"group": "", "kind": "Namespace"},
        {"group": "rbac.authorization.k8s.io", "kind": "ClusterRole"},
        {"group": "rbac.authorization.k8s.io", "kind": "ClusterRoleBinding"},
    ])
    errors = gate.check_project(doc, infra_held=set())
    assert _rules(errors) == ["R3"] and len(errors) == 2


@pytest.mark.parametrize("name", ["fuzeinfra", "default"])
def test_r1_reserved_names(name):
    assert "R1" in _rules(gate.check_project(_with(name=name), infra_held=set()))


def test_r2_a_project_fuzeinfra_still_holds_is_rejected():
    assert _rules(gate.check_project(COMPLIANT, infra_held={"fuzefront"})) == ["R2"]


@pytest.mark.parametrize("entry", [
    {"group": "rbac.authorization.k8s.io", "kind": "ClusterRole"},
    {"group": "rbac.authorization.k8s.io", "kind": "ClusterRoleBinding"},
    {"group": "apiextensions.k8s.io", "kind": "CustomResourceDefinition"},
    {"group": "admissionregistration.k8s.io", "kind": "ValidatingWebhookConfiguration"},
    {"group": "*", "kind": "*"},
    {"group": "", "kind": "*"},
])
def test_r3_only_namespace_is_cluster_scoped(entry):
    doc = _with(clusterResourceWhitelist=[{"group": "", "kind": "Namespace"}, entry])
    assert _rules(gate.check_project(doc, infra_held=set())) == ["R3"]


@pytest.mark.parametrize("dest", [
    {"namespace": "fuzeinfra", "server": IN_CLUSTER},
    {"namespace": "kube-system", "server": IN_CLUSTER},
    {"namespace": "*", "server": IN_CLUSTER},
    {"namespace": "fuze*", "server": IN_CLUSTER},
    {"namespace": "", "server": IN_CLUSTER},
    {"namespace": "fuzefront", "server": "*"},
    {"namespace": "fuzefront", "server": "https://other-cluster:6443"},
    {"namespace": "fuzefront", "name": "other"},
    {"namespace": "fuzefront"},
])
def test_r4_destinations(dest):
    doc = _with(destinations=[dest])
    assert _rules(gate.check_project(doc, infra_held=set())) == ["R4"]


def test_r4_in_cluster_by_name_is_allowed():
    doc = _with(destinations=[{"namespace": "fuzefront", "name": "in-cluster"}])
    assert gate.check_project(doc, infra_held=set()) == []


def test_r5_wildcard_source_repo():
    doc = _with(sourceRepos=["*"])
    assert _rules(gate.check_project(doc, infra_held=set())) == ["R5"]


def test_r6_identical_held_application_passes_divergent_fails():
    held = gate.infra_held_applications(APPLICATIONS)
    assert "fuzefront-apps" in held, "fixture assumption: FuzeInfra holds fuzefront-apps"
    same = {"kind": "Application", "metadata": {"name": "fuzefront-apps"},
            "spec": copy.deepcopy(held["fuzefront-apps"])}
    assert gate.check_application(same, held) == []
    diverged = copy.deepcopy(same)
    diverged["spec"]["source"]["targetRevision"] = "some-branch"
    assert _rules(gate.check_application(diverged, held)) == ["R6"]


def test_r6_application_fuzeinfra_does_not_hold_is_not_checked():
    app = {"kind": "Application", "metadata": {"name": "fuzefront"}, "spec": {"x": 1}}
    assert gate.check_application(app, gate.infra_held_applications(APPLICATIONS)) == []


# --- the projects FuzeInfra still holds -------------------------------------

def _held_consumer_projects():
    for path in sorted(PROJECTS.glob("*.yaml")):
        if path.stem == "fuzeinfra":
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") == "AppProject":
                yield path.name, doc


@pytest.mark.parametrize("filename,doc", list(_held_consumer_projects()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_projects_held_here_meet_the_same_boundary(filename, doc):
    """A consumer boundary is a consumer boundary, whichever repo holds the file."""
    assert gate.check_project(doc, infra_held=set(), consumer=False) == [], filename


def test_consumer_owned_projects_are_not_also_held_here():
    """Re-adding one of these is the two-writer conflict behind #1278."""
    owned = set(json.loads(OWNERSHIP.read_text())["projects"])
    held = gate.infra_held_projects(PROJECTS)
    assert not owned & held, (
        f"{sorted(owned & held)} are product-owned (#639) but also present in "
        "argocd/projects/, which deploy-prod re-applies every deploy — "
        "last-write-wins against the product's copy (#1278)"
    )


def test_fuzefront_project_is_product_owned():
    assert "fuzefront" in json.loads(OWNERSHIP.read_text())["projects"]
    assert not (PROJECTS / "fuzefront.yaml").exists()


# --- CLI: fail closed ----------------------------------------------------------

def _write(tmp_path, docs):
    d = tmp_path / "deploy" / "argocd"
    d.mkdir(parents=True)
    (d / "project.yaml").write_text(yaml.safe_dump_all(docs))
    return d


def test_cli_rejects_and_exits_nonzero(tmp_path, capsys):
    d = _write(tmp_path, [_with(destinations=[{"namespace": "fuzeinfra", "server": IN_CLUSTER}])])
    assert gate.main(["--infra-projects", str(PROJECTS), str(d)]) == 1
    out = capsys.readouterr().out
    assert "::error::" in out and "nothing was applied" in out


def test_cli_passes_compliant(tmp_path):
    d = _write(tmp_path, [COMPLIANT])
    assert gate.main(["--infra-projects", str(PROJECTS),
                      "--infra-applications", str(APPLICATIONS), str(d)]) == 0


def test_cli_with_no_appproject_passes(tmp_path):
    d = _write(tmp_path, [{"kind": "Application", "metadata": {"name": "x"}, "spec": {}}])
    assert gate.main(["--infra-projects", str(PROJECTS), str(d)]) == 0


# --- workflow wiring -----------------------------------------------------------

def _register_step():
    wf = yaml.safe_load(WORKFLOW.read_text())
    steps = wf["jobs"]["register"]["steps"]
    return steps, next(s for s in steps if s.get("name", "").startswith("Register Argo"))


def test_gate_runs_before_anything_is_applied():
    _, step = _register_step()
    script = step["run"]
    gate_at = script.find("validate_consumer_appproject.py")
    apply_at = script.find("kubectl apply")
    assert gate_at != -1, "argocd-register no longer runs the policy gate"
    assert gate_at < apply_at, "the gate must run before the first kubectl apply"


def test_gate_uses_fuzeinfras_files_not_the_consumers():
    """A consumer must not be able to ship its own validator or project list."""
    steps, step = _register_step()
    checkout = next(s for s in steps if s.get("with", {}).get("path") == "infra")
    sparse = checkout["with"]["sparse-checkout"]
    for needed in ("scripts/validate_consumer_appproject.py", "argocd/projects", "argocd/applications"):
        assert needed in sparse
    # Explicit, not defaulted: under a future `workflow_call` the default
    # checkout is the CALLER's repo, which would let a consumer ship its own
    # validator. (fuze-code-review on #1308 caught the earlier, inverted assert.)
    assert checkout["with"].get("repository") == "FuzeOne/FuzeInfra"
    assert checkout["with"].get("ref") == "main"
    assert re.search(r"@[0-9a-f]{40}$", checkout["uses"]), "pin the policy checkout by SHA"
    script = step["run"]
    assert "infra/scripts/validate_consumer_appproject.py" in script
    assert "--infra-projects infra/argocd/projects" in script
