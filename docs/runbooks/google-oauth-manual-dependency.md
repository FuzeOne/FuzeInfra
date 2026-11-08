# Google OAuth client — the one piece that is NOT GitOps

**Read this before recreating, rotating, or migrating the Google OAuth clients.**

Everything else in the admin-plane SSO stack is declarative: Cloudflare Access lives in
`terraform/contabo/cloudflare.tf`, Authentik's providers and groups live in blueprints in
`FuzeOne/FuzeFront`, consumer wiring lives in `helm/fuzeinfra`. All of it survives a
rebuild.

**Google OAuth clients do not.** Google exposes no API and no Terraform provider for
OAuth 2.0 Web-application client IDs or their authorized redirect URIs — the Cloud
Console is the only interface. `gcloud` cannot help either: its `alpha iap oauth-clients`
commands cover IAP brands only, not these.

So this is the single manual dependency in the chain, and if it is lost, **every admin
login and every product Google sign-in breaks at once**, with an error that points at the
wrong layer (`redirect_uri_mismatch` looks like an application bug).

---

## What exists today

Google Cloud project **`cojira`** (FuzeOne, project number `870279214034`) →
**APIs & Services → Credentials**.

### Client: `FuzeOne` — `870279214034-4rjq….apps.googleusercontent.com`

Used by BOTH the FuzeFront product login and Authentik's Google source.

| # | Authorized redirect URI | Used by | Breaks if missing |
|---|---|---|---|
| 1 | `https://app.fuzefront.com/source/oauth/callback/google/` | Authentik Google **source** on the app host | Authentik "sign in with Google" |
| 2 | `https://app.fuzefront.com/api/v1/security/social/google/callback` | security-service **brokered** login (`securityService.googleBrokered: "true"`) | ALL FuzeFront customer logins |
| 3 | `https://authentik.prod.fuzefront.com/source/oauth/callback/google/` | Authentik Google source on the **admin** host | Admin-plane SSO (Grafana / ArgoCD / Kafka UI) |

Authorized JavaScript origin: `https://app.fuzefront.com`

> Entry 3 is the one added most recently and the easiest to forget. Authentik builds its
> Google callback from **whichever host the browser is on**, so every host that can
> initiate a Google login needs its own entry.

### Client: `Cloudflare Access - FuzeInfra admin plane`

Separate client, deliberately. Used only by the Cloudflare Access identity provider.

| Authorized redirect URI |
|---|
| `https://fuzefront.cloudflareaccess.com/cdn-cgi/access/callback` |

Its credentials are wired as GitHub secrets `GOOGLE_ACCESS_CLIENT_ID` /
`GOOGLE_ACCESS_CLIENT_SECRET`, consumed by `TF_VAR_google_access_client_*` in
`.github/workflows/terraform-plan-apply.yml`.

**Do not merge the two clients.** They have different redirect URIs and independent
rotation. Cross-wiring them fails with `redirect_uri_mismatch`.

---

## Recreating from scratch

1. Console → **APIs & Services → Credentials → Create Credentials → OAuth client ID**,
   type **Web application**.
2. Add the redirect URIs from the tables above, exactly — including trailing slashes.
   `…/callback/google/` and `…/callback/google` are different URIs to Google.
3. For the **Cloudflare Access** client, put the new client id + secret into the two
   GitHub secrets above, then re-run the Terraform CD workflow. The IdP resource is
   count-gated on both values: if either is empty the plan is a **DESTROY** of the IdP
   and its policies, silently dropping the admin plane to email-OTP only.
4. For the **FuzeOne** client, the secret is consumed by Authentik and security-service
   as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, sealed in
   `deploy/contabo/sealed/fuzefront-secrets.yaml` (FuzeOne/FuzeFront). Reseal with
   `scripts/seal-secret.sh` and let Argo sync.

## Adding a new host that initiates Google login

Register `https://<new-host>/source/oauth/callback/google/` on the **FuzeOne** client
BEFORE pointing anything at that host. Skipping this produces `Access blocked: This app's
request is invalid — Error 400: redirect_uri_mismatch`, which reads like an application
fault rather than a missing Console entry.

## Consent screen — Testing mode

**Audience → Publishing status is `Testing`**, External, with one test user
(`izzy.weinberg@gmail.com`).

This is useful for the admin plane — it is a second allowlist on top of the Cloudflare
Access `require { email = … }` policy. But it is **project-wide**, so the FuzeOne client
is under the same restriction: *product* Google sign-in also only works for listed test
users. Adding a customer means adding a test user, or publishing the app.

Note also that in Testing mode Google refresh tokens expire after 7 days. Cloudflare
Access re-runs the authorization-code flow per session so it is unaffected; anything that
relies on a long-lived Google refresh token is not.

---

## Verifying without logging in

Whether a redirect URI is registered can be checked with an unauthenticated request —
no credentials, no browser:

```bash
CID="870279214034-4rjq….apps.googleusercontent.com"
RU="https%3A%2F%2Fauthentik.prod.fuzefront.com%2Fsource%2Foauth%2Fcallback%2Fgoogle%2F"
curl -s -o /dev/null -w '%{redirect_url}\n' \
  "https://accounts.google.com/o/oauth2/v2/auth?client_id=$CID&redirect_uri=$RU&response_type=code&scope=openid%20email"
```

- lands on `…/v3/signin…` → the URI **is** registered
- lands on `…/signin/oauth/error?authError=…` → decode the base64 `authError`; a
  `redirect_uri_mismatch` names the exact URI Google rejected

Run this for all three FuzeOne URIs after any Console change. It is the cheapest way to
confirm you did not clobber an existing entry while adding a new one — entry 2 carries
every customer login.

---

## Why this cannot be automated

Asked and answered, so nobody re-investigates:

- No Terraform provider manages Google OAuth **client** resources.
- The IAP OAuth API (`gcloud alpha iap oauth-clients`) covers IAP brands, not
  general-purpose Web-application clients.
- The Cloud Console reveals a client secret **once** at creation, and only offers
  "download JSON" afterwards.

If Google ever ships an API for this, replacing this runbook with Terraform is the right
move. Until then, treat the tables above as the source of truth and update them in the
same PR as any host change.
