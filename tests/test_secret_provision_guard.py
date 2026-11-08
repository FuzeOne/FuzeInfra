"""Executable invariants for the secret-provision request filter.

secret-provision is the only path by which an agent session can cause a write to
this repo's Actions secrets, so its guard carries two properties that are easy to
conflate and must both hold:

  1. no caller may transport a secret VALUE into the workflow, and
  2. no caller may target a credential the platform depends on.

(1) is the one that bites. This repo's job logs are PUBLIC — the same fact that
forces cluster-query to refuse `kubectl get secret` — so a value accepted as a
`workflow_dispatch` input or a `client_payload` field is a published credential
the moment anyone dispatches it. The defence is structural rather than a filter:
the workflow declares no such input at all, and `test_no_input_transports_a_value`
fails the build if one is ever added. A reviewer adding `value:` to make the
workflow "more useful" is exactly the regression this file exists to stop.

(2) is why PROTECTED exists. `SECRETS_ADMIN_PAT` is the workflow's OWN credential:
letting a run rewrite it either locks the workflow out of the repo or silently
swaps the identity that every subsequent run acts as. `copy_from` is guarded for
the same reason even though a copy never prints anything — re-keying a privileged
credential under a caller-chosen name is a lateral move, not a read.

These tests EXECUTE the workflow's real bash guard rather than grepping it for
keywords. A guard asserted by substring passes just as happily when the shell
logic around it is broken, and the subtle cases here — a lowercase name, a
leading digit, `copy_from` equal to the target — are precisely the ones a
keyword check cannot distinguish.

Offline: parses one YAML file and runs bash. No network, no GitHub, no cluster.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# The subject IS the Linux bash guard that runs on GitHub Actions runners; there
# is no PowerShell equivalent, and porting it would test something other than the
# real gate. On Windows, subprocess passes a multi-line `-c` script through
# CreateProcess/list2cmdline and truncates it at the first newline, so every
# BLOCKED case would exit 0 and pass vacuously.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="tests the real Linux CI bash guard — Git Bash truncates multi-line -c scripts",
)

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/secret-provision.yml"
ALLOWLIST_PATH = "governance/secret-provision-targets.json"

VALIDATE_STEP = "Validate request"
PROVISION_STEP = "Provision"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on() -> dict:
    """The workflow's trigger block.

    YAML 1.1 resolves a bare `on:` key to the boolean True, so `wf["on"]` is a
    KeyError against a perfectly valid workflow. Look up both spellings rather
    than depending on which resolver the installed PyYAML uses.
    """
    wf = _workflow()
    for key in ("on", True):
        if key in wf:
            return wf[key]
    raise AssertionError(f"no trigger block in {WORKFLOW}")


def _step(name: str) -> dict:
    for step in _workflow()["jobs"]["provision"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found in {WORKFLOW}")


def run_guard(tmp_path, allowlist=None, **env) -> subprocess.CompletedProcess:
    """Execute the real validation step with the given inputs.

    `allowlist`, when given, stands in for governance/secret-provision-targets.json so a
    test can exercise a target the REAL policy does not list. Without it, a test for the
    cross-repo copy rule would pass for the wrong reason — rejected by the allowlist it
    never got past, proving nothing about the copy rule itself.
    """
    github_env = tmp_path / "github_env"
    github_env.touch()
    if allowlist is None:
        cwd = ROOT
    else:
        (tmp_path / "governance").mkdir(exist_ok=True)
        (tmp_path / ALLOWLIST_PATH).write_text(
            json.dumps({"targets": allowlist}), encoding="utf-8"
        )
        cwd = tmp_path
    full = {
        "SECRET_NAME": "",
        "SOURCE": "",
        "COPY_FROM": "",
        "LENGTH": "48",
        "TARGET_REPO": "",
        "SELF_REPO": "FuzeOne/FuzeInfra",
        "GITHUB_ENV": str(github_env),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    full.update({k: str(v) for k, v in env.items()})
    return subprocess.run(
        ["bash", "-c", _step(VALIDATE_STEP)["run"]],
        env=full,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def ok(tmp_path, allowlist=None, **env) -> None:
    r = run_guard(tmp_path, allowlist=allowlist, **env)
    assert r.returncode == 0, f"expected ALLOWED, got rc={r.returncode}: {r.stdout}{r.stderr}"


def blocked(tmp_path, allowlist=None, **env) -> None:
    r = run_guard(tmp_path, allowlist=allowlist, **env)
    assert r.returncode != 0, f"expected BLOCKED, but it was allowed: {r.stdout}{r.stderr}"


# --------------------------------------------------------------------------
# (1) No caller may transport a value. Structural, not filtered.
# --------------------------------------------------------------------------

# Substrings that would indicate an input carrying plaintext. `secret_name` is
# legitimate, so match on the value-bearing words only.
VALUE_BEARING = ("value", "plaintext", "password", "passwd", "token", "credential")


def test_no_input_transports_a_value():
    """The workflow must declare no input that could carry a secret value.

    Job logs here are public; an input holding plaintext publishes it. Keep the
    three value-free sources (verify/generate/copy) instead.
    """
    inputs = _on()["workflow_dispatch"]["inputs"]
    for name in inputs:
        low = name.lower()
        assert not any(w in low for w in VALUE_BEARING), (
            f"input {name!r} looks like it transports a secret value. This repo's "
            f"job logs are public — use source=generate (mint in-runner), "
            f"source=copy (re-key an existing secret), or seal it offline with "
            f"scripts/seal-secret.sh instead."
        )


def test_declared_sources_are_exactly_the_value_free_three():
    opts = _on()["workflow_dispatch"]["inputs"]["source"]["options"]
    assert sorted(opts) == ["copy", "generate", "verify"], (
        "the source enum is the list of ways a value can originate WITHOUT a "
        f"caller transporting it; got {opts}"
    )


def test_provision_step_never_prints_a_value():
    """No line sends the value file to stdout except the ::add-mask:: call.

    Writing INTO the file (`> "$d/val"`) and feeding it to a command's stdin
    (`< "$d/val"`) are both safe and expected; only a read that lands in the log
    is a disclosure. ::add-mask:: is the one permitted read — it registers the
    value with the runner so any later accidental echo is redacted.
    """
    for raw in _step(PROVISION_STEP)["run"].splitlines():
        line = raw.strip()
        if "$d/val" not in line:
            continue
        if '> "$d/val"' in line or '< "$d/val"' in line:
            continue  # write / stdin redirect — nothing reaches the log
        assert "::add-mask::" in line, f"line may print a secret value: {line!r}"


# --------------------------------------------------------------------------
# (2) Protected credentials are unwritable, as target and as copy source.
# --------------------------------------------------------------------------

PROTECTED_SAMPLES = [
    "SECRETS_ADMIN_PAT",  # the workflow's own credential — lockout / identity swap
    "KUBE_CONFIG",  # rewriting it breaks cluster-query
    "FUZEINFRA_DISPATCH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "FUZE_AGENT_APP_PRIVATE_KEY",
]


@pytest.mark.parametrize("name", PROTECTED_SAMPLES)
def test_protected_names_cannot_be_written(tmp_path, name):
    blocked(tmp_path, SECRET_NAME=name, SOURCE="generate")


@pytest.mark.parametrize("name", PROTECTED_SAMPLES)
def test_protected_names_cannot_be_the_copy_source(tmp_path, name):
    """A copy prints nothing, but it still re-keys a credential to a new name."""
    blocked(tmp_path, SECRET_NAME="HARMLESS_TARGET", SOURCE="copy", COPY_FROM=name)


def test_admin_pat_is_protected_in_the_workflow_text():
    """The credential named in the Provision step must appear in PROTECTED.

    If the PAT secret is ever renamed, renaming it in `Provision` alone would
    quietly make it writable by its own workflow.
    """
    provision = _step(PROVISION_STEP)
    token_expr = provision["env"]["GH_TOKEN"]
    pat_name = token_expr.split("secrets.")[1].split("}")[0].strip()
    assert pat_name in _step(VALIDATE_STEP)["run"], (
        f"{pat_name} is the workflow's own credential but is not in PROTECTED"
    )


# --------------------------------------------------------------------------
# Name validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "",  # required
        "   ",  # whitespace-only
        "lowercase_name",
        "Mixed_Case",
        "1LEADING_DIGIT",
        "_LEADING_UNDERSCORE",
        "HAS-DASH",
        "HAS SPACE",
        "HAS$METACHAR",
        "AB",  # shorter than 3
        "A" * 65,  # longer than 64
        "GITHUB_TOKEN",  # reserved prefix
        "GITHUB_ANYTHING",
    ],
)
def test_invalid_names_blocked(tmp_path, name):
    blocked(tmp_path, SECRET_NAME=name, SOURCE="generate")


@pytest.mark.parametrize("name", ["TWILIO_AUTH_TOKEN_REF", "ABC", "A_1", "WEBHOOK_SHARED_SECRET"])
def test_valid_names_allowed(tmp_path, name):
    ok(tmp_path, SECRET_NAME=name, SOURCE="verify")


# --------------------------------------------------------------------------
# source / copy_from / length
# --------------------------------------------------------------------------


@pytest.mark.parametrize("src", ["", "delete", "read", "GENERATE", "generate; rm -rf /"])
def test_invalid_source_blocked(tmp_path, src):
    blocked(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE=src)


def test_copy_requires_a_source(tmp_path):
    blocked(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="copy", COPY_FROM="")


def test_copy_from_must_be_a_valid_name(tmp_path):
    blocked(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="copy", COPY_FROM="not-a-name")


def test_copy_to_itself_blocked(tmp_path):
    """A self-copy is always a mistake, and would rewrite a secret with itself."""
    blocked(tmp_path, SECRET_NAME="SAME_NAME", SOURCE="copy", COPY_FROM="SAME_NAME")


def test_copy_happy_path(tmp_path):
    ok(tmp_path, SECRET_NAME="NEW_NAME", SOURCE="copy", COPY_FROM="OLD_NAME")


@pytest.mark.parametrize("length", ["0", "15", "129", "abc", "48x", "-20"])
def test_invalid_length_blocked(tmp_path, length):
    blocked(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="generate", LENGTH=length)


def test_omitted_length_falls_back_to_the_default(tmp_path):
    """A repository_dispatch caller may omit length; empty means "use 48"."""
    ok(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="generate", LENGTH="")


@pytest.mark.parametrize("length", ["16", "48", "128"])
def test_valid_length_allowed(tmp_path, length):
    ok(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="generate", LENGTH=length)


def test_length_is_not_checked_for_non_generate(tmp_path):
    """verify/copy ignore length — a stale default must not block them."""
    ok(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="verify", LENGTH="not-a-number")


# --------------------------------------------------------------------------
# Both event shapes reach the same filter
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "var,payload_key",
    [
        ("SECRET_NAME", "secret_name"),
        ("SOURCE", "source"),
        ("COPY_FROM", "copy_from"),
        ("LENGTH", "length"),
    ],
)
def test_both_event_shapes_feed_the_filter(var, payload_key):
    """Drop either half of a fallback and that event runs with an empty arg.

    For repository_dispatch that is a silent no-op shaped like success; the same
    pin exists in tests/test_cluster_query_guard.py for the same reason.
    """
    expr = _step(VALIDATE_STEP)["env"][var]
    assert f"inputs.{payload_key}" in expr, f"{var} ignores workflow_dispatch"
    assert f"client_payload.{payload_key}" in expr, f"{var} ignores repository_dispatch"


def test_every_step_reads_both_event_shapes():
    """Generalises the pin above to EVERY step, not just the filter.

    The narrow version of this test checked the Validate step alone and therefore
    missed `COPY_VALUE` in the Provision step, which keyed off
    `github.event.inputs.copy_from` only. `github.event.inputs` is null on
    repository_dispatch, so that expression was permanently empty there and
    source=copy failed with a "resolved empty ... does not exist" error blaming the
    wrong thing. Any step reading a dispatch field has the same trap, so scan them
    all: wherever an expression reads `inputs.<key>`, it must also read
    `client_payload.<key>`.
    """
    pattern = re.compile(r"github\.event\.inputs\.([A-Za-z0-9_]+)")
    for step in _workflow()["jobs"]["provision"]["steps"]:
        for var, expr in (step.get("env") or {}).items():
            for key in set(pattern.findall(str(expr))):
                assert f"client_payload.{key}" in str(expr), (
                    f"{step.get('name')}/{var} reads github.event.inputs.{key} but never "
                    f"github.event.client_payload.{key} — it is empty on repository_dispatch"
                )


def test_repository_dispatch_type_is_registered():
    types = _on()["repository_dispatch"]["types"]
    assert "secret-provision" in types


def test_workflow_token_permissions_are_minimal():
    """The job needs no write scope of its own — the PAT carries the privilege."""
    assert _workflow()["permissions"] == {"contents": "read"}


# --------------------------------------------------------------------------
# Containment of the admin PAT
#
# SECRETS_ADMIN_PAT is the most powerful credential in this repo: it writes
# Actions secrets. secret-provision.yml is safe with it by construction — it
# pins `gh secret set --repo "$REPO"` to github.repository, so it can only ever
# touch THIS repo. Nothing structural stopped a DIFFERENT workflow from
# referencing the same secret and using it anywhere the token's own scope
# reaches, which is the whole repo set if the PAT was minted with "All
# repositories" access.
#
# That is not a hypothetical in a repo where agents author workflow files. Fork
# PRs never receive secrets, so the realistic path is a commit to a workflow on
# this repo — precisely what CI can refuse. Scope the PAT down as well (the
# token's own repository-access setting is the primary control); this is the
# second layer, and the one that fails loudly in review.
# --------------------------------------------------------------------------

ADMIN_PAT = "SECRETS_ADMIN_PAT"


def test_admin_pat_is_referenced_by_no_other_workflow():
    """Only secret-provision.yml may reference the secret-writing PAT.

    A second consumer would silently widen where that credential can be used,
    and would not show up in this file's other tests — they all inspect
    secret-provision.yml alone.
    """
    offenders = []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        if path.name == WORKFLOW.name:
            continue
        if ADMIN_PAT in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, (
        f"{ADMIN_PAT} is referenced outside {WORKFLOW.name}: {offenders}. That credential "
        f"writes Actions secrets; keep it to the one workflow whose guard is tested here, "
        f"and scope the PAT itself to this repository only."
    )


def test_provision_takes_its_target_from_the_validated_env_not_the_payload():
    """`REPO` must be the Validate step's allowlist-checked `TARGET`.

    This replaces an earlier pin to `github.repository`, which `target_repo`
    deliberately relaxes. The property that survives is the one that mattered: the
    repository written to is the one the guard approved. Re-reading
    `github.event.*.target_repo` here would route around the allowlist entirely —
    the check would pass on a listed value while the write used whatever the caller
    sent second.
    """
    provision = _step(PROVISION_STEP)
    repo_expr = provision["env"]["REPO"]
    assert "env.TARGET" in repo_expr, (
        f"REPO must come from the validated env.TARGET, got {repo_expr!r}"
    )
    assert "github.event" not in repo_expr, (
        f"REPO reads the dispatch payload directly ({repo_expr!r}), bypassing the allowlist"
    )
    for raw in provision["run"].splitlines():
        line = raw.strip()
        if line.startswith("gh secret "):
            assert '--repo "$REPO"' in line, f"unpinned gh secret call: {line!r}"


def test_target_is_allowlist_checked_before_it_is_exported(tmp_path):
    """The guard must consult the allowlist file, not a list inlined in the workflow."""
    script = _step(VALIDATE_STEP)["run"]
    assert ALLOWLIST_PATH in script, (
        f"the Validate step never reads {ALLOWLIST_PATH}; an inlined list of target repos "
        f"is invisible to anyone auditing who can write where"
    )
    # Exported only after the check: an early `TARGET=` export would be usable by the
    # Provision step even on a rejected repo, since a failed guard still wrote it.
    assert script.index(ALLOWLIST_PATH) < script.index('echo "TARGET=$TARGET"'), (
        "TARGET is exported to GITHUB_ENV before the allowlist check runs"
    )


def test_allowlist_file_is_well_formed():
    data = json.loads((ROOT / ALLOWLIST_PATH).read_text(encoding="utf-8"))
    targets = data["targets"]
    assert targets, "an empty allowlist would make every dispatch fail"
    slugs = [e["repo"] for e in targets]
    assert "FuzeOne/FuzeInfra" in slugs, (
        "this repository must stay listed — it is the default when target_repo is omitted"
    )
    assert len(slugs) == len({s.lower() for s in slugs}), f"duplicate entries: {slugs}"
    for entry in targets:
        assert entry["repo"].count("/") == 1, f"{entry['repo']} is not owner/repo"
        assert entry.get("why", "").strip(), (
            f"{entry['repo']} has no `why`. An entry nobody can justify in one sentence "
            f"is an entry to remove — it grants secret writes to that repository."
        )


# --- the live guard, exercised ------------------------------------------------


def test_target_defaults_to_this_repository(tmp_path):
    r = run_guard(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="verify", TARGET_REPO="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FuzeOne/FuzeInfra" in r.stdout


@pytest.mark.parametrize(
    "target",
    [
        "izzywdev/NotListed",
        "someoneelse/FuzeInfra",
        "izzywdev",  # not owner/repo
        "izzywdev/a/b",  # too many segments
        "/FuzeInfra",
        "izzywdev/",
        "izzywdev/Fuze Infra",  # space
        "izzywdev/Fuze;rm -rf /",  # metachars
    ],
)
def test_bad_or_unlisted_targets_blocked(tmp_path, target):
    blocked(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="verify", TARGET_REPO=target)


def test_listed_target_allowed_case_insensitively(tmp_path):
    """GitHub slugs are case-insensitive, so an allowlist that a lowercase spelling
    slips past would be a bypass rather than a nicety."""
    ok(tmp_path, SECRET_NAME="SOME_SECRET", SOURCE="verify", TARGET_REPO="fuzeone/fuzeinfra")


# An allowlist that DOES list a foreign repo, so the copy rule is tested on its own
# terms rather than being masked by an allowlist rejection.
TWO_TARGETS = [
    {"repo": "FuzeOne/FuzeInfra", "why": "self"},
    {"repo": "FuzeOne/FuzeFront", "why": "test fixture: a genuinely allowlisted peer"},
]


def test_copy_is_refused_cross_repo_even_when_the_target_is_allowlisted(tmp_path):
    """A cross-repo copy moves THIS repository's secret values into another repo.

    PROTECTED stops the worst names, but the operation itself is exfiltration wearing a
    re-key's clothes, so it is refused off-repo whatever the allowlist says. The target
    here IS listed, so an allowlist rejection cannot be what makes this pass.
    """
    blocked(tmp_path, allowlist=TWO_TARGETS, SECRET_NAME="NEW_NAME", SOURCE="copy",
            COPY_FROM="OLD_NAME", TARGET_REPO="FuzeOne/FuzeFront")


def test_generate_is_allowed_to_that_same_allowlisted_peer(tmp_path):
    """Pins that the refusal above is the COPY rule, not the target being foreign."""
    ok(tmp_path, allowlist=TWO_TARGETS, SECRET_NAME="SOME_SECRET", SOURCE="generate",
       TARGET_REPO="FuzeOne/FuzeFront")


def test_verify_is_allowed_to_that_same_allowlisted_peer(tmp_path):
    ok(tmp_path, allowlist=TWO_TARGETS, SECRET_NAME="SOME_SECRET", SOURCE="verify",
       TARGET_REPO="FuzeOne/FuzeFront")


def test_copy_still_allowed_same_repo(tmp_path):
    ok(tmp_path, SECRET_NAME="NEW_NAME", SOURCE="copy", COPY_FROM="OLD_NAME",
       TARGET_REPO="FuzeOne/FuzeInfra")


def test_protected_names_blocked_for_every_target(tmp_path):
    """The PROTECTED list is not weakened by pointing somewhere else."""
    blocked(tmp_path, SECRET_NAME="SECRETS_ADMIN_PAT", SOURCE="generate",
            TARGET_REPO="FuzeOne/FuzeInfra")
