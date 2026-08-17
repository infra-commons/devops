#!/usr/bin/env bash
# register-gh-runner.sh — register a self-hosted GitHub Actions runner as a service.
#
# Thin, idempotent wrapper around actions/runner's config.sh / svc.sh. It:
#   1. fetches a short-lived registration token (via gh api, if not passed),
#   2. resolves + downloads the CURRENT actions/runner linux-x64 release,
#   3. runs config.sh with your label(s) at repo OR org scope,
#   4. installs + starts the service (supports --ephemeral single-job runners).
#
# ***SECURITY — READ THIS.***
#   NEVER attach a self-hosted runner to a PUBLIC repo. A fork PR runs arbitrary
#   code on the runner = RCE on your host. GitHub documents this explicitly.
#   GitHub meters hosted minutes on PRIVATE repos only (public = unlimited hosted),
#   so self-hosting only pays off — and is only safe — for private repos.
#
#   The visibility gate below FAILS CLOSED. Only an affirmative "private" or
#   "internal" reading gets past it; an API error, a renamed/missing repo, a token
#   that cannot see the repo, a rate limit or a broken gh auth all read as "not
#   confirmed" and REFUSE. A non-answer is not evidence that a repo is not public.
#
#   ORG-SCOPE IS THE SHARPER EDGE: a repo-level gate cannot help an org-level
#   runner, which is reachable by EVERY repo in the org — including public ones,
#   i.e. the same fork-PR RCE one indirection away. So org-level registration
#   requires --group naming a runner group that is restricted to selected repos
#   and does not allow public repositories, verified over the API and fail-closed.
#
# Usage:
#   ./register-gh-runner.sh --org <ORG> [--repo <REPO>] --label <LABEL> --user <USER> \
#                           [--token <REG_TOKEN>] [--group <GROUP>] [--ephemeral] \
#                           [--dir <DIR>] [--name <NAME>] [--apply]
#
#   --org        GitHub org                                        [required]
#   --repo       repo for a repo-level runner; omit for org-level  [optional]
#   --label      custom runner label (e.g. beelink)                [required]
#   --user       OS user to run the service as                     [required]
#   --token      registration token; auto-fetched via gh api if omitted
#   --group      runner group; REQUIRED for org-level registration [optional]
#   --ephemeral  single-job runner: deregisters after one job (recommended at scale)
#   --dir        install dir                       (default: ~/gh-runner)
#   --name       runner name                       (default: <host>-gh)
#   --apply      actually register (default: dry-run)
#   --i-understand-public
#                skip the visibility gate entirely. Means "I have assessed the
#                isolation of this host myself" — it also covers the case where
#                visibility cannot be read at all. Do not use it to paper over
#                broken auth.
#   -h|--help    show this header.
#
# Re-running against an ALREADY-REGISTERED dir is safe: config is skipped (.runner
# present) and the service step refuses rather than installing a SECOND service
# competing with the live one for the same _work dir. See step 4.
#
# Auth for token auto-fetch: a gh authed with admin on the repo/org, e.g.
#   GH_CONFIG_DIR=~/.config/gh-rolliq ./register-gh-runner.sh ...
set -euo pipefail

ORG='' REPO='' LABEL='' USER_RUN='' TOKEN='' GROUP='' EPHEMERAL=0
DIR="$HOME/gh-runner" NAME='' APPLY=0 ALLOW_PUBLIC=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --org) ORG="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --user) USER_RUN="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --ephemeral) EPHEMERAL=1; shift ;;
    --dir) DIR="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --i-understand-public) ALLOW_PUBLIC=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done
for req in ORG LABEL USER_RUN; do
  if [ -z "${!req}" ]; then echo "missing --${req,,}" >&2; usage; exit 2; fi
done
NAME="${NAME:-$(hostname -s)-gh}"

say() { printf '%s\n' "$*"; }
run() { if [ "$APPLY" = 1 ]; then say "  + $*"; "$@"; else say "  would run: $*"; fi; }

[ "$APPLY" = 1 ] || say "DRY-RUN — nothing will change. Re-run with --apply."
SCOPE_DESC="org $ORG"; [ -n "$REPO" ] && SCOPE_DESC="repo $ORG/$REPO"
say "scope=$SCOPE_DESC label=$LABEL user=$USER_RUN name=$NAME ephemeral=$EPHEMERAL"

# --- SECURITY GATE ----------------------------------------------------------
# Fails closed in both scopes. Never infer "not public" from a failed read.
refuse_rce() {
  echo "REFUSING: $1" >&2
  echo "A self-hosted runner reachable by a public repo is an RCE risk: a fork PR runs" >&2
  echo "arbitrary code on your host. Public repos also get unlimited hosted minutes, so" >&2
  echo "there is no upside. Pass --i-understand-public only if this host is truly isolated." >&2
  exit 3
}

if [ "$ALLOW_PUBLIC" = 1 ]; then
  say "== visibility gate SKIPPED (--i-understand-public) =="
elif [ -n "$REPO" ]; then
  # Repo scope: require an affirmative private/internal reading.
  say "== visibility check (repo scope) =="
  VIS="$(gh api "repos/${ORG}/${REPO}" --jq '.visibility' 2>/dev/null)" || VIS=''
  VIS="$(printf '%s' "$VIS" | tr -d '[:space:]')"
  say "  ${ORG}/${REPO} visibility: ${VIS:-<unreadable>}"
  case "$VIS" in
    private|internal) say "  ok — not public." ;;
    public)  refuse_rce "${ORG}/${REPO} is PUBLIC." ;;
    *)       refuse_rce "could not confirm ${ORG}/${REPO} is private (read: '${VIS:-<none>}'). Fix gh auth, or check the org/repo name." ;;
  esac
else
  # Org scope: the repo gate cannot help here. An org runner serves every repo in
  # the org, so demand a runner group that is restricted and public-repo-denied.
  say "== runner-group check (org scope) =="
  [ -n "$GROUP" ] || refuse_rce "org-level registration needs --group naming a runner group restricted to selected repos. Without one, this runner serves EVERY repo in ${ORG}, public ones included."
  # NOT named GROUPS: bash reserves that as a special read-only-ish array (the
  # calling process's own supplementary group IDs). Assigning to it is silently
  # discarded and `$GROUPS` still reads back the process's GIDs instead of the
  # API response, so the match below would run against "1000 24 27 ..." and
  # never find a real runner group by name — permanently refusing even a
  # correctly configured one, which trains an operator to reach for
  # --i-understand-public instead. Verified with `GROUPS="x"; echo "$GROUPS"`.
  RUNNER_GROUPS="$(gh api "orgs/${ORG}/actions/runner-groups" \
              --jq '.runner_groups[] | [.name, .visibility, (.allows_public_repositories|tostring)] | @tsv' 2>/dev/null)" || RUNNER_GROUPS=''
  # Match in shell, not in the jq filter, so an operator-supplied name is never
  # interpolated into a jq program.
  GLINE="$(printf '%s\n' "$RUNNER_GROUPS" | awk -F'\t' -v g="$GROUP" '$1 == g {print; exit}')"
  [ -n "$GLINE" ] || refuse_rce "could not confirm runner group '$GROUP' exists in ${ORG} (needs an org-admin gh, and the group must already exist)."
  GVIS="$(printf '%s' "$GLINE" | cut -f2)"
  GPUB="$(printf '%s' "$GLINE" | cut -f3)"
  say "  group '$GROUP': visibility=$GVIS allows_public_repositories=$GPUB"
  [ "$GPUB" = "false" ] || refuse_rce "runner group '$GROUP' allows public repositories."
  case "$GVIS" in
    selected|private) say "  ok — group is restricted." ;;
    *)               refuse_rce "runner group '$GROUP' has visibility '$GVIS' — it is not restricted to selected repos." ;;
  esac
fi

# --- 1. registration token --------------------------------------------------
if [ -z "$TOKEN" ]; then
  say "== fetch registration token (gh api) =="
  if [ -n "$REPO" ]; then
    API="repos/${ORG}/${REPO}/actions/runners/registration-token"
    RURL="https://github.com/${ORG}/${REPO}"
  else
    API="orgs/${ORG}/actions/runners/registration-token"
    RURL="https://github.com/${ORG}"
  fi
  if [ "$APPLY" = 1 ]; then
    TOKEN="$(gh api -X POST "$API" --jq '.token')"
    say "  got a registration token (expires ~1h)"
  else
    say "  would POST $API to mint a short-lived token"
    TOKEN='<REG_TOKEN>'
  fi
else
  [ -n "$REPO" ] && RURL="https://github.com/${ORG}/${REPO}" || RURL="https://github.com/${ORG}"
fi

# --- 2. download the runner -------------------------------------------------
say "== resolve + download actions/runner =="
VER="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
        | sed -n 's/.*"tag_name": *"v\([0-9.]*\)".*/\1/p' | head -1 || true)"
VER="${VER:-${GH_RUNNER_VERSION:-2.328.0}}"
TARBALL="actions-runner-linux-x64-${VER}.tar.gz"
URL="https://github.com/actions/runner/releases/download/v${VER}/${TARBALL}"
say "  version=$VER url=$URL"
if [ -f "$DIR/.runner" ]; then
  say "  $DIR already configured (.runner present) — skip; remove $DIR to re-register."
else
  run mkdir -p "$DIR"
  run bash -c "cd '$DIR' && curl -fsSLO '$URL' && tar zxf '$TARBALL'"

  # --- 3. configure -------------------------------------------------------
  say "== config.sh =="
  CFG="--unattended --url '$RURL' --token '****' --name '$NAME' --labels 'self-hosted,$LABEL' --replace"
  [ -n "$GROUP" ] && CFG="$CFG --runnergroup '$GROUP'"
  [ "$EPHEMERAL" = 1 ] && CFG="$CFG --ephemeral"
  say "  would run: (cd $DIR && ./config.sh $CFG)"
  if [ "$APPLY" = 1 ]; then
    ARGS=(--unattended --url "$RURL" --token "$TOKEN" --name "$NAME" --labels "self-hosted,$LABEL" --replace)
    [ -n "$GROUP" ] && ARGS+=(--runnergroup "$GROUP")
    [ "$EPHEMERAL" = 1 ] && ARGS+=(--ephemeral)
    ( cd "$DIR" && ./config.sh "${ARGS[@]}" )
  fi
fi

# --- 4. install service -----------------------------------------------------
# Step 2 is idempotent (skips config when .runner exists), but installing a
# service is not: svc.sh writes a SYSTEM unit, so re-running here against a dir
# already served by a unit — including a systemd --user one, which svc.sh knows
# nothing about — yields two services racing for the same _work dir. Detect any
# existing manager and refuse instead.
say "== service (svc.sh) =="
EXISTING=''
[ -f "$DIR/.service" ] && EXISTING="svc.sh system unit ($(cat "$DIR/.service" 2>/dev/null))"
if [ -z "$EXISTING" ] && pgrep -f "${DIR}/bin/Runner.Listener" >/dev/null 2>&1; then
  EXISTING="a live Runner.Listener process for $DIR"
fi
if [ -z "$EXISTING" ]; then
  UNIT="$(grep -ls -e "^ExecStart=.*${DIR}" -e "^WorkingDirectory=.*${DIR}" \
            "$HOME/.config/systemd/user"/*.service 2>/dev/null | head -1 || true)"
  [ -n "$UNIT" ] && EXISTING="systemd --user unit $(basename "$UNIT")"
fi

if [ "$EPHEMERAL" = 1 ]; then
  say "  ephemeral runner: do NOT install as an always-on service — run once per job via"
  say "  an autoscaler / re-register loop:  (cd $DIR && ./run.sh)  then re-run this script."
elif [ -n "$EXISTING" ]; then
  say "  SKIPPING service install — $DIR is already served by $EXISTING."
  say "  Installing another would leave two services racing for $DIR/_work."
  say "  To move the runner to a different service, stop and remove the existing one first."
else
  run bash -c "cd '$DIR' && sudo ./svc.sh install '${USER_RUN}' && sudo ./svc.sh start && sudo ./svc.sh status"
fi

say "== done =="
[ "$APPLY" = 1 ] && say "Runner '$NAME' registered ($SCOPE_DESC), labels: self-hosted,$LABEL. Flip a job to runs-on: [self-hosted, $LABEL]." \
                 || say "Dry-run complete. Re-run with --apply."
