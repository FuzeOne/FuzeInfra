# Dark-node detection and automated remediation

## Why this exists

A Contabo-autoscaler-health alert investigation (2026-09-15) found
`fuze-core-3` — a control-plane/etcd node — silently dark: `Ready`,
`MemoryPressure`, `DiskPressure` and `PIDPressure` all `Unknown` ("Kubelet
stopped posting node status") for **4.5 days**. It still held its etcd voting
membership and its scheduled pods the entire time while contributing zero
usable capacity, and nothing had ever flagged it. Same "everything reports
fine, nothing actually works" shape as the 2026-08-30 Loki/Argo freeze that
[`deployment-watchdog.yml`](../../.github/workflows/deployment-watchdog.yml)
already exists for, so the detection lives there too.

**UPDATE (2026-09-16).** The 2026-09-15 version of this doc filed an issue
and stopped, on the reasoning that rebooting a node "is not provably
safe/reversible" the way terminating a stuck Argo op is. What actually
happened to `fuze-core-3` under that design: detected 2026-09-10, not
actually fixed until 2026-09-15/16 — **five days**, almost entirely spent on
a human needing to read the issue, decide, dispatch a reboot, wait, re-check,
and eventually decide to reinstall. The facts that make that decision safe
(reboot attempt count, elapsed dark time, quorum headroom, node class) are
exactly the facts a bounded state machine can check as well as a human can,
so detection now drives an automated **reboot → reinstall** ladder instead of
stopping at "file an issue." The two things that made an unconditional "yes"
genuinely unsafe — Longhorn data loss (2026-09-07 incident) and etcd quorum —
are enforced as hard gates, not assumed away; see "The escalation ladder"
below.

## The flow

```
deployment-watchdog.yml (every 15m, read-only + the ladder's dispatches)
  -> scripts-tools/deployment_watchdog.py: detect_dark_nodes()
  -> Node Ready condition == Unknown for > dark_node.unknown_minutes (default 30)?
       -> files a GitHub issue mentioning @fuze, with facts:
            node, unknown_since, unknown_minutes, internal_ip, external_ip,
            is_control_plane, is_etcd_voter, other_ready_control_plane_nodes
  -> decide_dark_node_escalation() reads the ladder's state (embedded in the
     LATEST tracked-issue comment carrying a `<!-- dark-node-state: {...} -->`
     marker) and decides the next action for THIS run:
       reboot           - attempts < max_reboot_attempts (default 3) and past
                           the reboot_cooldown_minutes since the last one
       reinstall        - attempts exhausted AND dark >= escalate_after_minutes
                           (default 120) AND (if an etcd voter) quorum-safe
       refuse-quorum    - would drop control-plane quorum; reported ONCE, then
                           left for a human
       refuse-elastic   - node isn't control-plane/durable; elastic nodes have
                           their own autoscaler-driven lifecycle, out of scope
       none             - cooling down, waiting out escalate_after_minutes, or
                           already escalated and waiting on the reinstall
  -> "reboot" dispatches contabo-instance-reboot.yml (node_name, external_ip,
     reason, confirm=RESTART, issue_number) — same workflow a human ran by
     hand before, holding its own Contabo credential, commenting its own
     result back onto the tracked issue
  -> "reinstall" dispatches ca-reinstall-controlplane.yml (node_name,
     external_ip, template=cp-userdata-eth1-join.template — the SELF-JOINING
     template, mode=reinstall, confirm=REINSTALL-CONTROL-PLANE, issue_number).
     That workflow independently re-checks Longhorn data safety
     (scripts/preflight_node_teardown.py) and refuses to delete a Node object
     that reports Ready=True, REGARDLESS of who dispatched it — the ladder
     decides WHEN to call it, never re-implements what it already enforces.
  -> once the node is no longer Ready=Unknown at all, the next run closes the
     tracked issue automatically with a recovery comment
```

Detection is read-only and holds no Contabo credential or cluster-write
credential. Every mutation happens inside a separate, narrowly-scoped,
independently-auditable workflow that holds its own credential — see "Why
separate workflows" below.

## The escalation ladder's safety gates

Nothing about **how** a reboot or reinstall happens changed from the manual
flow — only **who is allowed to trigger it**. The state machine
(`decide_dark_node_escalation` in `scripts-tools/deployment_watchdog.py`,
config in `governance/watchdog-thresholds.json`'s `dark_node` block) enforces
exactly the checks a human was asked to make before:

- **Bounded reboot, not infinite retry.** At most `max_reboot_attempts`
  (default 3), each spaced `reboot_cooldown_minutes` (default 30) apart. A
  node that doesn't recover after 3 reboots has a problem a reboot won't fix
  twice — hardware, a wedged disk, a Contabo host issue.
- **Reinstall needs BOTH gates, not either.** Attempts exhausted AND at least
  `escalate_after_minutes` (default 120) of total dark time — a node whose 3
  reboots happen to burn through quickly still gets a full 2h before its disk
  is wiped.
- **Quorum safety is a hard gate, not a suggestion.** `is_control_plane` +
  `is_etcd_voter` + `other_ready_control_plane_nodes` — the exact facts the
  filed issue already surfaced for a human — now also gate the automated
  path directly: fewer than `reinstall_min_other_ready_control_plane` (default
  2) other Ready control-plane nodes and it refuses (`refuse-quorum`),
  reported once on the issue rather than every 15-minute run.
- **Elastic nodes are out of scope**, not silently reinstalled with the wrong
  template — they already have autoscaler-driven replacement
  (`ca-salvage-enroll.yml` / the reaper).
- **Longhorn data safety is enforced downstream, not trusted to this flag.**
  `ca-reinstall-controlplane.yml` runs `scripts/preflight_node_teardown.py`
  itself before wiping anything, whether a human or the watchdog dispatched
  it — so this script never has to duplicate that judgement.
- **A recurring refusal reports once, not on every cycle** (`refused_reason`
  in the ladder's state) — the same anti-spam property the issue-filing
  dedup already has, applied to escalation comments too.

If `refuse-quorum` or `refuse-elastic` fires, the situation is left for a
human exactly as before: check the issue, decide, and either fix the
underlying cause or act manually per "Why separate workflows" below.

## Manual override

The automated ladder does not remove the manual path — it's still there for
a node the ladder refused, or for a human who wants to act before 3 reboots
+ 2h elapse:

```bash
gh workflow run contabo-instance-reboot.yml --repo FuzeOne/FuzeInfra \
  -f node_name=fuze-core-3 \
  -f external_ip=194.163.136.242 \
  -f reason="Ready=Unknown 4.6d, other_ready_control_plane_nodes=2 — not a quorum risk" \
  -f confirm=RESTART \
  -f issue_number=<the watchdog issue number>

gh workflow run ca-reinstall-controlplane.yml --repo FuzeOne/FuzeInfra \
  -f node_name=fuze-core-3 \
  -f external_ip=194.163.136.242 \
  -f template=cluster-autoscaler/contabo-externalgrpc/deploy/cp-userdata-eth1-join.template \
  -f mode=reinstall \
  -f confirm=REINSTALL-CONTROL-PLANE \
  -f issue_number=<the watchdog issue number>
```

## Why separate workflows, not one

`fuze-cluster.yml` (the cluster-capable `@fuze` responder) already holds prod
`KUBE_CONFIG` and runs an open-ended agent. Giving that same runner the
Contabo credential too would mean one compromised or misdirected agent
session holds both the cluster layer and the hypervisor layer under it.
Instead, `contabo-instance-reboot.yml` and `ca-reinstall-controlplane.yml`
hold the Contabo credential and each does exactly one narrow, auditable thing
(a typed `confirm` string required, one Contabo Action, one instance) — the
same split every other `contabo-*.yml` one-off workflow in this repo already
uses (see `contabo-rename-instance.yml`, `ca-delete-instance.yml`).
`deployment-watchdog.yml` decides *when*; it never holds the credential that
lets it act directly.

## When the reinstall "succeeds" but the node never appears

`ca-reinstall-controlplane.yml` reporting HTTP 200, and Contabo reporting
`status=running`, only mean the *hypervisor* did its job. Neither says anything
about whether k3s joined. On 2026-09-15 `fuze-core-3` sat in exactly that state:
instance running, `kubectl get node fuze-core-3` → `NotFound`, for ~50 minutes.

Check these in order — the first three need no host access:

1. **Is the OS up?** `nc -z <public-ip> 22`. Open ⇒ the guest booted; this is not
   a hung kernel and another reboot/power-cycle will not help.
2. **Did cloud-init's `runcmd` finish?** Probe the ufw fingerprint: 6443/10250/2380
   should answer with a fast **RST** (allowed, nothing listening yet) while 80/443
   **time out** (not in the allow-list). That asymmetry only exists if
   `ufw --force enable` ran, which is near the end of `runcmd`.
3. **Is k3s listening?** `nc -z <public-ip> 6443`. Refused ⇒ k3s is not up.
4. **Why isn't it up?** This needs the break-glass key (`NODE_SSH_PUBLIC_KEY`'s
   private half):
   ```bash
   ssh root@<public-ip> 'cat /var/log/fuzeinfra-cp-join.log; systemctl is-active k3s'
   ssh root@<public-ip> 'journalctl -u k3s -n 50 -o cat | grep level=fatal'
   ```

### Known failure: `K3S_SERVER_URL` pointing at the node being reinstalled

The symptom is a **silent 5-second crash-loop** (restart counter in the hundreds)
with:

```
failed to validate token: failed to get CA certs:
  Get "https://<THIS node's own IP>:6443/cacerts": connect: connection refused
```

A joining server fetches `$K3S_URL/cacerts` *before* it starts serving, so if that
URL is its own address it is waiting on a listener only it could provide. The
template header has always required `{{.K3SServerURL}}` be "a LIVE server to join
through. **Must not be this node**" — but nothing enforced it until the guard added
alongside this section, which now refuses and names the cause.

**Fix:** point the `K3S_SERVER_URL` repo secret at a *peer's VLAN* address
(`https://10.0.0.6:6443` = fuze-core-1, `https://10.0.0.2:6443` = fuze-core-2),
then re-dispatch `ca-reinstall-controlplane.yml`. Re-dispatching *before* fixing
the secret just repeats the same failure and wipes the disk again for nothing.

This compounded with a second bug: the failed install still wrote
`/etc/systemd/system/k3s.service`, so the next boot took the "already installed"
branch and wrote `/etc/fuzeinfra-cp-joined` — the sentinel that gates
`ConditionPathExists=!` on the join unit. The node could then never retry on any
future boot, while its own log still said "the unit retries on next boot". Both
are guarded by `tests/test_cp_userdata_join_guards.py`.

> A node stuck this way is **inert, not dangerous** — it holds no pods, no
> Longhorn replicas and no etcd membership (the reinstall workflow deletes the
> Node object first). There is no clock on fixing it, so fix the secret rather
> than improvising around it.

## Resolving instanceId by IP

Nodes are not labelled with their Contabo `instanceId` at bootstrap
(`modules/contabo-k3s-node/cloud-init.tftpl` sets none), so the only reliable
link from a Kubernetes Node object to a Contabo instance today is the public
IP: `kubectl get node <name> -o wide` → `EXTERNAL-IP` column, matched against
`GET /v1/compute/instances` → `.data[].ipConfig.v4.ip`. Both
`contabo-instance-reboot.yml` and `ca-reinstall-controlplane.yml` have a
"Resolve instanceId" step that does this when `instance_id` is not supplied
directly. See
[`.claude/agents/contabo-expert.md`](../../.claude/agents/contabo-expert.md)
for the full Contabo API endpoint map.

## Tuning

Thresholds live in
[`governance/watchdog-thresholds.json`](../../governance/watchdog-thresholds.json)
under `dark_node`. Detection logic (`detect_dark_nodes`) and the escalation
state machine (`decide_dark_node_escalation`) are both unit tested offline in
[`tests/test_deployment_watchdog.py`](../../tests/test_deployment_watchdog.py)
— no cluster required.
