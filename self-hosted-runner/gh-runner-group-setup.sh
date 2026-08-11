#!/usr/bin/env bash
# gh-runner-group-setup.sh — create/verify the restricted runner group that an
# org-level self-hosted runner must live in.
#
# WHY THIS EXISTS: an org-level runner with no restricted group is reachable by
# EVERY repo in the org, including public ones — a fork PR on a public repo runs
# arbitrary code on this box (RCE). rolliq-com has a public `.github` repo. So the
# group must be visibility=selected, allows_public_repositories=false, and contain
# ONLY private/internal repos. This script builds exactly that and refuses to add
# anything public. It is the same property infra-commons/devops#13 enforces at
# registration time; enforcing it here too means the group cannot drift into being
# unsafe between runs.
#
# Needs an ADMIN credential (the on-box identities are all refused). Use a
# short-lived fine-grained PAT:
#   Organization permissions -> "Self-hosted runners": Read and write
#   (resource owner: the org; then revoke it when done)
#   export GH_TOKEN=github_pat_...
#
# Usage:
#   ./gh-runner-group-setup.sh --org <ORG> [--group <NAME>] [--apply]
#     --group   group name (default: beelink-light)
#     --apply   actually create/update (default: dry-run)
set -euo pipefail

ORG='' GROUP='beelink-light' APPLY=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --org) ORG="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done
[ -n "$ORG" ] || { echo "need --org" >&2; usage; exit 2; }

say() { printf '%s\n' "$*"; }
refuse() { echo "REFUSING: $1" >&2; exit 3; }
[ "$APPLY" = 1 ] || say "DRY-RUN — nothing will change. Re-run with --apply."

# --- 1. enumerate repos and split by visibility -----------------------------
say "== enumerate ${ORG} repos =="
JSON="$(gh api "orgs/${ORG}/repos?per_page=100" --paginate \
          --jq '.[] | [.name, .visibility, (.archived|tostring), (.id|tostring)] | @tsv' 2>/dev/null)" || JSON=''
[ -n "$JSON" ] || refuse "could not enumerate ${ORG} repos — check the token can read the org."

PRIV=''; PUB=''; NPRIV=0; NPUB=0
while IFS=$'\t' read -r name vis arch id; do
  [ -n "$name" ] || continue
  case "$vis" in
    private|internal) PRIV="${PRIV}${id} "; NPRIV=$((NPRIV+1)) ;;
    public)           PUB="${PUB}${name} "; NPUB=$((NPUB+1)) ;;
    *)                refuse "repo ${name} has unreadable visibility '${vis}' — refusing to build a group on incomplete data." ;;
  esac
done <<< "$JSON"

say "  private/internal: ${NPRIV}"
say "  public (EXCLUDED): ${NPUB}${NPUB:+  -> ${PUB}}"
[ "$NPRIV" -gt 0 ] || refuse "no private repos found — nothing safe to add."

# --- 2. create or verify the group ------------------------------------------
say "== runner group '${GROUP}' =="
# A FAILED READ IS NOT "ABSENT". A member token gets 403 here, and treating that
# as "the group does not exist" would make --apply create a SECOND group — the
# same fail-open class fixed in infra-commons/devops#13. Distinguish them by exit
# status: a successful read that matches nothing is empty with status 0.
if EXIST="$(gh api "orgs/${ORG}/actions/runner-groups" \
             --jq '.runner_groups[] | [.name, (.id|tostring), .visibility, (.allows_public_repositories|tostring)] | @tsv' 2>/dev/null)"; then
  :
else
  refuse "could not READ runner groups in ${ORG} (needs org admin / 'runners and runner groups' permission). Refusing to decide whether '${GROUP}' exists — mistaking an unreadable list for an empty one would create a duplicate group."
fi
LINE="$(printf '%s\n' "$EXIST" | awk -F'\t' -v g="$GROUP" '$1 == g {print; exit}')"

IDS="$(printf '%s' "$PRIV" | tr ' ' '\n' | grep -c . || true)"
if [ -z "$LINE" ]; then
  say "  does not exist — would CREATE (visibility=selected, allows_public_repositories=false, ${IDS} private repos)"
  if [ "$APPLY" = 1 ]; then
    ARGS=(-f "name=${GROUP}" -f visibility=selected -F allows_public_repositories=false)
    for id in $PRIV; do ARGS+=(-F "selected_repository_ids[]=${id}"); done
    GID="$(gh api -X POST "orgs/${ORG}/actions/runner-groups" "${ARGS[@]}" --jq '.id')" \
      || refuse "create failed — the token needs Organization > Self-hosted runners: Read and write."
    say "  created group id=${GID}"
  fi
else
  GID="$(printf '%s' "$LINE" | cut -f2)"
  GVIS="$(printf '%s' "$LINE" | cut -f3)"
  GPUB="$(printf '%s' "$LINE" | cut -f4)"
  say "  exists id=${GID} visibility=${GVIS} allows_public_repositories=${GPUB}"
  [ "$GPUB" = "false" ] || refuse "existing group '${GROUP}' allows public repositories — fix or use a different name."
  case "$GVIS" in selected|private) ;; *) refuse "existing group '${GROUP}' visibility is '${GVIS}', not restricted." ;; esac
  say "  would REPLACE its repo list with the ${IDS} private repos"
  if [ "$APPLY" = 1 ]; then
    ARGS=()
    for id in $PRIV; do ARGS+=(-F "selected_repository_ids[]=${id}"); done
    gh api -X PUT "orgs/${ORG}/actions/runner-groups/${GID}/repositories" "${ARGS[@]}" \
      || refuse "could not set the group's repo list."
    say "  repo list updated"
  fi
fi

# --- 3. verify --------------------------------------------------------------
if [ "$APPLY" = 1 ]; then
  say "== verify =="
  V="$(gh api "orgs/${ORG}/actions/runner-groups" \
        --jq ".runner_groups[] | select(.name==\"${GROUP}\") | [.visibility, (.allows_public_repositories|tostring)] | @tsv")"
  say "  visibility/allows_public = ${V}"
  say "  member repos:"
  gh api "orgs/${ORG}/actions/runner-groups/${GID}/repositories" --jq '.repositories[] | "    \(.name) (\(.visibility))"'
  BAD="$(gh api "orgs/${ORG}/actions/runner-groups/${GID}/repositories" --jq '[.repositories[]|select(.visibility=="public")]|length')"
  [ "$BAD" = "0" ] || refuse "group contains ${BAD} PUBLIC repo(s) after the update — remove them before registering a runner."
  say "  ok — no public repo in the group."
  say ""
  say "Next: register the runner into this group:"
  say "  ./gh-runner-light-register.sh --slot 2 --org ${ORG} --group ${GROUP} --apply"
else
  say ""
  say "Dry-run complete. Re-run with --apply using an admin credential."
fi
