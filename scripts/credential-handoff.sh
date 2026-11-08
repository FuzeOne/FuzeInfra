#!/usr/bin/env bash
#
# credential-handoff.sh — the automated, plaintext-free credential hand-off
# between FuzeInfra (the provider) and a consumer repo. See
# docs/CREDENTIAL_HANDOFF.md for the design and FuzeInfra#510 for why it exists.
#
# ── The rule this file exists to enforce ─────────────────────────────────────
# A credential value NEVER reaches stdout, stderr, a job log, an artifact, a
# commit, or argv. Only three derived things are ever printed:
#   * SealedSecret ciphertext  — inert outside the target cluster
#   * sha256(value)[:16]       — a one-way fingerprint (the convention already
#                                established in docs/SECURE_AGENT_SECRET_HANDOVER.md)
#   * a pass/fail verdict
# Values live in files created under `umask 077` in a private temp dir that is
# wiped on exit, and are handed to consumers on **stdin** or through the
# environment — never as a command-line argument, which is visible in `ps`.
#
# This is deliberately NOT a relaxation of the cluster-query Secret-read guard
# (.github/workflows/cluster-query.yml). That guard blocks piping *arbitrary,
# caller-supplied* kubectl output into a retained public job log. Here the reads
# are fixed, named, and their output is provably never printed. The guard stays
# exactly as it is.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#   credential-handoff.sh publish [--id ID] [--force] [--registry FILE]
#   credential-handoff.sh verify  [--id ID]           [--registry FILE]
#   credential-handoff.sh list                        [--registry FILE]
#
#   publish  For every diverged hand-off: seal the current source value FOR the
#            consumer's namespace+name and open a PR on the consumer repo.
#            A hand-off that is already in sync is a NO-OP — no branch, no
#            commit, no PR. That is what keeps this from churning commits: the
#            work is driven by divergence (i.e. by rotation), never by the clock.
#   verify   Attempt a real authentication with the credential the consumer
#            ACTUALLY holds. Exits non-zero if any enabled hand-off fails.
#   list     Print the registry as TSV (ids + non-secret metadata only).
#
# Requires: kubectl (with a working KUBECONFIG), jq, python3, sha256sum|shasum.
#   publish additionally requires: kubeseal, git, gh (GH_TOKEN with write on the
#                                  consumer repo).
#   verify  additionally requires: psql.
#
set -euo pipefail

REGISTRY_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/governance/credential-handoff.json"
REGISTRY="${CREDENTIAL_HANDOFF_REGISTRY:-$REGISTRY_DEFAULT}"
ONLY_ID=""
FORCE=0
MODE=""

die()  { echo "error: $*" >&2; exit 1; }
note() { echo "$*" >&2; }   # progress → stderr; never a credential

# ── Private scratch space. Everything secret lives here and nowhere else. ─────
umask 077
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# fingerprint <file> → sha256(contents)[:16]. Safe to print (see header).
fingerprint() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -c1-16
  else
    shasum -a 256 "$1" | cut -c1-16
  fi
}

# read_secret_key <ns> <name> <key> <outfile>
# Writes the decoded value to <outfile>. Returns 1 (quietly) if the Secret or the
# key is absent — an absent target is a legitimate state ("never delivered"), not
# a crash. go-template + index is used rather than jsonpath so that keys
# containing dots need no escaping.
read_secret_key() {
  local ns="$1" name="$2" key="$3" out="$4" b64
  b64="$(kubectl -n "$ns" get secret "$name" \
           -o go-template="{{if .data}}{{index .data \"$key\"}}{{end}}" 2>/dev/null || true)"
  # go-template renders a missing map key as "<no value>"; treat that as absent.
  case "$b64" in ""|"<no value>") return 1 ;; esac
  printf '%s' "$b64" | base64 -d > "$out" 2>/dev/null || return 1
  [ -s "$out" ] || return 1
  return 0
}

# render_target <format> <password-file> <username> <host> <port> <database> <outfile>
# Produces the exact bytes the consumer's Secret key must contain.
render_target() {
  local fmt="$1" pwfile="$2" user="$3" host="$4" port="$5" db="$6" out="$7"
  case "$fmt" in
    raw)
      cp "$pwfile" "$out"
      ;;
    postgres-url)
      [ -n "$user" ] && [ -n "$host" ] && [ -n "$port" ] && [ -n "$db" ] \
        || die "format 'postgres-url' requires a complete verify block (username/host/port/database)"
      # Refuse to build a DSN out of a password that would need percent-encoding.
      # FuzeInfra generates these alphanumeric-only on purpose (a shell metachar in
      # a generated secret is what broke airflow-init); if that ever stops being
      # true, silently emitting an ambiguous URL would corrupt the credential in a
      # way that only shows up as an auth failure weeks later. Fail loudly instead.
      # The password is inspected with a quiet byte-class match — never printed.
      if LC_ALL=C grep -q '[^A-Za-z0-9]' "$pwfile"; then
        die "source password contains characters that require URL-encoding; refusing to compose a postgres-url (regenerate it alphanumeric-only, or use format 'raw')"
      fi
      printf 'postgresql://%s:%s@%s:%s/%s' \
        "$user" "$(cat "$pwfile")" "$host" "$port" "$db" > "$out"
      ;;
    *) die "unknown target format: $fmt" ;;
  esac
}

# password_from_target <format> <target-file> <outfile>
# Recovers the bare password from a rendered target value, so `verify` can attempt
# an authentication regardless of how the consumer stores it.
password_from_target() {
  local fmt="$1" in="$2" out="$3"
  case "$fmt" in
    raw) cp "$in" "$out" ;;
    postgres-url)
      python3 -c '
import sys, urllib.parse
u = urllib.parse.urlsplit(open(sys.argv[1]).read().strip())
sys.stdout.write(urllib.parse.unquote(u.password or ""))
' "$in" > "$out"
      ;;
    *) die "unknown target format: $fmt" ;;
  esac
  [ -s "$out" ] || return 1
  return 0
}

# ── Registry access ──────────────────────────────────────────────────────────
# Emitted as TSV with a FIXED column order so the shell can read a whole entry in
# one `read`. python3 rather than jq: python3 is already required (URL parsing),
# it is present on every runner and on a developer laptop, and one fewer runtime
# dependency is one fewer way for this to silently not run.
registry_py() {
  python3 - "$REGISTRY" "$ONLY_ID" "$1" <<'PY'
import json, sys
path, only_id, mode = sys.argv[1], sys.argv[2], sys.argv[3]
COLS = ("id consumerRepo source.namespace source.secretName source.secretKey "
        "target.namespace target.secretName target.secretKey target.format "
        "target.manifestPath target.branch verify.engine verify.host verify.port "
        "verify.database verify.username").split()

def dig(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return ""
        obj = obj.get(part)
        if obj is None:
            return ""
    return str(obj)

with open(path, encoding="utf-8") as fh:
    reg = json.load(fh)

if reg.get("version") != 1:
    sys.stderr.write("registry version must be 1\n"); sys.exit(1)

rows = reg.get("handoffs") or []
if mode == "ids":
    sys.stdout.write("\n".join(h.get("id", "") for h in rows) + "\n")
    sys.exit(0)
if mode == "enabled":
    rows = [h for h in rows if h.get("enabled") is True]
if only_id:
    rows = [h for h in rows if h.get("id") == only_id]

for h in rows:
    vals = [dig(h, c) for c in COLS]
    if any("\t" in v or "\n" in v for v in vals):
        sys.stderr.write("registry value contains a tab/newline; refusing\n"); sys.exit(1)
    sys.stdout.write("\t".join(vals) + "\n")
PY
}

entries() { registry_py enabled; }

# =============================================================================
# publish
# =============================================================================
do_publish() {
  command -v kubeseal >/dev/null 2>&1 || die "kubeseal not found"
  command -v gh       >/dev/null 2>&1 || die "gh not found"
  local seal="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/seal-secret.sh"
  [ -f "$seal" ] || die "seal-secret.sh not found next to this script"

  local rc=0 published=0 insync=0
  while IFS=$'\t' read -r id consumer s_ns s_name s_key \
                          t_ns t_name t_key t_fmt t_path t_branch \
                          v_engine v_host v_port v_db v_user; do
    [ -z "$id" ] && continue
    t_branch="${t_branch:-main}"

    note "── $id → $consumer ($t_ns/$t_name key $t_key)"

    local d="$WORK/$id"; mkdir -p "$d"
    if ! read_secret_key "$s_ns" "$s_name" "$s_key" "$d/src"; then
      note "   FAIL: source $s_ns/$s_name key '$s_key' not readable"; rc=1; continue
    fi
    render_target "$t_fmt" "$d/src" "$v_user" "$v_host" "$v_port" "$v_db" "$d/want"
    local fp_want; fp_want="$(fingerprint "$d/want")"

    local fp_have="absent"
    if read_secret_key "$t_ns" "$t_name" "$t_key" "$d/have"; then
      fp_have="$(fingerprint "$d/have")"
    fi
    note "   want=$fp_want have=$fp_have"

    if [ "$fp_want" = "$fp_have" ] && [ "$FORCE" != 1 ]; then
      note "   in sync — no branch, no commit, no PR"
      insync=$((insync + 1)); continue
    fi

    # The branch carries the fingerprint, so a run can never collide with an
    # earlier attempt and never needs a force-push: a NEW rotation gets a NEW
    # branch and a NEW PR, while a re-run for the SAME value reuses the branch
    # and is deduped below.
    local branch="handoff/$id-$fp_want"

    # Dedup: a hand-off PR may already be open and simply not merged yet. Without
    # this the reconciler would open a fresh PR on every run while it waits.
    local open_pr
    open_pr="$(gh pr list --repo "$consumer" --state open --head "$branch" \
                 --json number --jq '.[0].number' 2>/dev/null || true)"
    if [ -n "$open_pr" ]; then
      note "   PR #$open_pr already open on $consumer — waiting for it to merge"
      continue
    fi

    # ── Seal + PR against the consumer repo ─────────────────────────────────
    local repo_dir="$d/repo" clone_err
    # Report what gh ACTUALLY said. This used to guess "GH_TOKEN lacks access?",
    # and on 2026-08-20 that guess sent the reader after a permissions problem
    # when the real cause was a branch name: fuzex-registration asked for `main`
    # while FuzeX's default branch is `master`, so the ref simply did not exist
    # (FuzeInfra#595). gh's own message distinguishes the two immediately.
    # Safe to print: gh reports refs and HTTP status, never the token.
    if ! clone_err="$(gh repo clone "$consumer" "$repo_dir" -- --depth 1 --branch "$t_branch" 2>&1)"; then
      note "   FAIL: cannot clone $consumer at branch '$t_branch' — gh said: $(printf '%s' "$clone_err" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-300)"
      rc=1; continue
    fi
    # Fetch the handoff branch so we can start from it if it already exists on
    # the remote (previous PR merged/closed without branch deletion, or a
    # concurrent run). Without this, git push fails non-fast-forward.
    git -C "$repo_dir" fetch -q origin \
      "+refs/heads/$branch:refs/remotes/origin/$branch" 2>/dev/null || true
    case "$t_path" in
      /*|*..*) note "   FAIL: unsafe manifestPath '$t_path'"; rc=1; continue ;;
    esac
    if [ ! -f "$repo_dir/$t_path" ]; then
      note "   $t_path absent in $consumer — will create new SealedSecret manifest"
      mkdir -p "$(dirname "$repo_dir/$t_path")"
    fi

    # --merge-into: update exactly this one encryptedData key and leave every other
    # key in the manifest byte-identical. Regenerating the manifest instead would
    # DROP the consumer's out-of-band keys — the exact destructive footgun called
    # out in FuzeInfra#499 (MendysRobotics' force-reseal path composes only 16 of
    # 24 keys) and the reason MendysRobotics#273 hand-edited a single key.
    # For a new (absent) manifest seal-secret.sh creates it from scratch rather
    # than merging, so the consumer gets a single-key SealedSecret they own.
    bash "$seal" "$t_ns/$t_name" "${t_key}=@${d}/want" --out "$repo_dir/$t_path"

    local sub=0
    if (
      cd "$repo_dir"
      git config user.name  "FuzeInfra Credential Hand-off"
      git config user.email "handoff@fuzeinfra"
      # If the handoff branch already exists on the remote (a previous run's PR
      # was closed without merging, or a concurrent run beat us to the push),
      # start from it so our push is a fast-forward rather than a rejection.
      if git show-ref --verify --quiet "refs/remotes/origin/$branch" 2>/dev/null; then
        git checkout -q -B "$branch" "origin/$branch"
      else
        git checkout -q -B "$branch"
      fi
      if git diff --quiet -- "$t_path" && [ -z "$(git status --porcelain "$t_path")" ]; then
        note "   sealed output identical — nothing to commit"; exit 3
      fi
      git add "$t_path"
      {
        printf 'chore(secrets): re-seal %s from FuzeInfra hand-off (%s)\n\n' "$t_key" "$id"
        printf 'Automated credential hand-off from FuzeOne/FuzeInfra (FuzeInfra#510).\n\n'
        printf 'Only the encrypted `%s` entry changed; every other key in this\n' "$t_key"
        printf 'SealedSecret is byte-identical (kubeseal --merge-into).\n\n'
        printf 'The value was sealed --scope strict for %s/%s, so this ciphertext\n' "$t_ns" "$t_name"
        printf 'decrypts nowhere else. No plaintext was logged, committed, or handled\n'
        printf 'by a human at any point.\n\n'
        printf 'Credential fingerprint (sha256[:16]): %s\n' "$fp_want"
        printf 'Previously deployed fingerprint:      %s\n' "$fp_have"
      } > "$WORK/msg.txt"
      git commit -q -F "$WORK/msg.txt"
      git push -q origin "$branch"
    ); then
      sub=0
    else
      sub=$?
    fi
    if [ "$sub" = 3 ]; then insync=$((insync + 1)); continue; fi
    if [ "$sub" != 0 ]; then note "   FAIL: could not push branch to $consumer"; rc=1; continue; fi

    {
      printf 'Automated credential hand-off from `FuzeOne/FuzeInfra` (FuzeInfra#510).\n\n'
      printf -- '- Hand-off id: `%s`\n' "$id"
      printf -- '- Target scope: `%s/%s`, key `%s` (`--scope strict`)\n' "$t_ns" "$t_name" "$t_key"
      printf -- '- Manifest: `%s` (updated with `kubeseal --merge-into`; all other keys untouched)\n' "$t_path"
      printf -- '- New credential fingerprint: `%s`\n' "$fp_want"
      printf -- '- Fingerprint currently deployed in `%s`: `%s`\n\n' "$t_ns" "$fp_have"
      printf 'This PR contains **ciphertext only**. It was produced without any human\n'
      printf 'or job log ever seeing the plaintext: FuzeInfra read its own Secret\n'
      printf 'in-cluster, sealed it against the controller cert, and published the\n'
      printf 'result. Merging it lets Argo CD sync the value into `%s`.\n' "$t_ns"
    } > "$WORK/body.md"
    gh pr create --repo "$consumer" --base "$t_branch" --head "$branch" \
      --title "chore(secrets): re-seal ${t_key} from FuzeInfra hand-off (${id})" \
      --body-file "$WORK/body.md" >&2 || { note "   FAIL: gh pr create"; rc=1; continue; }
    published=$((published + 1))
  done < <(entries)

  note "publish: $published PR(s) opened, $insync already in sync"
  return $rc
}

# =============================================================================
# verify — the detection half. Attempt a REAL auth with what the consumer holds.
# =============================================================================
do_verify() {
  command -v psql >/dev/null 2>&1 || die "psql not found"
  local rc=0
  : > "$WORK/failures.txt"

  while IFS=$'\t' read -r id consumer s_ns s_name s_key \
                          t_ns t_name t_key t_fmt t_path t_branch \
                          v_engine v_host v_port v_db v_user; do
    [ -z "$id" ] && continue

    if [ -z "$v_engine" ]; then
      note "── $id: no verify block — skipped"; continue
    fi
    [ "$v_engine" = postgres ] || { note "── $id: engine '$v_engine' unsupported"; continue; }

    note "── $id ($consumer): authenticating as $v_user@$v_db with the value in $t_ns/$t_name"
    local d="$WORK/v-$id"; mkdir -p "$d"

    if ! read_secret_key "$t_ns" "$t_name" "$t_key" "$d/have"; then
      note "   FAIL: $t_ns/$t_name key '$t_key' is absent — the credential was never delivered"
      printf '%s\t%s\t%s\tnot-delivered\t%s/%s key %s is absent\n' \
        "$id" "$consumer" "$t_ns" "$t_ns" "$t_name" "$t_key" >> "$WORK/failures.txt"
      rc=1; continue
    fi
    if ! password_from_target "$t_fmt" "$d/have" "$d/pw"; then
      note "   FAIL: could not extract a password from the stored value (malformed?)"
      printf '%s\t%s\t%s\tmalformed\tstored %s is not a usable %s value\n' \
        "$id" "$consumer" "$t_ns" "$t_key" "$t_fmt" >> "$WORK/failures.txt"
      rc=1; continue
    fi
    note "   deployed fingerprint: $(fingerprint "$d/have")"

    # Reach the in-cluster database through a port-forward — the same access
    # pattern grafana-crit-fix.yml already uses for Loki.
    local svc="${v_host%%.*}" svc_ns="${v_host#*.}"; svc_ns="${svc_ns%%.*}"
    local lport=$((15432 + RANDOM % 1000))
    kubectl -n "$svc_ns" port-forward "svc/$svc" "$lport:$v_port" >/dev/null 2>&1 &
    local pf=$!
    local ready=0 i
    for i in $(seq 1 30); do
      if (exec 3<>"/dev/tcp/127.0.0.1/$lport") 2>/dev/null; then ready=1; break; fi
      sleep 1
    done
    if [ "$ready" != 1 ]; then
      kill "$pf" 2>/dev/null || true
      note "   FAIL: could not port-forward svc/$svc in $svc_ns"
      printf '%s\t%s\t%s\tunreachable\tport-forward to svc/%s in %s failed\n' \
        "$id" "$consumer" "$t_ns" "$svc" "$svc_ns" >> "$WORK/failures.txt"
      rc=1; continue
    fi

    # The password goes in through the ENVIRONMENT, never argv (`ps` is readable).
    # `SELECT 1` is the whole query: this proves authentication, nothing more.
    local out ok=0
    if out="$(PGPASSWORD="$(cat "$d/pw")" PGCONNECT_TIMEOUT=15 \
                psql -q -tAX -h 127.0.0.1 -p "$lport" -U "$v_user" -d "$v_db" \
                     -c 'SELECT 1' 2>&1)"; then
      [ "$(printf '%s' "$out" | tr -d '[:space:]')" = "1" ] && ok=1
    fi
    kill "$pf" 2>/dev/null || true
    wait "$pf" 2>/dev/null || true

    if [ "$ok" = 1 ]; then
      note "   OK: authenticated"
      continue
    fi
    # psql's message names the role and the failure mode, never the password.
    local reason; reason="$(printf '%s' "$out" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-200)"
    note "   FAIL: authentication rejected — $reason"
    printf '%s\t%s\t%s\tauth-failed\t%s\n' "$id" "$consumer" "$t_ns" "$reason" >> "$WORK/failures.txt"
    rc=1
  done < <(entries)

  # Hand the failure table to the caller so the workflow can route alerts.
  if [ -n "${HANDOFF_FAILURE_FILE:-}" ]; then
    cp "$WORK/failures.txt" "$HANDOFF_FAILURE_FILE"
  fi
  return $rc
}

do_list() { registry_py all; }

# ── Arg parsing ──────────────────────────────────────────────────────────────
main() {
  [ $# -gt 0 ] || die "usage: credential-handoff.sh {publish|verify|list} [--id ID] [--force] [--registry FILE]"
  MODE="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --id)       ONLY_ID="${2:?--id needs a value}"; shift 2 ;;
      --registry) REGISTRY="${2:?--registry needs a path}"; shift 2 ;;
      --force)    FORCE=1; shift ;;
      -h|--help)  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done

  [ -f "$REGISTRY" ] || die "registry not found: $REGISTRY"
  command -v python3 >/dev/null 2>&1 || die "python3 not found"

  # An --id that is not in the registry must be a hard error, not a silent no-op:
  # a typo would otherwise look exactly like "nothing to do".
  if [ -n "$ONLY_ID" ]; then
    registry_py ids | grep -qxF "$ONLY_ID" \
      || die "no hand-off with id '$ONLY_ID' in $REGISTRY"
  fi

  case "$MODE" in
    publish) do_publish ;;
    verify)  do_verify ;;
    list)    do_list ;;
    *) die "unknown mode: $MODE" ;;
  esac
}

# Only run when executed. Sourcing exposes the pure helpers (render_target,
# password_from_target, fingerprint) so tests/test_credential_handoff.py can
# exercise the value-rendering round-trip offline, with no cluster.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
