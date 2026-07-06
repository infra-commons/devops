#!/usr/bin/env bash
# scrub-remote-tokens.sh — remove embedded credentials from git remote URLs.
#
# Rewrites any remote of the form
#     https://<user>:<token>@github.com/<owner>/<repo>.git
# to the clean form
#     https://github.com/<owner>/<repo>.git
# so git authenticates through the configured credential helper
# (`gh auth git-credential`) instead of a token baked into .git/config.
#
# Why this matters:
#   * Secret-at-rest — a token in .git/config is readable by anything that can
#     read the file and leaks into backups / tarballs of the checkout.
#   * It goes stale — GitHub App installation tokens (ghs_) expire in ~1 hour,
#     after which the checkout cannot fetch or push at all, because git uses the
#     embedded credential verbatim and never consults the helper.
#
# A clean URL delegates auth to the credential helper, which mints/serves a live
# token per operation. See docs/git-remote-token-hygiene.md.
#
# Usage:
#     scripts/scrub-remote-tokens.sh [--dry-run] [ROOT ...]
#
#   ROOT       one or more directories to scan recursively for git repos.
#              Defaults to ~/repos.
#   --dry-run  report what would change without modifying anything.
#   -h|--help  show this header.
#
# Exit status: 0 on success (including "nothing to scrub").
set -euo pipefail

DRY_RUN=0
ROOTS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,/^set -euo/{/^set -euo/!p}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) ROOTS+=("$arg") ;;
  esac
done
[ ${#ROOTS[@]} -eq 0 ] && ROOTS=("$HOME/repos")

# https URLs that carry userinfo (…://<userinfo>@github.com/…). SSH remotes
# (git@github.com:…) and already-clean https remotes have no '@' before the
# host and are left untouched.
tokened='^https://[^/@]+@github\.com/'

scanned=0
scrubbed=0
while IFS= read -r gitdir; do
  repo="$(dirname "$gitdir")"
  scanned=$((scanned + 1))
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    name="${key#remote.}"; name="${name%.url}"
    url="$(git -C "$repo" config --get "$key")"
    if [[ "$url" =~ $tokened ]]; then
      clean="https://github.com/${url#*@github.com/}"
      if [ "$DRY_RUN" -eq 1 ]; then
        echo "WOULD scrub  $repo  [$name]  -> $clean"
      else
        git -C "$repo" remote set-url "$name" "$clean"
        echo "scrubbed     $repo  [$name]  -> $clean"
      fi
      scrubbed=$((scrubbed + 1))
    fi
  done < <(git -C "$repo" config --get-regexp '^remote\..*\.url' 2>/dev/null | awk '{print $1}')
done < <(find "${ROOTS[@]}" -type d -name .git 2>/dev/null)

echo ""
if [ "$DRY_RUN" -eq 1 ]; then
  echo "Scanned $scanned repo(s); would scrub $scrubbed remote(s) (dry-run)."
else
  echo "Scanned $scanned repo(s); scrubbed $scrubbed remote(s)."
fi
