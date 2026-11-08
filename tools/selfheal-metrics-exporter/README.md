# selfheal-metrics-exporter

Prometheus exporter that counts how often FuzeInfra's **self-closed automation
loop** actually closes — an autofix workflow opens a labelled GitHub issue/PR,
a cloud coding agent fixes it, the PR merges.

Before this existed the answer was only reachable by hand-running GitHub label
queries. Background, classification rules and the historical snapshot:
[`docs/self-heal-loop-metrics.md`](../../docs/self-heal-loop-metrics.md).

Stdlib only (`http.server` + `urllib`). No pip dependencies, no k8s API access,
no secrets printed.

## Run

```bash
python3 exporter.py                 # unauthenticated, public repo, 60 req/h
GITHUB_TOKEN=ghp_... python3 exporter.py
curl localhost:9109/metrics
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_REPO` | `FuzeOne/FuzeInfra` | `owner/name` to query. |
| `GITHUB_TOKEN` | *(empty)* | Optional. Empty = unauthenticated (60 req/h). Never logged. |
| `POLL_INTERVAL_SECONDS` | `600` | Seconds between GitHub polls. |
| `LISTEN_PORT` | `9109` | HTTP listen port. |
| `LABEL_SYSTEM_MAP` | `{"argo-autofix":"argocd","crit-autofix":"loki","alertmanager-autofix":"alertmanager"}` | JSON object mapping a GitHub label to the `system` label value. Adding a 4th autofix workflow is a config change, not a code change. |

## Endpoints

- `GET /metrics` — Prometheus text exposition.
- `GET /healthz` — `200 ok`.

## Metrics

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `fuzeinfra_selfheal_loop_opened_total` | gauge | `system` | Autofix items ever opened. |
| `fuzeinfra_selfheal_loop_resolved_total` | gauge | `system` | Loops closed by a **merged** labelled PR. |
| `fuzeinfra_selfheal_loop_open_total` | gauge | `system` | Still open. |
| `fuzeinfra_selfheal_loop_manual_close_total` | gauge | `system` | Issues closed without a merged PR resolving them. |
| `fuzeinfra_selfheal_loop_resolution_seconds` | histogram | `system` | `created_at` → `merged_at` for resolved PRs. |
| `fuzeinfra_selfheal_loop_last_scrape_success_timestamp_seconds` | gauge | — | Unix time of the last fully successful poll. |
| `fuzeinfra_selfheal_loop_scrape_errors_total` | counter | — | Poll cycles abandoned (rate limit, network). |

The four `*_total` counts are **gauges** despite the suffix: each is recomputed
from GitHub every poll rather than incremented in-process, so they survive a
restart and must be read directly, never through `rate()`/`increase()`.

## Log feed

Each newly observed resolution prints one JSON line to stdout, which Promtail
already ships to Loki:

```json
{"event":"selfheal_resolved","system":"loki","issue_number":22,"url":"https://github.com/FuzeOne/FuzeInfra/pull/22","title":"...","resolution_seconds":72104.0}
```

Query it with `{app="selfheal-metrics"} | json | event="selfheal_resolved"`.
The first poll after a restart seeds silently — otherwise every rollout would
republish the entire history into Loki.

## Deployment

Helm: `selfHealMetrics.*` in `helm/fuzeinfra/values.yaml`, rendered by
`helm/fuzeinfra/templates/selfheal-metrics.yaml`. Off everywhere except
`values-contabo.yaml` (prod is where the loop runs). Image built by
`.github/workflows/build-selfheal-metrics-exporter.yml`.
