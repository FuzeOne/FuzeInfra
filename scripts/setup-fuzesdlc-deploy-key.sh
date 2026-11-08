#!/usr/bin/env bash
# Provision the ONE read-only FuzeSDLC deploy key and distribute it EVERYWHERE IT IS NEEDED.
#
# ⚠️  RUN THIS YOURSELF — it modifies access controls (registers a deploy key on FuzeSDLC) and
#     writes repo secrets. It is intentionally NOT run by automation.
#
# Trust model (min-privilege, max-decoupling): a SINGLE read-only deploy key grants read to
# FuzeOne/FuzeSDLC ONLY. Its private half is distributed as the `FUZESDLC_DEPLOY_KEY` secret
# to each consuming repo, whose governance-sync.yml uses it to fetch the FuzeSDLC canonical.
# No repo ever gets write to another; each repo reconciles with its OWN GITHUB_TOKEN + this
# shared read key. FuzeSDLC never needs write to any consumer (no fan-out, no hub-held key
# that could read every repo).
#
# "Everywhere needed" is COMPUTED, not hardcoded: the targets are exactly the repos that have
# `.github/workflows/governance-sync.yml` on their default branch. Onboard a new repo with
# sdlc-bootstrap.sh, re-run this, and it picks the repo up. No list to drift.
#
# NOTE: you do NOT need FuzeSDLC's Actions "Access" setting for this. The stamped
# governance-sync workflow is deliberately SELF-CONTAINED (it fetches the canonical with this
# key rather than `uses:`-ing a cross-repo reusable workflow, which cannot resolve against a
# private FuzeSDLC and would red every PR). Earlier revisions of this script said otherwise —
# that instruction was obsolete.
#
# Idempotent: an existing key is reused (never silently rotated); an already-set secret is
# reported and skipped unless --force.
#
# Prereqs: `gh` authenticated as an owner/admin of FuzeOne/FuzeSDLC and the targets;
# `ssh-keygen` available.
#
# Usage:
#   scripts/setup-fuzesdlc-deploy-key.sh                 # auto-discover + provision + verify
#   scripts/setup-fuzesdlc-deploy-key.sh --dry-run       # show what it WOULD do, touch nothing
#   scripts/setup-fuzesdlc-deploy-key.sh FuzeOne/FuzeX  # restrict to explicit repos
set -uo pipefail

OWNER=FuzeOne
SDLC_REPO="$OWNER/FuzeSDLC"
KEY_TITLE="governance-sync-ro"
DRY=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1;;
    -h|--help) sed -n '2,32p' "$0"; exit 0;;
    *) ARGS+=("$a");;
  esac
done

command -v gh >/dev/null || { echo "gh CLI required"; exit 1; }
command -v ssh-keygen >/dev/null || { echo "ssh-keygen required"; exit 1; }

# ---- 1. Work out where the key is actually needed ---------------------------------------
if [ "${#ARGS[@]}" -gt 0 ]; then
  TARGETS=("${ARGS[@]}")
else
  echo ">> discovering repos that use governance-sync (i.e. that need the key)…"
  mapfile -t TARGETS < <(
    gh repo list "$OWNER" --limit 200 --json name,isArchived \
      --jq '.[] | select(.isArchived==false) | .name' 2>/dev/null |
    while read -r n; do
      if gh api "repos/$OWNER/$n/contents/.github/workflows/governance-sync.yml" >/dev/null 2>&1; then
        echo "$OWNER/$n"
      fi
    done
  )
fi
[ "${#TARGETS[@]}" -gt 0 ] || { echo "no target repos found (none have governance-sync.yml)"; exit 1; }
echo ">> ${#TARGETS[@]} repo(s) need FUZESDLC_DEPLOY_KEY:"
printf '     %s\n' "${TARGETS[@]}"

if [ "$DRY" = "1" ]; then
  echo ">> DRY RUN — would register a read-only '$KEY_TITLE' deploy key on $SDLC_REPO and set"
  echo "   FUZESDLC_DEPLOY_KEY on each repo above. Nothing changed."
  exit 0
fi

# ---- 2. The keypair ----------------------------------------------------------------------
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
key="$tmp/fuzesdlc_ro"

if gh api "repos/$SDLC_REPO/keys" --jq '.[].title' 2>/dev/null | grep -qx "$KEY_TITLE"; then
  cat <<EOF
>> A '$KEY_TITLE' deploy key ALREADY exists on $SDLC_REPO.
   Its private half exists only where you previously distributed it — it cannot be re-read
   from GitHub. If any repo below is missing the secret, ROTATE deliberately:
     1. delete the '$KEY_TITLE' key: $SDLC_REPO -> Settings -> Deploy keys
     2. re-run this script (it mints a fresh pair and re-distributes to every target)
   Refusing to guess. Nothing changed.
EOF
  exit 1
fi

ssh-keygen -t ed25519 -N "" -C "fuzesdlc-governance-sync-ro" -f "$key" >/dev/null
echo ">> generated an ephemeral ed25519 keypair (the private half leaves this run only as repo secrets)"

gh api "repos/$SDLC_REPO/keys" -f title="$KEY_TITLE" -f key="$(cat "$key.pub")" -F read_only=true >/dev/null \
  || { echo "!! failed to register the deploy key on $SDLC_REPO"; exit 1; }
echo ">> registered READ-ONLY deploy key '$KEY_TITLE' on $SDLC_REPO"

# ---- 3. Distribute -----------------------------------------------------------------------
fail=0
for repo in "${TARGETS[@]}"; do
  if gh secret set FUZESDLC_DEPLOY_KEY -R "$repo" < "$key" 2>/dev/null; then
    echo "   + $repo"
  else
    echo "   ! $repo — FAILED to set secret"; fail=1
  fi
done

# ---- 4. Verify (do not trust the writes) -------------------------------------------------
echo ">> verifying…"
missing=0
for repo in "${TARGETS[@]}"; do
  gh api "repos/$repo/actions/secrets" --jq '[.secrets[].name]|join(",")' 2>/dev/null \
    | grep -q 'FUZESDLC_DEPLOY_KEY' || { echo "   ! $repo — secret NOT present"; missing=1; }
done
[ "$missing" = "0" ] && echo "   all ${#TARGETS[@]} repo(s) have FUZESDLC_DEPLOY_KEY" || fail=1

cat <<EOF

>> DONE. governance-sync stops skipping and starts reconciling on each repo's NEXT PR.
   Watch the first few runs: this is the moment the fleet begins enforcing policy.

   Min-privilege check (worth doing once): the key reads $SDLC_REPO ONLY — it cannot write,
   and cannot read any other repo.

   Still needed for the expert-publish path (a SEPARATE credential, not this one):
   FUZESDLC_AGENT_PUSH_TOKEN — a fine-grained PAT scoped to $SDLC_REPO only, Contents +
   Pull requests = read/write. GitHub has NO API to mint a PAT, so it is browser-only:
   https://github.com/settings/personal-access-tokens/new
   Then: for r in ${TARGETS[*]}; do printf '%s' "\$T" | gh secret set FUZESDLC_AGENT_PUSH_TOKEN -R "\$r"; done
EOF
exit "$fail"
