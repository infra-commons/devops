# Self-hosted CI runner — org-agnostic runbook (ADO + GitHub Actions)

Stand up a **self-hosted CI runner** on a machine you own to escape the hosted-minute quota. The
first self-hosted job is **free and unlimited** on both Azure DevOps (agents) and GitHub Actions
(runners); you pay only for the box.

The concept is identical across both platforms: run a runner agent on your own host → the CI system
dispatches jobs to it instead of a metered cloud VM. Only the **binary** and the **pipeline-side
switch** differ. This runbook covers both; pick your platform section.

> **Reference implementation:** the ADO half was built and proven end-to-end on real PR code in a
> cashbucket session (full dev CD — Build → Deploy(slot) → DB migrate → Cypress → measured Swap — ran
> green on a Beelink). This toolkit generalizes that. See the [decision guide](README.md) for when
> self-hosting is worth it.

---

## Parameters (substitute throughout)

| Token | Meaning | Example |
|---|---|---|
| `<ORG>` | ADO org name / GitHub org | `CashBucket` / `rolliq-com` |
| `<REPO>` | GitHub repo (GH only) | `solution-recruitment-reference-check` |
| `<POOL_OR_LABEL>` | ADO pool name / GH runner label | `Beelink-Linux` / `beelink` |
| `<HOST>` | the machine running the agent | the Beelink |
| `<RUNNER_USER>` | OS user the service runs as | `kev` or a dedicated `ci-runner` |

---

## 0. Decide before you start

- **Which platform?** ADO (agents) or GitHub Actions (runners). You can run **both** binaries on the
  same host — they use separate work dirs and don't conflict.
- **Which host?** One shared box, or per-org hosts for volume/isolation. A high-volume GitHub org
  (lots of private-repo Actions) may warrant its own ephemeral/autoscaled runners; a low-volume ADO
  org is fine with one always-on service.
- **Ephemeral vs always-on?** Always-on service = simplest (cashbucket-scale). Ephemeral single-job
  runners = safer + autoscale-friendly (recommended for GitHub at scale). See platform sections.

---

## 1. Provision the host (both platforms)

Run the idempotent bootstrap once per host. It installs the tool matrix the reference org actually
needed (adjust with flags for your stack):

```bash
./provision-host.sh            # dry-run: prints what it would install
./provision-host.sh --apply    # install
# stack toggles (all default on except --python):
./provision-host.sh --apply --dotnet --pwsh --azcli --cypress --python
```

The tool matrix and **why each is needed** (Ubuntu noble reference):

| Tool | Why | Gotcha |
|---|---|---|
| .NET SDK | `dotnet build/publish/test`, migrators | user-local `~/.dotnet`; export `DOTNET_ROOT` in the agent `.env` (jobs won't see it otherwise) |
| PowerShell 7 (`pwsh`) | `PowerShell@2` / pwsh tasks | not default; apt tarball **or** `dotnet tool install -g PowerShell` |
| Azure CLI (`az`) | `AzureCLI@2` tasks calling `az` directly | `AzureWebApp@1`/`AzureAppServiceManage@0` self-auth via service connection; only direct `az` needs it on PATH |
| Node | `UseNode@1`/`setup-node` self-download | **not** a host prereq — the CI task fetches it per run |
| Chrome + libs | Cypress `--browser chrome` | `google-chrome-stable` via direct `.deb` (not in apt); on noble use the `t64` lib names |
| **Xvfb** | Cypress on a **headless** Linux runner | MS/GitHub-hosted images ship it; self-hosted does **not** → `Error: spawn Xvfb ENOENT`. `apt install xvfb`; binary on PATH is enough, no pipeline change |
| `python3.x-venv` | python-based gates (e.g. an adversarial-review gate) | `ensurepip` missing by default; self-hosted has **no `UsePythonVersion` tool cache** → build a venv from system python instead (also sidesteps PEP-668) |

---

## 2. Register the runner

### Azure DevOps

```bash
# short-lived PAT scoped **Agent Pools (Read & manage)** only — used once at config time
./register-ado-agent.sh \
  --org <ORG> \
  --pool <POOL_OR_LABEL> \
  --user <RUNNER_USER> \
  --token <REGISTRATION_PAT>
```

What it does: downloads the current `vsts-agent-linux-x64` tarball, writes a `.env` that injects
`DOTNET_ROOT`/`PATH` into the service job environment, runs `config.sh --unattended`, and installs +
starts the systemd service (`svc.sh install <user>`).

Then, **one-time in the ADO UI** (a Build PAT *cannot* do this — see gotcha #4):
1. **Organization settings → Agent pools → Add pool → Self-hosted →** name it `<POOL_OR_LABEL>`
   (must match `--pool`).
2. **Project settings → Agent pools → `<POOL_OR_LABEL>` → Security →** grant the pipeline **Use**
   permission (first run may prompt a one-click "Permit" resource-authorization).

### GitHub Actions

```bash
# short-lived registration token: gh api -X POST repos/<ORG>/<REPO>/actions/runners/registration-token
# or org-level: repos → orgs/<ORG>/actions/runners/registration-token (the script fetches it for you if gh is authed)
./register-gh-runner.sh \
  --org <ORG> \
  --repo <REPO> \            # omit --repo for an org-level runner
  --label <POOL_OR_LABEL> \
  --user <RUNNER_USER> \
  --ephemeral                # recommended: single-job runner, re-registered each run
```

What it does: downloads the current `actions/runner` release, runs `config.sh` with the registration
token + label, and installs the service (`svc.sh install`). With `--ephemeral` the runner deregisters
after one job (pair with an autoscaler/loop to re-register).

---

## 3. Flip the pipeline switch

The switch is **one edit, trivially reversible**. Snippets: [`templates/`](templates/).

### ADO — `agentPool` variable

Every heavy template takes an `agentPool` parameter; the entry pipeline threads it from one
compile-time variable. Flip that one line:

```yaml
# azure-pipelines-<stage>.yml
variables:
- name: agentPool
  value: 'Beelink-Linux'   # '' = MS-hosted windows-2022 (default). '<pool>' = self-hosted.
```

For a **required PR gate** (or anywhere a conditional pool must sit inside an explicit job), use the
job-level `sastAgentPool` pattern — ADO only permits a conditional `pool:` block at job level, not
pipeline root. See [`templates/ado-pipeline-switch.md`](templates/ado-pipeline-switch.md).

**Leave prod on hosted deliberately** — releases are infrequent, so the minute pressure is on
dev/PR. Flip dev first; move prod only once you trust the self-hosted path.

### GitHub Actions — `runs-on`

```yaml
jobs:
  build:
    runs-on: [self-hosted, beelink]   # was: ubuntu-latest
```

Or use a **runner group** for org-level fleets. See
[`templates/gh-runs-on-switch.md`](templates/gh-runs-on-switch.md).

---

## 4. Verify

- **ADO:** merge → confirm the run's job-log header reads **`Pool: <POOL_OR_LABEL> / Agent: <name>`**.
- **GitHub:** the job header shows the self-hosted runner name + labels; `gh run view <id>` confirms.
- Watch it green through the whole pipeline once before trusting it.

## 5. Rollback

Same one line, reversed:
- **ADO:** `agentPool` back to `''` → next run returns to MS-hosted `windows-2022`.
- **GitHub:** `runs-on:` back to `ubuntu-latest` (or `windows-latest`). Nothing else changes.

---

## Security

**This is the sharpest edge — read it.**

- A self-hosted runner that **compiles/runs CI code runs that code on your host.** Safe **only** for a
  **private repo with a trusted contributor set.**
- **GitHub-specific and severe: NEVER attach a self-hosted runner to a public repo.** A fork PR can
  run arbitrary code on the runner → RCE on your box. GitHub documents this explicitly. ADO's PR gate
  carries the same principle, but GitHub's fork-PR exposure is a sharper edge.
- Prefer **ephemeral, single-job runners** (GitHub); scope registration tokens least-privilege;
  isolate the runner OS user; consider a dedicated host/VM for org-level runners.
- ADO dev/prod pipelines run already-merged code → lower risk than a PR-build gate that compiles
  unmerged PR code.

---

## Gotchas (baked into the scripts, repeated here so you know why)

1. **ADO agent download host** — `vstsagentpackage.azureedge.net` is **DEAD**. Current URL is
   `https://download.agent.dev.azure.com/agent/<V>/vsts-agent-linux-x64-<V>.tar.gz`.
   `register-ado-agent.sh` resolves the latest version automatically.
2. **Ubuntu noble `t64` package renames** — `libasound2`→`libasound2t64`, `libgtk-3-0`→`libgtk-3-0t64`.
   `provision-host.sh` picks the right names per release.
3. **`UsePythonVersion@0` fails on self-hosted** — it selects from the agent tool cache and doesn't
   self-install. Build a **venv from system python** instead (also sidesteps PEP-668).
4. **ADO resource-authorization "Permit" gate** — a newly-referenced pool needs a one-time UI
   authorization before *any* build dispatches. Symptom before granting: the build shows "queued" but
   **no build record is created**. A Build-Read PAT can't do this — it's an admin UI action.
5. **`.env` in the agent dir** is the reliable way to inject `DOTNET_ROOT`/`PATH` into the systemd
   service's job environment so a user-local SDK is visible to jobs.
6. **Casualty to watch:** other hosted pipelines (e.g. an ADO→GitHub mirror) still break on exhausted
   minutes until they're also moved onto the pool or minutes reset (ADO resets 1st UTC; GitHub on the
   billing-cycle date).
7. **Self-hosted runners REUSE their work dir between runs** (hosted gets a clean VM each run). Any
   step assuming a *clean destination* — extract/copy/generate into a fixed path — passes on hosted
   and fails on the first self-hosted re-run (real hit: `Expand-Archive -DestinationPath` without
   `-Force` errored on existing files). **Audit every extract/copy-into-fixed-path step for
   idempotency before cutover** (overwrite or clean-first). Ephemeral GitHub runners avoid this.
