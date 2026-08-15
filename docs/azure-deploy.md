# Azure Container Apps deploy — the canonical reusable

`azure-deploy-reusable.yml` extracts the deploy pipeline that
`rolliq-com/solution-template` and `rolliq-com/solution-recruitment-reference-check` (rrc) each
carried as an independent, drifting `reusable-deploy.yml` — 4,174 lines in rrc against 1,899 in the
template, 2,400+ differing lines, the largest fork between the two repos. It builds a container
image, derives a per-client deploy matrix from a `clients-config` repo, and bootstraps + deploys to
Azure Container Apps per client, with the guardrails a production deploy path needs: prod refuses to
ship anything staging hasn't cosign-signed, image references are validated before they can reach
Terraform, and a zero-retention (Mid tier) client is refused unless its config actually satisfies
that promise.

**This document describes the core** — `build`, `load-matrix`, `deploy`, the `workflow_call`
contract, and this repo's self-test. It is not the whole extraction: caller-specific hardening one
source repo has and the other doesn't (pin-crash pre-flight, alert-rule-survived check, release
changelog, branding restart+verify, Easy Auth check, template-drift verify, `mode:
onboard-identities`) lands as later, independent PRs, each an opt-in `input:` flag. This file is
updated as those land.

## Caller shape

```yaml
jobs:
  deploy:
    permissions:
      contents: read
      id-token: write   # a called workflow cannot hold more than its caller grants
    uses: infra-commons/devops/.github/workflows/azure-deploy-reusable.yml@<SHA>
    with:
      allowed_class: test
      client_slug: ${{ inputs.client_slug }}
    secrets:
      AZURE_CLIENT_ID: ${{ secrets.AZURE_CLIENT_ID }}
      AZURE_TENANT_ID: ${{ secrets.AZURE_TENANT_ID }}
      AZURE_MANAGEMENT_SUBSCRIPTION_ID: ${{ secrets.AZURE_MANAGEMENT_SUBSCRIPTION_ID }}
      ACR_NAME: ${{ secrets.ACR_NAME }}
      ACR_LOGIN_SERVER: ${{ secrets.ACR_LOGIN_SERVER }}
      CLIENTS_CONFIG_TOKEN: ${{ secrets.CLIENTS_CONFIG_TOKEN }}
      ACR_IMPORT_PASSWORD: ${{ secrets.ACR_IMPORT_PASSWORD }}
      ACR_IMPORT_USERNAME: ${{ secrets.ACR_IMPORT_USERNAME }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      PLATFORM_IAC_TOKEN: ${{ secrets.PLATFORM_IAC_TOKEN }}
      TF_STATE_RESOURCE_GROUP: ${{ secrets.TF_STATE_RESOURCE_GROUP }}
      TF_STATE_STORAGE_ACCOUNT: ${{ secrets.TF_STATE_STORAGE_ACCOUNT }}
```

**The `deploy` job's two Azure-bootstrap steps use composite actions vendored in this repo**
(`.github/actions/bootstrap-tfstate-backend`, `.github/actions/register-resource-providers`),
not `rolliq-com/platform-iac`. They used to live there; `infra-commons/devops#23` moved them here
because GitHub only shares a private repo's actions with same-org callers via the default
`GITHUB_TOKEN`, never cross-organization — a canonical reusable that only one entity org's callers
could invoke wasn't canonical in any useful sense. `PLATFORM_IAC_TOKEN` above is unrelated to that
move: it authenticates Terraform's own HTTPS fetch of `platform-iac`'s Terraform *modules* (a
PAT-authenticated git fetch, not a `GITHUB_TOKEN`-gated Actions call), which stays a legitimate
cross-org dependency.

**Pin to an immutable SHA, not to a branch.** Decided for this extraction specifically (not the
general infra-commons default, which is unsettled across older reusables — see below): a hard SHA
pin per caller, not `@main` and not a moving tag. With exactly two callers today, a SHA bump is two
one-line PRs, and it's what lets one caller (`solution-template`) canary a bump before the other
(rrc, the live customer) follows — a moving tag would advance both at once with no way to stage the
customer behind the template. `secrets: inherit` is deliberately **not** used, unlike this
reusable's same-repo local predecessor: every secret is named explicitly, matching this repo's own
convention (`cloudflare-deploy-reusable.yml`, `auto-assign-reusable.yml`) rather than the
same-repo-only convenience rrc's and the template's local callers used.

Every job's `permissions:` requests only `contents: read` and `id-token: write` — grant exactly
that on the calling job, nothing more. A caller that grants less gets `startup_failure`: **no
check-run at all**, not a red one, which on a required check reads as an unmergeable PR rather than
a failing one. See `infra-commons/devops#18`/`#19` and this repo's `reusable-caller-docs.yml`, which
checks that this file's own header snippet (above) stays correct.

**One org-admin prerequisite, tracked separately (`infra-commons/meta#583`):** the `build` job's
`azure/login` step needs `azure/login@*` in infra-commons' own org Actions allow-list. Actions used
inside a reusable are governed by the org that *hosts* the workflow file, not the org that calls it,
so this blocks any real caller regardless of which org adopts it — not something an adopting org can
fix on its own side.

## Contract growth by layer

Declared once here rather than left for a caller to discover by trial and `startup_failure`:

| Layer | Added |
|---|---|
| `build` | `client_slug`, `allowed_class`, `target_environment`, `image_ref`, `mode` inputs; `image_digest` output; 5 secrets |
| `load-matrix` | `CLIENTS_CONFIG_TOKEN` secret only — no new inputs/outputs |
| `deploy` | 6 secrets (`ACR_IMPORT_PASSWORD`, `ACR_IMPORT_USERNAME`, `ANTHROPIC_API_KEY`, `PLATFORM_IAC_TOKEN`, `TF_STATE_RESOURCE_GROUP`, `TF_STATE_STORAGE_ACCOUNT`) — no new inputs/outputs |

`load-matrix`'s own `matrix` output and `deploy`'s per-client result both stay internal, consumed
via `needs:`/`strategy: matrix` — neither is exposed through `workflow_call.outputs`, matching the
source files' own design.

## What did *not* move here, and why

**rrc's caller-specific hardening** (pin-crash pre-flight, alert-rule-survived-bootstrap check,
outgoing-revision capture, release changelog, branding restart+verify, Easy Auth
federated-credential check, template-drift verify) is guardrail logic around the *shared*
bootstrap/OIDC/KV/revision mechanism, not rrc's business domain — it belongs in this reusable behind
opt-in `input:` flags, default off, so `solution-template` can adopt at parity with its own current
file on day one. Lands as separate, later PRs, each close to a pure lift from rrc with an `if:
inputs.enable_x` guard added.

**Nothing was excluded as "too product-specific to belong here."** An earlier version of this
document's companion PR comment claimed the Layer-2 staging E2E gate (rrc's
`scripts/staging-e2e.sh`, a Zoom→LLM→SharePoint driver) would stay a follow-on job in rrc's own
caller. That was wrong: the *workflow-level* step is a generic driver-script hook — it runs
`bash scripts/staging-e2e.sh` from inside the `deploy` job so it reuses the deploy identity's Azure
auth and Terraform outputs, and the solution-repo-local script is what's actually product-specific,
exactly like the `staging-acceptance.sh` hook one step earlier in the same job. Both source files
carry the hook; only the script content each repo ships behind it differs. The hook is already in
this extraction (verbatim from the template's baseline, under the generic step name "Layer-2 staging
E2E gate (real pipeline run)").

## Self-test scope

`infra-commons/devops` holds no Azure credentials, so nothing here can prove a real `terraform
apply`/bicep deploy — that proof is the extraction issue's DoD item 2, and it's the adopting org's
to run against its own pre-flight environment. What `scripts/test_azure_deploy_reusable.py` proves
instead, stdlib-only (no PyYAML, matching every other test script in this repo):

- No `run:` block in any `*-reusable.yml` here is within ~5,000 characters of GitHub's
  21,000-character expression-compile cap — the failure mode that made every deploy impossible for
  ~20 hours in the source repo (rrc `#865`/`#866`), because past the cap a run is recorded with zero
  jobs and no logs, so nothing but a dispatched deploy ever reveals it.
- Any expression-free `run:` block large enough to be safe *only* because it contains no
  interpolation declares that fact in its own first lines (`EXPRESSION-FREE BY DESIGN`), in words —
  because adding one interpolation to an unmarked block takes it from "large but fine" to "does not
  compile" in a single character, with nothing to warn the next editor.
- The guard's own pinned subjects (the largest expression-free block, the largest interpolated one)
  still exist — a guard that silently stops finding what it was written for is worse than no guard.

Run it locally: `python3 scripts/test_azure_deploy_reusable.py`. Also run
`python3 scripts/test_reusable_caller_docs.py` after any change to a job's `permissions:` or to this
file's header caller-pattern snippet — it fails the same way (loudly, before merge) that a
`startup_failure` would fail silently in a real caller's run.

## Changing the reusable

Two checks currently guard this file, both stdlib-only and run directly (this repo carries no
`ci.yml`, `testing: {suite: none}` in the fleet's own devops standard):

1. `python3 scripts/test_reusable_caller_docs.py` — every job's `permissions:` must still be
   grantable from the header's own documented caller snippet.
2. `python3 scripts/test_azure_deploy_reusable.py` — no `run:` block may drift toward the expression
   cap, and any large expression-free block must keep declaring itself as such.

Adding a new caller-specific hardening layer (the follow-on PRs described above): copy the step(s)
verbatim from whichever source repo has them, wrap the copied step(s) in `if: inputs.enable_<name>`
(default `false`), add the input to `on.workflow_call.inputs`, and re-run both checks — a new large
`run:` block is exactly the shape the expression-cap guard exists to catch before it reaches a real
caller.
