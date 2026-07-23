# Self-hosted CI runner toolkit

A reusable, org-agnostic toolkit for running CI on a machine you own — for **either** Azure DevOps
(agents) **or** GitHub Actions (runners) — to escape the hosted-minute quota. The first self-hosted
job is free and unlimited on both platforms; you pay only for a box you already have.

Any org in the portfolio (rolliq, cashbucket, klsjapan, …) can stand up a runner for its platform by
following the runbook and running the scripts, with **no re-derivation** of the gotchas we already
paid for.

## Contents

| File | What it is |
|---|---|
| [`runbook.md`](runbook.md) | The full org-agnostic runbook — both platforms, parameterized on `{org, repo, pool/label, host, user}`. Prereqs → register → switch → verify → rollback → security → gotchas. |
| [`provision-host.sh`](provision-host.sh) | Idempotent host bootstrap. Installs the tool matrix (.NET, pwsh, Azure CLI, Cypress/Chrome+Xvfb, python-venv). `--apply` to run; dry-run by default. |
| [`register-ado-agent.sh`](register-ado-agent.sh) | Thin wrapper around ADO `config.sh` + `svc.sh` — resolves the latest agent tarball, writes the `.env`, installs the systemd service. |
| [`register-gh-runner.sh`](register-gh-runner.sh) | Thin wrapper around GitHub `actions/runner` `config.sh` + `svc.sh` — fetches a registration token, supports repo/org level + `--ephemeral`. |
| [`templates/ado-pipeline-switch.md`](templates/ado-pipeline-switch.md) | Copy-paste `agentPool` / `sastAgentPool` snippets for the ADO one-variable switch. |
| [`templates/gh-runs-on-switch.md`](templates/gh-runs-on-switch.md) | Copy-paste `runs-on: [self-hosted, <label>]` + runner-group snippets for GitHub. |

## Quickstart

```bash
# 1. provision the host (once)
./provision-host.sh --apply

# 2a. register an ADO agent...
./register-ado-agent.sh --org <ORG> --pool <POOL> --user <USER> --token <REGISTRATION_PAT>
# 2b. ...or a GitHub Actions runner
./register-gh-runner.sh --org <ORG> --repo <REPO> --label <LABEL> --user <USER> --ephemeral

# 3. flip the pipeline switch (one line — see templates/) and verify one green run.
```

Full detail, the ADO "Permit" UI step, and the security guardrails: **read [`runbook.md`](runbook.md).**

---

## Decision guide — is self-hosting worth it here?

Before standing up a runner, sanity-check that it's the right lever:

| Situation | Recommendation |
|---|---|
| **GitHub, public repo** | **Don't self-host.** Public repos get **unlimited** hosted Actions minutes for free, and a self-hosted runner on a public repo is an RCE risk (fork PRs run arbitrary code on your box). Stay hosted. |
| **GitHub, private repo, low volume** | Free tier is 2,000 min/mo. If you're under it, stay hosted. Over it → self-host, or buy more minutes if the box maintenance isn't worth it. |
| **GitHub, private repo, high volume (e.g. rolliq)** | **Self-host, ephemeral + autoscaled.** This is the biggest cost lever in the portfolio. Prefer a dedicated host/VM and single-job ephemeral runners for isolation. |
| **ADO, free tier hitting the 1,800 min/mo cap** | **Self-host.** One always-on agent removes the cap; first self-hosted job is free. This is the proven cashbucket path. |
| **ADO, want a second hosted parallel job instead** | ~$40/mo per parallel job. Cheaper to self-host if you own a box; buy the parallel job only if you can't run a persistent host. |

**Rule of thumb:** self-hosting pays off when you're **quota-blocked on a private/gated pipeline** and
already own a machine. It is **never** worth it — and is actively dangerous — for public-repo CI.

## Where this came from

Generalized from the proven cashbucket ADO implementation (`cashbucket-com/app`
`docs/self-hosted-agent-runbook.md`, ADO PR 416) — a full dev CD ran green on a Beelink using the
`agentPool` switch. The GitHub Actions half follows the same shape with `actions/runner` + `runs-on`.
