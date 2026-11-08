# Secrets Management (Sealed Secrets)

FuzeInfra owns the **secrets-management methodology** for every FuzeOne consumer.
This is the single source of truth for how secrets are sealed, committed, and
decrypted across the platform.

The model is **asymmetric**:

- **Sealing needs only the PUBLIC cert** — anyone can encrypt, offline.
- **Decryption needs the PRIVATE key** — held only by the in-cluster
  [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) controller.

So consumers **seal offline against a published public cert and commit only
encrypted blobs**. No consumer ever needs a kubeconfig, cluster credentials, or
the private key. Ciphertext in git is useless to anyone but the cluster.

> **TL;DR for a consumer repo**
> ```bash
> # zero cluster access required
> curl -fsSL https://sealed-secrets.prod.fuzefront.com/v1/cert.pem -o /tmp/cert.pem
> seal-secret.sh myproject/myproject-secrets DATABASE_URL=@./db-url.txt --out deploy/sealed/myproject-secrets.yaml
> git add deploy/sealed/myproject-secrets.yaml   # commit the ciphertext, nothing else
> ```

---

## 1. The published public cert (stable URL)

The controller's current public cert is served at a stable, always-current URL:

```
https://sealed-secrets.prod.fuzefront.com/v1/cert.pem
```

- **Single source of truth.** It is the live controller's `/v1/cert.pem`
  endpoint exposed through an Ingress, so it is **always current** — when the
  controller rotates its key (see [Rotation](#5-key-rotation)), the URL serves
  the new cert automatically. Never vendor a copy of the cert into a repo;
  always fetch it.
- **Public by design.** A public key can only *encrypt*. The endpoint is
  exempted from Cloudflare Access (it is the one `*.prod.fuzefront.com` host that
  does not require OTP) precisely because it is safe to expose to anyone — CI,
  scripts, and developers alike.
- **Override** with `--cert <url|file>` or the `FUZEINFRA_SEALED_CERT_URL`
  environment variable for air-gapped/offline sealing against a pinned cert file.

How it is wired (for operators):

| Piece | Where |
|-------|-------|
| Controller install + cert Ingress | `argocd/applications/sealed-secrets.yaml` (Bitnami chart, `kube-system`) |
| Public-cert Access bypass | `terraform/contabo/cloudflare.tf` → `sealed_secrets_cert` |
| Apply / re-sync | `terraform/contabo/provisioning.tf` |

The wildcard `*.prod.fuzefront.com` already routes to Traefik, so the Ingress
host rule is all that's needed to publish the endpoint.

---

## 2. Per-service SealedSecrets (least privilege)

**Seal one SealedSecret per service, not one shared kitchen-sink Secret.**

A SealedSecret is **strict-scoped** by default: it only decrypts under the exact
`namespace` + `name` it was sealed for. Per-service secrets give you:

- **Least privilege** — a service mounts only its own secret; a leak or a
  compromised pod can't read another service's credentials.
- **Independent rotation** — re-seal `billing-secrets` without touching
  `auth-secrets`.
- **Clear ownership** — each secret maps to one service and one chart.

Name them after the service that consumes them:

```
fuzefront/billing-secrets        # STRIPE_*, BILLING_INTERNAL_TOKEN
fuzefront/auth-secrets           # JWT_SECRET, OAUTH_CLIENT_SECRET
myproject/myproject-secrets      # DATABASE_URL, REDIS_URL, ...
```

Do **not** create a single `myproject/all-secrets` that every workload mounts.

---

## 3. Seal offline with `seal-secret.sh`

The canonical tool lives at [`scripts/seal-secret.sh`](../scripts/seal-secret.sh).
It fetches the current public cert, builds the Secret **in memory**, seals it,
and writes/merges the SealedSecret manifest in place. It **never prints
plaintext** and **never needs cluster access**.

Requirements: `bash`, `kubeseal`, `curl`, and `base64`/`openssl`. **No kubectl.**

```bash
# Seal literals into a new manifest (scope = ns/name):
scripts/seal-secret.sh fuzefront/billing-secrets \
  STRIPE_SECRET_KEY=@./stripe.key \
  BILLING_INTERNAL_TOKEN=@./billing.token \
  --out deploy/sealed/billing-secrets.yaml

# Add/rotate a single key — merged into the existing file in place:
scripts/seal-secret.sh fuzefront/billing-secrets \
  STRIPE_WEBHOOK_SECRET=@./whsec.txt \
  --out deploy/sealed/billing-secrets.yaml

# Seal an entire dotenv file:
scripts/seal-secret.sh myproject/myproject-secrets --env-file .env.prod \
  --out deploy/sealed/myproject-secrets.yaml

# Raw mode — print one encrypted blob for manual templating:
scripts/seal-secret.sh --raw myproject/myproject-secrets API_KEY=@./api.key
```

Guidance:

- **Prefer `KEY=@file` or `--env-file`** over `KEY=value` on the command line —
  argv is visible to other processes on the machine.
- The output manifest is **safe to commit**. Delete the plaintext inputs after
  sealing.
- `--cert <url|file>` / `FUZEINFRA_SEALED_CERT_URL` override the cert source for
  offline use.

The resulting SealedSecret is mounted by your Deployment exactly like a normal
Secret (`envFrom`/`secretKeyRef`) — see
[`DEPLOYING_A_SERVICE_TO_K8S.md` §5](./DEPLOYING_A_SERVICE_TO_K8S.md#5-secrets-the-sealedsecrets-workflow).

---

## 4. Decryption is cluster-only

Nothing outside the cluster can decrypt a SealedSecret. The controller in
`kube-system` holds the private key, watches for `SealedSecret` objects, and
materializes the real `Secret` at sync time. Consequences:

- **No consumer holds the private key or a kubeconfig.** FuzeFront and every
  other consumer are **GitOps-only**: they commit ciphertext; Argo CD + the
  controller do the rest.
- You **cannot** `unseal` a committed blob locally to recover plaintext — that is
  the point. Keep the original plaintext in your own secret manager (or
  regenerate it), not in git.
- The **private key never leaves the cluster** and is backed up by the operator
  out-of-band (see below).

> **Delivering a value to another repo — never by reading it back.** If a
> consumer repo needs a credential FuzeInfra owns, do NOT recover the plaintext
> and pass it along. FuzeInfra publishes it **pre-sealed** for the consumer's
> namespace + Secret name, automatically, via
> [`CREDENTIAL_HANDOFF.md`](./CREDENTIAL_HANDOFF.md). That path exists precisely
> because `cluster-query` (correctly) refuses to read Secrets into a job log.

### Recovering a live value (operator only)

You cannot unseal the committed blob, but the *materialized* `Secret` in the
cluster still holds the plaintext, so an operator with node access can read it
back. Pull the kubeconfig off the node and read the key directly:

```bash
ssh root@<server_ip> "cat /etc/rancher/k3s/k3s.yaml" \
  | sed 's/127\.0\.0\.1/<server_ip>/g' > ~/.kube/contabo-prod.yaml
chmod 600 ~/.kube/contabo-prod.yaml

KUBECONFIG=~/.kube/contabo-prod.yaml kubectl -n fuzeinfra get secret <name> \
  -o jsonpath='{.data.<KEY>}' | base64 -d; echo
```

`server_ip` is Terraform's output; SSH is key-based (the public key is injected
via cloud-init in `terraform/contabo/vps.tf`), so there is no root password to
know. If the private key is lost, recovery is Contabo's control panel — or
rotation, which needs no cluster access at all.

**Do not route this through `cluster-query`.** That workflow echoes kubectl output
into a GitHub Actions job log, which is retained and readable by anyone with repo
access — so `get secret -o jsonpath=…` there leaks the credential even though it
is only a read. It is blocked for that reason
(`.github/workflows/cluster-query.yml`, covered by
`tests/test_cluster_query_guard.py`), as is `--raw` (`kubectl config view --raw`
prints the runner's cluster-admin kubeconfig). SealedSecrets *are* readable there,
being encrypted at rest — see
[`consuming-repos/CLUSTER_QUERY.md`](./consuming-repos/CLUSTER_QUERY.md) for what
`cluster-query` *is* for.

Treat any value recovered this way as exposed to whatever it passed through, and
rotate if that set is wider than you want.

---

## 5. Key rotation

The controller rotates its sealing key on a schedule (default ~30 days),
**adding** a new key while keeping old ones so previously-sealed secrets still
decrypt. Because consumers always fetch the cert from the published URL, rotation
is transparent: the next seal automatically uses the newest key. You do **not**
need to re-seal existing secrets when the key rotates.

You only re-seal when a secret's **value** changes. That must be a hands-off
automated primitive — never a manual runbook.

### Automated value rotation (`rotate-sealed-secret.yml`)

`.github/workflows/rotate-sealed-secret.yml` is the self-service rotation gate.
Trigger it (UI → Actions → *rotate-sealed-secret* → Run, or
`gh workflow run rotate-sealed-secret.yml -f scope=<ns>/<name> -f key=<KEY> -f manifest=<path>`)
and it:

1. generates a fresh value (`openssl rand -hex` + whitespace-scrubbed) — or takes
   an explicit `value` input;
2. seals it **offline** against the published public cert via `seal-secret.sh`
   (no cluster access, plaintext never logged) and merges it into the manifest;
3. opens a PR that **auto-merges on green** → Argo CD syncs the SealedSecret →
   the controller decrypts it → the Secret updates.

For the consumer to pick up the new value automatically, it must carry a
**reloader trigger** so its pods restart on the Secret change. (If none is
present, pass `reload_argocd_app=<app>` to have the workflow hard-refresh + sync
that Argo app.) The result: rotating a secret is **fire-the-workflow → done**,
with no human running `seal-secret.sh` by hand.

> **Use a `checksum/` annotation, not stakater/reloader.** Earlier revisions of
> this page offered both. Only one of them works here: the
> [stakater/reloader](https://github.com/stakater/Reloader) controller is **not
> deployed on this cluster** — it appears nowhere in `helm/` or `argocd/`, only
> in this document. Its annotation is therefore inert decoration that *reads* as
> a working rotation trigger, which is the most dangerous way for a rotation to
> fail: the Secret updates, the pod keeps running on the old credential, and
> nothing anywhere says so.
>
> Hash the source files in the pod template instead — no controller required:
>
> ```yaml
> template:
>   metadata:
>     annotations:
>       checksum/my-secret: {{ .Files.Get "sealed-my-secret.yaml" | sha256sum }}
> ```
>
> Two things to know. The hash is over the **sealed** file, and `kubeseal` is
> non-deterministic, so re-sealing an unchanged value still rolls the pod once —
> a cheap false positive, and much preferable to missing a real rotation. And
> verify the wiring rather than trusting it: change a byte in the sealed file,
> re-render, and confirm the digest actually moves.
>
> If Reloader is ever installed, revisit this note rather than assuming the
> annotation started working.

This is the family-standard primitive — replicate the workflow in each repo (like
`claude.yml` / `auto-merge.yml`) pointed at that repo's SealedSecret. An agent's
remediation for a bad/leaked secret should be *"triggered rotation → PR #N →
synced"*, never *"please run this script."*

`seal-secret.sh` remains available for the rare interactive case, but rotation
should go through the workflow.

**Operator responsibilities** (not consumers):

- **Back up the private key** so the controller can be restored to a new cluster
  without invalidating every committed SealedSecret:
  ```bash
  kubectl get secret -n kube-system \
    -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key-backup.yaml
  # store OUT-OF-BAND (not in git); restore with `kubectl apply` before reinstalling the controller
  ```
- **Disaster recovery**: restore the backed-up key Secret, then let Argo CD
  re-install the controller — all existing ciphertext decrypts again.
- If the private key is ever **compromised**, rotate it, then **re-seal every
  secret** against the new published cert and rotate the underlying credentials.

---

## 6. Distribution via the onboarding toolkit

The tool + cert are distributed to consumers so a joining repo is self-sealing on
day one — **no cluster access** required:

1. **Vendor the script.** Copy [`scripts/seal-secret.sh`](../scripts/seal-secret.sh)
   into the consumer repo (e.g. `deploy/scripts/seal-secret.sh`) or call it from a
   FuzeInfra checkout. It is standalone — only `kubeseal`/`curl`/`bash` needed.
2. **Point at the published cert.** The script already defaults to the published
   URL; consumers can set `FUZEINFRA_SEALED_CERT_URL` to pin or override it.
3. **Commit only ciphertext.** Add `deploy/sealed/*.yaml` to git; never commit
   plaintext `Secret` manifests or `.env` files (add them to `.gitignore`).

For the **fuzeone onboarding toolkit**, fetch both in one step:

```bash
# in a joining repo's bootstrap
curl -fsSL https://raw.githubusercontent.com/FuzeOne/FuzeInfra/main/scripts/seal-secret.sh \
  -o deploy/scripts/seal-secret.sh && chmod +x deploy/scripts/seal-secret.sh
export FUZEINFRA_SEALED_CERT_URL=https://sealed-secrets.prod.fuzefront.com/v1/cert.pem
```

That gives every onboarded repo offline self-sealing with zero cluster access.

---

## 7. GitHub **Actions** secrets (`secret-provision.yml`)

Everything above is about secrets bound for the **cluster**. GitHub *Actions* secrets are a
separate store with a separate problem: **no Claude session can write them.** The Anthropic
agent proxy refuses `/repos/*/actions/secrets` outright —

```
403 {"message":"Access to this GitHub Actions path is not permitted through this proxy."}
```

— because it injects *git* credentials only, never secret admin. The block is keyed on the
URL, so it is **not** fixable by adding a token to a session's environment, and every
managed-agent environment is `anthropic_cloud` behind the same proxy, so delegating to a
peer session does not help either.

A runner is not behind that proxy. `.github/workflows/secret-provision.yml` is therefore the
write path, dispatchable by any session (and by consumers via `repository_dispatch`, type
`secret-provision`, with the existing `FUZEINFRA_DISPATCH_TOKEN`). It holds
`SECRETS_ADMIN_PAT` — a fine-grained PAT with **Secrets: write** — so no session ever does.

### The one rule: a caller never transports a value

**This repo's job logs are public.** A secret value passed as a `workflow_dispatch` input or
a `client_payload` field is a published credential the moment anyone dispatches it — the
same lesson §4 records for `kubectl get secret`. So the workflow declares **no value-bearing
input at all**, and `tests/test_secret_provision_guard.py` fails the build if one is added.
The three sources are exactly those that need no caller-side plaintext:

| `source` | What it does | Use for |
|---|---|---|
| `generate` | mints a random **alphanumeric** value in-runner (a metachar like `&` breaks `airflow-init`) | passwords, webhook/shared secrets — anything the platform itself owns |
| `copy` | re-keys an existing secret under a new name, disclosing nothing | renames, fanning one credential out to a second consumer |
| `verify` | reports **presence + `updated_at`** only, never the value | confirming a human-injected key landed |

A `PROTECTED` list makes the workflow unable to write its own PAT, `KUBE_CONFIG`,
`FUZEINFRA_DISPATCH_TOKEN` and peers — as target *or* as `copy_from`, since re-keying a
privileged credential under a caller-chosen name is a lateral move even though a copy prints
nothing.

### Provisioning into another repository (`target_repo`)

`SECRETS_ADMIN_PAT` is scoped to **all** repositories on purpose, so each product's agents
can provision through this one guarded workflow instead of every repo holding its own
secret-writing credential. That scope means the token can reach repositories this workflow
has no business touching, so **what decides where a dispatch lands is the allowlist, not the
token**: `governance/secret-provision-targets.json`. Omit `target_repo` and it writes here.

Adding a repository there **is a real grant** — it lets anyone who can dispatch this
workflow (including via `repository_dispatch` with the widely distributed
`FUZEINFRA_DISPATCH_TOKEN`) create and overwrite that repository's Actions secrets. Add it
in a reviewed commit with a one-sentence `why`; an entry nobody can justify is an entry to
remove.

Two rules survive regardless of what is listed:

- **`copy` is same-repo only.** Copying cross-repo would move *this* repository's secret
  values into another one — exfiltration wearing a re-key's clothes. Off-repo dispatches may
  `generate` a fresh value or `verify` presence. `PROTECTED` still applies to every target.
- **The write uses the validated target.** The `Provision` step reads `env.TARGET`, set by
  the guard *after* the allowlist check — never the dispatch payload again. Re-reading the
  caller's value at the point of use would let the check pass on a listed repo while the
  write went somewhere else.

### Third-party keys (Twilio, Mailjet, …) — breaking the apparent loop

"Read it from a vault" looks circular: the agent needs a credential to fetch the credential.
It isn't a loop, because **one injection by a human is irreducible** — and it's also only
needed once, in one place. Pick by destination:

- **Bound for the cluster → don't put it in GitHub at all.** Seal it offline with
  `scripts/seal-secret.sh` (§3) on your own machine. Sealing is *public-key* encryption
  against the committed cert, so it needs no cluster access and no GitHub secret; commit the
  ciphertext and Argo delivers it. The plaintext never touches GitHub, a log, or an agent.
  **This is the default answer** and it removes the loop entirely.
- **Genuinely needed by CI → paste it once into the GitHub UI** (Settings → Secrets and
  variables → Actions), then have the agent confirm with `source=verify`. The value crosses
  exactly one boundary, browser → GitHub, with no agent and no log in the path.
- **Already in Actions and needed in-cluster → `seal-contabo-api-credentials.yml`** is the
  worked pattern: a runner reads the existing Actions secrets, seals them offline, and opens
  a PR with only ciphertext.

What an agent must **never** do is ask you to paste a key into chat, or accept one as a
workflow input. Both publish it — the first into a transcript, the second into a public log.

---

## 8. Rules

- **Never** commit a plaintext `Secret` manifest or a `.env` with real values.
- **One SealedSecret per service** — least privilege, independent rotation.
- **Always fetch** the cert from the published URL; never hardcode a vendored cert.
- **Seal offline**; decryption is **cluster-only**.
- **No consumer gets a kubeconfig** — sealing needs only the public cert.
- **Never read a `Secret` through a CI workflow.** Read-only is not the same as
  safe-to-log: job logs are retained and repo-readable. Use the operator SSH path
  in §4, or rotate.
- **Never pass a secret value as a workflow input**, and never paste one into an agent
  session. Use `secret-provision.yml`'s value-free sources or offline sealing (§7).

## Related docs

### GHCR package-pull credentials

Private GHCR pulls use the dedicated GitHub machine user `fuze-registry-bot`, not a
repository collaborator and not a human personal token. The bot's classic PAT is limited
to `read:packages` and is stored only as `GHCR_PULL_USERNAME` and `GHCR_PULL_TOKEN` in
FuzeInfra's protected `registry-credentials` Environment.

Run **provision-registry-bot-ghcr-pull** with `verify_only=true` and the exact deployed
image before changing a namespace. A successful probe proves that the package grant and
token work without reading a cluster Secret. Re-run with `verify_only=false` only after
review; it opens a PR containing a SealedSecret, and `apply-cluster-config` delivers it
after merge. Never place the token in a dispatch input, manifest, or chat.

The environment must retain reviewer approval and its branch policy. The workflow does not
need cluster credentials: it encrypts against the public Sealed Secrets certificate.

- [`DEPLOYING_A_SERVICE_TO_K8S.md`](./DEPLOYING_A_SERVICE_TO_K8S.md) — how a service
  consumes the sealed secret in its Deployment.
- [`CREDENTIAL_HANDOFF.md`](./CREDENTIAL_HANDOFF.md) — the automated, plaintext-free
  hand-off of a FuzeInfra-owned credential to a consumer repo, plus the periodic
  auth check that catches a stale one before it becomes an outage.
- [`gitops.md`](./gitops.md) — how Argo CD delivers FuzeInfra (and the controller).
- [Bitnami Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) — upstream project.
