#!/usr/bin/env bash
# provision-host.sh — idempotent bootstrap for a self-hosted CI runner host.
#
# Installs the tool matrix a self-hosted ADO agent / GitHub Actions runner needs
# to run typical portfolio pipelines (a .NET web app built + Cypress-tested on a
# headless Linux box). Every step is guarded and re-runnable: an already-present
# tool is skipped, so running this twice is a no-op.
#
# The self-hosted trap this defends against: MS/GitHub-hosted images ship these
# tools; a bare box does NOT. A pipeline that is green on hosted fails on
# self-hosted with "command not found" / "spawn Xvfb ENOENT" until the host has
# them. This installs exactly what the proven cashbucket ADO reference needed.
#
# Usage:
#     ./provision-host.sh [--apply] [stack toggles]
#
#   (no flag)   dry-run: print what WOULD be installed, change nothing.
#   --apply     actually install.
#
#   Stack toggles (all default ON except --python which is ON too; pass an
#   explicit subset to install only those):
#     --dotnet    .NET SDK (user-local ~/.dotnet)
#     --pwsh      PowerShell 7
#     --azcli     Azure CLI
#     --cypress   Chrome + Cypress system libs + Xvfb
#     --python    python venv support (python3-venv)
#     --none      start from nothing; only toggles you pass are installed
#   -h|--help     show this header.
#
# Exit status: 0 on success (including "nothing to do").
set -euo pipefail

APPLY=0
DOTNET=1 PWSH=1 AZCLI=1 CYPRESS=1 PYTHON=1
EXPLICIT=0
DOTNET_CHANNEL="${DOTNET_CHANNEL:-STS}"   # STS = latest stable; override e.g. 10.0

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; }

for arg in "$@"; do
  case "$arg" in
    --apply)   APPLY=1 ;;
    --none)    DOTNET=0; PWSH=0; AZCLI=0; CYPRESS=0; PYTHON=0; EXPLICIT=1 ;;
    --dotnet)  [ "$EXPLICIT" = 0 ] && { DOTNET=0 PWSH=0 AZCLI=0 CYPRESS=0 PYTHON=0; EXPLICIT=1; }; DOTNET=1 ;;
    --pwsh)    [ "$EXPLICIT" = 0 ] && { DOTNET=0 PWSH=0 AZCLI=0 CYPRESS=0 PYTHON=0; EXPLICIT=1; }; PWSH=1 ;;
    --azcli)   [ "$EXPLICIT" = 0 ] && { DOTNET=0 PWSH=0 AZCLI=0 CYPRESS=0 PYTHON=0; EXPLICIT=1; }; AZCLI=1 ;;
    --cypress) [ "$EXPLICIT" = 0 ] && { DOTNET=0 PWSH=0 AZCLI=0 CYPRESS=0 PYTHON=0; EXPLICIT=1; }; CYPRESS=1 ;;
    --python)  [ "$EXPLICIT" = 0 ] && { DOTNET=0 PWSH=0 AZCLI=0 CYPRESS=0 PYTHON=0; EXPLICIT=1; }; PYTHON=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; usage; exit 2 ;;
  esac
done

# ---- helpers ---------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# run a command, or just echo it in dry-run
run() {
  if [ "$APPLY" = 1 ]; then
    say "  + $*"; "$@"
  else
    say "  would run: $*"
  fi
}
# same, but for a shell pipeline passed as one string
run_sh() {
  if [ "$APPLY" = 1 ]; then
    say "  + $1"; bash -c "$1"
  else
    say "  would run: $1"
  fi
}

# Detect Ubuntu codename for the noble t64 rename (gotcha #2).
CODENAME="$( . /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-unknown}" )"
t64() { # print the codename-correct package name for a lib that got a t64 rename on noble+
  local base="$1"
  case "$CODENAME" in
    noble|oracular|plucky) echo "${base}t64" ;;
    *) echo "$base" ;;
  esac
}

[ "$APPLY" = 1 ] || say "DRY-RUN — nothing will be installed. Re-run with --apply. (codename: $CODENAME)"

# ---- .NET SDK --------------------------------------------------------------
if [ "$DOTNET" = 1 ]; then
  step ".NET SDK (user-local ~/.dotnet)"
  if have dotnet && dotnet --version >/dev/null 2>&1; then
    say "  present: dotnet $(dotnet --version) — skip"
  else
    run_sh "curl -sSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh"
    run_sh "bash /tmp/dotnet-install.sh --channel ${DOTNET_CHANNEL} --install-dir \$HOME/.dotnet"
    say "  NOTE: export DOTNET_ROOT=\$HOME/.dotnet + add to PATH — the register-*-agent.sh scripts"
    say "        write this into the agent .env so jobs can see the SDK (gotcha #5)."
  fi
fi

# ---- PowerShell 7 ----------------------------------------------------------
if [ "$PWSH" = 1 ]; then
  step "PowerShell 7 (pwsh)"
  if have pwsh; then
    say "  present: $(pwsh --version 2>/dev/null) — skip"
  elif have dotnet; then
    run dotnet tool install --global PowerShell
    say "  NOTE: installed as a dotnet global tool (~/.dotnet/tools) — needs DOTNET_ROOT set to launch."
  else
    run_sh "sudo apt-get update && sudo apt-get install -y powershell || echo 'apt has no powershell; use the aka.ms/powershell tarball or install --dotnet first'"
  fi
fi

# ---- Azure CLI -------------------------------------------------------------
if [ "$AZCLI" = 1 ]; then
  step "Azure CLI (az)"
  if have az; then
    say "  present: $(az version --query '\"azure-cli\"' -o tsv 2>/dev/null) — skip"
  else
    run_sh "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
  fi
fi

# ---- Cypress: Chrome + system libs + Xvfb ---------------------------------
if [ "$CYPRESS" = 1 ]; then
  step "Cypress browser deps (Chrome + libs + Xvfb)"
  # Xvfb — the one self-hosted headless Linux runners lack (gotcha: spawn Xvfb ENOENT).
  if have Xvfb; then
    say "  present: Xvfb — skip"
  else
    run_sh "sudo apt-get update && sudo apt-get install -y xvfb"
  fi
  # System libs Cypress/Chrome need — codename-correct names (t64 on noble+).
  LIBS="$(t64 libasound2) $(t64 libgtk-3-0) libgbm1 libnss3 libxss1 libxtst6 fonts-liberation"
  run_sh "sudo apt-get install -y ${LIBS}"
  # Chrome itself — not in apt; direct .deb.
  if have google-chrome-stable || have google-chrome; then
    say "  present: google-chrome — skip"
  else
    run_sh "curl -sL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb && sudo apt-get install -y /tmp/chrome.deb"
  fi
fi

# ---- python venv -----------------------------------------------------------
if [ "$PYTHON" = 1 ]; then
  step "python venv support"
  PYV="$(python3 -c 'import sys; print(f"python3.{sys.version_info.minor}")' 2>/dev/null || echo python3)"
  if python3 -c 'import ensurepip' >/dev/null 2>&1; then
    say "  present: ${PYV} venv/ensurepip — skip"
  else
    run_sh "sudo apt-get update && sudo apt-get install -y ${PYV}-venv || sudo apt-get install -y python3-venv"
    say "  NOTE: self-hosted has NO UsePythonVersion tool cache — build a venv from system python"
    say "        (python3 -m venv <dir>) in the pipeline instead of the tool-cache task (gotcha #3)."
  fi
fi

step "done"
[ "$APPLY" = 1 ] && say "Host provisioned. Next: register-ado-agent.sh or register-gh-runner.sh." \
                 || say "Dry-run complete. Re-run with --apply to install."
