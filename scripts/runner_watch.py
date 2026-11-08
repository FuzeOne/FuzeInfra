#!/usr/bin/env python3
"""runner_watch — decide WHICH runner each fleet repo's jobs should use, PRECOMPUTE that
decision on a cron, publish it as a repo variable, and recover the runs that a wrong
decision already stranded.

===========================================================================================
WHY THIS EXISTS AT ALL  (inherited verbatim in substance from the closed PR FuzeSDLC#249,
whose reasoning about WHAT to decide is unchanged here — only WHERE the decision runs)
===========================================================================================

`runs-on` is fixed at job dispatch. GitHub has no "this has queued too long, try somewhere
else" — a job pointed at a scale set that is down does not fail, it QUEUES FOREVER, and a
required context that never reports blocks merges with no red to look at
(governance/required-checks.json, invariant C3; governance/ci-runners.md, "the hazard that
makes the default matter").

The only place that decision can be made is BEFORE dispatch.

===========================================================================================
WHY THE SHAPE CHANGED FROM #249
===========================================================================================

#249 made that decision as a REUSABLE WORKFLOW, consumed by every workflow via
`uses:` + `needs:`. That works, and its decision logic was right, but the delivery shape
had two defects that no amount of care inside the workflow could fix:

  (a) It adds a JOB to every workflow run in the fleet. A chooser is overhead paid on
      every push, every PR, every re-run, forever — to answer a question whose answer
      changes maybe once a week.

  (b) It CANNOT RESOLVE for a public consumer of a private hub. A private repo's reusable
      workflow is not readable from a public repo's workflow run (FuzeInfra#758), so the
      five public repos in this fleet — FuzeFront, FuzeAgent, FuzeKeys, FuzeInfra, FuzeX —
      could never have consumed it. FuzeSDLC#261 INLINED two reusable workflows for exactly
      this reason; adding a third reusable workflow would have walked straight back into it.

Both defects are properties of "resolve the answer at dispatch time". Both disappear if the
answer is PRECOMPUTED and left somewhere every workflow can already read with no resolution
step at all — a repo variable:

    runs-on: ${{ vars.CI_RUNNER_LABELS || 'ubuntu-latest' }}

Zero extra jobs, no cross-repo workflow resolution, and a literal hosted default that keeps
governance/required-checks.md §2.2's "can never queue forever" property (see §2.2's
"precomputed variable" paragraph, and `runner_finding()` in scripts/gate_required_checks.py,
which already treats a hosted-defaulted expression as fit — invariant C3 is NOT weakened by
this pattern, it is satisfied by it).

===========================================================================================
WHAT PRECOMPUTING COSTS, AND WHAT THIS MODULE OWES BECAUSE OF IT
===========================================================================================

#249 could write, in its own comments, "there is no persisted state and no re-decision
inside a run ... that is what makes an oscillation impossible — there is no feedback loop
to oscillate." That guarantee was a FREE consequence of deciding per-run and writing
nothing.

This module writes. So the guarantee is no longer free, and buying it back is this module's
responsibility, discharged in three places:

  1. HYSTERESIS (`hysteresis()`): a newly-computed value is written only when it differs
     from the current one AND the SAME new value has been observed on N consecutive runs
     (default 2). A pool that flaps online/offline between two cron ticks therefore moves
     nobody. Without this, one transient blip rewrites the runner of every repo in the
     fleet, and the next tick rewrites them all back.

  2. FAIL-SAFE ON AN UNVERIFIABLE PROBE (`decide_runner()`, `online_runners is None`):
     when liveness cannot be established the decision is `None` — meaning KEEP THE CURRENT
     VALUE and warn loudly. This is where the precomputed form legitimately DIVERGES from
     #249's per-run form, and the divergence is deliberate:
       * #249 chose `ubuntu-latest` on a failed probe, because its output was consumed by
         exactly one run. Choosing hosted for one run is cheap and safe.
       * Here the output PERSISTS. Writing `ubuntu-latest` on a transient API blip would
         migrate the entire fleet off its runners on the strength of a 500, and would then
         need two more clean ticks to migrate back. "Do not invent an answer" is therefore
         the same fail-safe VALUE (never assume a pool is up) expressed correctly for a
         persistent sink: change nothing, and say so.

  3. RERUN CAP (`stranded_runs()` + the `recovered` state): a run is recovered AT MOST
     ONCE, ever. Without that cap, a run that is broken for a reason recovery cannot fix —
     a genuinely bad workflow file, a missing secret — gets cancelled, re-dispatched,
     queues again, and is cancelled again on the next tick, forever.

===========================================================================================
JOB B: WHY CANCEL+RERUN IS THE ONLY RECOVERY THAT EXISTS
===========================================================================================

`runs-on` is IMMUTABLE after dispatch, and there is no API to move a queued job to another
runner. Fixing the VARIABLE therefore does nothing for the runs already stranded behind the
old value — they keep queueing against a pool that is down, which is precisely the state
(a required check stuck at "Expected — waiting for status to be reported") that this whole
line of work exists to end.

So recovery is `gh run cancel` followed by `gh run rerun`: the re-dispatch reads
`vars.CI_RUNNER_LABELS` afresh and lands on the runner the watcher just chose.

That is a destructive operation, so its selector is written to be conservative in every
direction it can be:

  * ONLY jobs whose status is `queued` with NO runner assigned. A job with a runner is
    being served; it is slow, not stranded.
  * NEVER a run that has ANY job `in_progress`. Cancellation is per-RUN, not per-job, so
    cancelling a run to rescue its queued job would discard the real work its sibling job
    is doing. The selector therefore excludes the whole run — a strictly stronger guarantee
    than "do not select an in_progress job", and the honest one given the API's granularity.
  * only after a threshold (default 20 minutes) of queueing, so an ordinary busy-pool wait
    is never mistaken for a stranding.
  * at most once per run id, ever (see 3 above).
  * every action logged with the run URL and the reason, so the sweep is auditable after
    the fact rather than only while it is happening.

===========================================================================================
WHERE THIS RUNS  (the instance moved to FuzeInfra; the reasoning above did not change)
===========================================================================================

This file was authored in FuzeSDLC (PR #269) and the RUNNABLE INSTANCE was moved to
FuzeInfra before it ever ran on a schedule. One fact forced it:

    FuzeSDLC is PRIVATE, so its GitHub-hosted minutes are METERED. A hosted job in a
    private repo whose account budget is gone does not fail, it NEVER STARTS:
    `runner_id: 0`, zero steps, no log, "The job was not started because an Actions
    budget is preventing further use".

Every job of this watcher is hardcoded `ubuntu-latest` on purpose (a chooser that runs on
the pool it evaluates cannot report that pool down), so the watcher is hosted-only by
design — and BUDGET EXHAUSTION IS ONE OF THE TWO CONDITIONS IT EXISTS TO DETECT. A
hosted-only watcher in a metered repo therefore has an availability dependency CORRELATED
WITH THE FAILURE IT DETECTS: the budget running out both raises the alarm and silences it.
FuzeInfra is PUBLIC, where Actions is free and unmetered, so the same hosted-only design
has no billing dependency at all; it is also the infra repo, which makes fleet runner
health its natural domain.

    gh api repos/FuzeOne/FuzeSDLC --jq .private   => true
    gh api repos/FuzeOne/FuzeInfra --jq .private   => false

MEASURED, NOT ASSUMED (2026-09-01). The move was prompted by a report that FuzeSDLC's
budget was exhausted and its hosted jobs were failing instantly. That symptom is NOT
present as of the port — FuzeSDLC's `ubuntu-latest` jobs currently succeed with real
runner ids and full step lists. The structural argument does not depend on it (metered vs
free is a property of the repo, not of this week's balance), so the move stands; the
framing is corrected here rather than repeated. The OTHER detected condition, meanwhile,
IS live in the authoring repo right now: PR #269's own CI has 15+ jobs `queued` 32-38
minutes with `runner_id: 0` against `fuze-runner` — precisely what `stranded_runs()`
selects, and precisely the never-reporting required check this design exists to end.

Three consequences, all of them mechanical rather than design changes:

  1. THE HUB IS FuzeInfra. `--hub` defaults to `FuzeOne/FuzeInfra`, and the two durable
     state variables (STATE_VAR_DECIDE / STATE_VAR_RECOVER) live on FuzeInfra.

  2. THE FLEET LIST IS STILL FuzeSDLC'S. governance/ruleset-fleet.json stays in the hub of
     record, where ruleset_sync / repo_settings_sync / prune_merged_branches read it; it is
     NOT copied here, because a second copy is a second thing to forget and this module's
     whole claim is that a repo joining the fleet joins the watcher with no second list.
     runner-watch.yml fetches it with the same App token and passes `--fleet-file`, and
     fails loudly if it cannot. `fleet_repos()` itself is unchanged and still a pure read.

  3. THE GOVERNANCE STANDARDS DID NOT MOVE. governance/required-checks.md §2.2.1 and
     governance/ci-runners.md — cited throughout this file — remain FuzeSDLC's. Those are
     the standard; this is the instance that implements it. Section references here point
     at FuzeSDLC's copies.

FuzeInfra is `excluded` in ruleset-fleet.json, so the hub is not in its own fleet: the
watcher never writes its own CI_RUNNER_LABELS. That was already true before the move and
is the correct shape either way.

===========================================================================================
THE PROBE-TOKEN ESCAPE HATCH  (restored from #249, which had it and this port dropped)
===========================================================================================

#249's `workflow_call` declared:

    secrets:
      runner-probe-token:
        description: Token with `administration: read` for the liveness probe. Optional;
          without it the declared runner is used and liveness is reported as unverified.

Dropping it made the whole watcher depend on ONE App permission, and measurement showed
that dependency was already unmet. Probing every candidate credential against
`GET /repos/FuzeOne/FuzeHub/actions/runners` (run 33521833116; status codes only, no
values, logs here are public):

    APP_TOKEN                   403      <- authenticated (200 on repos/<slug>) but NOT granted
    GH_TOKEN                    200
    GH_TF_TOKEN                 200
    GH_RELEASE_PAT              404      <- no visibility into the repo at all
    FUZESDLC_AGENT_PUSH_TOKEN   404

So `RUNNER_PROBE_TOKEN` is read from the environment and used for the runners listing ONLY
(see `probe_repo`). Precedence, set in runner-watch.yml: `RUNNER_PROBE_TOKEN` -> `GH_TOKEN`
-> the App token as ambient fallback. Two properties of that ordering matter:

  * IT IS AN OVERRIDE, NOT A REQUIREMENT. Unset, the probe runs under the ambient token
    exactly as before. Nothing about the fail-safe changes.
  * IT DOES NOT SOFTEN THE FAILURE. An unverified probe on ONE repo is still a
    keep-current + warn; unverified on EVERY repo that needed it is still `exit 1`
    (`SystematicBlindness` in `run_decide`). The hatch exists so the probe can SUCCEED
    without waiting on a browser, not so that failing to probe can pass for success.

Deliberately NOT widened: the override applies to a single read. Variable writes and
cancel/rerun keep the App token, so a PAT supplied to observe runners cannot cancel a
build. Note the same measurement found the App also 403s on `actions/variables`, so
`--apply` cannot write `CI_RUNNER_LABELS` yet; that is tracked separately and is why apply
has not been run.

===========================================================================================
OUT OF SCOPE HERE (stage 2)
===========================================================================================

BUDGET AWARENESS. #249's precedence order had a budget arm; this stage deliberately has
none, and the fleet's private repos therefore keep routing to their pool whenever it is up,
regardless of Actions spend. Note that the account budget being exhausted is a LIVE
condition, not a hypothetical — it is what moved this watcher to FuzeInfra (see "WHERE THIS
RUNS") — so stage 2 has a real customer. Before it is written, the billing endpoint MUST be
re-verified: `GET /users/{owner}/settings/billing/actions` currently returns

    410 This endpoint has been moved

so any stage-2 design that assumes it is a live source of `included_minutes` /
`total_minutes_used` is starting from a fact that is no longer true. Find the replacement
endpoint (or accept that budget cannot be read at all and design around that) FIRST.

===========================================================================================
OFFLINE-TESTABILITY CONTRACT
===========================================================================================

Every decision in this file is a pure function over plain data — `decide_runner`,
`hysteresis`, `stranded_runs`, `load_state`, `dump_state`. The network lives only in the
thin `_gh*` helpers and in `main()`. scripts/__tests__/test_runner_watch.py drives the pure
functions with no token, no network and no cluster, the same discipline
scripts/prune_merged_branches.py holds for its own delete decision.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Dict, Iterable, List, Optional, Sequence, Set

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The fleet target list. It is OWNED BY FuzeSDLC (governance/ruleset-fleet.json there) and
#: is deliberately NOT committed here — see "WHERE THIS RUNS", point 2. This default path
#: therefore does not exist in a plain FuzeInfra checkout; runner-watch.yml materialises the
#: hub's copy into the workspace and passes it with `--fleet-file`. An absent file raises
#: FileNotFoundError, which is the intended behaviour: a watcher that cannot read the fleet
#: list must stop, not sweep an empty fleet and report a clean run.
FLEET_FILE = os.path.join(REPO_ROOT, "governance", "ruleset-fleet.json")
POLICY_FILE = os.path.join(REPO_ROOT, "governance", "runner-watch.json")

#: The label a repo falls back to whenever the answer is "not the self-hosted pool".
#: A LITERAL, never an expression — it is also the literal that the consumption pattern
#: `${{ vars.CI_RUNNER_LABELS || 'ubuntu-latest' }}` keeps as its default, which is what
#: makes the pattern unable to queue forever even if this watcher stops running entirely.
HOSTED = "ubuntu-latest"

#: The repo variable the fleet's workflows read. Writing it needs `variables: write`.
RUNNER_VAR = "CI_RUNNER_LABELS"

#: How many consecutive runs must agree on a NEW value before it is written. See the
#: module docstring, point 1. Two is the minimum that suppresses a single-tick blip; it is
#: configurable so a fleet that proves flappier can be slowed without a code change.
DEFAULT_HYSTERESIS = 2

#: How long a job must sit `queued` with no runner before Job B calls it stranded.
DEFAULT_STRANDED_MINUTES = 20

#: How long a recovered run id is remembered. Long enough that the cap is real, short
#: enough that the state variable cannot grow without bound. A run older than this can no
#: longer be queued anyway (GitHub expires queued jobs well inside it).
RECOVERED_TTL_DAYS = 30


# ======================================================================================
# Job A — the decision
# ======================================================================================

@dataclass(frozen=True)
class RepoFacts:
    """Everything the decision is allowed to depend on. Deliberately plain data: if a fact
    is not in here, `decide_runner` cannot consult it, so the decision cannot quietly grow
    a dependency on something a test cannot express."""

    slug: str
    #: repository visibility. Public => GitHub Actions is free and unmetered.
    private: bool
    #: the scale set this repo would use, from `.fuze/manifest.json`'s `ci.runner`.
    #: "" means the repo declares none, which is a legitimate answer (ci-runners.md).
    declared_pool: str = ""
    #: a capability that PINS the runner regardless of cost — today only "cluster",
    #: meaning the repo's jobs need kubectl/helm/argocd against the live cluster and a
    #: hosted runner cannot reach it at all. "" means no pin.
    capability: str = ""
    #: runners observed `status == "online"` for this repo.
    #: None means LIVENESS COULD NOT BE VERIFIED — not "zero". The two are opposites and
    #: conflating them is how a fleet gets migrated off its runners by a 500.
    online_runners: Optional[int] = None
    #: an operator has pinned this repo out of the watcher's reach
    #: (governance/runner-watch.json `pinned`). The watcher reports but never writes.
    pinned: bool = False
    #: True when this repo's pool is an EPHEMERAL, SCALE-TO-ZERO pool — every pool in this
    #: fleet is a `gha-runner-scale-set` with `minRunners: 0`
    #: (runners/arc/runner-scale-set-values.yaml), which registers a runner only while a
    #: job is assigned. For such a pool `online_runners == 0` is the NORMAL RESTING STATE,
    #: not evidence of a fault, so it must not be read as "down". Set False only for a
    #: genuinely static pool with always-registered runners, where 0 really does mean down.
    #: Comes from governance/runner-watch.json `ephemeral_pools`.
    ephemeral_pool: bool = True


@dataclass(frozen=True)
class Decision:
    """`labels is None` means DO NOT WRITE — keep whatever the repo currently has.

    That is a first-class outcome, not an error code. It is what "if liveness cannot be
    verified, do not invent an answer" looks like when the sink is persistent."""

    labels: Optional[str]
    reason: str
    liveness_verified: bool = False
    #: warnings that must reach the log even on an otherwise uneventful run.
    warning: str = ""


def needs_liveness(facts: RepoFacts) -> bool:
    """Would `decide_runner` have to consult `online_runners` to answer for this repo?

    Pure, and deliberately mirrors `decide_runner`'s precedence order rather than
    re-deriving it: every arm ABOVE the liveness check answers without a probe, so a failed
    probe on those repos is irrelevant and must not count as blindness. A public repo is
    answered by visibility; an undeclared pool by the documented default; a pinned repo by
    the operator. Only "private, has a declared pool, not pinned" actually needs the probe.

    Used by `run_decide` to tell a TRANSIENT probe failure (a few repos) from a SYSTEMATIC
    one (every repo that needed it) — see `SystematicBlindness`.
    """
    if facts.capability == "cluster":
        return False
    if facts.pinned:
        return False
    if not facts.private:
        return False
    return bool(facts.declared_pool)


def decide_runner(facts: RepoFacts) -> Decision:
    """#249's precedence order, unchanged, minus the budget arm.

    The order, and why it terminates:

      1. capability pin      — a job needing kubectl/helm/argocd against the live cluster
                               can ONLY run self-hosted; cost does not enter into it
                               (ci-runners.md §1). Beats visibility and beats liveness:
                               a hosted runner is not a degraded option for these jobs,
                               it is a non-option.
      2. operator pin        — governance/runner-watch.json. A human said "leave this one
                               alone"; the watcher reports and writes nothing.
      3. public repo         — Actions is free and unmetered; spending scarce self-hosted
                               capacity here costs a shared resource to save nothing.
      4. no declared pool    — hosted is the documented default (ci-runners.md).
      5. probe unverified    — KEEP CURRENT + warn. See the module docstring, point 2.
      6. 0 online            — hosted, because queue-forever is the worse failure: a human
                               can top up GitHub in minutes, whereas repairing the cluster
                               REQUIRES working CI. Never route onto a pool observed down.
      7. otherwise           — the declared pool (private repos conserve metered minutes).

    Note the ONE arm of #249 that could not survive the shape change: its step 1 was
    "explicit override via vars.CI_RUNNER_LABELS". That variable is now this watcher's OWN
    output, so honouring it as an override would make the watcher permanently defer to its
    own last write and never change anything again. The operator lever moved to
    governance/runner-watch.json's `pinned`, which is a separate sink and therefore still
    an override rather than a feedback loop.
    """
    if facts.capability == "cluster":
        if facts.declared_pool:
            return Decision(
                facts.declared_pool,
                "capability=cluster — only a self-hosted runner can reach the cluster",
            )
        return Decision(
            None,
            "capability=cluster but no pool is declared — refusing to guess a pool name",
            warning=(
                f"{facts.slug} declares capability=cluster with no ci.runner. A cluster job "
                f"cannot run on a hosted runner, and naming a pool that does not exist "
                f"queues forever (ci-runners.md). Declare ci.runner, or drop the capability."
            ),
        )

    if facts.pinned:
        return Decision(
            None,
            "operator-pinned in governance/runner-watch.json — the watcher reports, "
            "it does not write",
        )

    if not facts.private:
        return Decision(
            HOSTED, "public repo — GitHub-hosted Actions is free and unmetered"
        )

    if not facts.declared_pool:
        return Decision(HOSTED, "no self-hosted runner declared for this repo")

    if facts.online_runners is None:
        return Decision(
            None,
            f"liveness of '{facts.declared_pool}' UNVERIFIED — keeping the current value",
            warning=(
                f"{facts.slug}: could not verify runner liveness (the token may lack "
                f"administration:read, or the API was unreachable). NOT writing "
                f"{RUNNER_VAR}: on a persistent sink, guessing 'hosted' from one failed "
                f"probe would migrate the fleet off its runners on the strength of an API "
                f"blip, and guessing '{facts.declared_pool}' would route onto a pool nobody "
                f"has observed to be up. Keeping the current value is the only answer that "
                f"invents nothing."
            ),
        )

    if facts.online_runners == 0:
        # An EPHEMERAL scale-to-zero pool registers a runner only while a job is assigned,
        # so 0 online is its idle state and says NOTHING about health. Reading it as "down"
        # is what pinned this whole fleet to metered hosted runners: every private repo's
        # pool is `minRunners: 0`, so every probe of an idle pool returned 0, every decision
        # came out `ubuntu-latest`, and the account's Actions budget was spent on work the
        # cluster was sitting idle waiting to do. Measured 2026-09-28: 14 private repos
        # decided `ubuntu-latest` with the reason "has 0 runners online" while all 22
        # scale-set listeners were Running and healthy; the single repo that escaped
        # (MendysRobotics, online=5) did so only because the probe happened to land while
        # runners were mid-job — a race, not a health check.
        #
        # The original reasoning (pods Running is not proof a runner registered) is still
        # sound and is NOT reverted: it argues against trusting pod state, which this does
        # not do. It just cannot be carried by a metric whose healthy value is also 0.
        # The genuine down-pool case keeps its safety net one layer out, in the recovery
        # sweep (`stranded_runs`), which cancels and re-dispatches any run left queued with
        # no runner past `stranded_minutes` — an OUTCOME check that observes the pool
        # failing to serve a real job instead of guessing from an idle-runner count.
        if facts.ephemeral_pool:
            return Decision(
                facts.declared_pool,
                f"private repo, pool '{facts.declared_pool}' reports 0 idle runners — the "
                f"normal resting state of a scale-to-zero pool, not a fault; routing to it "
                f"and leaving a genuinely down pool to the stranded-run recovery sweep",
                liveness_verified=True,
            )
        return Decision(
            HOSTED,
            f"self-hosted pool '{facts.declared_pool}' has 0 runners online — refusing to "
            f"queue onto a down pool",
            liveness_verified=True,
        )

    return Decision(
        facts.declared_pool,
        f"private repo, {facts.online_runners} runner(s) online — conserving metered "
        f"hosted minutes",
        liveness_verified=True,
    )


# ======================================================================================
# Hysteresis — the anti-flap guarantee this shape has to buy back
# ======================================================================================

@dataclass(frozen=True)
class Observation:
    """How many CONSECUTIVE runs have now computed `value` while it disagreed with the
    repo's live variable."""

    value: str
    count: int


@dataclass(frozen=True)
class HysteresisResult:
    write: bool
    observation: Optional[Observation]
    reason: str


def hysteresis(
    current: Optional[str],
    computed: Optional[str],
    prior: Optional[Observation],
    threshold: int = DEFAULT_HYSTERESIS,
) -> HysteresisResult:
    """Decide whether `computed` may be WRITTEN over `current`, given what previous runs saw.

    Contract, in the order the branches are taken:

      * `computed is None`      — the decision declined to answer (unverified probe, or an
                                  operator pin). Nothing is written and the counter is left
                                  UNTOUCHED, so a single unreachable tick in the middle of a
                                  legitimate migration does not reset the progress toward it.
      * `computed == current`   — already correct. Nothing to write, and the counter is
                                  CLEARED: the divergence that was being counted is over.
      * `computed != current`   — count it. The counter only ever accumulates for the SAME
                                  candidate value; a different candidate restarts at 1.
                                  Writing is permitted only at `count >= threshold`.

    The property that matters: with threshold >= 2, no single run can change a repo's
    runner. A pool that flaps between two ticks produces count=1, then a DIFFERENT candidate
    that also produces count=1, and nothing is ever written.
    """
    if threshold < 1:
        raise ValueError("hysteresis threshold must be >= 1")

    if computed is None:
        return HysteresisResult(False, prior, "no decision to write (see the decision reason)")

    if current is not None and computed == current:
        return HysteresisResult(
            False, None, f"already {computed!r} — nothing to write"
        )

    if prior is not None and prior.value == computed:
        count = prior.count + 1
    else:
        count = 1

    obs = Observation(computed, count)
    if count >= threshold:
        return HysteresisResult(
            True,
            obs,
            f"{computed!r} observed on {count} consecutive run(s) (threshold {threshold}) "
            f"— writing over {current!r}",
        )
    return HysteresisResult(
        False,
        obs,
        f"{computed!r} differs from {current!r} but has only been observed {count}/"
        f"{threshold} time(s) — holding. A one-tick blip must never move the fleet.",
    )


# ======================================================================================
# Job B — the stranded-run selector
# ======================================================================================

@dataclass(frozen=True)
class JobFacts:
    """One job of one workflow run, reduced to what the selector may look at."""

    run_id: int
    job_id: int
    name: str
    #: "queued" | "in_progress" | "completed" | "waiting" | "requested" | "pending"
    status: str
    #: when the job was created/queued, UTC. None => age unknown.
    queued_at: Optional[_dt.datetime] = None
    #: the runner serving it. Empty/None => none assigned yet.
    runner_name: Optional[str] = None
    runner_id: Optional[int] = None
    #: for the audit log
    html_url: str = ""


def _has_runner(job: JobFacts) -> bool:
    """A runner is 'assigned' only when the API says so unambiguously.

    GitHub reports an unassigned job as `runner_id: 0` with an empty `runner_name` (the
    same shape ci-runners.md documents for a budget-refused job), so 0 and "" both mean
    NOT assigned — treating `runner_id: 0` as truthy assignment would make every stranded
    job look served and the whole sweep a no-op."""
    if job.runner_name:
        return True
    return bool(job.runner_id)


def stranded_runs(
    jobs: Iterable[JobFacts],
    *,
    now: _dt.datetime,
    threshold_minutes: int = DEFAULT_STRANDED_MINUTES,
    already_recovered: Optional[Set[int]] = None,
) -> List[int]:
    """The run ids that are safe to cancel + re-dispatch, in ascending order, each at most
    once.

    Safety properties, each of which has a named test in
    scripts/__tests__/test_runner_watch.py:

      * a run with ANY `in_progress` job is NEVER returned — cancellation is per-run, so
        rescuing a queued job would discard its sibling's real work;
      * a job with a runner assigned is never a candidate — it is being served;
      * a job queued for less than `threshold_minutes` is never a candidate — a busy pool
        is not a stranding;
      * a job whose queue age is UNKNOWN (`queued_at is None`) is never a candidate — the
        same "never guess on unknown state" discipline prune_merged_branches.py holds for
        `ahead_by is None`;
      * a run id in `already_recovered` is never returned, which is the hard rerun cap:
        recovery that cannot fix a run must not keep cancelling it forever;
      * the result is deduplicated, so a run with three stranded jobs is cancelled once.
    """
    already_recovered = already_recovered or set()
    cutoff = now - _dt.timedelta(minutes=threshold_minutes)

    busy: Set[int] = set()
    candidates: Set[int] = set()

    for job in jobs:
        if job.status == "in_progress":
            # Poison the whole run, not just this job. See the docstring.
            busy.add(job.run_id)
            continue
        if job.status != "queued":
            continue
        if _has_runner(job):
            continue
        if job.queued_at is None:
            continue
        if job.queued_at > cutoff:
            continue
        candidates.add(job.run_id)

    return sorted(candidates - busy - already_recovered)


# ======================================================================================
# Durable state (hysteresis counters + the recovered-run ledger)
# ======================================================================================
#
# Persisted in TWO repo variables on the hub, not one, and not in an Actions cache.
#
#   * Two, because Job A and Job B write independently; a single blob would let whichever
#     job finished second clobber the other's update.
#   * Variables rather than an Actions cache because a cache entry is evicted after 7 days
#     without a read and is scoped to the branch that wrote it. A weekly-ish signal stored
#     in a cache would silently reset itself, and a reset hysteresis counter is
#     indistinguishable from "this value has never been seen before" — which re-arms
#     exactly the flap the counter exists to prevent.

STATE_VAR_DECIDE = "CI_RUNNER_WATCH_STATE"
STATE_VAR_RECOVER = "CI_RUNNER_RECOVERY_STATE"


def load_state(raw: Optional[str]) -> Dict[str, Observation]:
    """Parse the hysteresis state. UNPARSEABLE OR ABSENT STATE IS AN EMPTY STATE, never an
    exception: a corrupt variable must degrade to 'nothing has been observed yet' (which
    costs one extra tick before any write) rather than taking the watcher down."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # `or {}` alone is NOT enough: a truthy non-dict (`{"observations": "nope"}`) would
    # sail past it and blow up on .items(). A crashed loader is strictly worse than an
    # empty one — it takes the whole watcher down, so nothing is published AND nothing is
    # recovered. Caught by test_corrupt_or_absent_state_degrades_to_empty_rather_than_raising.
    observations = data.get("observations")
    if not isinstance(observations, dict):
        return {}
    out: Dict[str, Observation] = {}
    for slug, entry in observations.items():
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        count = entry.get("count")
        if isinstance(value, str) and isinstance(count, int) and count > 0:
            out[slug] = Observation(value, count)
    return out


def dump_state(observations: Dict[str, Observation]) -> str:
    return json.dumps(
        {
            "version": 1,
            "observations": {
                slug: {"value": o.value, "count": o.count}
                for slug, o in sorted(observations.items())
            },
        },
        sort_keys=True,
    )


def load_recovered(raw: Optional[str], *, now: Optional[_dt.datetime] = None) -> Dict[int, str]:
    """Parse the recovered-run ledger, dropping entries older than RECOVERED_TTL_DAYS.

    Same degrade-to-empty contract as `load_state` with one asymmetry worth stating: an
    empty hysteresis state costs a delayed write, whereas an empty recovery ledger un-caps
    the rerun. That is why the TTL is generous (30 days) — the ledger is pruned by AGE, not
    by size, so it can never be trimmed to make room and quietly re-arm a cancel loop."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    recovered = data.get("recovered")
    if not isinstance(recovered, dict):   # same truthy-non-dict guard as load_state
        return {}
    now = now or _dt.datetime.now(_dt.timezone.utc)
    horizon = now - _dt.timedelta(days=RECOVERED_TTL_DAYS)
    out: Dict[int, str] = {}
    for key, when in recovered.items():
        try:
            run_id = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(when, str):
            continue
        ts = parse_ts(when)
        if ts is not None and ts < horizon:
            continue
        out[run_id] = when
    return out


def dump_recovered(recovered: Dict[int, str]) -> str:
    return json.dumps(
        {
            "version": 1,
            "recovered": {str(k): v for k, v in sorted(recovered.items())},
        },
        sort_keys=True,
    )


def parse_ts(value: Optional[str]) -> Optional[_dt.datetime]:
    """GitHub's `2026-08-31T12:00:00Z` into an aware datetime. None on anything else —
    an unparseable timestamp becomes 'age unknown', which the selector refuses to act on."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


# ======================================================================================
# Fleet + policy loading
# ======================================================================================

def fleet_repos(fleet_file: str = FLEET_FILE) -> List[str]:
    """`owner/name` for every repo in governance/ruleset-fleet.json minus `excluded` — the
    same target list ruleset_sync / repo_settings_sync / prune_merged_branches use, so a
    repo joining the fleet joins this watcher too with no second list to remember."""
    with open(fleet_file, encoding="utf-8") as fh:
        data = json.load(fh)
    owner = data.get("owner", "")
    excluded = set(data.get("excluded", {}))
    return [f"{owner}/{n}" for n in data.get("repos", []) if n not in excluded]


@dataclass(frozen=True)
class Policy:
    """governance/runner-watch.json — the operator's levers over the watcher."""

    pinned: Dict[str, str] = field(default_factory=dict)     # slug -> reason
    capability: Dict[str, str] = field(default_factory=dict)  # slug -> "cluster"
    hysteresis: int = DEFAULT_HYSTERESIS
    stranded_minutes: int = DEFAULT_STRANDED_MINUTES
    #: are the fleet's self-hosted pools ephemeral/scale-to-zero? True for every pool here
    #: (`gha-runner-scale-set`, `minRunners: 0`), which makes `0 runners online` the idle
    #: state rather than a fault — see RepoFacts.ephemeral_pool. Defaults True because that
    #: is what the fleet actually runs; set False only for always-registered static runners.
    ephemeral_pools: bool = True


def load_policy(policy_file: str = POLICY_FILE) -> Policy:
    """Absent policy file => defaults. The watcher must be operable before anyone has had
    an opinion, and the defaults are the safe end of every lever."""
    if not os.path.isfile(policy_file):
        return Policy()
    with open(policy_file, encoding="utf-8") as fh:
        data = json.load(fh)
    return Policy(
        pinned={k: str(v) for k, v in (data.get("pinned") or {}).items()},
        capability={k: str(v) for k, v in (data.get("capability") or {}).items()},
        hysteresis=int(data.get("hysteresis", DEFAULT_HYSTERESIS)),
        stranded_minutes=int(data.get("stranded_minutes", DEFAULT_STRANDED_MINUTES)),
        ephemeral_pools=bool(data.get("ephemeral_pools", True)),
    )


# ======================================================================================
# The thin network layer. Everything above this line is pure and offline-testable.
# ======================================================================================

class GhError(RuntimeError):
    pass


#: Env var carrying an OPTIONAL credential used for the liveness probe alone. See
#: `probe_repo` and the module docstring's "THE PROBE-TOKEN ESCAPE HATCH".
PROBE_TOKEN_ENV = "RUNNER_PROBE_TOKEN"


def _gh(args: Sequence[str], token: Optional[str] = None) -> str:
    """Run `gh`. With `token`, run that ONE call under a different credential.

    The override is scoped to a single subprocess env rather than exported process-wide,
    so a token handed in for a read cannot silently widen the blast radius of every other
    call in the run. Nothing here ever logs `token`: FuzeInfra's job logs are PUBLIC, and
    `gh`'s own error text (surfaced in GhError) is stderr, which does not echo the
    credential.
    """
    env = None
    if token:
        env = dict(os.environ)
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)  # gh prefers GH_TOKEN, but leave nothing ambiguous.
    res = subprocess.run(["gh", *args], capture_output=True, text=True, check=False,
                         env=env)
    if res.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout


def _gh_json(args: Sequence[str], token: Optional[str] = None):
    out = _gh(args, token=token)
    return json.loads(out) if out.strip() else None


def probe_repo(slug: str, probe_token: Optional[str] = None) -> RepoFacts:
    """Assemble one repo's facts from the live API.

    The liveness probe asks `repos/<slug>/actions/runners` for `status == "online"`,
    because PODS BEING RUNNING IS NOT PROOF A RUNNER REGISTERED — a dind scale set can have
    every pod Running and zero runners online (FuzeInfra's ARC incident: 8 sets Pending for
    11h with every listener up). That listing needs `administration: read`, which the
    default GITHUB_TOKEN does NOT carry; supply the fuze-agent App token.

    A failed probe leaves `online_runners=None` — unverified, NOT zero. `decide_runner`
    turns that into "keep the current value", never into a migration."""
    meta = _gh_json(["api", f"repos/{slug}", "--jq",
                     '{private: .private, default_branch: .default_branch}'])
    private = bool((meta or {}).get("private"))

    declared_pool = ""
    try:
        raw = _gh(["api", f"repos/{slug}/contents/.fuze/manifest.json",
                   "--jq", ".content"])
        import base64
        manifest = json.loads(base64.b64decode(raw.strip()).decode("utf-8"))
        declared_pool = str(((manifest.get("ci") or {}).get("runner") or ""))
    except (GhError, ValueError, TypeError):
        # No manifest, or no ci.runner. Absence is a legitimate answer (ci-runners.md),
        # and `decide_runner` maps it to the hosted default rather than to a guess.
        declared_pool = ""

    online: Optional[int] = None
    try:
        # `probe_token` (RUNNER_PROBE_TOKEN) is applied HERE and nowhere else. This is the
        # only call in the module that needs `administration: read`, so it is the only one
        # that should be able to run under a broader credential. Everything else — the
        # variable writes, the cancel/rerun — keeps the ambient App token, which is what
        # keeps a PAT handed in for a READ from becoming a PAT that can cancel builds.
        runners = _gh_json(["api", f"repos/{slug}/actions/runners", "--paginate",
                            "--jq", '[.runners[]? | select(.status=="online")] | length'],
                           token=probe_token)
        if isinstance(runners, int):
            online = runners
        elif isinstance(runners, list):
            online = sum(int(x) for x in runners)
    except (GhError, ValueError, TypeError):
        online = None

    return RepoFacts(slug=slug, private=private, declared_pool=declared_pool,
                     online_runners=online)


def read_variable(slug: str, name: str) -> Optional[str]:
    try:
        data = _gh_json(["api", f"repos/{slug}/actions/variables/{name}"])
    except GhError:
        return None
    if isinstance(data, dict):
        value = data.get("value")
        return value if isinstance(value, str) else None
    return None


def write_variable(slug: str, name: str, value: str) -> None:
    """Create-or-update. PATCH first (the common case), POST on 404. Needs `variables: write`."""
    try:
        _gh(["api", "--method", "PATCH", f"repos/{slug}/actions/variables/{name}",
             "-f", f"name={name}", "-f", f"value={value}"])
    except GhError:
        _gh(["api", "--method", "POST", f"repos/{slug}/actions/variables",
             "-f", f"name={name}", "-f", f"value={value}"])


def queued_jobs(slug: str) -> List[JobFacts]:
    """Every job of every currently-queued workflow run in `slug`."""
    runs = _gh_json(["api", f"repos/{slug}/actions/runs?status=queued&per_page=100",
                     "--jq", "[.workflow_runs[]? | {id, html_url, name}]"]) or []
    out: List[JobFacts] = []
    for run in runs:
        run_id = int(run["id"])
        jobs = _gh_json([
            "api", f"repos/{slug}/actions/runs/{run_id}/jobs?per_page=100", "--paginate",
            "--jq", "[.jobs[]? | {id, name, status, started_at, created_at, "
                    "runner_name, runner_id, html_url}]",
        ]) or []
        for job in jobs:
            out.append(JobFacts(
                run_id=run_id,
                job_id=int(job.get("id") or 0),
                name=str(job.get("name") or ""),
                status=str(job.get("status") or ""),
                queued_at=parse_ts(job.get("created_at") or job.get("started_at")),
                runner_name=job.get("runner_name") or None,
                runner_id=job.get("runner_id") or None,
                html_url=str(job.get("html_url") or run.get("html_url") or ""),
            ))
    return out


# ======================================================================================
# Drivers
# ======================================================================================

def run_decide(slugs: List[str], policy: Policy, *, apply: bool, hub: str,
               probe_token: Optional[str] = None) -> int:
    """Job A. Returns a process exit code."""
    state = load_state(read_variable(hub, STATE_VAR_DECIDE))
    warnings = 0
    writes = 0
    # See `SystematicBlindness` below for why these two are counted separately from
    # `warnings`, which also counts conditions that are not blindness at all.
    needed_liveness = 0
    blind = 0

    for slug in slugs:
        try:
            facts = probe_repo(slug, probe_token=probe_token)
        except GhError as exc:
            print(f"::warning title=runner-watch::{slug}: unreachable ({exc}). Left "
                  f"untouched — an unreachable repo is never treated as compliant.")
            warnings += 1
            continue

        facts = RepoFacts(
            slug=facts.slug,
            private=facts.private,
            declared_pool=facts.declared_pool,
            capability=policy.capability.get(slug, ""),
            online_runners=facts.online_runners,
            pinned=slug in policy.pinned,
            ephemeral_pool=policy.ephemeral_pools,
        )

        if needs_liveness(facts):
            needed_liveness += 1
            if facts.online_runners is None:
                blind += 1

        decision = decide_runner(facts)
        current = read_variable(slug, RUNNER_VAR)
        result = hysteresis(current, decision.labels, state.get(slug), policy.hysteresis)

        if decision.warning:
            print(f"::warning title=runner-watch::{decision.warning}")
            warnings += 1

        print(f"{slug}: online={facts.online_runners} pool={facts.declared_pool!r} "
              f"current={current!r} computed={decision.labels!r} "
              f"({decision.reason}) -> {result.reason}")

        if result.observation is None:
            state.pop(slug, None)
        else:
            state[slug] = result.observation

        if result.write and decision.labels is not None:
            if apply:
                write_variable(slug, RUNNER_VAR, decision.labels)
                print(f"::notice title=runner-watch::{slug}: {RUNNER_VAR}="
                      f"{decision.labels} ({decision.reason})")
                state.pop(slug, None)
            else:
                print(f"::notice title=runner-watch::{slug}: WOULD set {RUNNER_VAR}="
                      f"{decision.labels} ({decision.reason}) — check mode, nothing written")
            writes += 1

    if apply:
        write_variable(hub, STATE_VAR_DECIDE, dump_state(state))
    print(f"\ndecide: {len(slugs)} repo(s), {writes} write(s), {warnings} warning(s), "
          f"{blind}/{needed_liveness} liveness probe(s) unverified")

    # ==================================================================================
    # SystematicBlindness — the run must be RED when it could not see ANYTHING.
    # ==================================================================================
    # Per repo, an unverified probe is a keep-current + warn, and that is right: a single
    # 500 must not migrate a repo, and a partial outage must not take the watcher down.
    #
    # But when EVERY repo that needed a probe failed it, that is not a blip, it is a
    # missing grant — and the run exiting 0 is precisely the failure this whole design
    # refuses. Observed live on the first dispatch after the port (run 33517478654): the
    # fuze-agent App had no `administration: read`, so all 14 private repos with a declared
    # pool came back `online=None`, the watcher wrote nothing anywhere, and the job
    # reported SUCCESS. runner-watch.yml's own header names that outcome — "a clean-looking
    # no-op that publishes nothing and hides that it is publishing nothing" — but its guard
    # only checked that a token was MINTED, which says nothing about its scopes.
    #
    # ALL rather than a ratio, deliberately: `all` can only be reached by a systematic
    # cause, so this can never redden a run for a handful of flaky probes. Repos that never
    # needed liveness (public, no declared pool, pinned) are excluded by `needs_liveness`,
    # so a fleet of entirely public repos does not trip it either — needed_liveness is 0
    # and the branch is skipped.
    if needed_liveness and blind == needed_liveness:
        print(f"::error title=runner-watch::Liveness could not be verified for ANY of the "
              f"{needed_liveness} repo(s) whose decision depends on it. That is not a "
              f"transient probe failure, it is a missing grant: the fuze-agent App needs "
              f"`administration: read` to read repos/<slug>/actions/runners. Nothing was "
              f"written and nothing could have been. Failing rather than reporting a "
              f"green run that looked at the whole fleet and saw none of it.")
        return 1
    return 0


def run_recover(slugs: List[str], policy: Policy, *, apply: bool, hub: str) -> int:
    """Job B. Returns a process exit code."""
    now = _dt.datetime.now(_dt.timezone.utc)
    recovered = load_recovered(read_variable(hub, STATE_VAR_RECOVER), now=now)
    acted = 0

    for slug in slugs:
        try:
            jobs = queued_jobs(slug)
        except GhError as exc:
            print(f"::warning title=runner-watch::{slug}: could not list queued runs ({exc})")
            continue

        targets = stranded_runs(
            jobs, now=now, threshold_minutes=policy.stranded_minutes,
            already_recovered=set(recovered),
        )
        by_run = {}
        for job in jobs:
            by_run.setdefault(job.run_id, job)

        for run_id in targets:
            job = by_run.get(run_id)
            url = (job.html_url if job else "") or f"https://github.com/{slug}/actions/runs/{run_id}"
            why = (f"queued > {policy.stranded_minutes}m with NO runner assigned — "
                   f"re-dispatching so it picks up the current {RUNNER_VAR}")
            if not apply:
                print(f"::notice title=runner-watch::{slug} run {run_id}: WOULD "
                      f"cancel+rerun — {why} ({url})")
                acted += 1
                continue
            try:
                _gh(["run", "cancel", str(run_id), "-R", slug])
            except GhError as exc:
                print(f"::warning title=runner-watch::{slug} run {run_id}: cancel failed "
                      f"({exc}) — {url}")
                continue
            # The ledger is written BEFORE the rerun, not after. A rerun that fails must
            # still consume the run's one and only recovery attempt: the alternative is a
            # run that fails to rerun being cancelled again on every subsequent tick.
            recovered[run_id] = now.isoformat().replace("+00:00", "Z")
            try:
                _gh(["run", "rerun", str(run_id), "-R", slug])
                print(f"::notice title=runner-watch::{slug} run {run_id}: cancelled and "
                      f"re-dispatched — {why} ({url})")
            except GhError as exc:
                print(f"::warning title=runner-watch::{slug} run {run_id}: cancelled but "
                      f"rerun failed ({exc}). Recovery attempt consumed; this run will NOT "
                      f"be touched again — {url}")
            acted += 1

    if apply:
        write_variable(hub, STATE_VAR_RECOVER, dump_recovered(recovered))
    print(f"\nrecover: {acted} run(s) acted on across {len(slugs)} repo(s)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["decide", "recover"])
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", default=True,
                       help="report only; write nothing (the default)")
    group.add_argument("--apply", action="store_true",
                       help="write CI_RUNNER_LABELS / cancel+rerun stranded runs")
    ap.add_argument("--fleet-file", default=FLEET_FILE)
    ap.add_argument("--policy-file", default=POLICY_FILE)
    ap.add_argument("--repos", default="",
                    help="comma-separated owner/name slugs — empty = the whole fleet")
    ap.add_argument("--hub", default=os.environ.get("RUNNER_WATCH_HUB", "FuzeOne/FuzeInfra"),
                    help="repo whose variables hold the watcher's durable state")
    ap.add_argument("--stranded-minutes", type=int, default=None,
                    help="override governance/runner-watch.json's stranded_minutes")
    args = ap.parse_args(argv)

    policy = load_policy(args.policy_file)
    if args.stranded_minutes is not None:
        if args.stranded_minutes < 1:
            ap.error("--stranded-minutes must be >= 1: a threshold of 0 would make every "
                     "freshly-queued job a cancellation candidate")
        # `replace` rather than re-listing fields: a hand-built Policy() silently reset
        # every lever the author forgot to copy forward, which would have discarded an
        # operator's explicit `ephemeral_pools: false` on any run that passed
        # --stranded-minutes. Overriding one knob must never reset the others.
        policy = _dc_replace(policy, stranded_minutes=args.stranded_minutes)
    if args.repos.strip():
        slugs = [s.strip() for s in args.repos.split(",") if s.strip()]
    else:
        slugs = fleet_repos(args.fleet_file)

    if args.mode == "decide":
        return run_decide(slugs, policy, apply=args.apply, hub=args.hub,
                          probe_token=os.environ.get(PROBE_TOKEN_ENV) or None)
    return run_recover(slugs, policy, apply=args.apply, hub=args.hub)


if __name__ == "__main__":
    sys.exit(main())
