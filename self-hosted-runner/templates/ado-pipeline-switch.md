# ADO pipeline switch — `agentPool` / `sastAgentPool`

The switch that moves all of CI between MS-hosted and self-hosted with **one variable flip**, and
rolls back the same way. Proven pattern from `cashbucket-com/app`.

## The idea

- Each **heavy template** takes an `agentPool` string parameter (default `''` = MS-hosted).
- Each **entry pipeline** declares one compile-time `agentPool` variable and threads it into every
  template call. Flip that one line → every stage moves.

## 1. Heavy template — accept the parameter, apply it at job level

```yaml
# pipelines/build.yml (same shape in deploy/test/dbmigrate/swap)
parameters:
# ...existing parameters...
# Name of a self-hosted agent pool to run this heavy job on instead of the MS-hosted image.
# Empty = hosted (default, unchanged). Threaded from the entry pipeline's agentPool variable.
- name: agentPool
  type: string
  default: ''

stages:
- stage: ${{ parameters.stageName }}
  jobs:
  - job: Build
    pool:
      ${{ if ne(parameters.agentPool, '') }}:
        name: ${{ parameters.agentPool }}
      ${{ else }}:
        vmImage: windows-2022        # unchanged hosted default
    steps:
      # ...unchanged...
```

## 2. Entry pipeline — one variable, threaded into every template call

```yaml
# azure-pipelines-dev.yml
variables:
- name: agentPool
  value: ''          # '' = MS-hosted windows-2022.  'Beelink-Linux' = run on the self-hosted pool.

stages:
- template: pipelines/build.yml
  parameters:
    agentPool: ${{ variables.agentPool }}
- template: pipelines/deploy.yml
  parameters:
    agentPool: ${{ variables.agentPool }}
# ...thread agentPool into test/dbmigrate/swap the same way...
```

**To switch:** change the one `value: ''` → `value: 'Beelink-Linux'` (match the registered pool
name). **To roll back:** change it back to `''`.

**Leave prod on hosted deliberately** — `azure-pipelines-prod.yml` keeps `value: ''`. Releases are
infrequent, so the minute pressure is on dev/PR. Flip dev first; move prod only once trusted.

## 3. Required PR gate — job-level `sastAgentPool` (the gotcha)

ADO **only allows a conditional `pool:` block at the job level, not the pipeline root.** For a
required PR gate whose pool must be conditional, use a separate `sastAgentPool` parameter and put the
`${{ if }}` inside the job:

```yaml
# azure-pipelines-pr.yml
parameters:
  # Defaults to the self-hosted pool: a required gate must be satisfiable even when hosted
  # minutes are exhausted (they reset 1st UTC). Override to '' to force hosted.
  - name: sastAgentPool
    type: string
    default: 'Beelink-Linux'

jobs:
  - job: AdversarialGate
    # A conditional pool block is only valid at the job level, so it lives here, not at root.
    pool:
      ${{ if ne(parameters.sastAgentPool, '') }}:
        name: ${{ parameters.sastAgentPool }}
      ${{ else }}:
        vmImage: ubuntu-latest
    steps:
      - checkout: self
      # ...gate steps...
```

> **Security:** a PR gate compiles/runs **unmerged** PR code. Only point `sastAgentPool` at a
> self-hosted pool for a **private repo with a trusted contributor set.** Never for a repo that could
> run an untrusted fork PR.
