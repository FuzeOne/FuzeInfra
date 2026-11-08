# Domain agents

Single-responsibility agents for building features in FuzeInfra. Each agent owns
**one domain**, hard-stops at its boundary, and is **forbidden from grading the
whole feature** — it reports only its own slice. This exists because a monolithic
feature-agent once reported "DONE/GREEN" while siblings (UI/tests) were unbuilt.
Scoped agents that report an honest, scoped "done" are the guardrail.

> Boundary = **domain, not repo**. Agents *enforce scope*; skills are *procedure*.
> This set mirrors `FuzeOne/FuzeFront`'s domain-agent set, adapted for FuzeInfra.

## The roles

| Agent | Owns | Never touches |
|---|---|---|
| **contract-designer** | Frozen OpenAPI + event schemas + generated client, PR'd | Implementation of anything |
| **backend-engineer** | Services, APIs, DB, migrations + own unit tests | Contract, UI, acceptance tests, infra, docs |
| **frontend-engineer** | UI / dashboard / operator config surface | Backend, infra, tests, docs |
| **test-engineer** | INDEPENDENT acceptance/contract/integration tests | Implementing or fixing product code |
| **devops-engineer** | Helm chart/values, Argo, CI, kubeconform/helm-lint, reconciliation | Product code, UI, contract, product tests |
| **docs-maintainer** | Operator/onboarding docs only | Code, infra, tests, contract |

Every agent consults the repo expert **`fuzeinfra-expert`** for conventions.

## The mandatory DONE contract (non-negotiable)

No agent may ever declare the *feature* done. Each reports **only its slice**, in
exactly this shape:

```
SCOPE DONE (verified): <what exists in MY domain and how I verified it>
OUT OF SCOPE — NOT DONE: <everything outside my domain> — owned by their agents.
```

If something can't be verified, the agent says so. "Tests pass" is only ever
emitted by **test-engineer**, about tests it actually ran.

## Orchestration sequence

1. **contract-designer runs FIRST and alone** — the sequential gate. It freezes the
   OpenAPI + event schemas + generated client and opens a PR. Nothing else starts
   until that contract is merged.
2. Once the contract is frozen, **fan out the rest in parallel**, gated only on the
   contract: **backend-engineer**, **frontend-engineer**, **test-engineer**,
   **devops-engineer**, **docs-maintainer**.
3. **test-engineer is the independent grader** — it tests against the spec, not the
   implementation, and reports real results. A failing acceptance test is a correct
   output, not a blocker to hide.
4. The orchestrator (not any single agent) assembles the slice-level "done" reports
   to judge whether the *feature* is actually complete.

```
                 contract-designer  (sequential gate: freeze + PR contract)
                          │
        ┌──────────┬──────┴───────┬───────────────┬─────────────┐
backend-engineer  frontend-eng  test-engineer  devops-engineer  docs-maintainer
   (impl+unit)     (UI/dash)    (independent)   (chart/Argo/CI)    (operator docs)
```

## FuzeInfra platform facts every agent honors
- **Dual delivery:** local `docker-compose.FuzeInfra.yml` **and** Helm chart at `helm/fuzeinfra` (Argo CD).
- **GitOps prod / self-heal (`selfHeal: true`):** change git, let Argo reconcile — **never** `kubectl patch`/`edit` live resources.
- **Ingress:** Traefik → ClusterIP, **Cloudflare-tunnel-only** (no public LoadBalancer/NodePort).
- **Validation:** `helm lint` + `kubeconform -strict -ignore-missing-schemas` (per `.github/workflows/helm-validate.yml`).
- **CI secrets:** alphanumeric-only passwords.
