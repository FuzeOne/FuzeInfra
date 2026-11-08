# FuzeInfra Service Contract

This document is FuzeInfra's **public interface**. It describes the stable, consumer-agnostic service endpoints that any workload in the cluster can rely on. FuzeInfra does not know about, reference, or adapt to any specific consumer.

---

## Namespace

All FuzeInfra services run in the **`fuzeinfra`** namespace.

```
kubectl -n fuzeinfra get svc
```

---

## Stable Service DNS

Every service is named `fuzeinfra-<svc>`. The full in-cluster FQDN is:

```
fuzeinfra-<svc>.fuzeinfra.svc.cluster.local
```

Short-form (works from within any namespace in the same cluster):

```
fuzeinfra-<svc>.fuzeinfra
```

### Datastores

| Service | Short DNS | Port | Protocol | Notes |
|---------|-----------|------|----------|-------|
| PostgreSQL | `fuzeinfra-postgres.fuzeinfra` | **5432** | TCP | Headless StatefulSet; connect via JDBC/psycopg2 |
| MongoDB | `fuzeinfra-mongodb.fuzeinfra` | **27017** | TCP | Headless StatefulSet; auth required |
| Redis | `fuzeinfra-redis.fuzeinfra` | **6379** | TCP | Headless StatefulSet; AOF persistence enabled |
| Neo4j Bolt | `fuzeinfra-neo4j.fuzeinfra` | **7687** | TCP (Bolt) | APOC plugins loaded |
| Neo4j HTTP | `fuzeinfra-neo4j.fuzeinfra` | **7474** | HTTP | Browser / REST API |
| Elasticsearch | `fuzeinfra-elasticsearch.fuzeinfra` | **9200** | HTTP | Single-node, security disabled |
| ChromaDB | `fuzeinfra-chromadb.fuzeinfra` | **8000** | HTTP | Vector store; REST API |

### Message Queues

| Service | Short DNS | Port | Protocol | Notes |
|---------|-----------|------|----------|-------|
| Kafka (broker) | `fuzeinfra-kafka.fuzeinfra` | **9092** | PLAINTEXT | KRaft mode, no ZooKeeper |
| RabbitMQ (AMQP) | `fuzeinfra-rabbitmq.fuzeinfra` | **5672** | AMQP | Auth required |
| RabbitMQ (Management) | `fuzeinfra-rabbitmq.fuzeinfra` | **15672** | HTTP | Web UI / API |

### Observability (internal use / dashboards)

| Service | Short DNS | Port |
|---------|-----------|------|
| Prometheus | `fuzeinfra-prometheus.fuzeinfra` | **9090** |
| Grafana | `fuzeinfra-grafana.fuzeinfra` | **3000** |
| Alertmanager | `fuzeinfra-alertmanager.fuzeinfra` | **9093** |
| Loki | `fuzeinfra-loki.fuzeinfra` | **3100** |

---

## Credentials

FuzeInfra manages its own credentials in the `fuzeinfra-secrets` Secret (or an existing Secret supplied via `credentials.existingSecret` in the Helm values).

**Consumers must NOT read from this Secret.** Instead:

1. Create your own Secret in your own namespace with the connection string(s) you need.
2. Reference the stable DNS above to construct URLs at deploy time.

Example — consumer Secret in namespace `my-app`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: my-app-infra-config
  namespace: my-app
type: Opaque
stringData:
  DATABASE_URL: "postgresql://my_user:my_password@fuzeinfra-postgres.fuzeinfra:5432/my_db"
  REDIS_URL: "redis://fuzeinfra-redis.fuzeinfra:6379/0"
  KAFKA_BOOTSTRAP_SERVERS: "fuzeinfra-kafka.fuzeinfra:9092"
```

The consumer is responsible for:
- Creating any application-specific databases/keyspaces on the shared datastore.
- Managing its own credentials (the FuzeInfra admin credentials are for platform ops only).

---

## How to Reference FuzeInfra from a Consumer Service

### Docker Compose (local dev)

Join the `FuzeInfra` Docker network and use the container hostnames:

```yaml
networks:
  FuzeInfra:
    external: true

services:
  my-service:
    image: my-image
    networks: [FuzeInfra]
    environment:
      DATABASE_URL: postgresql://user:pass@postgres:5432/mydb
      REDIS_URL: redis://redis:6379/0
```

> Note: Docker Compose container names (`postgres`, `redis`, …) differ from the k8s service names (`fuzeinfra-postgres`, `fuzeinfra-redis`, …). Use environment variables so the same application image works in both environments.

### Kubernetes (staging / production)

Reference services by their FQDN or short form. No special network attachment or label is required — standard in-cluster DNS resolution is enough:

```yaml
env:
  - name: DATABASE_URL
    value: "postgresql://user:pass@fuzeinfra-postgres.fuzeinfra:5432/mydb"
  - name: REDIS_URL
    value: "redis://fuzeinfra-redis.fuzeinfra:6379/0"
  - name: KAFKA_BOOTSTRAP_SERVERS
    value: "fuzeinfra-kafka.fuzeinfra:9092"
  - name: MONGODB_URL
    value: "mongodb://user:pass@fuzeinfra-mongodb.fuzeinfra:27017"
  - name: ELASTICSEARCH_URL
    value: "http://fuzeinfra-elasticsearch.fuzeinfra:9200"
  - name: NEO4J_BOLT_URL
    value: "bolt://fuzeinfra-neo4j.fuzeinfra:7687"
  - name: CHROMADB_URL
    value: "http://fuzeinfra-chromadb.fuzeinfra:8000"
  - name: RABBITMQ_URL
    value: "amqp://user:pass@fuzeinfra-rabbitmq.fuzeinfra:5672"
```

---

## Read-only cluster introspection (`cluster-query`)

Consumers do not get a kubeconfig — but they are **not** blind to the cluster. FuzeInfra
publishes a dispatchable read-only introspection workflow, `cluster-query`, so any
consuming repo can answer "is my app running / why is that pod down / did Argo sync"
itself instead of relaying `kubectl` output through a human.

```bash
gh api --method POST repos/FuzeOne/FuzeInfra/dispatches \
  -f event_type=cluster-query \
  -f 'client_payload[kubectl_args]=-n <my-namespace> get pods -o wide'
# then read the job log for the output
```

It is read-only by construction: a read verb must be present, every mutating/exec verb
is refused, `Secret` reads and `--raw` are blocked (their output *is* a credential, and
FuzeInfra's job logs are public), and there is no `exec`/`port-forward`/`proxy`. Full
usage, the dispatch credential it needs, and the caveats:
[`docs/consuming-repos/CLUSTER_QUERY.md`](docs/consuming-repos/CLUSTER_QUERY.md).

Changing anything in the cluster remains GitOps (your own `deploy/**`) or an `@fuze`
delegation to FuzeInfra. This is the read half only.

---

## Network Policy note

If your cluster enforces NetworkPolicy, consumers must allow egress to namespace `fuzeinfra` on the ports listed above. FuzeInfra itself runs no NetworkPolicy that restricts ingress from other namespaces.

---

## Versioning

The service contract (namespace, service names, and ports) follows semantic versioning in `version.json`. Breaking changes (service renames, port changes) increment the **major** version and are announced in the changelog before taking effect.

Current version: see `version.json`.

---

## Deployment

FuzeInfra is deployed via Helm:

```bash
# Local (kind) — staging
helm upgrade --install fuzeinfra helm/fuzeinfra \
  -n fuzeinfra --create-namespace \
  -f helm/fuzeinfra/values-local.yaml

# Production
helm upgrade --install fuzeinfra helm/fuzeinfra \
  -n fuzeinfra --create-namespace

# Local development — Docker Compose
./infra-up.sh
```

CI deploys automatically:
- **`staging` branch** → local k8s cluster via the `deploy-staging` workflow.
- **`main` branch** → production cluster via the `deploy-prod` workflow (requires `KUBE_CONFIG` secret).
