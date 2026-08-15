#!/usr/bin/env bash
#
# Register the Azure resource-provider namespaces a solution's Terraform needs,
# up-front, in a customer subscription.
#
# A brand-new customer subscription starts with most resource providers
# UNregistered. The azurerm provider's default ("core") registration set does not
# include Microsoft.App (Container Apps) and several others a typical solution
# creates, so the first `terraform apply` otherwise fails with
#   409 MissingSubscriptionRegistration: ... namespace 'Microsoft.App'.
# Register the exact set the solution needs before apply. The deployer SP's
# Contributor role includes */register/action, so it can self-register.
# Idempotent: already-registered namespaces return immediately.
#
# Fails closed: verifies every namespace reached Registered rather than
# re-discovering a 409 mid-apply.

set -euo pipefail

SUBSCRIPTION_ID="${SUBSCRIPTION_ID:?SUBSCRIPTION_ID is required}"

# Default set covers the current Rolliq container-app + postgres + key-vault +
# log-analytics + storage + ACR + monitoring stack. Callers override NAMESPACES
# (whitespace- or newline-separated) for a different footprint.
DEFAULT_NAMESPACES="\
Microsoft.App
Microsoft.ManagedIdentity
Microsoft.KeyVault
Microsoft.OperationalInsights
Microsoft.Storage
Microsoft.ContainerRegistry
Microsoft.DBforPostgreSQL
Microsoft.Insights
Microsoft.Network"

# Read the caller-supplied list (whitespace/newline separated) into an array,
# falling back to the default set when unset or blank.
read -r -a NS_ARRAY <<<"$(echo "${NAMESPACES:-$DEFAULT_NAMESPACES}" | tr '\n' ' ')"

if [ "${#NS_ARRAY[@]}" -eq 0 ]; then
  echo "ERROR: no resource-provider namespaces to register." >&2
  exit 1
fi

# Reject anything that is not a valid provider namespace before handing it to az,
# so a malformed / injected entry can't turn into an arbitrary CLI argument.
NS_RE='^[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]+$'
for ns in "${NS_ARRAY[@]}"; do
  if ! echo "$ns" | grep -qE "$NS_RE"; then
    echo "ERROR: '$ns' is not a valid Azure resource-provider namespace." >&2
    exit 1
  fi
done

for ns in "${NS_ARRAY[@]}"; do
  echo "Registering $ns ..."
  az provider register --namespace "$ns" \
    --subscription "$SUBSCRIPTION_ID" --wait
done

# Verify every namespace reached Registered before terraform apply relies on it.
# `az provider register --wait` is meant to block until the namespace is
# Registered, but on a brand-new ("cold") subscription it can return while a
# namespace is still "Registering" — Microsoft.Network is the observed
# straggler. A single verify pass then trips the fail-closed guard even though
# registration is still progressing and would finish moments later. So poll each
# namespace to the Registered terminal state with a bounded budget instead of
# checking once.
REGISTER_TIMEOUT="${REGISTER_TIMEOUT:-600}"   # seconds to allow for cold-sub stragglers
deadline=$(( SECONDS + REGISTER_TIMEOUT ))
for ns in "${NS_ARRAY[@]}"; do
  while :; do
    state=$(az provider show --namespace "$ns" \
      --subscription "$SUBSCRIPTION_ID" --query registrationState -o tsv)
    if [ "$state" = "Registered" ]; then
      echo "$ns = Registered"
      break
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "ERROR: resource provider $ns did not reach Registered (last state: ${state:-unknown}) within ${REGISTER_TIMEOUT}s." >&2
      exit 1
    fi
    echo "$ns = ${state:-unknown} (waiting for Registered ...)"
    sleep 15
  done
done
