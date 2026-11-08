# FuzeInfra — Service Usage Guidelines for Consumer Projects

FuzeInfra is **shared** infrastructure. Multiple projects run against the same databases, brokers, and monitoring stack simultaneously. This document describes the safe way for a consumer project to provision its own resources on each service, and the hard rules that protect every other project sharing the platform.

---

## Golden Rules (read these first)

| Rule | Why it matters |
|------|---------------|
| **Never run `docker compose down -v`** (or any `--volumes` / `-v` flag) against FuzeInfra | `-v` deletes *all* named volumes — PostgreSQL, MongoDB, Redis, Elasticsearch, etc. — for every project at once. Data loss is instant and irreversible. |
| **Never run `kubectl delete namespace fuzeinfra`** | Destroys every Kubernetes resource and PVC in the shared namespace. |
| **Never `kubectl delete pvc`** on a shared PVC | Volumes are shared; another project's data is on the same disk. |
| **Never issue `DROP DATABASE`** or `DROP USER` unless you created the object yourself | Other projects depend on the same Postgres/Mongo cluster. |
| **Never `FLUSHALL`** on Redis | Clears every project's keys from the shared instance. |
| **Never delete Kafka topics** you did not create | Consumers from other projects will break silently. |
| **One owner per Argo CD Application** | Each Application must be owned by exactly one project; never edit another project's Application manifest. |
| **Never modify `docker-compose.FuzeInfra.yml`** from your project | Treat the shared compose file as read-only infrastructure — open a PR against `FuzeOne/FuzeInfra` instead. |

---

## PostgreSQL

### Connection details

```
Host (Docker):      postgres          (container name on FuzeInfra network)
Host (local):       localhost
Port:               5432
Superuser:          fuzeinfra / <POSTGRES_PASSWORD from .env>
Shared DB:          fuzeinfra_db      (do NOT store project data here)
```

### Adding a database for your project

Drop an SQL file into `docker/postgres/init/` in this repo (open a PR):

```sql
-- docker/postgres/init/02-create-myproject-db.sql
SELECT 'CREATE DATABASE myproject'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'myproject')\gexec
```

Init scripts run **only on first initialisation** (empty data volume). For an already-running cluster, run the SQL directly:

```bash
docker exec -it fuzeinfra-postgres \
  psql -U fuzeinfra -c "CREATE DATABASE myproject;"
```

### Adding a dedicated user

```sql
-- Never use the fuzeinfra superuser in application code.
CREATE USER myproject_user WITH PASSWORD 'strong-password';
GRANT ALL PRIVILEGES ON DATABASE myproject TO myproject_user;

-- After connecting to the database:
\c myproject
GRANT ALL ON SCHEMA public TO myproject_user;
```

Automate this in `docker/postgres/init/03-myproject-user.sql` (PR required) so it runs on fresh installs:

```sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'myproject_user') THEN
    CREATE USER myproject_user WITH PASSWORD 'strong-password';
  END IF;
END$$;

SELECT 'CREATE DATABASE myproject'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'myproject')\gexec

GRANT ALL PRIVILEGES ON DATABASE myproject TO myproject_user;
```

### Environment variable pattern

```env
DATABASE_URL=postgresql://myproject_user:strong-password@postgres:5432/myproject
```

### Safety checklist

- [ ] Use a dedicated database per project — never write to `fuzeinfra_db`
- [ ] Use a dedicated user per project — never use `fuzeinfra` credentials in app code
- [ ] Do not issue `DROP DATABASE`, `DROP ROLE`, or `REVOKE` on objects you did not create
- [ ] Do not change `max_connections`, `shared_buffers`, or other server-level settings

---

## MongoDB

### Connection details

```
Host (Docker):      mongodb
Port:               27017
Root user:          admin / <MONGO_INITDB_ROOT_PASSWORD from .env>
Management UI:      http://localhost:8081  (Mongo Express)
```

### Adding a database and user

MongoDB creates a database automatically when you first write to it. Provision a dedicated user via the Mongo shell or a startup script:

```javascript
// Run via: docker exec -it fuzeinfra-mongodb mongosh -u admin -p <password>
use myproject_db

db.createUser({
  user: "myproject_user",
  pwd:  "strong-password",
  roles: [{ role: "readWrite", db: "myproject_db" }]
})
```

To make this reproducible, add an init script at `docker/mongo/init/02-myproject.js` (PR required):

```javascript
// docker/mongo/init/02-myproject.js
db = db.getSiblingDB('myproject_db');
if (db.getUser('myproject_user') === null) {
  db.createUser({
    user: 'myproject_user',
    pwd:  'strong-password',
    roles: [{ role: 'readWrite', db: 'myproject_db' }]
  });
}
```

Mount it by adding to `docker-compose.FuzeInfra.yml` (PR against FuzeInfra):

```yaml
mongodb:
  volumes:
    - ./docker/mongo/init:/docker-entrypoint-initdb.d:ro
```

### Environment variable pattern

```env
MONGODB_URL=mongodb://myproject_user:strong-password@mongodb:27017/myproject_db
```

### Safety checklist

- [ ] Use a dedicated database per project — never use the `admin` database for app data
- [ ] Use a `readWrite` role scoped to your own database — never use the root user in app code
- [ ] Do not drop other projects' collections or databases
- [ ] Do not change replica-set configuration or create global indexes

---

## Redis

Redis is a single shared instance. There are no hard boundaries; **namespacing by key prefix is mandatory**.

### Connection details

```
Host (Docker):      redis
Port:               6379
Password:           <REDIS_PASSWORD from .env>
```

### Key naming convention

Prefix every key with your project name:

```
myproject:session:<id>
myproject:cache:users:<id>
myproject:queue:tasks
myproject:lock:resource-name
```

Use a colon (`:`) as the namespace separator. Redis Cluster and most client libraries treat the first colon-delimited segment as a hash slot hint.

### Database index allocation

Redis has 16 logical databases (`0`–`15`). Database 0 is the default and may be used by multiple projects. Agree on index allocation:

| DB index | Reserved for |
|----------|-------------|
| 0        | General / default |
| 1        | Airflow Celery broker (internal) |
| 2–15     | Allocate one per project on a first-come basis; document your claim here |

Set your database index in the connection URL:

```env
REDIS_URL=redis://:redis_secure_password@redis:6379/2
```

### Safety checklist

- [ ] Always prefix keys with `<myproject>:`
- [ ] Never call `FLUSHALL` or `FLUSHDB` on shared databases
- [ ] Never modify `maxmemory` or `maxmemory-policy` server settings
- [ ] Do not use `KEYS *` in production — use `SCAN` with your prefix

---

## Apache Kafka

### Connection details

```
Bootstrap (Docker):   kafka:9092       (internal, from other containers)
Bootstrap (local):    localhost:29092  (from host machine)
```

### Creating a topic

```bash
# From inside the kafka container
docker exec -it fuzeinfra-kafka \
  kafka-topics.sh --bootstrap-server localhost:9092 \
    --create --topic myproject.events \
    --partitions 3 --replication-factor 1

# Or from the host
kafka-topics.sh --bootstrap-server localhost:29092 \
  --create --topic myproject.events \
  --partitions 3 --replication-factor 1
```

### Topic naming convention

```
<project>.<domain>.<event-type>

myproject.orders.created
myproject.orders.updated
myproject.payments.processed
myproject.users.registered
```

### Consumer group naming convention

```
<project>-<service>-<purpose>

myproject-api-order-processor
myproject-worker-notification-sender
```

### Safety checklist

- [ ] Prefix all topic names with your project name
- [ ] Prefix all consumer group names with your project name
- [ ] Never delete topics you did not create
- [ ] Never alter the `__consumer_offsets` topic or internal Kafka topics
- [ ] Set `auto.create.topics.enable=false` in your producer config and create topics explicitly
- [ ] Do not change broker-level configs (`log.retention`, `num.partitions` defaults, etc.)

---

## Elasticsearch

### Connection details

```
Host (Docker):    elasticsearch
Port:             9200
```

### Index naming convention

All indexes must be prefixed with your project name:

```
myproject-users
myproject-products
myproject-logs-2024.01
myproject-metrics
```

Use ILM (Index Lifecycle Management) aliases for time-series data:

```json
PUT /myproject-logs-000001
{
  "aliases": { "myproject-logs": { "is_write_index": true } }
}
```

### Template pattern

Create an index template scoped to your prefix:

```json
PUT /_index_template/myproject
{
  "index_patterns": ["myproject-*"],
  "template": {
    "settings": { "number_of_shards": 1, "number_of_replicas": 0 },
    "mappings": { ... }
  }
}
```

### Safety checklist

- [ ] Prefix every index, alias, and template with your project name
- [ ] Do not delete indexes you did not create (`DELETE /myproject-*` is safe; `DELETE /*` is not)
- [ ] Do not change cluster-level settings (`cluster.routing.allocation.*`, `xpack.*`)
- [ ] Do not use the `.` prefix — those are Elastic system indexes

---

## Neo4j

### Connection details

```
Host (Docker):      neo4j
Bolt port:          7687
HTTP port:          7474
Auth:               neo4j / <NEO4J_AUTH password from .env>
Browser UI:         http://localhost:7474
```

### Database isolation (Neo4j 4+)

Neo4j Enterprise allows multiple databases per instance. The Community edition (used here) has a single `neo4j` default database. Isolate by **label and property prefix**:

```cypher
// Good — all your nodes carry your project label
CREATE (:MyProject:User {id: 'u1', name: 'Alice'})
CREATE (:MyProject:Product {id: 'p1', sku: 'ABC'})

// Relationship types also prefixed
MERGE (u:MyProject:User {id: 'u1'})-[:MYPROJECT_PURCHASED]->
      (p:MyProject:Product {id: 'p1'})
```

Query scope to your labels to avoid scanning other projects' data:

```cypher
MATCH (n:MyProject) RETURN n LIMIT 100
```

### Safety checklist

- [ ] Label every node with your project name as the primary label
- [ ] Never issue `MATCH (n) DETACH DELETE n` (drops *all* data, not just yours)
- [ ] Scope all MATCH/MERGE/DELETE to `(:MyProject …)` labels
- [ ] Do not modify constraints/indexes that belong to another prefix

---

## RabbitMQ

### Connection details

```
Host (Docker):        rabbitmq
AMQP port:            5672
Management UI:        http://localhost:15672
Admin user:           admin / <RABBITMQ_DEFAULT_PASS from .env>
```

### Creating a dedicated vhost and user

Every project must use its own **vhost**. Never publish or consume on the default `/` vhost.

```bash
# Create vhost
docker exec fuzeinfra-rabbitmq \
  rabbitmqctl add_vhost myproject

# Create user
docker exec fuzeinfra-rabbitmq \
  rabbitmqctl add_user myproject_user strong-password

# Grant full permissions on the vhost
docker exec fuzeinfra-rabbitmq \
  rabbitmqctl set_permissions -p myproject myproject_user ".*" ".*" ".*"
```

Or via the Management API:

```bash
curl -u admin:<password> -X PUT http://localhost:15672/api/vhosts/myproject
curl -u admin:<password> -X PUT http://localhost:15672/api/users/myproject_user \
  -H "Content-Type: application/json" \
  -d '{"password":"strong-password","tags":""}'
curl -u admin:<password> -X PUT \
  "http://localhost:15672/api/permissions/myproject/myproject_user" \
  -H "Content-Type: application/json" \
  -d '{"configure":".*","write":".*","read":".*"}'
```

### Environment variable pattern

```env
RABBITMQ_URL=amqp://myproject_user:strong-password@rabbitmq:5672/myproject
```

### Safety checklist

- [ ] Always use a dedicated vhost per project — never use `/` (the default vhost)
- [ ] Use a dedicated user per project — never use `admin` credentials in app code
- [ ] Do not delete or modify exchanges/queues in other vhosts
- [ ] Do not change global RabbitMQ policies (`rabbitmqctl set_policy`)
- [ ] Do not enable or disable RabbitMQ plugins (requires a FuzeInfra PR)

---

## ChromaDB

### Connection details

```
Host (Docker):   chromadb
Port:            8003
REST API:        http://chromadb:8003
```

### Collection naming convention

Prefix every collection with your project name:

```python
import os
import chromadb
from chromadb.config import Settings

client = chromadb.HttpClient(
    host="fuzeinfra-chromadb.fuzeinfra.svc.cluster.local",
    port=8000,
    tenant="myproject",
    database="myproject",
    settings=Settings(
        chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider",
        chroma_client_auth_credentials=os.environ["CHROMA_TOKEN"],
    ),
)

# Always use your project prefix
collection = client.get_or_create_collection(name="myproject_documents")
collection = client.get_or_create_collection(name="myproject_embeddings_v2")
```

### Safety checklist

- [ ] Use only the tenant/database assigned in the allocation registry
- [ ] Load `CHROMA_TOKEN` from the consumer's SealedSecret, never values or code
- [ ] Prefix shared collection names with `<myproject>_` for readability
- [ ] Confirm a cross-tenant request is denied during onboarding
- [ ] Do not change the ChromaDB server settings

---

## Nginx (Reverse Proxy)

FuzeInfra runs an nginx container that proxies all services. Project-specific vhosts and upstreams live in:

```
infrastructure/conf.d/
```

### Adding a vhost for your project

Create a file `infrastructure/conf.d/myproject.conf`:

```nginx
upstream myproject_backend {
    server myproject-api:3000;
}

server {
    listen 80;
    server_name myproject.dev.local;

    location / {
        proxy_pass         http://myproject_backend;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Then reload nginx (no restart needed):

```bash
docker exec fuzeinfra-nginx nginx -s reload
```

### HTTPS (mkcert)

If you have certificates in `infrastructure/certs/`, add the SSL block:

```nginx
server {
    listen 443 ssl;
    server_name myproject.dev.local;

    ssl_certificate     /etc/nginx/certs/dev.local.pem;
    ssl_certificate_key /etc/nginx/certs/dev.local-key.pem;

    location / {
        proxy_pass http://myproject_backend;
        ...
    }
}
```

### Rules

- **Do not edit** `infrastructure/nginx.conf` (the main config file) — changes there affect every project
- **Do not add** `worker_processes`, `events`, or `http {}` blocks in your `conf.d/*.conf` — that belongs in the main config
- Files in `conf.d/` are auto-included by the main config via `include /etc/nginx/conf.d/*.conf;`
- Your upstream service must be on the `FuzeInfra` Docker network for nginx to reach it

---

## Argo CD (GitOps Deployments)

FuzeInfra uses Argo CD to continuously reconcile the cluster. Consumer projects deploy their own Applications using the same Argo CD instance.

### File placement

Application manifests for **FuzeInfra-managed** infrastructure live in:

```
argocd/applications/fuzeinfra-local.yaml
argocd/applications/fuzeinfra-aws.yaml
```

**Your project's** Application manifests live in your own repo. You apply them to the cluster directly:

```bash
kubectl apply -f myproject/argocd/application.yaml
```

### AppProject scope

FuzeInfra's `AppProject` only scopes the FuzeInfra chart. Your project needs its own project or can deploy into the `default` project if the `fuzeinfra` AppProject does not cover your repo:

```yaml
# myproject/argocd/application.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myproject
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default          # or create your own AppProject
  source:
    repoURL: https://github.com/myorg/myproject.git
    targetRevision: main
    path: helm/myproject
  destination:
    server: https://kubernetes.default.svc
    namespace: myproject    # your own namespace, NOT fuzeinfra
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

### Rules

- **Deploy into your own namespace** — never target `namespace: fuzeinfra`
- **Never edit** `argocd/applications/fuzeinfra-*.yaml` — those belong to FuzeInfra
- **Never delete** the `fuzeinfra` AppProject or the `fuzeinfra-local`/`fuzeinfra-aws` Applications
- If you need access to shared FuzeInfra services from your namespace, the FuzeInfra AppProject's `destinations` block will need updating — open a PR against this repo

---

## Monitoring (Prometheus & Grafana)

### Scraping your application

Add a scrape target without modifying the main `prometheus.yml`. Drop a file into `monitoring-shared/prometheus/`:

```yaml
# monitoring-shared/prometheus/myproject.yml
- job_name: 'myproject-api'
  static_configs:
    - targets: ['myproject-api:9090']
  metrics_path: /metrics
```

Then open a PR against FuzeInfra to add it, or mount it in your own compose:

```yaml
prometheus:
  volumes:
    - ./myproject-scrape.yml:/etc/prometheus/conf.d/myproject.yml:ro
```

### Grafana dashboards

Import dashboards via the UI (Dashboards → Import → Upload JSON) or provision them with a ConfigMap in Kubernetes. Use a unique dashboard UID and title prefix to avoid collisions.

### Rules

- Do not modify shared alerting rules in `monitoring-shared/prometheus/rules/`
- Do not change the Grafana admin password or delete shared datasources
- Do not create Prometheus recording rules that overlap with FuzeInfra's rule names

---

## Docker Network

All projects must join the `FuzeInfra` external network to reach shared services:

```yaml
# your project's docker-compose.yml
networks:
  FuzeInfra:
    external: true

services:
  myproject-api:
    networks:
      - FuzeInfra
    # Now reachable at 'myproject-api' from nginx and other containers
```

### Rules

- **Do not** set `driver: bridge` on the `FuzeInfra` network — it is external and already created
- **Do not** set `internal: true` on the FuzeInfra network reference
- Use container names (e.g. `postgres`, `redis`, `mongodb`) as hostnames — DNS is resolved by Docker
- If your container needs to be reached by name from nginx, give it a predictable `container_name`

---

## Lifecycle Commands — What's Safe vs Dangerous

| Command | Safe? | Notes |
|---------|-------|-------|
| `docker compose up -d` (your project) | ✅ | Starts your services only |
| `docker compose down` (your project, no `-v`) | ✅ | Stops your services; leaves FuzeInfra running |
| `docker compose logs` | ✅ | Read-only |
| `docker compose restart <service>` (your service) | ✅ | Only restarts your container |
| `./infra-up.sh` | ✅ | Starts FuzeInfra (idempotent) |
| `./infra-down.sh` | ⚠️ | Stops FuzeInfra for **all** projects — coordinate first |
| `docker compose down -v` | ❌ | Destroys **all** volumes — never run this |
| `docker compose down --volumes` | ❌ | Same as above |
| `docker volume rm fuzeinfra_*` | ❌ | Manual volume deletion — destroys shared data |
| `kubectl delete namespace fuzeinfra` | ❌ | Destroys entire Kubernetes platform |
| `kubectl delete pvc -n fuzeinfra` | ❌ | Destroys persistent storage |
| Editing `docker-compose.FuzeInfra.yml` | ❌ | Open a PR instead |
| `argocd app delete fuzeinfra-local` | ❌ | Removes Argo CD control over FuzeInfra |

---

## Summary: Per-Service Checklist

| Service | Isolation unit | Naming convention |
|---------|---------------|-------------------|
| PostgreSQL | Database + dedicated user | `myproject_db`, `myproject_user` |
| MongoDB | Database + dedicated user | `myproject_db`, `myproject_user` |
| Redis | Key prefix (+ optional DB index) | `myproject:*` |
| Kafka | Topic prefix + consumer group prefix | `myproject.*`, `myproject-*` |
| Elasticsearch | Index prefix | `myproject-*` |
| Neo4j | Node label | `:MyProject` |
| RabbitMQ | vhost + dedicated user | `/myproject`, `myproject_user` |
| ChromaDB | Collection prefix | `myproject_*` |
| Nginx | Separate `conf.d/*.conf` file | `myproject.conf` |
| Argo CD | Separate Application + own namespace | deploy to `myproject` NS |
| Monitoring | Separate scrape config file | `myproject.yml` |
