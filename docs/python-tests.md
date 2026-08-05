# Python tests + coverage — the canonical reusable

`.github/workflows/python-tests-reusable.yml` runs a repository's pytest suite and,
optionally, measures its coverage. It exists so that "does this repository execute its
tests, and what does it do with the number" stops being a per-repository answer.

The workflow's own header carries the design reasoning and is the authoritative input
reference — it is the file that cannot go stale. This document is the adoption guide: what
a caller changes, and what a caller must change **at the same time**.

---

## Caller shape

```yaml
jobs:
  test:
    uses: infra-commons/devops/.github/workflows/python-tests-reusable.yml@<SHA>
    with:
      source_dir: src
      test_path: tests
      install: -e .[dev]
      coverage: floor:80
```

Pin to an immutable SHA, not to a branch.

### `install` is a pip argument list, not a shell command

Only `pip install` is ever invoked with it. That covers the install shapes a Python
repository normally has:

| shape | `install:` |
|---|---|
| editable package with a dev extra | `-e .[dev]` |
| requirements files | `-r requirements.txt -r requirements-dev.txt` |
| explicit pins, no packaging | `pytest==8.3.4 pyyaml==6.0.2` |
| nothing but the test tooling | *(empty; set `pytest_version`)* |

The value is word-split under `set -f`, so an extras spec survives intact. Without that,
`-e .[dev]` expands against the working directory and becomes `-e .d .e .v` wherever files
with those names exist — a silently wrong install, not an error.

### The reusable will not install test tooling behind your back

`pytest_version` and `pytest_cov_version` default to empty, meaning "the caller's `install`
provides it". If it does not, the job fails with an error naming the input to set. A
repository whose requirements carry pytest but not pytest-cov gets a loud failure rather
than a hidden install of a version it never pinned.

### Coverage posture

`coverage` takes `none`, `report`, or `floor:N`:

- **`none`** — do not measure. For suites that are per-directory unit tests over separate
  components, where a whole-repository percentage means nothing.
- **`report`** — measure and print, never fail on the number. Coverage counts lines a test
  caused to execute and cannot see whether anything was asserted about them; a suite whose
  every assertion is replaced with `pass` can still clear a high gate. What it does answer,
  and nothing else does, is which modules are untested *at all* — a new file with no tests
  appears as a 0% row instead of being silently absent.
- **`floor:N`** — measure and fail below N. Set *below* the measured rate it is a
  regression floor, not a target: it never blocks ordinary work, and it fails when a change
  deletes or bypasses tested code wholesale.

Both positions are reasoned, which is why the standard expresses either. An unrecognised
value **fails the job** rather than degrading to report-only — silently downgrading a
misspelled `floor:80` would remove a gate while the log still reported success.

---

## Read this before wiring the first caller

**Adopting the reusable renames the status check.** It becomes `<caller-job-id> / pytest` —
a job written as `test:` produces `test / pytest`.

Where the previous name is a **required** status check, the required check then never
reports, and every pull request in that repository becomes unmergeable until the branch
protection rule or ruleset entry is updated. **Rename the required check in the same change
as the wiring, not after it.**

This makes the adoption order almost automatic:

1. **Repositories with no test workflow at all** are the cheapest and highest-value first
   callers — there is no check to rename, and they are by definition the ones where a suite
   exists but nothing runs it.
2. **Repositories with a non-required test check** are next: the rename is visible but
   harmless.
3. **Repositories where the test check is required** last, each paired with its branch
   protection change.

## Multi-target repositories

A repository whose suites live in several directories with *different* dependency sets —
one per component — calls the reusable once per component, not once with a matrix. Each
call keeps its own status check and its own `install`. A matrix would collapse the check
names and force a single dependency set on all of them.

A component whose tests build throwaway git repositories, or read the repository's own
refs, needs `fetch_depth: 0`. The default shallow clone is still a real work tree, which is
all a suite shelling out to `git ls-files` requires.

## Self-hosted runners

`runner_labels` takes a JSON array string, e.g. `'["self-hosted","<your-label>"]'`.

The reusable always builds a venv and always sets `PYTHONNOUSERSITE=1`, and neither is an
input. On a shared self-hosted runner a bare `pip install` both reads and mutates the host
user's `~/.local`; user-site lives in `$HOME`, not the work directory, so an ephemeral
runner does not fix it. The venv is created under `RUNNER_TEMP` rather than in the
workspace, because a self-hosted runner reuses its work directory between runs and a venv
inside the checkout is a tree `--cov` could end up measuring. See
[`self-hosted-runner/runbook.md`](../self-hosted-runner/runbook.md) gotchas #8 and #9.

---

## Two follow-ons any adopting organisation will hit

**1. A drift auditor that reads workflow text will report every adopter as drift.** The
usual way to check "does this repository run its tests" is to look for a pytest invocation
in the named workflow file. A caller that delegates contains a `uses:` line, no literal
pytest invocation, and no `--cov` flag — so a text-matching auditor reports *"does not
invoke pytest"*, which is exactly backwards. Teach it to recognise delegation **before**
wiring callers, or the change trades a real gap for a false alarm, and a false alarm on a
drift board is how the board stops being read.

The `coverage` input is a plain string with a small fixed grammar (`none` / `report` /
`floor:N`) partly for this reason: an auditor can read the posture straight out of the
caller's `with:` block.

**2. Register the reusable wherever pinned-SHA governance lives**, so a caller drifting off
the pinned SHA is surfaced the same way every other shared reusable's callers are.

---

## Changing the reusable

`.github/workflows/python-tests-self-test.yml` runs on any change to the reusable, the
fixture, or `scripts/test_python_tests_reusable.py`. Three layers:

| layer | what it catches |
|---|---|
| `unit` | every rejection the reusable is supposed to make — 32 negative controls |
| `e2e-*` | the plumbing: venv, install, pytest actually executing, coverage actually measuring |
| `negative-controls` | that the reusable can go **red**: unsatisfiable floor, zero tests collected, coverage that measured nothing |

The third layer is the one that matters. A gate that cannot fail is indistinguishable from
one that passes, and a change that makes those controls *pass* has not fixed anything.

The two Python programs inside the reusable are tested by lifting them **out of the shipped
YAML**, not by re-implementing them, so a test cannot stay green while the workflow rots. If
you add a third heredoc, give it its own `PYEOF_*` sentinel — the extractor asserts each
sentinel appears exactly once, and fails loudly rather than extracting the wrong block.

The fixture at `tests/fixtures/sample-project/` is deliberately **partly covered**, so that
one self-test job can prove a satisfiable floor passes and another can prove an
unsatisfiable one fails. Its `empty/` directory is the `--cov` target for the census
control: a path that exists, satisfying the pre-flight check, that still contributes no
measured file. Change the fixture and re-check both floors.
