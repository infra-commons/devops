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
# ***SECURITY — READ THIS.***
#   An ADO agent pool is an org-level resource shared into projects by the manual
#   UI step above — meaning at registration time, this script cannot see which
#   project(s) will end up authorized to dispatch jobs to it. Unlike GitHub, ADO
#   fork-PR builds don't get secrets by default (an admin must separately opt in
#   via "Make secrets available to builds of forks" / enforceNoAccessToSecretsFromForks),
#   so project visibility alone is NOT the right gate to port from register-gh-runner.sh
#   verbatim — it would check a real signal but miss the actual attack-relevant one.
#   Arbitrary code execution from an untrusted contributor is a real risk on its own
#   though (network/host pivot, resource abuse, whatever local state this process
#   holds), independent of secrets. So this gate checks BOTH, per named project:
#     - project visibility must read exactly "private" (ADO has no "internal" tier).
#     - the project's pipeline general settings' `buildsEnabledForForks` must read
#       "false" (https://dev.azure.com/<ORG>/<PROJECT>/_apis/build/generalsettings) —
#       the literal ADO-native "build PRs from forks" toggle. This is the decisive
#       field: it gates whether a fork PR can run code here AT ALL, regardless of
#       the separate secrets toggle.
#   Both fields are queryable via `az devops`, and the gate FAILS CLOSED on anything
#   else: a read error, empty output, an unrecognized token or a missing `az`/
#   extension all refuse rather than being read as "not exposed". A non-answer is
#   not evidence of safety.
#   RESIDUAL GAP: this only covers the project(s) named via --project at the moment
#   you run this script. A project granted access to the pool LATER (the manual UI
#   step) is not covered — re-run with --project naming it to verify that grant too.
#   --i-understand-public skips the gate entirely; it asserts you've verified this
#   host's isolation yourself, including for projects added after this run. Do not
#   use it to paper over broken `az` auth.
#
# Usage:
#   ./register-ado-agent.sh --org <ORG> --pool <POOL> --user <USER> \
#                           --token <REGISTRATION_PAT> \
#                           [--project <PROJECT> ...] [--i-understand-public] \
#                           [--name <AGENT>] [--dir <DIR>]
#                           [--dotnet-root <PATH>] [--apply]
#
#   --org     ADO org (https://dev.azure.com/<ORG>)              [required]
#   --pool    self-hosted pool name (matches the agentPool value) [required]
#   --user    OS user to run jobs as (e.g. kev / ci-runner)       [required]
#   --token   registration PAT, scoped **Agent Pools (Read & manage)** only, short-lived [required]
#   --project ADO project this pool will be shared with; repeatable for pools that
#             serve multiple projects. Required unless --i-understand-public. Checked
#             via a SEPARATE `az` auth (az login / AZURE_DEVOPS_EXT_PAT with Project
#             and Build read) — not the narrow --token PAT above.
#   --i-understand-public
#             skip the exposure gate entirely. Means "I have assessed this host's
#             isolation myself" — it also covers the case where the settings can't
#             be read at all. Do not use it to paper over broken `az` auth.
#   --name    agent name                              (default: <host>-1)
#   --dir     install dir                             (default: ~/ado-agent)
#   --dotnet-root  SDK path to expose to jobs         (default: $HOME/.dotnet if present)
#   --apply   actually register (default: dry-run — print the plan, change nothing)
#   -h|--help show this header.
#
# Needs the `azure-devops` az extension (`az extension add --name azure-devops`,
# auto-installs on first use) plus an authenticated `az` (az login, or
# AZURE_DEVOPS_EXT_PAT) with read access to Projects and Pipeline settings in each
# --project. This is intentionally separate from --token: --token is scoped to
# Agent Pools only and cannot read project visibility or pipeline settings.
set -euo pipefail

ORG='' POOL='' USER_RUN='' TOKEN='' NAME='' DIR="$HOME/ado-agent" DOTNET_ROOT_ARG='' APPLY=0
PROJECTS=() ALLOW_PUBLIC=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --org) ORG="$2"; shift 2 ;;
    --pool) POOL="$2"; shift 2 ;;
    --user) USER_RUN="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --project) PROJECTS+=("$2"); shift 2 ;;
    --i-understand-public) ALLOW_PUBLIC=1; shift ;;
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

# --- SECURITY GATE ----------------------------------------------------------
# Fails closed. Never infer "not exposed" from a failed or missing read. Runs
# unconditionally — dry-run and --apply alike, and even when $DIR is already
# configured — so a misconfiguration surfaces immediately, not just on first run.
refuse_rce() {
  echo "REFUSING: $1" >&2
  echo "A self-hosted ADO agent reachable by fork-PR builds is an RCE risk even" >&2
  echo "without secrets: arbitrary code runs on this host (network/host pivot," >&2
  echo "resource abuse, whatever local state this process holds). Pass" >&2
  echo "--i-understand-public only if this host is truly isolated." >&2
  exit 3
}

if [ "$ALLOW_PUBLIC" = 1 ]; then
  say "== exposure gate SKIPPED (--i-understand-public) =="
else
  say "== exposure check =="
  [ "${#PROJECTS[@]}" -gt 0 ] || refuse_rce "no --project given. An ADO agent pool is an org-level resource shared into projects after registration (see header); without at least one --project naming an intended consumer, this script cannot check that project's fork-PR-build exposure and refuses rather than registering blind."
  command -v az >/dev/null 2>&1 || refuse_rce "the 'az' CLI is not on PATH — cannot verify project visibility or fork-PR-build settings. Install it, then 'az extension add --name azure-devops' and 'az login' (or set AZURE_DEVOPS_EXT_PAT)."
  for PROJECT in "${PROJECTS[@]}"; do
    say "  -- project '$PROJECT' --"
    VIS="$(az devops project show --organization "https://dev.azure.com/${ORG}" --project "$PROJECT" --query visibility -o tsv 2>/dev/null)" || VIS=''
    VIS="$(printf '%s' "$VIS" | tr -d '[:space:]')"
    say "     visibility: ${VIS:-<unreadable>}"
    case "$VIS" in
      private) : ;;
      public)  refuse_rce "project '${PROJECT}' is PUBLIC." ;;
      *)       refuse_rce "could not confirm project '${PROJECT}' is private (read: '${VIS:-<none>}'). Fix az auth ('az login' or AZURE_DEVOPS_EXT_PAT), or check the org/project name." ;;
    esac

    # The attack-relevant ADO setting: does this project build PRs from forks at
    # all? (PipelineGeneralSettings.buildsEnabledForForks — see header.) Visibility
    # alone doesn't answer this: ADO fork-PR builds never get secrets by default
    # regardless of visibility, and conversely a private project can still have
    # forks-build enabled for whichever contributors CAN reach it. This is the one
    # field that gates whether a fork PR can run code on this pool's agents at all.
    SETTINGS_TSV="$(az devops invoke --organization "https://dev.azure.com/${ORG}" \
        --area build --resource generalsettings \
        --route-parameters "project=${PROJECT}" --api-version=7.1 \
        --query "[buildsEnabledForForks, enforceNoAccessToSecretsFromForks]" -o tsv 2>/dev/null)" || SETTINGS_TSV=''
    FORKS="$(printf '%s' "$SETTINGS_TSV" | cut -f1 | tr -d '[:space:]')"
    NOSECRETS="$(printf '%s' "$SETTINGS_TSV" | cut -f2 | tr -d '[:space:]')"
    say "     buildsEnabledForForks=${FORKS:-<unreadable>} enforceNoAccessToSecretsFromForks=${NOSECRETS:-<unreadable>}"
    case "$FORKS" in
      False|false) : ;;
      *) refuse_rce "project '${PROJECT}' could not be confirmed to have fork-PR builds disabled (buildsEnabledForForks read: '${FORKS:-<unreadable>}', enforceNoAccessToSecretsFromForks: '${NOSECRETS:-<unreadable>}'). A fork PR could run arbitrary code on this agent independent of the secrets toggle." ;;
    esac
    say "     ok — '${PROJECT}' is private and does not build PRs from forks."
  done
fi

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
