# agent-templates — Managed-Agents role packs for a self-dispatching SDLC

> ⚠️ **This feature belongs in [FuzeAgent](https://github.com/FuzeOne/FuzeAgent), not FuzeInfra.**
> It was prototyped here (richest cluster-capable example) but is application-level agent
> orchestration, not shared infra. Port it to FuzeAgent — see
> [PORTING-TO-FUZEAGENT.md](PORTING-TO-FUZEAGENT.md). The generic role/environment/permission
> *pattern* is propagated up to the FuzeSDLC L0 baseline.

Portable **engineer-role agents** (frontend, backend, qa, devops) built on Anthropic
**Claude Managed Agents**. Each role bundles three things so a coding session never
stalls asking a human to "run this on the cluster" or "do this on GitHub":

1. **Persona** — the existing `.claude/agents/*.md` (git source of truth) becomes the agent `system`.
2. **Environment** — a `POST /v1/environments` sandbox with the role's packages + network reach.
3. **Permissions** — per-tool policies (`always_allow` / `always_ask`) + credential scoping.

A **coordinator** agent routes each request to the role whose environment already holds
the needed access, so work is dispatched rather than bounced back to a human.

> Vendor-semi-agnostic: the `.md` personas keep driving Claude Code and the `@fuze`
> GitHub Action. This layer *projects* them into `/v1/agents`; nothing is forked.

> **Provider-abstracted.** The definitions + orchestration are provider-neutral; a
> `providers/<name>/adapter.py` targets a backend. `anthropic` is the reference impl;
> `openai`/`hermes` are stubs. Pick with `AGENT_PROVIDER`. See [providers/README.md](providers/README.md).

## Roles are one-per-role, not per-repo

There is **one `backend` agent template**, not `fuzeinfra-backend` + `fuzefront-backend` + …. A
repo is not a different agent — it's a different **environment** bound at launch (toolchain,
checkout, networking, creds) plus its repo-expert. A **session** is one `role × environment`
instantiation; you run many in parallel per feature. So *"fuzefront-backend"* = the Backend
template × the FuzeFront env (+ its vault/skills), assembled at `create_session` (optionally via
`agent_with_overrides`) — not a stored 16th agent. See [docs/fuzeone-agent-map.html](docs/fuzeone-agent-map.html).

## Layout

| Path | What |
|---|---|
| `providers/` | provider seam: `base.py` interface, registry, `anthropic/` (ref) + `openai`/`hermes` stubs, `provision.py` |
| `schema/` | JSON Schemas for `role.json`, environment, vault, memory configs |
| `roles/_base/role.json` | shared guardrail system-prompt, default tools + policies, github + handoff MCP |
| `roles/{frontend,backend,qa,devops}/role.json` | per-role overrides (`qa` reuses `test-engineer`) |
| `coordinator/coordinator.json` | routing agent (`multiagent` roster over the 4 roles) |
| `environments/*.json` | `cloud-*` (Anthropic sandbox) + `selfhosted-devops` (our worker) |
| `vaults/`, `memory/` | MCP-auth vault + shared cross-session handoff memory store |
| `orchestration/` | handoff MCP server + `relay.py` (session-resume/memory hand-forward) |
| `worker/` | self-hosted worker image + guard shims + k8s deploy |
| `sync/` | REST client, session driver, manifest loader, validate, launch |

## Role → environment → guardrail matrix

| Role | Env | Access | Guardrails |
|---|---|---|---|
| frontend | `cloud-frontend` | github MCP; no cluster | agent toolset `always_allow` |
| backend | `cloud-backend` | github MCP; DB client libs; no cluster | `always_allow` |
| qa (test-engineer) | `cloud-qa` | github MCP; app URLs read-only | `always_allow`; never patch to force green |
| devops | **`selfhosted-devops`** | real kubeconfig + PAT on our worker; reaches private k3s | `bash` **`always_ask`**; guard shims block `kubectl delete\|patch\|edit`, `helm uninstall\|rollback`, `terraform apply\|destroy`; prod-sanity system prompt |
| openapi-maintainer | `cloud-openapi-maintainer` | github MCP; spectral/redocly/openapi-typescript/prism | designs+freezes the contract; refreshes live Swagger on merge |
| mcp-engineer | `cloud-mcp-engineer` | github MCP; MCP SDK + inspector | keeps stdio + remote SSE MCP server current; **triggered on merge to main** |
| mobile-engineer | `cloud-mobile-engineer` | github MCP; playwright + lighthouse + workbox + capacitor | verifies responsive/mobile UI + mobile delivery (PWA/RN/Flutter) vs `mobile.requirements.yaml`; **triggered on merge to main** |

### Executive + persona tiers

Beyond the engineer roles:

| Role | Env | What it does |
|---|---|---|
| **CEO / CTO / CFO / CISO** | `cloud-exec` | Strategy / architecture / finance / security **oversight**. Read across GitHub/Jira/Slack (+ Stripe for CFO); **delegate** execution via the handoff tools; escalate binding decisions to the human via `reach_human`. Never implement; money/pricing/prod actions are `always_ask` + human-approved. |
| **digital-persona** | `cloud-digital-persona` | A **shared template representing one human** — bound at launch to that person's **vault** (email/Slack/GitHub/WhatsApp/Telegram/phone creds) + **metadata** (identity, preferred channels, quiet hours). Answers as them for non-binding questions; for real decisions it **reaches the human on their channels**, collects their actual reply, and **`resume_session`s** the waiting session. One human = one launch (their vault), never a separate agent. |

The handoff MCP server exposes **`reach_human(human, message, reply_to_session_id, channels)`** — spawns that human's digital persona (their vault) to get a real decision and relay it back. This is how an `always_ask`/human-needed pause is fulfilled asynchronously across a person's real channels instead of stalling. Per-human vault template: `vaults/examples/persona.json` → copy to `vaults/persona-<human>.json` and provision.

### Product + go-to-market tiers

Beyond engineering + executives, the org roles (reached by the executive coordinator via the handoff MCP — each in its own environment):

| Role | Env | What it does |
|---|---|---|
| **product-manager** | `cloud-product` | Defines **what** to build + why: prioritized requirements, specs, acceptance criteria, roadmap; owns backlog *content* (feeds ux-designer / openapi-maintainer / the SDLC coordinator). Distinct from `agile-manager` (sprint cadence). |
| **ux-designer** | `cloud-product` | UX + interface **design** — research, IA, flows, wireframes, interaction + visual design in **Figma**, a11y — and hands design-system-aligned specs to `frontend-engineer`. **Design, not implementation** (very different from frontend-engineer). |
| **sales-rep** | `cloud-gtm` | Pipeline/CRM, account research, call prep, outreach + proposal **drafts**. Drafts/proposes; the human sends/signs. Never commits pricing/contracts or moves money (outbound `always_ask`). |
| **marketing** | `cloud-gtm` | Content, campaigns, SEO, brand, performance reporting. Drafts/plans; publishing public content or list sends is `always_ask` (human-approved). |
| **support-rep** | `cloud-gtm` | Ticket triage, KB-grounded response **drafts**, KB upkeep, correct escalation (bugs→engineering, security→CISO, billing→CFO). Sending a customer reply is `always_ask`; never commits refunds/SLAs/roadmap. |

All outbound/customer-facing tools are `always_ask` and route to a human via `reach_human`. Extra MCP URLs these reference: `FIGMA_MCP_URL` (ux), `CRM_MCP_URL` + `CALENDAR_MCP_URL` (sales), `SUPPORT_MCP_URL` (support), plus the shared `SLACK_MCP_URL` / `GMAIL_MCP_URL` / `ATLASSIAN_MCP_URL`.

### Two-tier coordinators

- **SDLC coordinator** (`coordinator/coordinator.json`) routes engineering work via the **handoff MCP** (`ask_agent`/`spawn_agent`) so each role runs in its **own** environment (cloud-* or the devops self-hosted worker) — not as in-session sub-agents, which would share one environment and lose each role's access.
- **Executive coordinator** (`coordinator/exec-coordinator.json`) sits above it: it delegates strategy/architecture/finance/security questions to the CEO/CTO/CFO/CISO (they share `cloud-exec`, so as in-session sub-agents) and hands anything to be **built/deployed/tested down to the SDLC coordinator** via the handoff MCP. Binding human decisions go through `reach_human`.

## Prerequisites

- `ANTHROPIC_API_KEY` (org has Managed Agents; beta `managed-agents-2026-04-01`).
- `GITHUB_MCP_URL` — URL of a GitHub MCP server (auth handled at the server); referenced by `roles/_base/role.json`.
- For **devops**: a self-hosted environment key (Console: Environments → the env → *Generate environment key*),
  a host inside our network to run `worker/` (holds a **scoped-RBAC** kubeconfig + fine-scoped PAT), and the image built/pushed.
- For the **executive + persona** roles: the extra MCP server URLs their manifests reference —
  `ATLASSIAN_MCP_URL`, `SLACK_MCP_URL`, `STRIPE_MCP_URL` (CFO), and `GMAIL_MCP_URL` / `TELEGRAM_MCP_URL` /
  `TWILIO_MCP_URL` (digital-persona) — plus per-human vault tokens (`vaults/examples/persona.json`). Roles
  whose MCP URLs aren't set can't be launched live until they are (dry-run/validate still pass).
- `pip install -r sync/requirements.txt` (only `jsonschema`, for `validate.py`).

## Usage

```bash
cd agent-templates

# 1. validate every manifest (schema + persona render) before touching the API
python sync/validate.py

# 2. preview the full create-plan offline — no API calls (works even without a valid key)
python providers/provision.py --provider anthropic --dry-run

# 3. create/update EVERYTHING for the provider (environments + vaults + memory + agents +
#    coordinator), idempotently; prints every id and writes .state/*.json. This is the
#    "all agents created with their envs" step. (reads ${GITHUB_MCP_URL/TOKEN}, ${HANDOFF_MCP_URL})
GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/ GITHUB_MCP_TOKEN=... HANDOFF_MCP_URL=... \
  python providers/provision.py --provider anthropic
# (the individual sync/sync_*.py scripts still exist for piecemeal runs)

# 3d. run the handoff MCP server (agent-to-agent spawn/resume/ask + memory) — container in prod
python orchestration/handoff_mcp/server.py          # local; or deploy the image (see orchestration/)

# 4a. run a self-hosted worker on an in-network host (devops role executes here)
docker build -t fuzeinfra/agent-worker:dev worker
ANTHROPIC_ENVIRONMENT_ID=env_... ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-... \
  docker run --rm -e ANTHROPIC_ENVIRONMENT_ID -e ANTHROPIC_ENVIRONMENT_KEY \
  -v "$HOME/.kube:/workspace/.kube:ro" fuzeinfra/agent-worker:dev

# 4b. launch a session
python sync/launch_session.py --role qa --prompt "list the tests you would run"
python sync/launch_session.py --coordinator --prompt "validate and deploy the new Grafana dashboard"
```

`launch_session.py` opens the SSE stream, sends the prompt, prints output, and on an
`always_ask` pause surfaces each blocking tool call for `allow`/`deny` (interactive by
default; `--auto allow|deny` for headless; the `approve()` hook is where a Telegram
round-trip plugs in).

## How this removes the "I don't have access" stall

Access is a property of the **environment**, fixed at definition time — not negotiated
mid-session. Cloud roles hold no cluster/prod credentials; the **devops** role runs on a
**self-hosted worker inside our network** where the kubeconfig, PAT and cloud keys live and
the private k3s is reachable. The **coordinator** classifies each request and delegates to
the role whose environment already has the access. Irreversible/prod actions are gated three
ways: control-plane `always_ask` approval, OS-level guard shims on the worker, and a
prod-sanity system prompt. Scoped-RBAC on the worker's ServiceAccount is the primary control;
the shims are a backstop.

## Propagation

Prototyped here in FuzeInfra. The tree is repo-agnostic and lifts into the **FuzeSDLC** L0
baseline as the canonical source; `governance-nightly` drift-checks each repo's roles against
it. `.fuze/manifest.json` gains a `roles` section mirroring `agents`.

## Notes / to confirm before production

- The `ant` CLI install package in `worker/Dockerfile` (`ANT_CLI_PKG`) is a best guess —
  confirm against the Managed Agents quickstart.
- Environments are **not versioned**; changing an `environments/*.json` config means
  delete+recreate (or rename). `sync_environments.py` is create-if-missing by name.
- Give the devops worker a **scoped-RBAC** kubeconfig, not cluster-admin — the guard shims
  are defense-in-depth, not the primary boundary.
