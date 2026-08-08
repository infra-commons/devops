#!/usr/bin/env python3
"""Every reusable's DOCUMENTED caller pattern must actually work.

A called workflow may never hold more permission than its caller grants, so a
caller copying a snippet that omits the `permissions:` block gets a
**startup_failure** — and a startup_failure produces *no check-run at all*, not
a red one. The adopter sees no check, reads that as "not wired up yet" rather
than "broken", and the PR silently reaches nobody's queue: exactly the condition
the reusable existed to prevent.

This has now happened twice from the same blind spot
(infra-commons/devops#18, infra-commons/marketing-engine#17). Both reusables
shipped genuine self-tests, and both self-tests call the workflow **from inside
this repository**, where the permission ceiling is whatever the self-test grants
rather than whatever an external caller's snippet says. A self-test that
exercises a component from inside its own repo has not tested the external
contract. So the executed form and the documented form drifted apart, and only
the documented one is what an adopter reads.

The rule enforced here:

    if a reusable's jobs request permissions, its documented caller snippet
    must grant them ON THE CALLING JOB — that is, in a `permissions:` block
    appearing AFTER the snippet's `jobs:` line.

The position matters and is the whole defect in #18. `auto-assign-reusable.yml`
documented `permissions: {}` at *workflow* level, above `jobs:`. That is not a
grant to the calling job; it is a ceiling of nothing, which the reusable's
`pull-requests: write` immediately exceeds.

Stdlib-only, matching the other scripts here: this runs in a job that installs
nothing.

Run: python3 scripts/test_reusable_caller_docs.py
"""

import pathlib
import re
import sys
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

_JOB_KEY_RE = re.compile(r"^  (?P<name>[A-Za-z0-9_-]+):\s*$")
_JOB_PERMS_RE = re.compile(r"^    permissions:\s*$")
_SCOPE_RE = re.compile(r"^      (?P<scope>[a-z-]+):\s*(?P<level>[a-z-]+)\s*$")


def required_permissions(path: pathlib.Path) -> dict:
    """Union of the job-level permissions the reusable's own jobs request.

    Workflow-level `permissions:` is deliberately ignored: it is the reusable's
    own default, not something the caller must grant. Only what a job actually
    asks for can exceed the caller's ceiling.
    """
    required: dict = {}
    in_jobs = False
    collecting = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            continue
        if raw.rstrip() == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if _JOB_KEY_RE.match(raw):
            collecting = False
            continue
        if _JOB_PERMS_RE.match(raw):
            collecting = True
            continue
        if collecting:
            match = _SCOPE_RE.match(raw)
            if match:
                scope, level = match.group("scope"), match.group("level")
                # `write` outranks `read` when two jobs disagree.
                if required.get(scope) != "write":
                    required[scope] = level
            elif raw.strip():
                collecting = False
    return required


def documented_snippet(path: pathlib.Path) -> str:
    """The header comment block, dedented back into the YAML it depicts."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.startswith("#"):
            # The header ends at the first non-comment line; `name:` is line 1,
            # so skip it and keep going until real YAML starts.
            if lines:
                break
            continue
        lines.append(raw[1:])
    return textwrap.dedent("\n".join(lines))


def check(path: pathlib.Path) -> list:
    required = required_permissions(path)
    if not required:
        return []

    snippet = documented_snippet(path)
    if f"workflows/{path.name}" not in snippet:
        return [
            f"{path.name}: no documented caller pattern found in its header. An "
            f"adopter has nothing correct to copy."
        ]

    problems = []
    if "jobs:" not in snippet:
        return [f"{path.name}: documented caller pattern has no `jobs:` block"]

    before_jobs, after_jobs = snippet.split("jobs:", 1)

    if "permissions:" not in after_jobs:
        # Two different failures, and they deserve different words. Claiming the
        # milder one is certain would be its own kind of wrong.
        if re.search(r"^\s*permissions:", before_jobs, re.MULTILINE):
            problems.append(
                f"{path.name}: its jobs request {required}, and the documented caller "
                f"pattern sets `permissions:` at WORKFLOW level, above `jobs:`. That is "
                f"a ceiling, not a grant to the calling job, so a caller copying it "
                f"ALWAYS fails at startup — and a startup_failure produces no check-run "
                f"at all, so it reads as 'not wired up yet' rather than as broken."
            )
        else:
            problems.append(
                f"{path.name}: its jobs request {required} but the documented caller "
                f"pattern grants nothing, so the caller falls back to whatever its "
                f"org/repo default GITHUB_TOKEN permissions happen to be. That works "
                f"under a permissive default and fails at startup under a restrictive "
                f"one — the snippet's correctness depends on a setting it never "
                f"mentions. Grant it explicitly."
            )
        return problems

    for scope, level in sorted(required.items()):
        # A trailing `# comment` on the line is fine and common in these snippets;
        # requiring end-of-line here would reject correct documentation.
        if not re.search(rf"^\s*{re.escape(scope)}:\s*{re.escape(level)}\s*(?:#.*)?$",
                         after_jobs, re.MULTILINE):
            problems.append(
                f"{path.name}: its jobs request `{scope}: {level}` but the documented "
                f"caller pattern does not grant it on the calling job."
            )
    return problems


def main() -> int:
    reusables = sorted(WORKFLOW_DIR.glob("*-reusable.yml"))
    if not reusables:
        # This check's entire input. Finding none means the glob or the naming
        # convention changed, not that every caller pattern is correct.
        print(
            "FAIL: found no *-reusable.yml workflows to check. This check reads "
            f"{WORKFLOW_DIR}; if the layout changed, update or delete it deliberately.",
            file=sys.stderr,
        )
        return 1

    problems = []
    checked = 0
    for path in reusables:
        if not required_permissions(path):
            print(f"  {path.name}: requests no permissions, nothing to document")
            continue
        checked += 1
        problems.extend(check(path))

    if not checked:
        print(
            "FAIL: no reusable in this repo requests any permissions. That is "
            "possible but unlikely, and it would make this check vacuous.",
            file=sys.stderr,
        )
        return 1

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} documented caller pattern(s) would fail at startup.",
              file=sys.stderr)
        return 1

    print(f"All {checked} reusable(s) that need permissions document them. OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
