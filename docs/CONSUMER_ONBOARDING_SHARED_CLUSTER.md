# Onboarding a consumer product onto the shared cluster (zero-touch)

How a product repo (e.g. MendysRobotics) goes live on the shared FuzeInfra
Contabo k3s cluster **without hand-deploys, per-app ingress controllers, or
plaintext secrets** — the pattern to reuse for every future consumer repo.
Everything below is GitOps: the consumer commits manifests, Argo (run by
FuzeInfra) reconciles.

> Boundary: the consumer owns its `deploy/**` (Helm/kustomize + Argo Applications
> + sealed secrets). FuzeInfra owns the cluster, Argo, the tunnel, and the shared
> datastores. Consumers never edit FuzeInfra or operate the cluster; they delegate
> via `@fuze` (baseline §1).

## 1. Ingress — reuse Traefik + the Cloudflare Tunnel (do NOT add nginx/cert-manager)

The shared cluster is **CF-tunnel-only**: Traefik is pinned to `ClusterIP`, nothing
binds host ports 80/443, and Cloudflare terminates TLS at the edge. A consumer that
adds its own ingress-nginx + cert-manager would open ports and break that invariant.

Instead:
- Ingress `className: traefik`, **no** `tls:` block, **no** cert-manager annotations
  (CF does TLS). The cluster is HTTP-only behind the tunnel.
- If you pin a Traefik entrypoint at all, pin **`web`** — never `websecure`, and
  never set `traefik.ingress.kubernetes.io/router.tls: "true"`. The tunnel's
  catch-all hits Traefik as plaintext HTTP on port 80, so a `websecure`-only router
  leaves nothing listening on `web` and every request falls through to Traefik's
  default 404. The symptom is deceptive — **DNS resolves, TLS works, HTTP returns
  404** — and reads exactly like a missing tunnel route, which sends people hunting
  for a `Tunnel:Edit` API token they do not need. It is the entrypoint.
  (Cost FuzeHub a round trip: FuzeOne/FuzeHub#66.)
- Per-product hostnames route through the **existing** tunnel; DNS is proxied CNAMEs
  in the product's own Cloudflare zone via the generic `modules/cloudflare-dns`
  Terraform module — **no product hostnames are hard-coded in FuzeInfra** (the
  tunnel catch-all is generic `→ http://traefik.kube-system:80`).
- Public sites: no CF Access policy on those hostnames. Admin UIs: add a CF Access
  app (FuzeInfra `terraform/contabo/cloudflare.tf`).

### 1a. Shared-zone hostnames (`*.prod.fuzefront.com`)

Products that use FuzeInfra's shared subdomain (`myapp.prod.fuzefront.com`) declare
their hostnames via the **consumer CF host declaration flow** — they do NOT hardcode
them in FuzeInfra Terraform. The full contract is in
[governance/cf-host-declaration.md](../governance/cf-host-declaration.md).

Short version:
1. Create `deploy/cf/hosts.yaml` in your repo (see doc for schema).
2. Add a CI job that fires a `repository_dispatch` (`event_type: cf-hosts-declare`)
   to FuzeInfra whenever `hosts.yaml` changes.
3. FuzeInfra validates, materializes into `consumers.tfvars`, and opens a PR — no
   human touches `consumers.tfvars` directly.

> Products on their **own domain** (e.g. mendysrobotics.com) use the generic
> `modules/cloudflare-dns` Terraform module in FuzeInfra instead — no `hosts.yaml`
> needed.

## 2. Git auth for Argo — one GitHub App for ALL private repos

Argo pulls private consumer repos using a **repo-creds credential template** backed
by a **GitHub App** (contents+packages: read), scoped by URL prefix
`https://github.com/izzywdev`. One credential covers every current and future
`izzywdev` private repo — no per-repo deploy keys.

- Sealed and applied from `argocd/cluster-config/` (see `apply-cluster-config.yml`).
- **App tokens are short-lived** → they are for Argo *git* auth only. The kubelet
  cannot use them as image pull secrets (see §3).

## 3. Secrets — zero-touch sealed provisioning

All secrets are Bitnami **SealedSecrets** (only ciphertext in git), synced by the
consumer's `*-sealed` Argo app. The consumer's `seal-secrets.yml`
(`workflow_dispatch`, idempotent — skip if the sealed file already exists) seals:

- **Image pull secret** — a `docker-registry` secret (GHCR read) from the repo's
  `GHCR_PAT`. **Name it consistently** with what the pods reference (a `ghcr` vs
  `ghcr-pull-secret` name mismatch silently causes `ImagePullBackOff`).
- **Generated app secrets** — DB passwords, WordPress salts, etc. generated in-
  workflow (`openssl`) and sealed. No human input, no rotation churn (idempotent).
- **Operator/3rd-party secrets** — API keys (OpenAI, WooCommerce, etc.) sourced
  from GitHub Actions secrets, empty-tolerant so a partial launch still starts.

To rotate a generated secret: delete its file under `deploy/argocd/sealed/` and
re-run the workflow.

## 4. Shared datastores — cross-repo data-tier request

A consumer's backend authenticates to FuzeInfra's shared Postgres/Mongo/Redis/Neo4j
with a **dedicated least-privilege role**. FuzeInfra owns those datastores, so the
consumer **requests provisioning via an `@fuze` issue on FuzeInfra** (role +
database per datastore; creds delivered as GH Actions secrets on the consumer repo,
never in the issue). The consumer composes its connection URLs from those creds +
the published host/port contract (`fuzeinfra-<svc>.fuzeinfra`).

### Database provisioning — the canonical FuzeInfra-side flow (Postgres)

FuzeInfra provisions per-service Postgres roles/databases with a **two-secret
pattern**. Get this order wrong and the provisioning Job wedges the whole cluster
(one `CreateContainerConfigError` pod blocks **every** enabled service — this cost
~4 days of downtime once, see #228). The two secrets:

| Secret (both named `<service>-db-credentials`) | Namespace | Contents | Sealed by |
|---|---|---|---|
| Password secret | `fuzeinfra` | just `password` | **FuzeInfra** (`deploy/sealed-secrets/`) |
| App credentials | app namespace | `DATABASE_URL`, `DB_HOST`, … | the **app repo** (`deploy/**`) |

The `fuzeinfra`-namespace password secret is what the provisioning Job
(`helm/fuzeinfra/templates/service-db-provisioning.yaml`) reads via
`secretKeyRef` (`Optional: false`) to create the role/database. It is committed as a
SealedSecret and reconciled onto the cluster by the **`fuzeinfra-sealed-secrets`**
Argo app (`argocd/applications/fuzeinfra-sealed-secrets.yaml`, path
`deploy/sealed-secrets/`). That reconciler is what makes the fix durable: if the
`fuzeinfra` namespace is deleted or the cluster is re-provisioned, Argo re-applies
the SealedSecrets and the controller re-creates the plain Secrets before the Job
needs them.

Steps for a new service (`<service>`):

1. **In FuzeInfra**, seal the password secret:
   ```bash
   ./scripts/seed-service-db.sh <service>        # generates + seals a 32-char password
   # or ./scripts/seed-service-db.sh <service> <password>   to use a given one
   ```
   This writes `deploy/sealed-secrets/<service>-db-credentials.yaml` (scope
   `fuzeinfra/<service>-db-credentials`, key `password`) and prints the password.
   It is idempotent — it refuses to overwrite an existing manifest (use `--force`
   only for an intentional rotation).
2. **In the app repo**, seal the app-namespace credentials with the **same
   password** (its own `seal-db-credentials.sh`/`seal-secrets.yml`), producing the
   `DATABASE_URL` secret the app consumes.
3. **Open a single PR on FuzeInfra** that adds BOTH the sealed
   `deploy/sealed-secrets/<service>-db-credentials.yaml` **and** flips the service
   to `enabled: true` under `serviceDatabases:` in
   `helm/fuzeinfra/values-contabo.yaml` (role `<service>_svc`, database
   `<service>`, `passwordSecret.name: <service>-db-credentials`, `key: password`).
4. **Merge.** Argo applies the SealedSecret → the controller decrypts it → the
   post-sync provisioning Job creates the role + database (idempotently).

> **The load-bearing invariant:** never flip `enabled: true` in
> `values-contabo.yaml` without the matching `fuzeinfra`-namespace SealedSecret in
> the **same** PR. The Job is a single pod mounting every enabled service's
> password; one missing Secret fails the pod and blocks provisioning for all
> services. `seed-service-db.sh` prints these steps on every run.

MongoDB follows the same shape — see `docs/consuming-repos/MONGODB_PROVISIONING.md`
(`<service>-mongo-credentials`, `serviceMongoDatabases:`).

### Neo4j — dedicated instance per consumer

Neo4j **Community edition** (what FuzeInfra runs) supports exactly one database
and no RBAC — sharing the admin credential with multiple products is the only
Community option. FuzeInfra therefore provisions a **dedicated Neo4j StatefulSet**
per consumer: its own container, PVC, and credentials, giving full isolation at
the cost of ~1-2 GiB extra heap.

Provisioning is a **single GitOps workflow** — no manual sealing, no local
kubectl access needed:

1. Open an `@fuze` issue on FuzeInfra requesting a Neo4j instance named `<service>`.
   FuzeInfra creates and runs a `provision-<service>-neo4j` workflow that:
   - Generates a random hex password runner-side (masked, never logged).
   - Seals it as `neo4j-<service>-credentials` (key: `password`) for the
     `fuzeinfra` namespace, offline via the committed `sealing-cert.pem`.
   - Sets `<SERVICE>_NEO4J_PASSWORD` on your repo as a GH Actions secret.
   - Flips `serviceNeo4jInstances[<service>].enabled` to `true` in
     `helm/fuzeinfra/values-contabo.yaml`.
   - Opens a PR in FuzeInfra with the sealed ciphertext + gate flip.
2. The PR merges → Argo applies the SealedSecret → the StatefulSet boots with
   `NEO4J_AUTH=neo4j/<password>`.
3. In your app repo, use the delivered GH Actions secret to seal the
   app-namespace credential:
   ```
   NEO4J_URI=bolt://fuzeinfra-neo4j-<service>.fuzeinfra.svc.cluster.local:7687
   NEO4J_USER=neo4j
   NEO4J_PASSWORD=<the GH Actions secret value>
   ```
4. In your `.fuze/manifest.json`, declare the neo4j store so the dataTier
   reconciler tracks the provision:
   ```json
   { "type": "neo4j", "store": "<service>" }
   ```

The `DISABLED` advisory from the reconciler means the instance exists but
`enabled: false` — the provision workflow has not been run yet.

> **Never connect to `fuzeinfra-neo4j.fuzeinfra` (the shared instance).** That
> instance uses a single shared `neo4j` database and the same admin credentials
> for all callers — it is a legacy integration path that will be deprecated as
> each consumer migrates to their own `fuzeinfra-neo4j-<service>` instance.

## 5. Verifying it — you have read access to the cluster

Consumers never *operate* the cluster, but they are not blind to it. FuzeInfra's
**`cluster-query`** workflow gives you read-only `kubectl` against prod — no kubeconfig
changes hands, and no human has to relay output back to you. It reuses the
**`FUZEINFRA_DISPATCH_TOKEN`** you already hold for infra-requests, so there is
nothing new to mint:

```bash
gh api --method POST repos/FuzeOne/FuzeInfra/dispatches \
  -f event_type=cluster-query \
  -f 'client_payload[kubectl_args]=-n <my-namespace> get pods -o wide'
```

Drop-in consumer workflow: [`workflows/consumer/cluster-query.yml`](workflows/consumer/cluster-query.yml).

Use it to confirm a rollout actually landed (`-n argocd get applications`), to read
pod events/logs when it didn't, and as the evidence in your done-report. Mutating and
exec verbs are refused, and `Secret` reads plus `--raw` are blocked because FuzeInfra's
job logs are **public**. Full usage + caveats:
[`consuming-repos/CLUSTER_QUERY.md`](consuming-repos/CLUSTER_QUERY.md).

## 6. Self-heal covers it going forward

Once live, `docs/argo-selfheal-autofix.md` keeps it healthy: any `OutOfSync`,
`Degraded`, or `Unknown`/ComparisonError on the consumer's Argo apps fires an
`@fuze` issue back to the owning repo automatically.

## Gotchas proven in practice

- **ArgoCD rejects a repo with any out-of-bounds symlink** (repo-wide, before path
  selection) — e.g. a committed venv's `bin/python3 → /usr/bin/python3`. Manifest
  generation fails for the whole repo. Don't commit venvs; `.gitignore` them.
- **Values-file integrity is load-bearing.** A single stray character on line 1 of a
  Helm values file (a `t>` prefix) makes Helm parse the entire document as a string
  → `cannot unmarshal string into map` → the app sits in `Unknown` and never rolls.
- **php-fpm + nginx-sidecar WordPress** must **share the full docroot** (initContainer
  seeds WP core + baked wp-content into a shared volume both mount at
  `/var/www/html`). Mounting only `wp-content` into nginx → `403` on `/` because
  nginx has no `index.php`. (Traefik is the cluster ingress; the pod's nginx is just
  the web server in front of php-fpm — both layers are expected.)
