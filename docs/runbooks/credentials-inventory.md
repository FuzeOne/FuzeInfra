# Where every credential lives, and how to fetch or reset it

Written because "what is the Grafana admin password?" had no good answer: it was
generated, sealed, and the plaintext shredded in the same command — correct
handling, but it left no documented way to get it back.

**The rule that follows from that:** a sealed secret is write-only. Once sealed,
the plaintext exists only inside the cluster. If a human ever needs to read a
credential, it must either be handed over at generation time or rotated to a new
known value. There is no "look it up later".

---

## 1. Read this first: you usually cannot read a secret back

| Path | Works? |
|---|---|
| Decrypt the SealedSecret in git | **No.** Only the in-cluster controller holds the private key. |
| `cluster-query` workflow | **No, by design.** It refuses `kubectl get secret`; its job logs are public. |
| Ask an agent that sealed it | **No.** Plaintext is shredded immediately after sealing. |
| `kubectl get secret -o jsonpath` as an operator with the real kubeconfig | **Yes** — this is the only read path. |
| Rotate to a new value and capture it at generation | **Yes** — the preferred path. |

---

## 2. Inventory

### Admin-UI logins (what a human types)

| What | Where the value lives | How to reset |
|---|---|---|
| **Grafana** local admin (`admin`) | SealedSecret `fuzeinfra/grafana-admin`, key `GRAFANA_ADMIN_PASSWORD`. Chart default is the literal `admin`; the overlay points at this Secret via `grafana.adminPasswordSecret`. | Reseal + **bump `grafana.adminPasswordSecret.rotation`** in `values-contabo.yaml`. The bump is what rolls the pod — see §4. |
| **ArgoCD** local admin | `argocd-initial-admin-secret` in ns `argocd`, created by ArgoCD itself. | `argocd account update-password`, or delete the secret and restart the server. |
| **Authentik** `akadmin` | SealedSecret `fuzefront/fuzefront-secrets`, key `AUTHENTIK_BOOTSTRAP_PASSWORD` (FuzeOne/FuzeFront). | Reseal that key, then roll the Authentik pods (§4). |
| **Airflow** admin | SealedSecret `fuzeinfra/fuzeinfra-app-credentials`, key `AIRFLOW_ADMIN_PASSWORD`. | Reseal. **Only applies on a fresh metadata DB** — `airflow-init` runs `users create ... \|\| true`. For an existing user: `airflow users reset-password`. |
| **RabbitMQ** admin | SealedSecret `fuzeinfra/fuzeinfra-app-credentials`, key `RABBITMQ_PASSWORD`. | Reseal. **Only applies on first boot against empty mnesia.** Otherwise `rabbitmqctl change_password`. |
| Everything behind Cloudflare Access with no login of its own (Prometheus, Alertmanager, Mongo Express, ChromaDB) | n/a — Access **is** the auth layer | Change `allowed_admin_emails` in `terraform/contabo/variables.tf`. |

### OIDC client secrets — always a PAIR, always sealed twice

Each value is sealed once for the IdP (FuzeFront) and once for the consumer
(FuzeInfra). **Rotating one side alone breaks the handshake** with a generic
"Invalid credentials" that implicates neither side.

| Client | IdP side — `fuzefront/fuzefront-secrets` | Consumer side |
|---|---|---|
| Cloudflare Access | `CF_ACCESS_CLIENT_SECRET` | GitHub secret `AUTHENTIK_CF_ACCESS_CLIENT_SECRET` |
| Grafana | `GRAFANA_CLIENT_SECRET` | `fuzeinfra/grafana-oidc` → `CLIENT_SECRET` |
| ArgoCD | `ARGOCD_CLIENT_SECRET` | `argocd/argocd-oidc` → `clientSecret` (camelCase) |
| Kafka UI | `KAFKA_UI_CLIENT_SECRET` | `fuzeinfra/kafka-ui-oidc` → `CLIENT_SECRET` |
| Airflow | `AIRFLOW_CLIENT_SECRET` | `fuzeinfra/airflow-oidc` → `CLIENT_SECRET` |
| LiteLLM | `LITELLM_CLIENT_SECRET` | `fuzeinfra/litellm-oidc` → `CLIENT_SECRET` |

Procedure: `deploy/sealed-secrets/authentik-oidc-secrets.yaml.template`.

### Not GitOps at all

**Google OAuth clients** — no API, no Terraform provider, Console only. Full
detail in [google-oauth-manual-dependency.md](google-oauth-manual-dependency.md).
If that client is recreated, every redirect URI must be re-added by hand.

---

## 3. Reading a value as an operator

The only supported read path, and it needs the real kubeconfig — not
`cluster-query`, which blocks Secret reads on purpose:

```bash
kubectl -n fuzeinfra get secret grafana-admin \
  -o jsonpath='{.data.GRAFANA_ADMIN_PASSWORD}' | base64 -d; echo
```

Do not paste the output into a chat, an issue, or a CI log. That is exactly what
the `cluster-query` guard exists to prevent.

---

## 4. Resealing is only HALF a rotation

**Kubernetes does not restart a pod when a Secret changes** — env is read once at
startup. The chart cannot hash a SealedSecret's ciphertext either, because Argo
applies it from outside the release. So every consumer needs its own explicit
roll trigger, and a reseal without one leaves the OLD value live:

| Consumer | Roll trigger |
|---|---|
| Authentik (server + worker) | `checksum/blueprints` — touch any file under `authentik/blueprints/` |
| Grafana | `checksum/credentials` — bump `grafana.adminPasswordSecret.rotation` |
| Kafka UI | `checksum/oidc` — bump `kafkaUi.oidc.secretRotation` |
| Airflow webserver | `checksum/oidc` — bump `airflow.oidc.secretRotation` |
| LiteLLM | `checksum/sso` — bump `sso.secretRotation` |

A full OIDC rotation is therefore: reseal **both** halves, bump the consumer's
rotation marker, touch a blueprint so Authentik rolls, and merge every PR
together. Merged apart, the halves are mismatched in the window between.

---

## 5. Handing a credential to a human

Never put it in a chat transcript, a PR body, or a CI log. Write it to a
`*.local.txt` file in the repo root — gitignored by rule — for the operator to
file in a password manager and then delete.
