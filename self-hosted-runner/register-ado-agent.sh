#!/usr/bin/env bash
# register-ado-agent.sh — register a self-hosted Azure DevOps agent as a systemd service.
#
# Thin, idempotent wrapper around ADO's config.sh / svc.sh. It:
#   1. resolves + downloads the CURRENT linux-x64 agent tarball (the old
#      vstsagentpackage.azureedge.net host is DEAD — see gotcha #1),
#   2. writes a .env that injects DOTNET_ROOT/PATH into the service job env so a
#      user-local ~/.dotnet SDK is visible to jobs (gotcha #5),
#   3. runs config.sh --unattended against your org + pool,
#   4. installs + starts the systemd service running jobs as <user>.
#
# One-time UI step this CANNOT do (a Build PAT lacks the rights, gotcha #4):
#   Org settings → Agent pools → Add pool → Self-hosted → name it <pool>, then
#   Project settings → Agent pools → <pool> → Security → grant the pipeline Use.
#   Symptom if skipped: build shows "queued" but no build record is created.
#
# Usage:
#   ./register-ado-agent.sh --org <ORG> --pool <POOL> --user <USER> \
#                           --token <REGISTRATION_PAT> [--name <AGENT>] [--dir <DIR>]
#                           [--dotnet-root <PATH>] [--apply]
#
#   --org     ADO org (https://dev.azure.com/<ORG>)              [required]
#   --pool    self-hosted pool name (matches the agentPool value) [required]
#   --user    OS user to run jobs as (e.g. kev / ci-runner)       [required]
#   --token   registration PAT, scoped **Agent Pools (Read & manage)** only, short-lived [required]
#   --name    agent name                              (default: <host>-1)
#   --dir     install dir                             (default: ~/ado-agent)
#   --dotnet-root  SDK path to expose to jobs         (default: $HOME/.dotnet if present)
#   --apply   actually register (default: dry-run — print the plan, change nothing)
#   -h|--help show this header.
set -euo pipefail

ORG='' POOL='' USER_RUN='' TOKEN='' NAME='' DIR="$HOME/ado-agent" DOTNET_ROOT_ARG='' APPLY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --org) ORG="$2"; shift 2 ;;
    --pool) POOL="$2"; shift 2 ;;
    --user) USER_RUN="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --dotnet-root) DOTNET_ROOT_ARG="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

for req in ORG POOL USER_RUN TOKEN; do
  if [ -z "${!req}" ]; then echo "missing --${req,,}" >&2; usage; exit 2; fi
done
NAME="${NAME:-$(hostname -s)-1}"
DOTNET_ROOT_EFF="${DOTNET_ROOT_ARG:-$HOME/.dotnet}"

say() { printf '%s\n' "$*"; }
run() { if [ "$APPLY" = 1 ]; then say "  + $*"; "$@"; else say "  would run: $*"; fi; }

[ "$APPLY" = 1 ] || say "DRY-RUN — nothing will change. Re-run with --apply."
say "org=$ORG pool=$POOL user=$USER_RUN name=$NAME dir=$DIR"

# 1. resolve the latest agent version from the live download host.
say "== resolve latest agent version =="
VER="$(curl -fsSL https://api.github.com/repos/microsoft/azure-pipelines-agent/releases/latest \
        | sed -n 's/.*"tag_name": *"v\([0-9.]*\)".*/\1/p' | head -1 || true)"
if [ -z "$VER" ]; then
  say "  could not auto-resolve version; set VER manually. Falling back to a known-good default."
  VER="${ADO_AGENT_VERSION:-4.255.0}"
fi
TARBALL="vsts-agent-linux-x64-${VER}.tar.gz"
URL="https://download.agent.dev.azure.com/agent/${VER}/${TARBALL}"
say "  version=$VER"
say "  url=$URL"

# 2. download + extract into DIR (idempotent: skip if already configured).
say "== download + extract =="
if [ -f "$DIR/.agent" ]; then
  say "  $DIR already configured (.agent present) — skip download/config; remove $DIR to re-register."
else
  run mkdir -p "$DIR"
  run bash -c "cd '$DIR' && curl -fsSLO '$URL' && tar zxf '$TARBALL'"

  # 3. .env — inject DOTNET_ROOT/PATH into the service job environment (gotcha #5).
  say "== write $DIR/.env (DOTNET_ROOT/PATH for jobs) =="
  if [ "$APPLY" = 1 ]; then
    { printf 'DOTNET_ROOT=%s\n' "$DOTNET_ROOT_EFF"
      printf 'PATH=%s:%s/tools:/usr/local/bin:/usr/bin:/bin\n' "$DOTNET_ROOT_EFF" "$DOTNET_ROOT_EFF"
    } > "$DIR/.env"
    say "  wrote $DIR/.env"
  else
    say "  would write DOTNET_ROOT=$DOTNET_ROOT_EFF + PATH into $DIR/.env"
  fi

  # 4. configure unattended.
  say "== config.sh (unattended) =="
  run bash -c "cd '$DIR' && ./config.sh --unattended \
    --url 'https://dev.azure.com/${ORG}' \
    --auth pat --token '****' \
    --pool '${POOL}' --agent '${NAME}' --acceptTeeEula"
  # NOTE: the real token is passed only under --apply; masked in the dry-run echo above.
  if [ "$APPLY" = 1 ]; then
    ( cd "$DIR" && ./config.sh --unattended \
        --url "https://dev.azure.com/${ORG}" \
        --auth pat --token "${TOKEN}" \
        --pool "${POOL}" --agent "${NAME}" --acceptTeeEula )
  fi
fi

# 5. install + start the systemd service.
say "== systemd service (svc.sh) =="
run bash -c "cd '$DIR' && sudo ./svc.sh install '${USER_RUN}' && sudo ./svc.sh start && sudo ./svc.sh status"

say "== done =="
[ "$APPLY" = 1 ] && say "Agent '${NAME}' registered in pool '${POOL}'. Now do the one-time ADO UI Permit (see header), then flip the pipeline agentPool variable." \
                 || say "Dry-run complete. Re-run with --apply."
