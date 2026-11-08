#!/usr/bin/env python3
"""Authorize (or refuse) a dispatched arc-register request.

Used by .github/workflows/arc-register.yml to decide whether a consumer's
`repository_dispatch` (or an operator's `workflow_dispatch`) may install/
uninstall an ARC AutoscalingRunnerSet for a given repo.

WHY THIS DOES NOT JUST TRUST client_payload.repo:
repository_dispatch carries no field GitHub itself vouches for that names the
*sending* repo. The identity GitHub does attach (`github.actor` / the PAT
owner) is the human who minted the dispatch token, and every consumer repo's
FUZEINFRA_DISPATCH_TOKEN is minted by the same account — so that identity is
identical no matter which repo actually dispatched. The claimed `repo` string
in the payload is therefore just a claim, not a proof.

The mitigation: `repo` must be an EXACT key in config/arc-register-allowlist.json,
and once matched, every value actually used to register the runner (the GitHub
repo URL, the scale-set name) is read from THAT config entry -- never taken
verbatim from the payload. A forged or mistyped `repo` can, at worst, cause the
handler to act on a DIFFERENT already-allowlisted repo (a mapping visible and
diffable in this file's git history); it can never point a `helm upgrade
--install` at an arbitrary, non-allowlisted target. This is the same shape as
config/infra-request-whitelist.json + validate_infra_request.py, applied here
because arc-register's blast radius (which repo gets write access into the
shared arc-runners namespace) is exactly the kind of thing that whitelist
pattern exists to bound.

Two possible decisions:
  * "authorized" -> repo is an allowlist key AND action is install|uninstall.
  * "denied"      -> anything else. Fails closed: an unlisted repo, a missing
                     repo, or an unrecognized action is always denied.

Usage:
    validate_arc_register.py --allowlist config/arc-register-allowlist.json \
        --repo FuzeOne/FuzeHub --action install [--github-output "$GITHUB_OUTPUT"]

Exit code is always 0 (the decision is conveyed via output, not process
failure) so the calling workflow step can branch on `decision` rather than
parse a crash. The workflow's own next step is responsible for turning
`decision=denied` into a failed job.

Writes to stdout and (if given) --github-output:
  decision=authorized|denied
  repo=<canonical allowlist key, only set when authorized>
  repo_url=https://github.com/<canonical allowlist key>
  scale_set_name=<from the allowlist entry, only set when authorized>
  action=install|uninstall
  reasons=<human-readable explanation>
"""
import argparse
import json
import sys


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate(allowlist, claimed_repo, claimed_action):
    """Return a dict with decision + derived fields. Never trusts claimed_repo
    for anything beyond a lookup key, and never trusts claimed_action beyond
    membership in allowed_actions."""
    allowed_repos = allowlist.get("allowed_repos", {})
    allowed_actions = allowlist.get("allowed_actions", ["install", "uninstall"])

    reasons = []

    # Blank/unset action is the workflow's `||` fallback sentinel for "nothing
    # supplied" (neither client_payload.action nor inputs.action set) — that is
    # the ONE normalization performed here, and it is an explicit default, not
    # fuzzy matching. Everything else is compared EXACTLY: no case-folding, no
    # whitespace-trimming, on either repo or action. A claim that differs from
    # an allowlist key by so much as trailing whitespace or casing is treated
    # as a non-match rather than "close enough" — the whole point of this gate
    # is that only an exact, auditable string decides the outcome.
    action = "install" if not claimed_action else claimed_action
    if action not in allowed_actions:
        reasons.append(f"action '{action}' not in allowed_actions {allowed_actions}")

    repo = claimed_repo or ""
    entry = allowed_repos.get(repo)
    if entry is None:
        reasons.append(
            f"repo '{repo}' is not an exact key in allowed_repos "
            f"({sorted(allowed_repos.keys())}) — refusing to derive a registration "
            "target from an unlisted claim"
        )

    if reasons:
        return {
            "decision": "denied",
            "repo": "",
            "repo_url": "",
            "scale_set_name": "",
            "action": action,
            "reasons": "; ".join(reasons),
        }

    scale_set_name = entry.get("scale_set_name")
    if not scale_set_name:
        return {
            "decision": "denied",
            "repo": "",
            "repo_url": "",
            "scale_set_name": "",
            "action": action,
            "reasons": f"allowlist entry for '{repo}' has no scale_set_name configured",
        }

    return {
        "decision": "authorized",
        # Echo back the CANONICAL key (from the config), not the raw claim.
        "repo": repo,
        "repo_url": f"https://github.com/{repo}",
        "scale_set_name": scale_set_name,
        "action": action,
        "reasons": "repo is an allowlisted key; action recognized",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--repo", required=True, help="claimed owner/name of the requesting repo")
    parser.add_argument("--action", default="install", help="install or uninstall")
    parser.add_argument("--github-output", default=None, help="path to $GITHUB_OUTPUT to append to")
    args = parser.parse_args(argv)

    allowlist = load_json(args.allowlist)
    result = validate(allowlist, args.repo, args.action)

    for key in ("decision", "repo", "repo_url", "scale_set_name", "action", "reasons"):
        print(f"{key}={result[key]}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            for key in ("decision", "repo", "repo_url", "scale_set_name", "action", "reasons"):
                fh.write(f"{key}={result[key]}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
