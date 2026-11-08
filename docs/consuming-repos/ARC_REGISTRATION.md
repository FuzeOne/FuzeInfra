# `arc-register` — ARC runner scale-set registration without a cluster credential

**You do not need `KUBE_CONFIG` to get (or remove) a self-hosted GitHub Actions
runner.** FuzeInfra publishes a dispatchable handler —
[`.github/workflows/arc-register.yml`](../../.github/workflows/arc-register.yml)
— that installs or uninstalls your repo's ARC `AutoscalingRunnerSet` on the
shared cluster on your behalf.

> **Status: this handler exists and is allowlisted for six repos (below). No
> consumer repo has migrated to it yet.** Today those repos still run
> `runners/arc/register-repo.sh` themselves via their own copy of the old
> `.github/workflows/arc-register.yml` template
> (`runners/arc/workflow-template/arc-register.yml`), which requires each of
> them to hold a **full cluster-admin `KUBE_CONFIG`** — obtained via `kubectl
> config view --raw`, i.e. cluster-admin, copied into six separate repos, for
> an operation that only ever needs write access to the `arc-runners`
> namespace. Migrating a repo's own `arc-register.yml` to dispatch here
> instead, and then revoking that repo's `KUBE_CONFIG`, is the follow-up this
> doc sets up — do it one repo at a time and prove the runner comes back
> online before touching the next.

---

## Who this is for

Exactly the repos in
[`config/arc-register-allowlist.json`](../../config/arc-register-allowlist.json):

- `FuzeOne/FuzeContact`
- `FuzeOne/FuzeHub`
- `FuzeOne/FuzeSales`
- `FuzeOne/FuzeService`
- `FuzeOne/FuzeSocial`
- `izzywdev/MendysRobotics`

**A repo not on that list is refused, loudly, before any kubectl/helm step
runs.** The handler does not trust the dispatch payload's claim about which
repo is asking — see the allowlist file's own docstring for why (short
version: `repository_dispatch` carries no field GitHub itself vouches for that
names the sending repo, so the claimed name is checked against this exact
allowlist, and the actual registration target — repo URL, scale-set name — is
read from the matching config entry, never from the payload). Onboarding a
7th repo is a reviewed PR to that JSON file, not a workflow change.

---

## What you need

**`FUZEINFRA_DISPATCH_TOKEN`** — the same fine-grained PAT (Contents: write on
FuzeInfra) your repo already holds if it uses `infra-request` or
`cluster-query`. If you don't have it yet, see
[`INFRA_REQUEST_DISPATCH.md`](../INFRA_REQUEST_DISPATCH.md#scoped-dispatch-token--fuzeinfra_dispatch_token)
for how to mint one. **No new credential, and — this is the point — no
`KUBE_CONFIG`.**

---

## Dispatch

```bash
# install (or upgrade in place — idempotent)
gh api --method POST repos/FuzeOne/FuzeInfra/dispatches \
  -f event_type=arc-register \
  -f 'client_payload[repo]=FuzeOne/FuzeHub' \
  -f 'client_payload[action]=install'

# uninstall
gh api --method POST repos/FuzeOne/FuzeInfra/dispatches \
  -f event_type=arc-register \
  -f 'client_payload[repo]=FuzeOne/FuzeHub' \
  -f 'client_payload[action]=uninstall'
```

`repo` must be spelled exactly as it appears in the allowlist (case-sensitive,
`owner/name`). `action` is `install` or `uninstall`; omitted defaults to
`install`.

### Watching the result

Neither dispatch event returns a run id, and the run takes a moment to appear:

```bash
RUN=$(gh run list --repo FuzeOne/FuzeInfra --workflow=arc-register.yml \
        --event=repository_dispatch --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" --repo FuzeOne/FuzeInfra --exit-status
gh run view  "$RUN" --repo FuzeOne/FuzeInfra --log
```

If your repo happens to listen for `repository_dispatch` events of type
`arc-register-result`, the handler also makes a best-effort dispatch back to
you with `{status, run_url, scale_set_name, action}` — best-effort, not
guaranteed (it needs FuzeInfra's cross-repo token to have write access to your
repo, and your repo isn't required to listen for it). The run URL above is the
guaranteed source of truth either way.

---

## What "success" actually means here

A green run does **not** mean "helm exited 0" or "pods are `Running`". Both of
those have been true in production while the runner was completely unusable —
see `runners/arc/ONBOARD-REPO.md` Troubleshooting section C (the dind
container's default `CMD` exits in 2–3 seconds without an explicit
`run.sh` entrypoint) and the "8 scale sets `Pending` for 11h" /
"every runner registered but stayed `online=0`" incidents in FuzeInfra's
project history.

The handler's `install` path only reports success after polling the **GitHub
runners API for your repo** — not FuzeInfra's — for up to 5 minutes until it
sees a runner carrying your scale-set's label report `status: online`:

```bash
gh api repos/FuzeOne/FuzeHub/actions/runners --jq '.runners[] | "\(.name) \(.status)"'
```

If that never happens, the job fails with the specific things to check next
(dind entrypoint, GitHub App installation coverage, node capacity) and dumps
the relevant `kubectl` output for the person triaging it.

The `uninstall` path likewise confirms the `AutoscalingRunnerSet` is actually
gone from the cluster afterward, not just that the `helm uninstall` command
returned.

---

## After registration

Use the scale set exactly as before — nothing about `runs-on:` changes:

```yaml
jobs:
  build:
    runs-on: fuzehub   # your repo's scale_set_name from the allowlist
```

Re-running `install` is safe (idempotent `helm upgrade --install`) — do this
to pick up a runner-image bump or a `register-repo.sh` change without
uninstalling first.

---

## What this does NOT do (yet)

- **It does not remove `KUBE_CONFIG` from your repo.** That is a deliberate,
  separate step — do not delete the secret until you've dispatched an
  `install` here and confirmed the runner is online, on this exact repo, at
  least once.
- **It does not edit your repo's `.github/workflows/arc-register.yml`.**
  Swapping your local copy (the one that runs `register-repo.sh` directly
  against your own `KUBE_CONFIG`) for one that dispatches here instead is the
  consumer-side migration, tracked separately per repo.
- **It is not a general cluster-write API.** It does exactly one thing:
  install/uninstall your repo's own `AutoscalingRunnerSet` in the shared
  `arc-runners` namespace, for repos on the allowlist above. For anything
  else that changes the cluster, use GitOps (your own `deploy/**`) or file an
  `@fuze` issue.

---

## Related docs

- [`CLUSTER_QUERY.md`](CLUSTER_QUERY.md) — the read-only `kubectl` sibling of this
  handler; same dispatch token, same "you don't need a kubeconfig" idea.
- [`../INFRA_REQUEST_DISPATCH.md`](../INFRA_REQUEST_DISPATCH.md) — the Terraform
  node-provisioning bridge and how to mint `FUZEINFRA_DISPATCH_TOKEN`.
- [`../../runners/arc/ONBOARD-REPO.md`](../../runners/arc/ONBOARD-REPO.md) — the
  full ARC runner reference: DinD, capacity, and the troubleshooting table this
  handler's verify step cites.
- [`../../config/arc-register-allowlist.json`](../../config/arc-register-allowlist.json) —
  the authorization source of truth.
