# Runbook — reaching Elasticsearch / ChromaDB now that they have no public host

## Why this exists

`elasticsearch.prod.fuzefront.com` and `chromadb.prod.fuzefront.com` used to publish
the raw datastore HTTP APIs to the internet, guarded only by the `*.prod` Cloudflare
Access wildcard (email-OTP) — no other authorization layer in front of a datastore
API. M3-S3 removed both Ingress routes and their App Launcher tiles. **Neo4j Browser
is the deliberate exception** (owner decision) and keeps its Ingress, its tile, and
its `*.prod` Access gating — see `helm/fuzeinfra/templates/neo4j-ingress.yaml` and the
comment block in `helm/fuzeinfra/templates/ingress.yaml`.

This is a **de-exposure, not a removal of access** — the AC3.2 requirement is that a
replacement path exists so this doesn't get quietly reverted the next time someone
needs to poke Elasticsearch or ChromaDB from outside the cluster. This doc is that
path.

## What did NOT change

- **In-cluster service DNS is unaffected.** Any pod in the `fuzeinfra` namespace (or
  any namespace, since these are plain ClusterIP Services) still reaches them exactly
  as before:
  - `fuzeinfra-elasticsearch.fuzeinfra.svc.cluster.local:9200`
  - `fuzeinfra-chromadb.fuzeinfra.svc.cluster.local:8000`
- The Services, Deployments/StatefulSets, PVCs and ChromaDB's token auth are all
  unchanged — only their external publication (Ingress host + Cloudflare Access
  Launcher tile) is gone.

## Option 1 — read-only queries via `cluster-query.yml` (no port-forward, no kubeconfig)

`port-forward` itself is a refused verb on `cluster-query.yml` (it opens a channel,
which the workflow deliberately does not allow — see
[`docs/consuming-repos/CLUSTER_QUERY.md`](../consuming-repos/CLUSTER_QUERY.md) §4).
But most "is it up / what does it say" questions don't need a channel — `describe`,
`logs`, and `get ... -o wide` already answer them:

```bash
# pod health
gh workflow run cluster-query.yml --repo FuzeOne/FuzeInfra \
  -f kubectl_args='-n fuzeinfra get pods -l app=elasticsearch -o wide'
gh workflow run cluster-query.yml --repo FuzeOne/FuzeInfra \
  -f kubectl_args='-n fuzeinfra get pods -l app=chromadb -o wide'

# recent logs
gh workflow run cluster-query.yml --repo FuzeOne/FuzeInfra \
  -f kubectl_args='-n fuzeinfra logs -l app=elasticsearch --tail=200'

# is the Service/endpoints healthy
gh workflow run cluster-query.yml --repo FuzeOne/FuzeInfra \
  -f kubectl_args='-n fuzeinfra get svc,endpoints fuzeinfra-elasticsearch fuzeinfra-chromadb'
```

(Adjust the label selector to whatever the chart actually applies — confirm with
`-n fuzeinfra get pods --show-labels` if unsure.) This is the self-service path from
**any** repo, per `FUZEINFRA_DISPATCH_TOKEN` — see `CLUSTER_QUERY.md` for the dispatch
mechanics and its read-verb allowlist.

## Option 2 — an operator port-forward for interactive/HTTP access

For anything that genuinely needs a live HTTP connection (calling the ES query API
by hand, poking ChromaDB's `/api/v2` with a token, using Kibana-less dev tooling),
that requires an actual kubeconfig — which is **not** something any CI session holds
or can be delegated (see the GitOps + self-heal section of this repo's `CLAUDE.md`:
prod write/exec access has no delegation path, by design). This is an **operator**
(human, with their own break-glass kubeconfig) action, not something any agent session
performs itself:

```bash
# Elasticsearch — HTTP API on 9200
kubectl -n fuzeinfra port-forward svc/fuzeinfra-elasticsearch 9200:9200
curl -s http://localhost:9200/_cluster/health?pretty

# ChromaDB — HTTP API on 8000 (token-authed; see values.yaml chromadb.auth)
kubectl -n fuzeinfra port-forward svc/fuzeinfra-chromadb 8000:8000
curl -s -H "Authorization: Bearer <token>" http://localhost:8000/api/v2/heartbeat
```

Get the ChromaDB admin token via the SealedSecret decryption path in
[`docs/SECRETS_MANAGEMENT.md` §4](../SECRETS_MANAGEMENT.md#4-decryption-is-cluster-only)
(operator-SSH-only, same as any other live secret value — never via `cluster-query.yml`,
which refuses `Secret` reads outright).

## If a consumer app needs Elasticsearch or ChromaDB regularly

Don't reopen the public Ingress. Run the consumer inside the cluster (or attach it to
the shared namespace's network path) and use the in-cluster service DNS above — the
same pattern already documented for every other consumer in
[`docs/DEPLOYING_A_SERVICE_TO_K8S.md`](../DEPLOYING_A_SERVICE_TO_K8S.md) and
[`docs/SERVICE_USAGE_GUIDELINES.md`](../SERVICE_USAGE_GUIDELINES.md). If a genuine
external-HTTP need shows up, that is a new design decision (its own Access app +
its own authorization layer beyond email-OTP), not a revert of this hardening —
raise it as a design doc, not a quiet Ingress re-add.

## Related

- [`docs/consuming-repos/CLUSTER_QUERY.md`](../consuming-repos/CLUSTER_QUERY.md) — the read-only self-service path used in Option 1.
- [`docs/SECRETS_MANAGEMENT.md`](../SECRETS_MANAGEMENT.md) — live-secret recovery for Option 2's ChromaDB token.
- `helm/fuzeinfra/templates/ingress.yaml` — where the elasticsearch/chromadb routes used to be; see the comment there for the M3-S3 rationale.
- `terraform/contabo/cloudflare.tf` (`launcher_services`) — where their App Launcher tiles used to be.
