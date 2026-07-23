# GitHub Actions switch — `runs-on: [self-hosted, <label>]`

The GitHub equivalent of the ADO `agentPool` switch. Moves a job from a hosted image to your
self-hosted runner with one edit, and rolls back the same way.

## 1. Per-job label switch

```yaml
jobs:
  build:
    runs-on: [self-hosted, beelink]   # was: ubuntu-latest
    steps:
      # ...unchanged...
```

- The array is an **AND** of labels: the job lands on a runner carrying **all** of them. Every
  self-hosted runner implicitly has the `self-hosted` label plus its OS/arch labels
  (`linux`, `x64`) and any custom labels you set at registration (`--label beelink`).
- **Roll back** by setting `runs-on: ubuntu-latest` (or `windows-latest`). Nothing else changes.

## 2. Parameterize it (matrix / input) for a clean flip

To keep one place to flip, drive `runs-on` from a workflow input or a matrix value:

```yaml
on:
  workflow_dispatch:
    inputs:
      runner:
        description: 'ubuntu-latest (hosted) or self-hosted label'
        default: 'ubuntu-latest'

jobs:
  build:
    runs-on: ${{ github.event.inputs.runner || 'ubuntu-latest' }}
```

For reusable workflows, expose `runner` as a `workflow_call` input with an `ubuntu-latest` default so
callers opt in per repo.

## 3. Runner groups (org-level fleets)

For an org fleet, register runners at the **org** level and put them in a **runner group**, then
target the group + labels. Restrict the group to selected repos so a runner never serves an
untrusted repo:

```yaml
jobs:
  build:
    runs-on:
      group: beelink-fleet
      labels: [self-hosted, linux, x64]
```

## 4. Prefer ephemeral runners at scale

Register with `--ephemeral` (see `register-gh-runner.sh`) so each runner takes exactly one job then
deregisters. Pair with an autoscaler (or a simple re-register loop) that spins a fresh runner per
queued job. Benefits:

- **Clean work dir every run** — sidesteps the "reused `_work` dir" idempotency trap that bites
  always-on runners (a step that extracts/copies into a fixed path fails on the second run).
- **Smaller blast radius** — a compromised job can't persist into the next.

## Security (read this)

- **NEVER attach a self-hosted runner to a public repo.** A fork PR runs arbitrary code on the runner
  → RCE on your host. GitHub documents this explicitly. Private repos + trusted contributors only.
- GitHub meters hosted minutes on **private repos only** (public repos = unlimited hosted). So
  self-hosting **only pays off for private repos** — and on public repos it is pure downside.
- Isolate the runner OS user; scope registration tokens least-privilege; prefer a dedicated host/VM
  for org-level runners.
