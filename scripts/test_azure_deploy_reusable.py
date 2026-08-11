#!/usr/bin/env python3
"""Contract test: no `run:` block in a `*-reusable.yml` here may approach the
Actions expression cap.

Ported from rolliq-com/solution-recruitment-reference-check's
tests/unit/test_workflow_expression_limits.py (#865/#866, threshold lowered by
#971), adapted to run stdlib-only against this repo's reusables instead of
against pytest + a shared `workflow_yaml` helper — matching the other test
scripts in this directory (test_reusable_caller_docs.py,
test_auto_assign_reusable.py), none of which import a third-party package.

GitHub compiles a `run:` block that contains any interpolation marker (dollar
followed by two braces) into a SINGLE expression, capped at **21,000
characters**. Everything in the block counts, including shell comments. Cross
the line and the workflow does not fail the way an error fails:

    - the run is recorded with ZERO jobs and NO logs,
    - `gh run view --log-failed` answers "log not found",
    - the only trace is a one-line compile error visible when dispatching,
    - and nothing at all is annotated on the commit that broke it.

That is exactly what happened to the source file this reusable was extracted
from: its Key Vault bootstrap step crossed the cap and every deploy was
impossible for ~20 hours, across all clients including the live customer,
while consecutive push-triggered run records piled up looking like noise. It
was found by trying to deploy, not by CI — which is the whole reason this
guard exists rather than a promise to "re-measure after extraction."

The threshold here is deliberately BELOW the real cap, matching the source
test's current value (lowered once already from 18,000 after the guarded step
ate most of the original headroom): a test that fires at 20,999 would be true
and useless, because the PR that trips it is already the one that cannot
deploy. Failing with a few thousand characters to spare leaves room to land a
config knob and refactor afterwards rather than in a panic.

If this fails: lift the longest COMMENT blocks out of the `run:` scalar into
YAML comments immediately above the step, and move any long literal list out
via `env:`. Both cost nothing at YAML level and change no executable behavior.

── the second hazard: a huge block that is safe only by category ───────────

A `run:` block with NO interpolation marker at all is not compiled as an
expression and is not capped, however large — `azure-deploy-reusable.yml`'s
matrix-building step is ~17,000 characters and fine today for exactly that
reason. But that safety is one character deep: the single most natural edit
in the world — splicing an `inputs.*` value into the script instead of routing
it through `env:` — takes it from working to not compiling, with no gradient
in between and nothing about reading the step to warn you.

So a large expression-free block must SAY it is expression-free, in its own
first lines, where the person about to edit it will see it — the "EXPRESSION-
FREE BY DESIGN" marker checked below. The marker text must describe the
interpolation marker in WORDS, never spell it out literally: doing so would
make the note itself the very thing it warns against (this bit the source
test's own author on its first draft).

Run: python3 scripts/test_azure_deploy_reusable.py
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# The real Actions limit. Not the assertion threshold — see the module docstring.
_HARD_CAP = 21_000

# Fail here instead, matching the source test's current (post-#971) value.
_THRESHOLD = 16_000

# Built from two half-strings so this file itself never contains the literal
# sequence it is checking for — see the module docstring's own warning about that.
_EXPRESSION = re.compile(re.escape("$" + "{{"))

_EXPRESSION_FREE_MARKER = "EXPRESSION-FREE BY DESIGN"

_RUN_KEY_RE = re.compile(r"^(?P<indent>[ \t]*)run:[ \t]*(?P<rest>.*?)\s*$")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?$")
_STEP_NAME_RE = re.compile(r"^[ \t]*(?:-\s*)?name:\s*(?P<name>.+?)\s*$")


def _run_blocks(path: pathlib.Path) -> list[tuple[str, str]]:
    """Every (step name, run script) in a workflow file.

    Regex-based rather than a YAML parse, matching this repo's other test
    scripts (stdlib-only, no PyYAML dependency). A `run:` step is recognised
    by its own `run:` line, either a block scalar (`|`, `|-`, `|+`) or a
    single-line command on the same line; the step name is whatever the most
    recent `name:` line at or before that step's `- ` boundary said. Good
    enough for this file's shape (flat `steps:` lists, no anchors/aliases
    inside a step) without needing a real parser — verified against a real
    `yaml.safe_load` for every step in this file's three jobs while writing
    this (34 steps, exact content match) before trusting it for anything
    smaller than that.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[str, str]] = []
    current_name = "<unnamed step>"
    i = 0
    while i < len(lines):
        line = lines[i]
        name_match = _STEP_NAME_RE.match(line)
        if name_match and (line.lstrip().startswith("- name:") or line.lstrip().startswith("name:")):
            current_name = name_match.group("name")
        run_match = _RUN_KEY_RE.match(line)
        if run_match and _BLOCK_SCALAR_RE.match(run_match.group("rest")):
            indent = len(run_match.group("indent"))
            i += 1
            body_lines = []
            while i < len(lines):
                body_line = lines[i]
                if body_line.strip() == "":
                    body_lines.append(body_line)
                    i += 1
                    continue
                body_indent = len(body_line) - len(body_line.lstrip(" \t"))
                if body_indent <= indent:
                    break
                body_lines.append(body_line)
                i += 1
            # YAML's literal block scalar (`|`) dedents by the FIRST non-blank
            # body line's own indentation, then keeps every line's content
            # relative to that — it does not keep each line's raw leading
            # whitespace verbatim. Reproduce that here: without it, every
            # captured block carries ~10 extra characters of indentation per
            # line that GitHub's parser never sees, which measured ~3,000
            # characters too long on this file's largest block alone. Caught by
            # comparing this function's output against a real `yaml.safe_load`
            # for every step in this file before trusting it (see the
            # docstring above) — worth re-checking the same way against
            # whatever `*-reusable.yml` trips this function up next.
            block_indent = next(
                (len(bl) - len(bl.lstrip(" ")) for bl in body_lines if bl.strip() != ""),
                indent + 2,
            )
            dedented = "\n".join(
                bl[block_indent:] if len(bl) >= block_indent else bl.lstrip(" ")
                for bl in body_lines
            )
            blocks.append((current_name, dedented))
            continue
        if run_match and run_match.group("rest"):
            # `run: <command>` on one line — no block scalar at all. Short by
            # construction (a step author writing 16,000 characters on one
            # YAML line is its own problem), so these never trip the cap
            # checks below, but they still count as run: steps and belong in
            # this function's output for anything that cares about coverage.
            blocks.append((current_name, run_match.group("rest")))
            i += 1
            continue
        i += 1
    return blocks


def _reusables() -> list[pathlib.Path]:
    return sorted(WORKFLOW_DIR.glob("*-reusable.yml"))


def check_no_run_block_approaches_the_cap(path: pathlib.Path) -> list[str]:
    problems = []
    for name, script in _run_blocks(path):
        if _EXPRESSION.search(script) and len(script) > _THRESHOLD:
            problems.append(
                f"{path.name}: step {name!r} is {len(script)} characters and contains an "
                f"interpolation — within {_HARD_CAP - len(script)} characters of GitHub's "
                f"{_HARD_CAP}-character expression cap. Past the cap the workflow stops "
                f"compiling and every run is recorded with zero jobs and no logs. Move the "
                f"longest comment blocks into YAML comments above the step, and route long "
                f"literal values through `env:` instead of splicing them into the script."
            )
    return problems


def check_large_expression_free_blocks_declare_themselves(path: pathlib.Path) -> list[str]:
    problems = []
    for name, script in _run_blocks(path):
        if (
            not _EXPRESSION.search(script)
            and len(script) > _THRESHOLD
            and _EXPRESSION_FREE_MARKER not in script
        ):
            problems.append(
                f"{path.name}: step {name!r} is {len(script)} characters, under the "
                f"{_HARD_CAP}-character cap ONLY because it contains no interpolation. That "
                f"safety is one character deep and invisible to whoever edits the block next. "
                f"Either shrink it below {_THRESHOLD} characters, or open it with a comment "
                f"containing {_EXPRESSION_FREE_MARKER!r} explaining that a single interpolation "
                f"would stop the whole workflow compiling — describe the marker in WORDS, not "
                f"literally."
            )
    return problems


def check_expression_free_markers_are_honest(path: pathlib.Path) -> list[str]:
    problems = []
    for name, script in _run_blocks(path):
        if _EXPRESSION_FREE_MARKER in script and _EXPRESSION.search(script):
            problems.append(
                f"{path.name}: step {name!r} carries {_EXPRESSION_FREE_MARKER!r} while also "
                f"containing an interpolation. Either the block gained one since the marker was "
                f"written (remove the marker, or drop the interpolation and accept the cap), or "
                f"the marker's own explanatory text spells the interpolation sequence out "
                f"literally, which creates the exact hazard it warns about — describe it in "
                f"words instead."
            )
    return problems


def check_the_guard_can_still_see_its_subjects() -> list[str]:
    """A guard that silently stops finding its subject is worse than no guard.

    Pins two live subjects in azure-deploy-reusable.yml so a rename or a
    refactor that breaks `_run_blocks` fails loudly here, rather than every
    check above passing vacuously because there was nothing left to check.
    """
    problems = []
    target = WORKFLOW_DIR / "azure-deploy-reusable.yml"
    if not target.exists():
        return [f"azure-deploy-reusable.yml not found at {target} — re-point this test if it moved."]

    blocks = dict(_run_blocks(target))
    if not blocks:
        return ["parsed azure-deploy-reusable.yml but found no run: blocks at all — the regex-based extractor may have broken."]

    marker_subject = "Build deployment matrix"
    if marker_subject not in blocks:
        problems.append(
            f"the step {marker_subject!r} no longer exists. It is the block over {_THRESHOLD} "
            f"characters with no interpolation that motivated the marker check; if it was "
            f"renamed or split, re-point this test at whatever now holds that property."
        )
    else:
        script = blocks[marker_subject]
        if len(script) <= _THRESHOLD:
            problems.append(
                f"{marker_subject!r} is now {len(script)} characters, under the {_THRESHOLD} "
                f"threshold, so it is no longer the landmine this pinned check exists for. "
                f"Retire this assertion rather than lowering the threshold to keep it alive."
            )
        if _EXPRESSION.search(script):
            problems.append(
                f"{marker_subject!r} now contains an interpolation, so at {len(script)} "
                f"characters it is past the {_HARD_CAP} cap and the workflow will not compile."
            )
        if _EXPRESSION_FREE_MARKER not in script:
            problems.append(
                f"{marker_subject!r} lost its {_EXPRESSION_FREE_MARKER!r} note — without it the "
                f"block can silently re-enter the capped category and nothing says so until a "
                f"deploy is dispatched and returns zero jobs."
            )

    live_subject = "Resolve deploy image"
    if live_subject not in blocks:
        problems.append(
            f"the step {live_subject!r} no longer exists. Since this file has no other block "
            f"anywhere near the threshold, it is what keeps "
            f"check_no_run_block_approaches_the_cap from having an empty subject set for a "
            f"reason nobody checked. Re-point this test at whatever now holds that property."
        )
    elif not _EXPRESSION.search(blocks[live_subject]):
        problems.append(
            f"{live_subject!r} no longer contains any interpolation. If every block in this "
            f"file is now expression-free, the cap-approach check can no longer fail for any "
            f"input and passes identically whether or not `_run_blocks` still works — verify "
            f"the extractor against a workflow that DOES interpolate before deleting this "
            f"assertion."
        )
    return problems


def main() -> int:
    reusables = _reusables()
    if not reusables:
        print(
            f"FAIL: found no *-reusable.yml workflows to check. This check reads "
            f"{WORKFLOW_DIR}; if the layout changed, update or delete it deliberately.",
            file=sys.stderr,
        )
        return 1

    problems: list[str] = []
    for path in reusables:
        problems.extend(check_no_run_block_approaches_the_cap(path))
        problems.extend(check_large_expression_free_blocks_declare_themselves(path))
        problems.extend(check_expression_free_markers_are_honest(path))
    problems.extend(check_the_guard_can_still_see_its_subjects())

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s) found.", file=sys.stderr)
        return 1

    print(f"Checked {len(reusables)} reusable(s) against the {_HARD_CAP}-character expression cap. OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
