# `cluster-query` — read-only cluster access for consuming repos

**You can inspect the prod cluster yourself.** You do not need a kubeconfig, an
operator, or a human to paste `kubectl` output back to you. FuzeInfra publishes a
dispatchable read-only introspection workflow —
[`.github/workflows/cluster-query.yml`](../../.github/workflows/cluster-query.yml) —
that any repo with a FuzeInfra dispatch credential can trigger.

This is the one thing a consumer may do **directly against the cluster**. Everything
that *changes* the cluster stays GitOps or `@fuze`-delegated (see
[`CONSUMER_ONBOARDING_SHARED_CLUSTER.md`](../CONSUMER_ONBOARDING_SHARED_CLUSTER.md)) —
`cluster-query` is read, and only read.

> **Why it exists.** "Is my app actually running in prod?" used to be answered by a
> human (or a Cowork session) relaying `kubectl` output by hand. That relay is slow,
> lossy, and it tempted people to hand kubeconfigs around. `cluster-query` answers the
> question directly without any session ever holding the raw kubeconfig — the
> credential stays in FuzeInfra's CI, and the caller gets output, not access.

---

## 1. Before you dispatch: the job log is PUBLIC

`FuzeOne/FuzeInfra` is a **public** repo (`class: oss-public`). Actions job logs on a
public repo are readable by **anyone on the internet**, and they are retained.

Whatever you query is published. That is the single rule that governs how you use
this workflow:

- **Never query anything whose *output* is a credential**, even though reading it is a
  read. Reading `Secret` objects is blocked outright for exactly this reason (§4) —
  but the guard cannot know that *your* app logs a token, so `logs` is on you.
- Prefer the narrowest query that answers your question (`-n <ns> get pods` beats
  `get all -A -o yaml`).
- If something sensitive does land in a log, deleting the run is **after-the-fact** —
  treat the value as exposed and rotate it.

---

## 2. What you need — almost certainly nothing new

Split the two halves, because they have different answers:

**Reading the result needs no credential at all.** FuzeInfra is a public repo, so its
run metadata and job logs are world-readable — `GET /repos/FuzeOne/FuzeInfra/actions/runs/<id>`
answers `200` unauthenticated. Nothing needs an Actions grant to *read* an answer.

**Triggering** needs a credential, and there are two events, so two ways in:

| Event | Permission on FuzeInfra | Who already has it |
|---|---|---|
| **`repository_dispatch`** (preferred) | **Contents: write** | any repo holding **`FUZEINFRA_DISPATCH_TOKEN`** — the infra-request token from [`INFRA_REQUEST_DISPATCH.md`](../INFRA_REQUEST_DISPATCH.md) |
| `workflow_dispatch` | **Actions: write** | a classic PAT with `repo`, or a fine-grained PAT / App token scoped to FuzeInfra |

**Use `repository_dispatch` and reuse the token you already have.** It is the same
secret that fires infra-requests — no new credential, and no repo-wide Actions grant.
It is also *less* privilege than it sounds: a token that can trigger `infra-request`
can already apply Terraform and create VPS nodes, so letting it run a guarded
read-only `kubectl` reduces relative privilege rather than escalating it.

```bash
gh api --method POST repos/FuzeOne/FuzeInfra/dispatches \
  -f event_type=cluster-query \
  -f 'client_payload[kubectl_args]=-n <my-namespace> get pods -o wide'
```

A ready-made consumer workflow that dispatches this and prints the answer back into
your own job log is at
[`docs/workflows/consumer/cluster-query.yml`](../workflows/consumer/cluster-query.yml) —
drop it at `.github/workflows/cluster-query.yml`.

<details>
<summary>If you have no FuzeInfra token at all</summary>

Either mint one (a human step — GitHub has no API for creating PATs):

1. GitHub → **Settings → Developer settings → Fine-grained personal access tokens → Generate new token**
2. **Resource owner:** `izzywdev` · **Repository access:** *Only select repositories* → **FuzeInfra**
3. **Repository permissions → Contents: Read and write** (leave everything else *No access*)
4. Short expiry + a rotation reminder
5. `gh secret set FUZEINFRA_DISPATCH_TOKEN --repo izzywdev/<your-repo>`

…or skip the credential entirely and **delegate**: open an `@fuze` issue on
FuzeInfra asking for the read. That path always works; it is only slower.

</details>

> Choosing `workflow_dispatch` instead? Note the grant is repo-wide — GitHub cannot
> scope Actions:write to a single workflow, so such a token can dispatch *any*
> `workflow_dispatch` workflow in FuzeInfra, including deploys. That is why
> `repository_dispatch` is the documented default.

---

## 3. Dispatch and read the result

```bash
# 1. fire the query (repository_dispatch — FUZEINFRA_DISPATCH_TOKEN)
gh api --method POST repos/FuzeOne/FuzeInfra/dispatches \
  -f event_type=cluster-query \
  -f 'client_payload[kubectl_args]=-n mendys-prod get pods -o wide'

# 2. grab the run it created, wait for it, print the output
RUN=$(gh run list --repo FuzeOne/FuzeInfra --workflow=cluster-query.yml \
        --event=repository_dispatch --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" --repo FuzeOne/FuzeInfra --exit-status
gh run view  "$RUN" --repo FuzeOne/FuzeInfra --log
```

> Neither dispatch event returns the run id, and the run takes a moment to appear — if
> step 2 comes back with an older run, wait a few seconds and re-list. The `concurrency`
> group serializes runs, so they queue rather than interleave.

### If `gh run view --log` returns 403, read the ANNOTATIONS instead

Step 2 above is the normal route, but it is not available everywhere. Job logs are served
from `results-receiver.actions.githubusercontent.com` and from **sharded**
`productionresultssa*.blob.core.windows.net` hosts (`sa6`, `sa10` and `sa15` were all
observed serving one batch of three jobs). An agent session behind a restrictive egress
policy gets `403` on CONNECT to those and cannot fetch a log at all — so for a while the
session that dispatched a query could not read its own answer. "FuzeInfra is public, so
the logs are world-readable" is true about *permissions* and says nothing about whether
your environment can reach the host.

The workflow therefore also mirrors the kubectl output into **check-run annotations**,
which come back from `api.github.com`:

```bash
gh api "repos/FuzeOne/FuzeInfra/actions/runs/$RUN/jobs" --jq '.jobs[].check_run_url' \
  | while read u; do gh api "${u#https://api.github.com/}/annotations" --jq '.[].message'; done
```

Same content, same visibility, different transport. The mirror is capped (200 lines,
20 per annotation — `CQ_ANNOTATE_MAX_LINES` / `CQ_ANNOTATE_CHUNK`); the job log stays
complete and authoritative, so prefer it when you can reach it.

If you hold an **Actions: write** token instead, the `workflow_dispatch` form is
equivalent — same guard, same output — and both inputs are optional there, so
dispatching with neither runs the `get nodes` default:

```bash
gh workflow run cluster-query.yml --repo FuzeOne/FuzeInfra \
  -f kubectl_args='-n mendys-prod get pods -o wide'
```

### Optional second input — `curl_url`

Probe a public URL from inside the cluster's network path (`curl -sSIL`, headers only):

```bash
gh api --method POST repos/FuzeOne/FuzeInfra/dispatches \
  -f event_type=cluster-query \
  -f 'client_payload[kubectl_args]=-n mendys-prod get ingress' \
  -f 'client_payload[curl_url]=https://www.mendysrobotics.com/'
```

Only `*.mendysrobotics.com` and `*.prod.fuzefront.com` are allowlisted; anything else
fails the step. Adding a hostname is a FuzeInfra change — delegate via `@fuze`.

---

## 4. What is allowed, and what is refused

A dispatch must contain **at least one read verb**, and must contain **no** mutating or
exec token, and must not name the `Secret` resource. All three checks run before
`kubectl` is invoked; the invariants are executable in
[`tests/test_cluster_query_guard.py`](../../tests/test_cluster_query_guard.py).

**Read verbs (one must be present):**
`get` · `describe` · `logs` · `top` · `events` · `version` · `api-resources` ·
`api-versions` · `explain` · `cluster-info` · `config`

**Refused anywhere in the args — mutation and exec:**
`exec` · `attach` · `cp` · `port-forward` · `proxy` · `run` · `delete` · `apply` ·
`edit` · `patch` · `replace` · `scale` · `rollout` · `cordon` · `uncordon` · `drain` ·
`annotate` · `label` · `set` · `create` · `taint` · `debug`

**Refused — reads whose output is a credential:**

- **`Secret` objects are blocked**, in every spelling (`secret/foo`, `pods,secrets`,
  `secrets.v1.`, `Secret`). `get secret -o jsonpath=…` is a read that prints the
  credential verbatim into a public log — this is not hypothetical, it happened on
  2026-07-29 and the run's logs had to be deleted after the fact. Read-only is not the
  same as safe-to-log. To recover a live secret value, use the operator SSH path in
  [`SECRETS_MANAGEMENT.md` §4](../SECRETS_MANAGEMENT.md#4-decryption-is-cluster-only),
  or just rotate it.
- **`SealedSecrets` are readable on purpose** — they are ciphertext at rest and useless
  to a reader, so `get sealedsecret <name> -o yaml` is allowed and is usually what you
  actually want when debugging a secret that isn't materializing.
- **`--raw` is blocked.** `kubectl config view --raw` would print the runner's
  cluster-admin kubeconfig into the public log.

A name that merely *contains* "secret" is fine — `describe deployment
litellm-secret-reader` and `logs job/fuzeinfra-sealed-secrets-sync` both pass.

---

## 5. Limits

- **Serialized.** The workflow has `concurrency: cluster-query` with
  `cancel-in-progress: false`, so queries queue behind each other rather than
  cancelling. Expect to wait if someone else is querying.
- **Truncated.** kubectl output is cut at 200 KB, curl output at 20 KB.
- **Self-hosted runners.** It runs on `runs-on: staging` (the ARC scale set), so it is
  independent of hosted-minutes exhaustion — but if the scale set is down, the run
  queues instead of failing fast.
- **One cluster.** The kubeconfig is the Contabo k3s prod cluster. There is no
  staging-cluster equivalent.
- **Read, not reach.** No `port-forward`, no `exec`, no `proxy` — you cannot open a
  channel into the cluster, only print things out of it.

---

## 6. When to use it

Use it whenever you would otherwise ask a human "what's it doing in prod?":

```bash
# did my Argo app actually sync?
-n argocd get applications
# are my pods up?
-n <my-namespace> get pods -o wide
# why is that pod not up?
-n <my-namespace> describe pod <pod>
-n <my-namespace> get events --sort-by=.lastTimestamp
# what did it say before it died?
-n <my-namespace> logs <pod> --previous --tail=200
# is my ingress wired to the tunnel?
-n <my-namespace> get ingress,svc,endpoints
```

This is the evidence half of the verification protocol for anything you ship to the
shared cluster: **a deploy is not verified because CI is green — it is verified
because the pods are `Running` and the endpoint answers.** Quote the run URL as your
evidence.

When the answer is "it's broken and needs a change," stop querying and go back to the
normal paths: your own `deploy/**` (GitOps) for your app, or an `@fuze` issue on
FuzeInfra for anything the platform owns.

---

## Related docs

- [`CONSUMER_ONBOARDING_SHARED_CLUSTER.md`](../CONSUMER_ONBOARDING_SHARED_CLUSTER.md) — how a product goes live on the shared cluster.
- [`DEPLOYING_A_SERVICE_TO_K8S.md`](../DEPLOYING_A_SERVICE_TO_K8S.md) — namespace, Argo Application, SealedSecrets, resource limits.
- [`SECRETS_MANAGEMENT.md`](../SECRETS_MANAGEMENT.md) — why Secret reads are blocked here and what to do instead.
- [`INFRA_REQUEST_DISPATCH.md`](../INFRA_REQUEST_DISPATCH.md) — the *write* side: declaring infra needs and dispatching them to FuzeInfra.
- [`../../CONTRACT.md`](../../CONTRACT.md) — FuzeInfra's stable service interface.
- [`../argo-selfheal-autofix.md`](../argo-selfheal-autofix.md) — the automatic loop that opens an `@fuze` issue when your app goes unhealthy.
