# Datastore allocation registry

Names, prefixes, and indexes only — **never credentials**. Update in the same
PR (or provisioning run) that creates an allocation. See
`governance/datastore-provisioning.md` for the process.

## Postgres (shared `fuzeinfra-postgres`)

| App | Role | Database | Consumer repo | Status |
|---|---|---|---|---|
| fuzex | `fuzex_svc` | `fuzex_design_frames` | FuzeOne/FuzeX | declared; generic `provision-service-postgres.yml` seals and enables the runtime declaration atomically, then `fuzex-postgres` handoff delivers the consumer URL |
| fuzekeys | `fuzekeys_user` | `fuzekeys` | FuzeOne/FuzeKeys | active (FuzeInfra#136) |
| fuzesales | `fuzesales_svc` | `fuzesales` | FuzeOne/FuzeSales | declared (FuzeInfra#153) |
| fuzecontact | `fuzecontact_svc` | `fuzecontact` | FuzeOne/FuzeContact | declared (FuzeInfra#153) |
| fuzeservice | `fuzeservice_svc` | `fuzeservice` | FuzeOne/FuzeService | declared (FuzeInfra#153) |
| fuzesocial | `fuzesocial_app` | `fuzesocial` | FuzeOne/FuzeSocial | active (FuzeInfra#150) — owner+grants verified; consumer-authoritative credential in `fuzesocial/fuzesocial-secrets:DB_PASSWORD` |
| fuzequality | `fuzequality_user` | `fuzequality` | FuzeOne/FuzeFront (`FuzeQuality`) | enabled in prod GitOps; automated consumer hand-off declared |
| authentik-mendys | `authentik_mendys_user` | `authentik_mendys` | FuzeOne/FuzeFront (MendysRobotics IdP silo) | declared — gated off until FuzeFront seals `authentik-mendys-db-credentials` for the `fuzeinfra` namespace |
| fuzehub | `fuzehub_svc` | `fuzehub` | FuzeOne/FuzeHub | declared |

> `authentik_mendys` backs a **second Authentik instance** deployed by the
> FuzeFront chart, serving MendysRobotics as an isolated identity silo
> (`live.mendysrobotics.com` + `marketplace.mendysrobotics.com`). It is a
> separate database — not a schema and not a brand — because Authentik has no
> realm: one instance is one user directory, brands are branding-only, and
> schema-per-tenant is Enterprise/alpha/API-managed and therefore incompatible
> with this platform's blueprint-GitOps model. FuzeFront accounts and
> MendysRobotics accounts are unrelated in both directions; the same email may
> exist independently in each.

> `fuzesales` / `fuzecontact` / `fuzeservice` are provisioned **declaratively**
> by the `fuzeinfra-service-db-provision` hook Job (chart values
> `serviceDatabases` in `helm/fuzeinfra/values.yaml`, enabled in
> `values-contabo.yaml`) — the GitOps successor to the imperative recipe in
> `datastore-provisioning.md`. Each stays `enabled: false` until its consumer
> repo seals a `<app>-db-credentials` Secret (key `password`) FOR the
> `fuzeinfra` namespace, matching the password behind its own `DATABASE_URL`.

## MariaDB (shared `fuzeinfra-mariadb`)

MySQL-protocol engine for consumers that cannot use Postgres (WordPress,
Laravel, ...). Provisioned declaratively by the `fuzeinfra-service-mariadb-provision`
PostSync Job from `serviceMariadbDatabases`. Each user gets
`ALL PRIVILEGES ON <database>.*` and nothing else.

| App | User | Database | Consumer repo | Status |
|---|---|---|---|---|
| mendys-wp | `mendys_wp_svc` | `mendys_wp` | izzywdev/mendys-wp | **declared, gated off** — waiting on the consumer to seal `mendys-wp-db-credentials` (key `password`) FOR the `fuzeinfra` namespace |

> mendys-wp currently runs its **own** `mendys-wp-mariadb` StatefulSet in the
> `mendys-wp` namespace, whose PVC was on `local-path` and was destroyed by a
> node reinstall. That is the drift this engine exists to close. Migration is a
> two-step handshake owned by the consumer repo — seal the credential first,
> flip `enabled: true` second, in the same PR as the sealed Secret. See
> `docs/consuming-repos/MARIADB_PROVISIONING.md`.

Host: `fuzeinfra-mariadb.fuzeinfra.svc.cluster.local:3306`

## Redis (shared `fuzeinfra-redis`)

| App | ACL user | Key prefix | DB index | Consumer repo | Status |
|---|---|---|---|---|---|
| fuzekeys | `fuzekeys` | `fuzekeys:` | 1 | FuzeOne/FuzeKeys | active (FuzeInfra#136) |
| authentik-mendys | (shared password) | authentik-internal | 2 | FuzeOne/FuzeFront (MendysRobotics IdP silo) | declared |

DB index 0 is reserved for FuzeInfra platform services.

> `authentik-mendys` takes an **index, not an ACL user**: authentik owns its own
> key layout and does not prefix keys, so a prefix-scoped ACL cannot be applied
> to it. The index matters for correctness rather than security — authentik
> keeps cache and sessions in Redis under non-namespaced key names, so the
> Mendys instance must not share index 0 with the FuzeFront authentik (which
> uses authentik's default of 0) or the two would collide and leak session
> state across the very boundary the separate instance creates.

## Neo4j (dedicated instance per consumer)

Neo4j Community edition cannot RBAC or multi-tenant; each consumer gets its own
StatefulSet (`fuzeinfra-neo4j-<name>`), PVC, and credential pair. Provision via
the `provision-<name>-neo4j.yml` workflow (generates sealed cred + pushes GH
secret + flips `serviceNeo4jInstances[<name>].enabled` gate — no manual steps).

| App | Instance name | Sealed secret | Consumer repo | Status |
|---|---|---|---|---|
| FuzePlan | `fuzeplan` | `neo4j-fuzeplan-credentials` | FuzeOne/FuzePlan | declared (FuzeInfra#157) — run `provision-fuzeplan-neo4j` workflow to activate |

Bolt address template: `bolt://fuzeinfra-neo4j-<name>.fuzeinfra.svc.cluster.local:7687`

> The shared `fuzeinfra-neo4j` instance (the original single-node) is a legacy
> integration path. New consumers MUST use their own dedicated instance; the
> shared instance will be deprecated as each consumer migrates.

## ChromaDB (shared `fuzeinfra-chromadb`)

| App | Tenant | Database | Bootstrap collection | Consumer repo | Status |
|---|---|---|---|---|---|
| FuzePlan repo-digester | `fuzeplan` | `repo-digester` | `repo_digester_ready` | FuzeOne/FuzePlan | declared (FuzeInfra#168 corrective follow-up) |
| FuzeQuality | `fuzequality` | `fuzequality` | `fuzequality_ready` | izzywdev/FuzeQuality | declared (FuzeInfra#168 corrective follow-up) |

Each row has a unique sealed token and an explicit NetworkPolicy peer. Tenant
and database binding is enforced by Chroma; collection prefixes are not an
authorization mechanism.
