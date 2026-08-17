#!/usr/bin/env bash
# gh-runner-light-register.sh — finish registering a pre-staged "light" runner slot.
#
# The slots are ALREADY staged on this box: ~/gh-runner-2 and -3 hold an
# extracted actions/runner 2.336.0 (version-matched to the live primary runner), a
# .env (PYTHONNOUSERSITE=1 + the job-started cleanup hook) and a job-cleanup.sh.
# Their systemd --user units exist but are deliberately DISABLED, because run.sh
# without a .runner would crash-loop under Restart=always.
#
# The ONLY remaining step needs a credential this box does not have: minting a
# registration token requires repo/org ADMIN, and all three on-box identities are
# refused (infra-commons-agent 404 member-masked, the infra-commons-bot App 403
# "not accessible by integration", rolliqsharedinfra 404 — it owns infra-commons,
# not rolliq-com). So run THIS script with an admin credential.
#
# Label is beelink-light, NOT beelink. These slots are memory-capped (MemoryMax
# 1536M) for the swarm of sub-minute PR jobs; sharing the 'beelink' label would let
# heavy ci.yml jobs (pytest/Trivy, ~3G peak) land on a capped slot and OOM.
# Heterogeneous capacity needs distinct labels.
#
# Usage:
#   ./gh-runner-light-register.sh --slot <2|3> --org <ORG> [--repo <REPO>]
#                                [--group <GROUP>] [--token <TOK>] [--apply]
#
#   --slot   which staged slot to register (2 or 3)            [required]
#   --org    GitHub org                                        [required]
#   --repo   repo-scoped runner; omit for ORG scope            [optional]
#   --group  runner group — REQUIRED for org scope, and verified to be
#            restricted to selected repos with allows_public_repositories=false
#   --token  registration token; minted via gh api if omitted (needs admin)
#   --apply  actually register (default: dry-run)
#
# ORG scope is recommended here: reaching $0 on the rolliq Actions bill needs the
# top ~5 private repos, and a repo-scoped runner serves exactly one repo. One
# org-level runner in a restricted group covers them all from a single pool.
# Create the group first (needs admin):
#   gh api -X POST /orgs/<ORG>/actions/runner-groups -f name=beelink-light \
#     -f visibility=selected -F allows_public_repositories=false
# then add the private repos to it. NEVER let it include a public repo.
set -euo pipefail

SLOT='' ORG='' REPO='' GROUP='' TOKEN='' APPLY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --slot) SLOT="$2"; shift 2 ;;
    --org) ORG="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done
[ -n "$SLOT" ] && [ -n "$ORG" ] || { echo "need --slot and --org" >&2; usage; exit 2; }
case "$SLOT" in 2|3) ;; *) echo "--slot must be 2 or 3" >&2; exit 2 ;; esac

DIR="$HOME/gh-runner-$SLOT"
UNIT="gh-runner-$SLOT.service"
NAME="$(hostname -s)-gh-light-$SLOT"
LABEL="beelink-light"

say() { printf '%s\n' "$*"; }
run() { if [ "$APPLY" = 1 ]; then say "  + $*"; "$@"; else say "  would run: $*"; fi; }
refuse() { echo "REFUSING: $1" >&2; exit 3; }

[ "$APPLY" = 1 ] || say "DRY-RUN — nothing will change. Re-run with --apply."

# --- 0. the slot must be staged and idle -----------------------------------
say "== preflight =="
[ -d "$DIR" ] || refuse "$DIR does not exist — slot not staged."
[ -x "$DIR/config.sh" ] || refuse "$DIR/config.sh missing — slot not staged properly."
[ -f "$DIR/.env" ] || refuse "$DIR/.env missing — the contamination guard would be absent."
grep -q '^PYTHONNOUSERSITE=1' "$DIR/.env" || refuse "$DIR/.env lacks PYTHONNOUSERSITE=1."
[ -x "$DIR/job-cleanup.sh" ] || refuse "$DIR/job-cleanup.sh missing or not executable."
if [ -f "$DIR/.runner" ]; then refuse "$DIR is ALREADY configured (.runner present). Remove the dir to re-register, or pick the other slot."; fi
systemctl --user cat "$UNIT" >/dev/null 2>&1 || refuse "user unit $UNIT not found."
say "  slot $SLOT staged, unconfigured, unit present. label=$LABEL name=$NAME"

# --- 1. scope + safety gate -------------------------------------------------
if [ -n "$REPO" ]; then
  say "== visibility check (repo scope) =="
  VIS="$(gh api "repos/${ORG}/${REPO}" --jq '.visibility' 2>/dev/null)" || VIS=''
  VIS="$(printf '%s' "$VIS" | tr -d '[:space:]')"
  say "  ${ORG}/${REPO} visibility: ${VIS:-<unreadable>}"
  case "$VIS" in
    private|internal) say "  ok — not public." ;;
    public) refuse "${ORG}/${REPO} is PUBLIC — a fork PR would run arbitrary code on this box." ;;
    *) refuse "could not confirm ${ORG}/${REPO} is private. Fix auth or check the name." ;;
  esac
  RURL="https://github.com/${ORG}/${REPO}"
  API="repos/${ORG}/${REPO}/actions/runners/registration-token"
else
  say "== runner-group check (org scope) =="
  [ -n "$GROUP" ] || refuse "org scope needs --group. Without a restricted group this runner serves EVERY repo in ${ORG}, public ones included."
  # NOT named GROUPS: bash reserves that as a special read-only-ish array (the
  # calling process's own supplementary group IDs) — see register-gh-runner.sh
  # for the full explanation of the collision this avoids.
  RUNNER_GROUPS="$(gh api "orgs/${ORG}/actions/runner-groups" \
              --jq '.runner_groups[] | [.name, .visibility, (.allows_public_repositories|tostring)] | @tsv' 2>/dev/null)" || RUNNER_GROUPS=''
  GLINE="$(printf '%s\n' "$RUNNER_GROUPS" | awk -F'\t' -v g="$GROUP" '$1 == g {print; exit}')"
  [ -n "$GLINE" ] || refuse "could not confirm runner group '$GROUP' exists in ${ORG} (needs an org-admin gh; create the group first — see the header)."
  GVIS="$(printf '%s' "$GLINE" | cut -f2)"
  GPUB="$(printf '%s' "$GLINE" | cut -f3)"
  say "  group '$GROUP': visibility=$GVIS allows_public_repositories=$GPUB"
  [ "$GPUB" = "false" ] || refuse "runner group '$GROUP' allows public repositories."
  case "$GVIS" in
    selected|private) say "  ok — group is restricted." ;;
    *) refuse "runner group '$GROUP' visibility is '$GVIS' — not restricted to selected repos." ;;
  esac
  RURL="https://github.com/${ORG}"
  API="orgs/${ORG}/actions/runners/registration-token"
fi

# --- 2. registration token --------------------------------------------------
if [ -z "$TOKEN" ]; then
  say "== mint registration token =="
  if [ "$APPLY" = 1 ]; then
    TOKEN="$(gh api -X POST "$API" --jq '.token')" \
      || refuse "could not mint a registration token from $API — this needs ADMIN. Use an owner gh config or a short-lived fine-grained PAT with Administration:write, or pass --token."
    say "  got a token (expires ~1h)"
  else
    say "  would POST $API (needs ADMIN — all on-box identities are refused)"
    TOKEN='<REG_TOKEN>'
  fi
fi

# --- 3. configure -----------------------------------------------------------
say "== config.sh =="
say "  would run: (cd $DIR && ./config.sh --unattended --url $RURL --token **** --name $NAME --labels self-hosted,$LABEL --replace${GROUP:+ --runnergroup $GROUP})"
if [ "$APPLY" = 1 ]; then
  ARGS=(--unattended --url "$RURL" --token "$TOKEN" --name "$NAME" --labels "self-hosted,$LABEL" --replace)
  [ -n "$GROUP" ] && ARGS+=(--runnergroup "$GROUP")
  ( cd "$DIR" && ./config.sh "${ARGS[@]}" )
fi

# --- 4. enable the user unit (NOT svc.sh — that writes a competing system unit)
say "== enable + start $UNIT =="
run systemctl --user enable --now "$UNIT"
if [ "$APPLY" = 1 ]; then
  sleep 5
  systemctl --user --no-pager --lines=0 status "$UNIT" | sed -n '1,4p' || true
fi

say "== done =="
if [ "$APPLY" = 1 ]; then
  say "Runner '$NAME' registered. Point jobs at:  runs-on: [self-hosted, $LABEL]"
  say "Verify it appears idle, then migrate ONE workflow and re-measure queue wait."
  say "Do NOT point deploy/promote/release workflows here — they carry production"
  say "Azure credentials and this runner is not ephemeral."
else
  say "Dry-run complete. Re-run with --apply using an ADMIN credential."
fi
