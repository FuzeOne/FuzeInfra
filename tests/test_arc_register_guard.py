"""Executable invariants for the arc-register allowlist guard.

arc-register.yml lets a consumer repo register/unregister an ARC runner
scale-set on the shared cluster via `repository_dispatch`. Unlike
cluster-query (a bash filter inside the workflow), the authorization logic
here is a standalone Python script (scripts-tools/validate_arc_register.py) —
these tests import and call it directly rather than shelling out to a
truncated workflow script, and therefore run on every platform (no Windows
skip needed).

Two properties have to hold:

  1. An unlisted `repo` claim is ALWAYS denied — never partially trusted,
     never used to derive a registration target.
  2. On a match, the values actually used to register the runner (repo_url,
     scale_set_name) come from the ALLOWLIST ENTRY, not from whatever the
     caller claimed — so even a same-string match can't be used to smuggle a
     different target in via extra payload fields the validator ignores.

Offline: pure Python + one JSON fixture, no cluster, no network, no bash.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts-tools" / "validate_arc_register.py"
REAL_ALLOWLIST = ROOT / "config" / "arc-register-allowlist.json"


@pytest.fixture()
def allowlist_path(tmp_path):
    """A small, deterministic fixture allowlist — independent of prod's real
    list so this test doesn't silently start failing when someone onboards a
    7th repo."""
    data = {
        "allowed_repos": {
            "FuzeOne/FuzeHub": {"scale_set_name": "fuzehub"},
            "FuzeOne/FuzeSales": {"scale_set_name": "fuzesales"},
        },
        "allowed_actions": ["install", "uninstall"],
    }
    p = tmp_path / "arc-register-allowlist.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def run_validator(allowlist_path, repo, action="install", github_output=None):
    args = [
        sys.executable,
        str(SCRIPT),
        "--allowlist",
        str(allowlist_path),
        "--repo",
        repo,
        "--action",
        action,
    ]
    if github_output is not None:
        args += ["--github-output", str(github_output)]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"validator should always exit 0 and convey the decision via output, "
        f"got rc={result.returncode}\n{result.stdout}{result.stderr}"
    )
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


# --- allowlisted repos are authorized, and the target is DERIVED -----------

def test_allowlisted_repo_is_authorized(allowlist_path):
    out = run_validator(allowlist_path, "FuzeOne/FuzeHub", "install")
    assert out["decision"] == "authorized"
    assert out["repo"] == "FuzeOne/FuzeHub"
    assert out["repo_url"] == "https://github.com/FuzeOne/FuzeHub"
    assert out["scale_set_name"] == "fuzehub"


def test_second_allowlisted_repo_gets_its_own_scale_set_name(allowlist_path):
    out = run_validator(allowlist_path, "FuzeOne/FuzeSales", "uninstall")
    assert out["decision"] == "authorized"
    assert out["scale_set_name"] == "fuzesales"
    assert out["action"] == "uninstall"


# --- unlisted / forged repo claims are ALWAYS denied ------------------------

@pytest.mark.parametrize(
    "claimed_repo",
    [
        "izzywdev/NotOnboarded",
        "FuzeOne/FuzeHub ",  # trailing space — must not fuzzy-match
        "IZZYWDEV/FUZEHUB",  # case must not fuzzy-match
        "",
        "../../etc/passwd",
        "FuzeOne/FuzeHub; rm -rf /",
    ],
)
def test_unlisted_repo_claims_are_denied(allowlist_path, claimed_repo):
    out = run_validator(allowlist_path, claimed_repo, "install")
    assert out["decision"] == "denied", (
        f"arc-register would have authorized an unlisted claim: {claimed_repo!r}"
    )
    # A denied decision must never carry a usable registration target.
    assert out["repo_url"] == ""
    assert out["scale_set_name"] == ""
    assert out["repo"] == ""


def test_denied_decision_explains_why(allowlist_path):
    out = run_validator(allowlist_path, "izzywdev/NotOnboarded", "install")
    assert "not an exact key" in out["reasons"] or "allowed_repos" in out["reasons"]


# --- action must be recognized ----------------------------------------------

@pytest.mark.parametrize("bad_action", ["delete", "patch", "INSTALL", "install "])
def test_unrecognized_action_is_denied(allowlist_path, bad_action):
    out = run_validator(allowlist_path, "FuzeOne/FuzeHub", bad_action)
    assert out["decision"] == "denied"


def test_default_action_is_install_when_blank(allowlist_path):
    # The workflow feeds '' when neither client_payload.action nor
    # inputs.action is set; the validator must default sanely rather than deny
    # a well-formed install-only dispatch.
    out = run_validator(allowlist_path, "FuzeOne/FuzeHub", action="")
    assert out["decision"] == "authorized"
    assert out["action"] == "install"


# --- GITHUB_OUTPUT wiring ----------------------------------------------------

def test_writes_github_output_file(allowlist_path, tmp_path):
    gh_out = tmp_path / "github_output.txt"
    gh_out.write_text("", encoding="utf-8")
    run_validator(allowlist_path, "FuzeOne/FuzeHub", "install", github_output=gh_out)
    content = gh_out.read_text(encoding="utf-8")
    assert "decision=authorized" in content
    assert "scale_set_name=fuzehub" in content


# --- the real prod allowlist: sanity + parity with the task's repo list ----

def test_real_allowlist_is_valid_json_with_expected_repos():
    assert REAL_ALLOWLIST.exists(), "config/arc-register-allowlist.json must exist"
    data = json.loads(REAL_ALLOWLIST.read_text(encoding="utf-8"))
    allowed = data["allowed_repos"]
    expected = {
        "FuzeOne/FuzeContact",
        "FuzeOne/FuzeHub",
        "FuzeOne/FuzeSales",
        "FuzeOne/FuzeService",
        "FuzeOne/FuzeSocial",
        "izzywdev/MendysRobotics",
    }
    assert expected.issubset(allowed.keys()), (
        f"expected repos missing from the real allowlist: {expected - allowed.keys()}"
    )
    for repo, entry in allowed.items():
        assert entry.get("scale_set_name"), f"{repo} has no scale_set_name"


def test_real_allowlist_denies_an_unlisted_repo():
    out = run_validator(REAL_ALLOWLIST, "izzywdev/SomeRandomRepo", "install")
    assert out["decision"] == "denied"
