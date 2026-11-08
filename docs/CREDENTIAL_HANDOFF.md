# Automated credential hand-off (FuzeInfra → consumer repo)

How a credential FuzeInfra owns reaches a consumer repo **without any human, job
log, artifact or commit ever seeing the plaintext** — and how a stale one is
caught in hours instead of weeks.

This is the concrete, automated implementation of *Channel C* in
[SECURE_AGENT_SECRET_HANDOVER.md](SECURE_AGENT_SECRET_HANDOVER.md), built for
FuzeInfra [#510](https://github.com/FuzeOne/FuzeInfra/issues/510).

---

## 1. Why the old channel had to go, and what replaced it

Consumers used to refresh their sealed DB credential by dispatching
`cluster-query` with:

```
-n fuzeinfra get secret mendys-db-credentials -o json
```

…and scraping the value out of FuzeInfra's **job log**. On 2026-07-29
`cluster-query` was hardened to reject reading *any* Secret object, because that
log is retained and readable by anyone with repo access — a lesson learned when
`LITELLM_MASTER_KEY` was fetched exactly that way.

**That guard is correct and is not touched here.** But it removed the last
automated hand-off channel and nothing replaced it. MendysRobotics' sealed
`DATABASE_URL` silently froze on 2026-07-09 and stayed wrong until pods happened
to restart weeks later, at which point it became a 5–6 day outage
([#499](https://github.com/FuzeOne/FuzeInfra/issues/499) /
[#500](https://github.com/FuzeOne/FuzeInfra/issues/500), fixed by hand in
[MendysRobotics#273](https://github.com/izzywdev/MendysRobotics/pull/273)).

The fix inverts the direction of trust:

> The consumer never reads a Secret. **FuzeInfra reads its own Secret in-cluster
> and publishes the value already sealed.**

Sealed-Secret ciphertext is safe in a log, an artifact or a commit — only the
in-cluster controller's private key can decrypt it. And `kubeseal --scope strict`
binds the ciphertext to exactly one namespace **and** one Secret name, so a value
sealed for `mendys-prod/mendys-secrets` is inert everywhere else. That scoping is
a security feature, and it is also why the publisher needs a registry.

---

## 2. The registry — and why it lives here, not in the consumer

`governance/credential-handoff.json` (schema: `credential-handoff.schema.json`)
maps each source Secret to the consumer scope it may be sealed for:

```jsonc
{
  "id": "mendys-postgres",
  "enabled": true,
  "consumerRepo": "izzywdev/MendysRobotics",
  "source": { "namespace": "fuzeinfra", "secretName": "mendys-db-credentials", "secretKey": "password" },
  "target": {
    "namespace": "mendys-prod", "secretName": "mendys-secrets",
    "secretKey": "DATABASE_URL", "format": "postgres-url",
    "manifestPath": "deploy/argocd/sealed/mendys-secrets.yaml", "branch": "main"
  },
  "verify": { "engine": "postgres", "host": "fuzeinfra-postgres.fuzeinfra",
              "port": 5432, "database": "mendys", "username": "mendys_svc" }
}
```

**Decision: a standalone registry in FuzeInfra, not a field in the consumer's
`.fuze/manifest.json`.** Both were considered; this one wins for one reason that
outweighs the convenience of co-locating the declaration with the consumer:

- **The mapping *is* the authorization decision.** "Which FuzeInfra credential
  may be sealed into which namespace" is a grant, and a grant belongs to the
  grantor. If the consumer declared its own target, a compromised or merely
  careless consumer repo could name a *different* source — asking FuzeInfra to
  seal, say, the LiteLLM master key into a namespace it controls, which it could
  then read. Keeping the mapping here puts every change behind FuzeInfra's
  CODEOWNERS + ruleset review. It is the same rule that already puts
  `consumerRegistrationRbac` grants and AppProjects in this repo rather than in
  the consumers' charts.
- Secondary, practical reasons: no cross-repo fetch (and no token) is needed to
  decide what to publish; it is machine-readable and schema-validated offline by
  `tests/test_credential_handoff.py`; and it sits beside the allocation registry
  it complements (`governance/datastore-allocations.md`).

The consumer still owns its half — it owns the manifest file the ciphertext is
merged into, and it merges the PR. Nothing lands in a consumer repo without a
consumer-side merge.

`format` exists because consumers do not all consume a bare password.
MendysRobotics consumes a composed `DATABASE_URL`, so the publisher renders
`postgresql://<user>:<password>@<host>:<port>/<db>` from the `verify` block
before sealing. `raw` seals the password verbatim.

---

## 3. Publishing (`publish-sealed-handoff.yml`)

```
fuzeinfra/<source-secret>  ──read in-cluster──▶  render per `format`
        │                                              │
        │                                     kubeseal --scope strict
        │                                     for <target ns>/<target name>
        ▼                                              ▼
 sha256[:16] fingerprint                    kubeseal --merge-into <manifestPath>
        │                                              │
        └────────────── compared ──────────▶  PR on the consumer repo (ciphertext only)
```

- **`--merge-into`, never regenerate.** It updates exactly the one
  `encryptedData` key and leaves every other key byte-identical. #499 recorded
  the alternative's cost: MendysRobotics' force-reseal path composes only 16 of
  24 keys, so regenerating the manifest would *drop* 8 out-of-band ones. That is
  why the manual fix in #273 touched a single key.
- **The manifest must already exist** in the consumer repo. The publisher
  refuses to create it — establishing a Secret is the consumer's decision.
- **The branch name carries the fingerprint** (`handoff/<id>-<fp>`), so a run can
  never collide with an earlier attempt and never needs a force-push. An open PR
  for the same fingerprint is detected and not duplicated.

### Rotation-driven, not schedule-driven

Each run compares the fingerprint of the **rendered source value** with the
fingerprint of what the consumer's **live** Secret holds. Equal → the run exits
having created no branch, no commit and no PR. So a commit is only ever produced
by an actual divergence, i.e. a rotation.

The trigger is nevertheless a 6-hourly schedule rather than a push on
`deploy/sealed-secrets/**`, and that is deliberate: at push time Argo CD has not
synced yet, so the cluster still holds the **old** value and a push-triggered run
would see "in sync" and never republish. A reconciler that no-ops unless
something really diverged is immune to that race and cannot churn commits.

---

## 4. Detection (`verify-consumer-credentials.yml`)

A working channel is not enough — a channel can break again, and it will break
silently for the same reason it broke the first time. So every 6 hours:

1. Read the credential the consumer **actually holds** (the live Secret in the
   consumer namespace — whatever Argo last decrypted from their committed
   SealedSecret).
2. Port-forward the datastore and **attempt a real authentication** with it
   (`SELECT 1`).
3. On failure: file a deduped issue in the owning repo, send the Telegram 🔴, and
   ask `publish-sealed-handoff` to re-publish so the common case self-heals into
   a ready-to-merge PR.

Applied to #499 this fires on **day one** — and, the part that actually matters,
**before any pod restarts**, i.e. before there is an outage at all.

Alert routing reuses what the platform already does, rather than inventing a
channel: the owning repo is resolved from the `fuzeinfra.io/owner-repo`
annotation on the consumer's namespace, exactly as `grafana-crit-fix.yml` does
(falling back to the registry's `consumerRepo`, then to FuzeInfra), and the
Telegram alert uses the same `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` as
`argo-outofsync-autofix.yml`.

---

## 5. What never leaves the cluster

`scripts/credential-handoff.sh` is the only place a value is handled. Three
things — and only these — are ever printed:

| Emitted | Why it is safe |
|---|---|
| SealedSecret ciphertext | Inert outside the target cluster; only the controller's private key decrypts it |
| `sha256(value)[:16]` | One-way; the convention already used in `SECURE_AGENT_SECRET_HANDOVER.md` |
| A pass/fail verdict + psql's own error text | Names the role and the failure mode, never the password |

Values live only in files created under `umask 077` in a temp dir wiped on exit.
The password reaches `psql` through `PGPASSWORD` — the **environment**, never
`argv`, which is visible in `ps` — and reaches `kubeseal` as a `KEY=@file`
reference for the same reason.

---

## 6. Onboarding a new consumer

1. The consumer's Secret + SealedSecret manifest must already exist in their repo
   (they own it), and their namespace should be annotated
   `fuzeinfra.io/owner-repo=<owner>/<repo>` so alerts route to them.
2. Add an entry to `governance/credential-handoff.json` and merge it. Adding it
   is the grant — review it as one.
3. Run `publish-sealed-handoff` (or wait up to 6h). Merge the PR it opens on the
   consumer repo.
4. Confirm with `verify-consumer-credentials` (`workflow_dispatch`, `id=<your-id>`).

## 7. Known gaps

- **Only `engine: postgres` is verifiable today.** A hand-off with no `verify`
  block is still published, but is not covered by the detector.
- **There is no Mongo credential in the mendys hand-off Secret.**
  `fuzeinfra/mendys-db-credentials` holds a single `password` key (Postgres);
  `mendys-mongo-credentials` is a separate Secret that the consumer's automation
  never read, despite the consumer runbook implying a `MONGODB_URL` is
  propagated. Nothing here invents one — it needs its own registry entry once
  someone confirms which role it belongs to.
- **`fuzequality-db-credentials` / `fuzequality-chroma-credentials` have no
  matching repo** (`izzywdev/FuzeQuality` 404s; FuzeQuality is maintained inside
  FuzeFront). They are not registered here.
- Superseded hand-off PRs are not auto-closed when a newer rotation opens a new
  one.

## Related

- [SECRETS_MANAGEMENT.md](SECRETS_MANAGEMENT.md) — the sealing methodology
- [SECURE_AGENT_SECRET_HANDOVER.md](SECURE_AGENT_SECRET_HANDOVER.md) — the channel taxonomy this implements
- [crit-log-autofix.md](crit-log-autofix.md) — the per-repo alert-routing rule reused here
