# `argocd/cluster-settings/`

ArgoCD's **own** configuration ConfigMaps (`argocd-cm`, `argocd-rbac-cm`),
applied by `.github/workflows/apply-cluster-config.yml` on every commit here.

## Why this is separate from `argocd/cluster-config/`

Different apply semantics, and the difference is load-bearing.

`argocd/cluster-config/` holds whole objects that FuzeInfra owns outright
(repo-cred templates, image-pull secrets), so a plain client-side
`kubectl apply -f` is correct there.

The files here **do not own the objects they modify**. ArgoCD is installed from
the upstream `stable` `install.yaml` over SSH
(`terraform/contabo/provisioning.tf`), which ships real defaults inside
`argocd-cm.data` — `resource.exclusions` and the
`resource.customizations.ignoreResourceUpdates.*` set. A subsequent
`kubectl patch` in the same provisioner adds `url` and a custom Ingress health
check.

A client-side `kubectl apply` of a partial ConfigMap three-way-merges against
install.yaml's `last-applied-configuration` and **deletes every key it omits**.
Losing the Ingress health customization would leave every Argo Application
stuck `Progressing` forever, because Traefik is pinned to ClusterIP behind the
Cloudflare tunnel and Ingresses therefore never get an `ADDRESS`.

So this directory is applied with:

```bash
kubectl apply --server-side --force-conflicts \
  --field-manager=fuzeinfra-cluster-settings -f argocd/cluster-settings/
```

`ConfigMap.data` is a *granular* map under server-side apply, so ownership is
tracked per key. We own exactly the keys we declare; everything else stays with
whichever manager set it.

## Rules for adding to this directory

- **Declare only the keys you intend to own.** Do not "helpfully" restate
  upstream defaults — you would then be responsible for them across every ArgoCD
  upgrade.
- **Never declare `accounts.*` or `admin.*` in `argocd-cm`.** The local `admin`
  account is deliberate break-glass: a malformed `oidc.config` can make the UI
  reject every login, and `argocd login --username admin` over a port-forward is
  the way back in.
- **Anything applied here must be reversible without cluster access**, since the
  only route in when ArgoCD's UI is broken is the port-forward above.

## What lives here

- **`argocd-cm.yaml`** — `oidc.config` only. Points ArgoCD at Authentik
  (`authentik.prod.fuzefront.com`, inside the Access wall) so login uses the
  platform identity rather than the local admin password.
- **`argocd-rbac-cm.yaml`** — maps the Authentik group `fuzeinfra-admins` to
  `role:admin`, with `policy.default: ''` (fail closed).

The client secret comes from the `argocd-oidc` Secret in the `argocd` namespace;
see `deploy/sealed-secrets/authentik-oidc-secrets.yaml.template`. The matching
OIDC provider is defined as an Authentik blueprint in `FuzeOne/FuzeFront`
(`deploy/helm/fuzefront/authentik/blueprints/provider-oidc-fuzeinfra-admin.yaml`).
