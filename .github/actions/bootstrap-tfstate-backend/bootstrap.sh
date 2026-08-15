#!/usr/bin/env bash
#
# Bootstrap a Terraform remote-state backend in a customer's OWN Azure tenant.
#
# client_owned solutions keep their Terraform state in the customer subscription,
# not Rolliq's central backend. The backend storage account cannot itself be
# created by a Terraform run that uses it as its backend (chicken-and-egg), so it
# is provisioned imperatively with the az CLI before `terraform init`. The whole
# operation is idempotent: on every deploy after the first it is a no-op.
#
# First-deploy resilience: a freshly federated deployer SP becomes ARM-effective
# asymmetrically. For a brand-new customer subscription the OIDC-token auth path
# lags secret-auth and propagates inconsistently across ARM replicas, so the first
# calls intermittently return SubscriptionNotFound / AuthorizationFailed /
# InvalidAuthenticationTokenTenant even though the role assignment is correct.
# Retry the idempotent bootstrap on those transient propagation errors; fail fast
# on any other (real) error.
#
# The deployer SP must hold Storage Blob Data Owner on the state account (granted
# at onboarding) so that use_azuread_auth works for the container create and later
# `terraform init`.
#
# Requires: az CLI (already logged in as the deployer SP) and `id-token: write` on
# the calling job (used to re-exchange a fresh OIDC token between retries).

set -euo pipefail

# --- inputs (from the composite action's env) -------------------------------
CLIENT_SLUG="${CLIENT_SLUG:?CLIENT_SLUG is required}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:?SUBSCRIPTION_ID is required}"
REGION="${REGION:?REGION is required}"
AZURE_CLIENT_ID="${AZURE_CLIENT_ID:?AZURE_CLIENT_ID is required}"
AZURE_TENANT_ID="${AZURE_TENANT_ID:?AZURE_TENANT_ID is required}"

STATE_RG="${STATE_RG:-rg-${CLIENT_SLUG}-tfstate}"
CONTAINER_NAME="${CONTAINER_NAME:-tfstate}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"        # ~15 min at RETRY_SECONDS spacing
RETRY_SECONDS="${RETRY_SECONDS:-30}"

# Storage account names are globally unique, 3-24 chars, lowercase alphanumeric
# only. Compose from the slug with the dashes stripped; callers may override the
# whole name via STATE_SA. "sttfstate" is 9 chars, leaving 15 for the slug.
STATE_SA="${STATE_SA:-sttfstate$(echo "$CLIENT_SLUG" | tr -d '-')}"
if ! echo "$STATE_SA" | grep -qE '^[a-z0-9]{3,24}$'; then
  echo "ERROR: derived state storage account name '$STATE_SA' is not a valid Azure" >&2
  echo "       storage account name (3-24 lowercase alphanumeric). Pass a shorter" >&2
  echo "       client-slug or override state-storage-account." >&2
  exit 1
fi

bootstrap_backend() {
  az group create -n "$STATE_RG" -l "$REGION" -o none && \
  az storage account create \
    -n "$STATE_SA" -g "$STATE_RG" -l "$REGION" \
    --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
    --allow-blob-public-access false --allow-shared-key-access false -o none && \
  az storage container create \
    --account-name "$STATE_SA" --name "$CONTAINER_NAME" --auth-mode login -o none
}

# Re-exchange a fresh GitHub OIDC token and re-login the deployer SP. First-deploy
# SubscriptionNotFound is per-JOB ARM-replica affinity: a runner pins to one ARM
# replica for its lifetime, and a brand-new federated SP is effective on only some
# replicas for the first hour+. Retrying the same az session hits the same replica
# every time, so we re-authenticate between attempts (the in-job analogue of
# "fresh job = fresh replica draw"). Requires id-token: write on the job.
fresh_login() {
  local tok
  [ -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ] && [ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || return 1
  tok=$(curl -sS \
    -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
    "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=api://AzureADTokenExchange" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])') || return 1
  az login --service-principal -u "$AZURE_CLIENT_ID" -t "$AZURE_TENANT_ID" \
    --federated-token "$tok" --output none || return 1
  az account set --subscription "$SUBSCRIPTION_ID" --output none || return 1
}

attempt=1
while true; do
  if out=$(bootstrap_backend 2>&1); then
    echo "$out"
    echo "State backend bootstrap succeeded (attempt $attempt)."
    break
  fi
  if echo "$out" | grep -qiE "SubscriptionNotFound|was not found|AuthorizationFailed|InvalidAuthenticationTokenTenant"; then
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
      echo "$out"
      echo "ERROR: bootstrap still failing after $attempt attempts — cross-tenant OIDC propagation did not complete in time." >&2
      exit 1
    fi
    echo "Transient cross-tenant OIDC propagation error (attempt $attempt/$MAX_ATTEMPTS) — re-authenticating and retrying in ${RETRY_SECONDS}s:"
    echo "$out" | tail -2
    sleep "$RETRY_SECONDS"
    # Draw a fresh OIDC token / az session so the next attempt can land on a
    # different ARM replica (best-effort; ignore re-login hiccups).
    fresh_login || echo "WARNING: re-login before retry failed — continuing with existing session."
    attempt=$((attempt + 1))
    continue
  fi
  # Any other failure is a real error — surface it immediately.
  echo "$out" >&2
  echo "ERROR: state backend bootstrap failed with a non-transient error." >&2
  exit 1
done

# Emit the resolved backend coordinates for the caller's `terraform init`.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "state-storage-account=$STATE_SA"
    echo "state-resource-group=$STATE_RG"
    echo "container-name=$CONTAINER_NAME"
  } >> "$GITHUB_OUTPUT"
fi
