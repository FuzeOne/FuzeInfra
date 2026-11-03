# Deploying a Service to Kubernetes (GitOps)

How a **downstream repo** onboards a new service onto the shared FuzeInfra
cluster. FuzeInfra itself is delivered via Argo CD (see [`gitops.md`](./gitops.md)
and [`kubernetes-migration.md`](./kubernetes-migration.md)); your service follows
the same GitOps model: **you commit manifests to your own repo, and Argo CD
reconciles them into the cluster.** Nothing is `kubectl apply`'d by hand except
the one Argo CD `Application` that points at your repo.

This guide is the Kubernetes counterpart to
[`SERVICE_USAGE_GUIDELINES.md`](./SERVICE_USAGE_GUIDELINES.md) (which covers
consuming shared services over Docker/Compose). The isolation and naming rules
are identical; only the deployment mechanics differ.

> **Read first:** the [Golden Rules](./SERVICE_USAGE_GUIDELINES.md#golden-rules-read-these-first).
> The two that matter most here:
> **deploy into your own namespace — never `fuzeinfra`**, and
> **one owner per Argo CD Application — never edit FuzeInfra's.**

---

## TL;DR checklist

- [ ] Create your **own namespace** (`myproject`), never deploy into `fuzeinfra`.
- [ ] Package your service as a Helm chart (or Kustomize/plain manifests) **in your repo**.
- [ ] Add an `imagePullSecret` for the private registry (sealed, see below).
- [ ] Reference shared services by **in-cluster DNS**: `<svc>.fuzeinfra.svc.cluster.local`.
- [ ] Provision per-project resources with your prefix (DB+user, `myproject.*` topics, `myproject:` Redis keys…).
- [ ] Store every secret as a **SealedSecret** committed to git — never a plaintext `Secret`.
- [ ] Set **resource requests and limits** on every container (required — the cluster is shared).
- [ ] Add **Prometheus scrape annotations** + dashboard/alert **ConfigMap labels** for observability.
- [ ] Commit an Argo CD **`Application`** (repoURL / path / values) and apply it once.
- [ ] **Verify it in the cluster yourself** with `cluster-query` (read-only `kubectl`, dispatchable from your repo — see [`consuming-repos/CLUSTER_QUERY.md`](./consuming-repos/CLUSTER_QUERY.md)). Green CI is not a deploy.

---

## 1. Your own namespace

Every downstream service gets its own namespace. Never target `namespace: fuzeinfra` —
that belongs to the shared platform and deleting/altering it takes down every project.

```yaml
# myproject/k8s/namespace.yaml (or let Argo create it via CreateNamespace=true)
apiVersion: v1
kind: Namespace
metadata:
  name: myproject
  labels:
    app.kubernetes.io/part-of: myproject
```

Cross-namespace traffic is allowed by default in k3s/Flannel — your pods in
`myproject` reach shared services in `fuzeinfra` over ClusterIP DNS with no
NetworkPolicy changes required.

---

## 2. Your Argo CD Application (repoURL / path / values)

Your service is owned by **one** Argo CD `Application`, which lives in *your*
repo. It points Argo CD at the chart path in your repo and the values file to
render with. Apply it once; everything after flows through git.

```yaml
# myproject/argocd/application.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myproject
  namespace: argocd                       # the Application object lives in argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default                        # or your own AppProject
  source:
    repoURL: https://github.com/myorg/myproject.git
    targetRevision: main
    path: helm/myproject                  # chart path within your repo
    helm:
      valueFiles:
        - values.yaml                     # add values-prod.yaml etc. as needed
  destination:
    server: https://kubernetes.default.svc
    namespace: myproject                  # YOUR namespace, never fuzeinfra
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

Apply it once (CI or by hand), then never touch the cluster directly again:

```bash
kubectl apply -f myproject/argocd/application.yaml
argocd app get myproject          # watch it sync
```

**Rules**

- Never edit `argocd/applications/fuzeinfra-*.yaml` — those are FuzeInfra's.
- Never delete the `fuzeinfra` AppProject or its Applications.
- If your repo is private, Argo CD needs read credentials — see
  [`gitops.md` → Private repository](./gitops.md#private-repository).

---

## 3. Private-registry image pull (`imagePullSecrets`)

Images from a private registry need a docker-registry pull secret in your
namespace, referenced by your workload. **Do not commit the plaintext secret** —
seal it (Section 5).

Generate the secret locally (do not apply the plaintext to the cluster), then seal it:

```bash
kubectl create secret docker-registry myproject-registry \
  --namespace myproject \
  --docker-server=ghcr.io \
  --docker-username="$REGISTRY_USER" \
  --docker-password="$REGISTRY_TOKEN" \
  --dry-run=client -o yaml \
| kubeseal --format yaml > myproject/k8s/sealed-registry.yaml   # commit THIS
```

Reference it from the pod spec (or the ServiceAccount):

```yaml
spec:
  template:
    spec:
      imagePullSecrets:
        - name: myproject-registry
      containers:
        - name: api
          image: ghcr.io/myorg/myproject-api:1.4.0   # pin a tag/digest, not :latest
```

---

## 4. Consuming shared services (in-cluster Service DNS)

Inside the cluster, shared FuzeInfra services resolve at their **fully-qualified
Service DNS name**:

```
<service>.fuzeinfra.svc.cluster.local
```

The `<service>` names match the Compose container names, so connection logic
carries over unchanged. Use the FQDN form from another namespace:

| Service | In-cluster address | Port |
|---------|--------------------|------|
| PostgreSQL | `postgres.fuzeinfra.svc.cluster.local` | 5432 |
| MongoDB | `mongodb.fuzeinfra.svc.cluster.local` | 27017 |
| Redis | `redis.fuzeinfra.svc.cluster.local` | 6379 |
| Neo4j (Bolt) | `neo4j.fuzeinfra.svc.cluster.local` | 7687 |
| Elasticsearch | `elasticsearch.fuzeinfra.svc.cluster.local` | 9200 |
| Kafka (bootstrap) | `kafka.fuzeinfra.svc.cluster.local` | 9092 |
| RabbitMQ (AMQP) | `rabbitmq.fuzeinfra.svc.cluster.local` | 5672 |
| ChromaDB | `chromadb.fuzeinfra.svc.cluster.local` | 8003 |
| Prometheus | `prometheus.fuzeinfra.svc.cluster.local` | 9090 |

Example env (delivered via a SealedSecret, Section 5):

```env
DATABASE_URL=postgresql://myproject_user:***@postgres.fuzeinfra.svc.cluster.local:5432/myproject
REDIS_URL=redis://:***@redis.fuzeinfra.svc.cluster.local:6379/2
KAFKA_BROKER=kafka.fuzeinfra.svc.cluster.local:9092
```

> The short name `postgres` only resolves **within** the `fuzeinfra` namespace.
> From `myproject` you must use the FQDN (or `postgres.fuzeinfra`).

---

## 5. Secrets: the SealedSecrets workflow

Plaintext `Secret` manifests must **never** be committed to git. FuzeInfra's
GitOps model uses [Bitnami **Sealed Secrets**](https://github.com/bitnami-labs/sealed-secrets):
you encrypt a `Secret` with the cluster's **public** cert into a `SealedSecret`
that is safe to commit; the in-cluster controller decrypts it back into a real
`Secret` at sync time. Only the controller (holding the private key) can decrypt
it, so the ciphertext in git is useless to anyone else.

> **Full methodology:** [`SECRETS_MANAGEMENT.md`](./SECRETS_MANAGEMENT.md) — the
> single source of truth for per-service secrets, offline sealing, the published
> cert URL, rotation, and onboarding distribution. The summary below is the
> day-to-day path.

You **seal offline** against FuzeInfra's **published public cert** — you need
**zero cluster access** and no kubeconfig. The canonical tool is
[`scripts/seal-secret.sh`](../scripts/seal-secret.sh); it fetches the current
cert, builds the Secret in memory, and writes the SealedSecret without ever
printing plaintext:

```bash
# Seal one secret PER SERVICE (least privilege) — scope is ns/name.
scripts/seal-secret.sh myproject/myproject-secrets \
  DATABASE_URL=@./db-url.txt \
  REDIS_URL=@./redis-url.txt \
  --out myproject/k8s/myproject-secrets-sealed.yaml

# Commit the SealedSecret (safe); delete the plaintext inputs.
git add myproject/k8s/myproject-secrets-sealed.yaml
```

By default the tool fetches `https://sealed-secrets.prod.fuzefront.com/v1/cert.pem`
(override with `--cert` or `FUZEINFRA_SEALED_CERT_URL`). The committed
`SealedSecret` is strict-scoped — it only decrypts in the `myproject` namespace
under the same name. Mount the resulting `Secret` via `envFrom`/`secretKeyRef` in
your Deployment.

> **One SealedSecret per service**, not one shared kitchen-sink — see
> [`SECRETS_MANAGEMENT.md` §2](./SECRETS_MANAGEMENT.md#2-per-service-sealedsecrets-least-privilege).
> The controller itself is installed cluster-side via
> `argocd/applications/sealed-secrets.yaml`; consumers never touch it.

---

## 6. Resource requests and limits (required)

The cluster is shared and (on the single-VPS k3s overlay) small. **Every
container must declare both `requests` and `limits`** so one workload can't
starve the node or the shared data services. Pods without requests/limits are
considered misconfigured and may be rejected or evicted first under pressure.

```yaml
resources:
  requests:        # what the scheduler reserves — be honest, this is your floor
    cpu: 100m
    memory: 128Mi
  limits:          # hard ceiling — the pod is throttled (CPU) / OOMKilled (mem) above this
    cpu: 500m
    memory: 256Mi
```

Guidance:

- Start from observed usage; set `requests` near steady-state, `limits` with
  headroom for spikes.
- Keep `memory` limit ≥ request and realistic — exceeding it OOMKills the pod.
- See the [Contabo overlay](../helm/fuzeinfra/values-contabo.yaml) for the
  sizing FuzeInfra's own services use on a 4 vCPU / 8 GB node — leave the node
  ~2 GB of headroom for k3s, Argo CD, and system processes.

---

## 7. Observability conventions

### Prometheus scrape annotations

Expose metrics on a `/metrics` endpoint and annotate the pod so Prometheus's
Kubernetes service-discovery picks it up — no edit to the shared `prometheus.yml`:

```yaml
spec:
  template:
    metadata:
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9090"
        prometheus.io/path: "/metrics"
```

### Dashboard & alert ConfigMaps (sidecar labels)

Grafana and Prometheus pick up dashboards and rules from labeled ConfigMaps via
the sidecar pattern. Ship yours as ConfigMaps **in your namespace** with the
expected labels:

```yaml
# Grafana dashboard — JSON in a labeled ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: myproject-dashboards
  namespace: myproject
  labels:
    grafana_dashboard: "1"          # picked up by the Grafana sidecar
data:
  myproject.json: |
    { "uid": "myproject-overview", "title": "myproject — Overview", ... }
---
# Prometheus alert rules — labeled so the rules sidecar loads them
apiVersion: v1
kind: ConfigMap
metadata:
  name: myproject-alerts
  namespace: myproject
  labels:
    prometheus_rule: "1"
data:
  myproject.rules.yaml: |
    groups:
      - name: myproject
        rules:
          - alert: MyProjectHighErrorRate
            expr: rate(myproject_http_requests_total{status=~"5.."}[5m]) > 0.05
            for: 10m
            labels: { severity: warning }
```

**Rules**

- Use a **unique** dashboard UID and a `myproject` title/alert-name prefix to
  avoid collisions with other projects and with FuzeInfra's own dashboards.
- Do not modify shared rules in `monitoring-shared/prometheus/rules/`.
- Do not change the Grafana admin password or delete shared datasources.
- A `prometheus_rule: "1"` ConfigMap must hold **PromQL only**, and should pass
  `promtool check rules <file>` before you ship it. A log query
  (`{namespace="..."} |= "..."`) is LogQL and Prometheus cannot parse it. On
  2026-10-13 one such expression kept the shared Prometheus crash-looping for 15
  days, which turned off metric alerting for every product. Every discovered file
  now goes through `promtool check rules` before Prometheus sees it. A file that
  fails is not loaded; it is logged and alerted on as `PrometheusRulesRejected`.
  A broken file now loses only its own rules, but it still loses all of them,
  including the valid rules in the same file. Log-based alerts belong in Loki: ask
  FuzeInfra (via `@fuze`) to add the rule to `helm/fuzeinfra/loki-rules/`.

---

## 8. Per-project isolation (same rules as Compose)

Provision resources on the shared services with **your project prefix** —
identical to the [Compose guidelines](./SERVICE_USAGE_GUIDELINES.md). Summary:

| Service | Isolation unit | Naming convention |
|---------|---------------|-------------------|
| PostgreSQL | Database + dedicated user | `myproject` DB, `myproject_user` |
| MongoDB | Database + dedicated user | `myproject_db`, `myproject_user` |
| Redis | Key prefix (+ optional DB index) | `myproject:*` |
| Kafka | Topic + consumer-group prefix | `myproject.*`, `myproject-*` |
| Elasticsearch | Index prefix | `myproject-*` |
| Neo4j | Node label | `:MyProject` |
| RabbitMQ | vhost + dedicated user | `/myproject`, `myproject_user` |
| ChromaDB | Collection prefix | `myproject_*` |

Never `DROP`/`FLUSHALL`/`DETACH DELETE`/delete topics you didn't create, and
never use shared superuser/root/admin credentials in app code. See the
[per-service safety checklists](./SERVICE_USAGE_GUIDELINES.md#postgresql).

---

## 9. Putting it together — minimal Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myproject-api
  namespace: myproject
  labels:
    app.kubernetes.io/name: myproject-api
    app.kubernetes.io/part-of: myproject
spec:
  replicas: 2
  selector:
    matchLabels: { app.kubernetes.io/name: myproject-api }
  template:
    metadata:
      labels: { app.kubernetes.io/name: myproject-api }
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9090"
        prometheus.io/path: "/metrics"
    spec:
      imagePullSecrets:
        - name: myproject-registry
      containers:
        - name: api
          image: ghcr.io/myorg/myproject-api:1.4.0
          ports:
            - { name: http, containerPort: 3000 }
            - { name: metrics, containerPort: 9090 }
          envFrom:
            - secretRef: { name: myproject-secrets }   # from the SealedSecret
          resources:
            requests: { cpu: 100m, memory: 128Mi }
            limits:   { cpu: 500m, memory: 256Mi }
          readinessProbe:
            httpGet: { path: /health, port: http }
          livenessProbe:
            httpGet: { path: /health, port: http }
```

Commit this (plus the Service, SealedSecrets, and ConfigMaps) under your chart
`path`, push to `main`, and Argo CD reconciles it. Done.

---

## Related docs

- [`consuming-repos/CLUSTER_QUERY.md`](./consuming-repos/CLUSTER_QUERY.md) — read-only `kubectl` against prod, dispatchable from your own repo.
- [`gitops.md`](./gitops.md) — how FuzeInfra itself is delivered via Argo CD.
- [`kubernetes-migration.md`](./kubernetes-migration.md) — chart layout, kind/EKS bring-up.
- [`SERVICE_USAGE_GUIDELINES.md`](./SERVICE_USAGE_GUIDELINES.md) — shared-service isolation rules (Compose & k8s).
- [`K3S_SECOND_NODE_RUNBOOK.md`](./K3S_SECOND_NODE_RUNBOOK.md) — scale the k3s cluster to a second node.
