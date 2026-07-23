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
#   This script refuses a public repo unless --i-understand-public is passed.
#   GitHub meters hosted minutes on PRIVATE repos only (public = unlimited hosted),
#   so self-hosting only pays off — and is only safe — for private repos.
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
#   --group      runner group (org-level fleets)                   [optional]
#   --ephemeral  single-job runner: deregisters after one job (recommended at scale)
#   --dir        install dir                       (default: ~/gh-runner)
#   --name       runner name                       (default: <host>-gh)
#   --apply      actually register (default: dry-run)
#   -h|--help    show this header.
#
# Auth for token auto-fetch: a gh authed with admin on the repo/org, e.g.
#   GH_CONFIG_DIR=/home/kev/.config/gh-rolliq ./register-gh-runner.sh ...
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

# --- SECURITY GATE: refuse a public repo -----------------------------------
if [ -n "$REPO" ]; then
  say "== visibility check =="
  VIS="$(gh api "repos/${ORG}/${REPO}" --jq '.visibility' 2>/dev/null || echo unknown)"
  say "  ${ORG}/${REPO} visibility: $VIS"
  if [ "$VIS" = "public" ] && [ "$ALLOW_PUBLIC" != 1 ]; then
    echo "REFUSING: ${ORG}/${REPO} is PUBLIC. A self-hosted runner on a public repo is an RCE risk" >&2
    echo "(fork PRs run arbitrary code on your host). Public repos also get unlimited hosted minutes," >&2
    echo "so there is no upside. Pass --i-understand-public only if you have truly isolated the runner." >&2
    exit 3
  fi
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
say "== service (svc.sh) =="
if [ "$EPHEMERAL" = 1 ]; then
  say "  ephemeral runner: do NOT install as an always-on service — run once per job via"
  say "  an autoscaler / re-register loop:  (cd $DIR && ./run.sh)  then re-run this script."
else
  run bash -c "cd '$DIR' && sudo ./svc.sh install '${USER_RUN}' && sudo ./svc.sh start && sudo ./svc.sh status"
fi

say "== done =="
[ "$APPLY" = 1 ] && say "Runner '$NAME' registered ($SCOPE_DESC), labels: self-hosted,$LABEL. Flip a job to runs-on: [self-hosted, $LABEL]." \
                 || say "Dry-run complete. Re-run with --apply."
