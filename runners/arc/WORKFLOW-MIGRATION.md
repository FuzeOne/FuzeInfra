# Migrating FuzeInfra workflows off `ubuntu-latest` → self-hosted `staging`

Tracking issue: **#236**. The Claude GitHub App cannot write to `.github/workflows/**`
(it lacks the `workflows` permission), so this migration is delivered as
ready-to-apply diffs + an apply script that a maintainer runs locally.

## Why

When org-hosted Actions minutes are exhausted, every `ubuntu-latest` job dies in
~2s with no logs — including **`claude.yml`**, the cross-repo `@fuze` handler.
That is exactly why delegated issues sat un-actioned. Moving the handler and the
CI/build jobs onto the always-available ARC scale set (`runs-on: staging`) makes
delegation, builds, and prod introspection independent of hosted minutes.

> Prerequisite for the compose/build jobs: the runners must be on the FuzeInfra
> runner image (docker compose + buildx). See `runners/arc/Dockerfile` and flip
> `image:` in `runner-scale-set-values.yaml` first — otherwise `infrastructure-tests`
> etc. will still fail with `unknown shorthand flag: 'f' in -f`.

## Current state (already migrated)

Most infra workflows are **already** `runs-on: staging`: `helm-validate`,
`deploy-prod`, `harden-gate`, `argocd-register`, `cluster-query`,
`nightly-integration`, `governance-nightly`, `tf-elastic-drift-check`,
`infra-request-handler`, `infra-request-apply-on-approve`,
`argo-outofsync-autofix`, `apply-cluster-config`, `arc-smoke-test`.
`kind-validate` runs on **`ubuntu-latest`** — it was moved off the
`[self-hosted, kind-host]` host runner after that runner went offline on
2026-07-24 and silently stopped the gate for days (see runners/README.md).

## MUST stay on `ubuntu-latest` (chicken-and-egg — they rebuild the runners)

`arc-bootstrap`, `arc-reset`, `arc-reinstall-scaleset`, `arc-restart-listener`,
`arc-debug`, `arc-diagnose`, and the new `build-runner-image` (it builds the
image the runners use). **Do not touch these.**

## Migrate: `ubuntu-latest` → `staging`

| Workflow | Line | Priority | Why |
|---|---|---|---|
| `claude.yml` | 44 | **critical** | the `@fuze` handler — must not depend on hosted minutes |
| `claude-smoke.yml` | 22 | high | smoke-checks the handler |
| `claude-auto-pr.yml` | 18 | high | opens PRs from `claude/**` branches |
| `infrastructure-tests.yml` | 22 | high | uses `docker compose` (needs the runner image) |
| `ci-failure-triage.yml` | 45 | medium | CI triage |
| `auto-merge.yml` | 43 | medium | PR automation (`gh` only) |
| `update-ignore-list.yml` | 17 | medium | repo housekeeping |
| `argo-deploy-notify.yml` | 32 | medium | notification |
| `grafana-crit-fix.yml` | 59, 104 | medium | cluster-facing |
| `provision-mendys.yml` | 23 | medium | runs `kubectl` against the cluster |
| `rotate-sealed-secret.yml` | 59 | medium | runs `kubectl`/`kubeseal` against the cluster |
| `ca-provider-image.yml` | 22 | optional | image build (needs buildx — runner image has it) |
| `terraform-plan-apply.yml` | 94, 232 | optional | Terraform CD |
| `deploy-ec2.yml` | 75, 184, 241, 684 | optional | AWS EC2 deploy (independent of the k3s cluster) |
| `actionlint.yml` | 27 | optional | pure YAML lint |

> `claude-ci-autofix.yml` is a **reusable-workflow caller**
> (`uses: FuzeOne/FuzeSDLC/.github/workflows/reusable-claude-ci-autofix.yml@main`). It has
> no `runs-on` of its own — the runner is now chosen **here**, by passing a `runner:` input
> to the reusable workflow. (This previously said the `runs-on` lived in the AITools repo
> and had to be migrated there. AITools has been retired into FuzeSDLC, and hard-coding the
> runner in the hosting repo was the defect: a reusable workflow's job runs in the CALLER's
> context and bills to the CALLER, so the hosting repo can never know the right value.)
> `cluster-query.yml` is **already** on `staging` (issue text was stale).

Use the established bare-label form to match the existing self-hosted workflows:

```diff
-    runs-on: ubuntu-latest
+    runs-on: staging
```

## Apply script

```bash
bash runners/arc/migrate-workflows-to-staging.sh          # critical + high + medium
bash runners/arc/migrate-workflows-to-staging.sh --all    # also the optional ones
git diff .github/workflows                                 # review, then commit with a PAT
```
