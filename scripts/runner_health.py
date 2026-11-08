"""Detect ARC runners that are RUNNING but not actually serving jobs.

WHY THIS EXISTS. On 2026-09-23 the whole fleet's CI stopped (FuzeInfra#1187) and every
signal an operator would normally look at said the cluster was fine:

    $ kubectl -n arc-runners get pods
    fuzeplan-bhn6d-runner-976qb   2/2   Running   0   64m

Eighteen of nineteen runner pods looked exactly like that -- Ready, no restarts, no
events -- while GitHub reported every one of them `offline` and ~275 jobs queued. The
pods could not resolve DNS, so they never completed registration; `2/2 Running` only
means the containers started, never that the runner is reachable by GitHub.

That gap is the whole point of this module. `Running` is a local fact, `online` is a
remote one, and the outage lived precisely in the space between them. Nothing watched
that space, which is why this went unnoticed until someone chased a failing PR into it.

TWO DESIGN CHOICES WORTH KEEPING.

1. The scale-set -> repo mapping is read from the CLUSTER, from each
   AutoscalingRunnerSet's own `spec.githubConfigUrl`, never from a hardcoded fleet
   list. A hardcoded list silently stops covering a repo the day someone adds one, and
   a monitor with a blind spot is worse than no monitor because it reports green.

2. Findings name the NODES the offline pods sit on. In #1187 the diagnosis turned
   entirely on noticing that every offline runner was on `fuzeinfra-ci-runner-1/-2`
   while the one online runner was on an elastic node. That correlation took a while
   to spot by hand; it costs nothing to print it.

This module is deliberately pure: it takes already-fetched JSON and returns findings, so
the interesting logic is testable offline with no cluster and no network.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

# A runner pod that has only just been created has not had time to register, and
# flagging it would make this monitor cry wolf during every scale-up. ARC runners
# normally come online within a few seconds; minutes is a generous margin.
DEFAULT_MIN_AGE_SECONDS = 300

CRITICAL = "critical"
WARNING = "warning"

_CONFIG_URL = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?$")


@dataclass(frozen=True)
class Finding:
    level: str
    scale_set: str
    repo: str
    message: str
    nodes: tuple = field(default=())

    def render(self) -> str:
        where = f" nodes={','.join(self.nodes)}" if self.nodes else ""
        return f"[{self.level.upper()}] {self.repo} set={self.scale_set}: {self.message}{where}"


def repo_from_config_url(url):
    """'https://github.com/FuzeOne/FuzePlan' -> 'FuzeOne/FuzePlan'.

    Returns None for an org-level or unparseable URL rather than guessing: a wrong
    repo here would query the wrong runner list and produce a confident false result.
    """
    if not url:
        return None
    match = _CONFIG_URL.search(url.strip())
    if not match:
        return None
    repo = match.group("repo")
    # An org-level config URL has no repo segment; `github.com/izzywdev` parses as a
    # repo named 'izzywdev' with no owner, which the regex above cannot produce, but
    # be explicit about empty segments anyway.
    if not repo or not match.group("owner"):
        return None
    return f"{match.group('owner')}/{repo}"


def parse_scale_sets(payload):
    """AutoscalingRunnerSet list JSON -> {scale_set_name: repo}."""
    out = {}
    for item in (payload or {}).get("items", []):
        name = item.get("metadata", {}).get("name")
        repo = repo_from_config_url(item.get("spec", {}).get("githubConfigUrl"))
        if name and repo:
            out[name] = repo
    return out


def _age_seconds(timestamp, now):
    if not timestamp:
        return 0
    parsed = _dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return max(0.0, (now - parsed).total_seconds())


def parse_runner_pods(payload, now, min_age_seconds=DEFAULT_MIN_AGE_SECONDS):
    """Pod list JSON -> [{name, node, scale_set, settled}].

    `settled` means the pod is Running and old enough that it should have registered.
    """
    pods = []
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name")
        if not name:
            continue
        phase = item.get("status", {}).get("phase")
        age = _age_seconds(meta.get("creationTimestamp"), now)
        pods.append(
            {
                "name": name,
                "node": item.get("spec", {}).get("nodeName") or "<unscheduled>",
                "phase": phase,
                "age_seconds": age,
                "settled": phase == "Running" and age >= min_age_seconds,
            }
        )
    return pods


def pods_for_scale_set(pods, scale_set):
    """ARC names ephemeral runner pods '<scale-set>-<listener-hash>-runner-<id>'.

    Matching on the '<scale-set>-' prefix alone would let 'fuze-runner' swallow a set
    named 'fuze-runner-extra', so anchor on the '-runner-' segment that ARC always
    inserts.
    """
    return [p for p in pods if re.match(rf"^{re.escape(scale_set)}-[^-]+-runner-", p["name"])]


def online_runner_names(runners_payload, scale_set):
    """GitHub runner list -> set of names for this scale set that are ONLINE."""
    names = set()
    for runner in (runners_payload or {}).get("runners", []):
        name = runner.get("name", "")
        if re.match(rf"^{re.escape(scale_set)}-[^-]+-runner-", name):
            if runner.get("status") == "online":
                names.add(name)
    return names


def parse_queued_runs(payload, labels_hint=None):
    """GitHub workflow-run list -> names of runs that are QUEUED.

    `queued` here means GitHub has a run it cannot place. We deliberately do NOT
    try to match a run to a specific scale set by label: the runs API does not
    report the `runs-on` labels of a pending job without a second call per run,
    and the failure this feeds is pool-wide anyway -- when a repo's self-hosted
    pool is empty, everything targeting it stalls together. Over-reporting a
    queued run against a sibling set in the same repo is the safe direction: it
    raises an alarm a human resolves in seconds, whereas under-reporting is the
    silence that hid an 18-hour outage.
    """
    runs = (payload or {}).get("workflow_runs") or []
    return [
        f"{r.get('name') or r.get('display_title') or r.get('id')}"
        for r in runs
        if r.get("status") == "queued"
    ]


def evaluate(scale_sets, pods, runners_by_repo, min_age_seconds=DEFAULT_MIN_AGE_SECONDS,
             queued_by_repo=None):
    """Compare local pod state against GitHub's view. Returns findings, worst first.

    The load-bearing case is CRITICAL: pods that are Running and settled while GitHub
    sees nothing online for that set. That is the #1187 signature -- capacity that
    exists, costs money, looks healthy, and serves no job.
    """
    findings = []
    for scale_set, repo in sorted(scale_sets.items()):
        set_pods = pods_for_scale_set(pods, scale_set)
        settled = [p for p in set_pods if p["settled"]]
        online = online_runner_names(runners_by_repo.get(repo), scale_set)

        if not settled:
            # Zero runners is the normal idle state for an ephemeral scale set --
            # but ONLY while nothing is waiting for it. An empty pool with jobs
            # queued against it is starvation, and it is the failure this check
            # used to miss completely.
            #
            # 2026-09-27: the `staging` pool sat at zero pods for ~18h with 30+
            # jobs queued, and this function reported "no stranded runners" the
            # whole time, because it returned here before looking at anything.
            # The old check only ever asked the inverse question -- pods running
            # that GitHub does not see -- so zero pods AND zero registrations
            # read as perfectly healthy. A gate that cannot see the outage it is
            # meant to catch is worse than no gate, because it is believed.
            queued = queued_by_repo.get(repo) if queued_by_repo else None
            if queued:
                findings.append(
                    Finding(
                        CRITICAL,
                        scale_set,
                        repo,
                        f"0 runner pod(s) and {len(online)} online runner(s), but "
                        f"{len(queued)} queued workflow run(s) are waiting -- the pool "
                        f"is starved and those jobs will never start",
                        tuple(sorted({q for q in queued}))[:5],
                    )
                )
            continue

        nodes = tuple(sorted({p["node"] for p in settled}))

        # Count the stranded PODS directly rather than deriving a count from
        # len(settled) - len(online). GitHub's runner list is not a subset of the
        # settled pods: it can carry a runner whose pod is still too young to be
        # settled, or a stale registration whose pod is already gone. Subtracting
        # the two lengths then reports a number that disagrees with the node list
        # printed beside it -- observed live as "1 of 3 ... not online" followed by
        # three node names, which makes the operator distrust the whole check.
        stranded_pods = [p for p in settled if p["name"] not in online]

        if not online:
            findings.append(
                Finding(
                    CRITICAL,
                    scale_set,
                    repo,
                    f"{len(settled)} runner pod(s) Running for >{min_age_seconds // 60}m "
                    f"but GitHub reports 0 online for this set -- jobs will queue forever",
                    nodes,
                )
            )
        elif stranded_pods:
            findings.append(
                Finding(
                    WARNING,
                    scale_set,
                    repo,
                    f"{len(stranded_pods)} of {len(settled)} settled runner pod(s) "
                    f"are Running but not online",
                    tuple(sorted({p["node"] for p in stranded_pods})),
                )
            )

    findings.sort(key=lambda f: (f.level != CRITICAL, f.repo, f.scale_set))
    return findings


def dns_endpoint_findings(endpoints_payload, minimum_nodes=2):
    """kube-dns must have Ready endpoints on more than one node.

    Single-homed cluster DNS is what turned one node's network fault into a fleet-wide
    outage in #1187. This is the regression guard for the coredns-ha manifest.
    """
    nodes = set()
    ready = 0
    for subset in (endpoints_payload or {}).get("subsets", []) or []:
        for address in subset.get("addresses", []) or []:
            ready += 1
            node = address.get("nodeName")
            if node:
                nodes.add(node)

    if ready == 0:
        return [Finding(CRITICAL, "kube-dns", "cluster", "kube-dns has NO ready endpoints")]
    if len(nodes) < minimum_nodes:
        return [
            Finding(
                WARNING,
                "kube-dns",
                "cluster",
                f"cluster DNS is single-homed: {ready} ready endpoint(s) on "
                f"{len(nodes)} node(s); one node's network fault takes out all DNS",
                tuple(sorted(nodes)),
            )
        ]
    return []


# --------------------------------------------------------------------------- CLI
#
# Everything above is pure so it can be tested offline. Everything below is the thin
# I/O shell: fetch, evaluate, print, choose an exit code.


def _run_json(argv):
    import json
    import subprocess

    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])}... failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout or "{}")


def main(argv=None):
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", default="arc-runners")
    parser.add_argument("--dns-namespace", default="kube-system")
    parser.add_argument("--min-age-seconds", type=int, default=DEFAULT_MIN_AGE_SECONDS)
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="report findings but always exit 0 (for a soak period before enforcing)",
    )
    args = parser.parse_args(argv)

    now = _dt.datetime.now(_dt.timezone.utc)

    scale_sets = parse_scale_sets(
        _run_json(["kubectl", "-n", args.namespace, "get", "autoscalingrunnersets", "-o", "json"])
    )
    pods = parse_runner_pods(
        _run_json(["kubectl", "-n", args.namespace, "get", "pods", "-o", "json"]),
        now,
        args.min_age_seconds,
    )

    runners_by_repo = {}
    for repo in sorted(set(scale_sets.values())):
        try:
            runners_by_repo[repo] = _run_json(
                ["gh", "api", f"repos/{repo}/actions/runners", "--paginate"]
            )
        except RuntimeError as exc:
            # A repo we cannot read must not silently count as healthy.
            print(f"::warning::could not read runners for {repo}: {exc}")
            runners_by_repo[repo] = None

    # Queued demand per repo. Only needed to tell an idle pool (fine) from a
    # starved one (not fine), so a failure here degrades to "cannot tell" rather
    # than to a false all-clear: an unreadable repo contributes no queued runs and
    # the zero-pod case stays silent, exactly as it did before this check existed.
    queued_by_repo = {}
    for repo in sorted(set(scale_sets.values())):
        try:
            queued_by_repo[repo] = parse_queued_runs(
                _run_json(
                    ["gh", "api", f"repos/{repo}/actions/runs?status=queued&per_page=100"]
                )
            )
        except RuntimeError as exc:
            print(f"::warning::could not read queued runs for {repo}: {exc}")
            queued_by_repo[repo] = None

    findings = evaluate(
        scale_sets, pods, runners_by_repo, args.min_age_seconds, queued_by_repo
    )

    try:
        findings += dns_endpoint_findings(
            _run_json(["kubectl", "-n", args.dns_namespace, "get", "endpoints", "kube-dns", "-o", "json"])
        )
    except RuntimeError as exc:
        print(f"::warning::could not read kube-dns endpoints: {exc}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [f.render() for f in findings]
    critical = [f for f in findings if f.level == CRITICAL]

    if not findings:
        print(f"OK: {len(scale_sets)} scale set(s) checked, no stranded runners.")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(
                    f"### Runner health: OK\n\n{len(scale_sets)} scale set(s) checked; "
                    "every settled runner pod is online and cluster DNS is multi-homed.\n"
                )
        return 0

    for line in lines:
        print(line)
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### Runner health: findings\n\n")
            handle.write(
                "A runner pod can be `2/2 Running` and still be invisible to GitHub; "
                "that gap is what this check watches.\n\n```text\n"
                + "\n".join(lines)
                + "\n```\n"
            )

    for finding in critical:
        print(f"::error title=runner-health::{finding.render()}")

    if args.warn_only:
        return 0
    return 1 if critical else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
