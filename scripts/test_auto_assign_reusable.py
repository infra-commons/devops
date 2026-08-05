#!/usr/bin/env python3
"""Tests for .github/workflows/auto-assign-reusable.yml.

The workflow decides who a pull request lands on. Every way it can go wrong is quiet:
assigning nobody, assigning the wrong person, or skipping a PR that needed assigning all
produce a green check and an empty queue. So these are mostly negative controls.

The decision program is executed **out of the workflow file itself**, not out of a copy —
a test that reasons about a re-implementation stays green while the shipped workflow rots.

Run: python3 scripts/test_auto_assign_reusable.py
"""

import io
import os
import pathlib
import shutil
import sys
import tempfile
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-assign-reusable.yml"

SENTINEL = "PYEOF_DECIDE"

ENV_KEYS = (
    "INPUT_ASSIGNEE",
    "VAR_ASSIGNEE",
    "PR_AUTHOR",
    "SKIP_ACTORS",
    "OVERWRITE",
    "CURRENT_ASSIGNEES",
    "DECISION_FILE",
)


def program_source():
    text = WORKFLOW.read_text(encoding="utf-8")
    opener = f"python3 - <<'{SENTINEL}'"
    if text.count(opener) != 1:
        raise AssertionError(
            f"expected exactly one {opener} block in {WORKFLOW.name}; the extraction is no "
            "longer unambiguous, so these tests would assert against the wrong program"
        )
    lines = text.split(opener, 1)[1].splitlines()
    for index, line in enumerate(lines):
        if line.strip() == SENTINEL:
            break
    else:
        raise AssertionError(f"no {SENTINEL} terminator found in {WORKFLOW.name}")
    body = textwrap.dedent("\n".join(lines[:index]))
    if "PR_ASSIGNEE" not in body:
        raise AssertionError("the extracted block is not the decision program")
    return compile(body, f"{WORKFLOW}:{SENTINEL}", "exec")


DECIDE = program_source()

PASSED, REJECTED, CRASHED = "passed", "rejected", "crashed"


def decide(author="alice", explicit="", variable="", skip="dependabot[bot]",
           overwrite="false", current=""):
    """Run the decision program. Returns (verdict, output, target-or-None).

    `target` is None when the program decided to do nothing, which is a DIFFERENT outcome
    from failing — conflating them would let a workflow that silently skips every PR pass
    as one that correctly skipped a few.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="auto-assign-"))
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    captured = io.StringIO()
    real_stdout = sys.stdout
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "PR_AUTHOR": author,
            "INPUT_ASSIGNEE": explicit,
            "VAR_ASSIGNEE": variable,
            "SKIP_ACTORS": skip,
            "OVERWRITE": overwrite,
            "CURRENT_ASSIGNEES": current,
            "DECISION_FILE": str(tmp / "decision"),
        })
        sys.stdout = captured
        try:
            exec(DECIDE, {"__name__": "__main__"})  # noqa: S102 - the artefact under test
            verdict = PASSED
        except SystemExit as exc:
            verdict = PASSED if exc.code in (0, None) else REJECTED
        except Exception as exc:  # noqa: BLE001
            return CRASHED, captured.getvalue() + f"\n<{type(exc).__name__}: {exc}>", None
        decision = tmp / "decision"
        target = decision.read_text(encoding="utf-8").strip() if decision.exists() else None
        return verdict, captured.getvalue(), (target or None)
    finally:
        sys.stdout = real_stdout
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


TESTS = []


def test(name):
    def register(fn):
        TESTS.append((name, fn))
        return fn

    return register


# ── Precedence ──────────────────────────────────────────────────────────────────


@test("the assignee input wins over everything")
def _():
    verdict, out, target = decide(author="alice", explicit="carol", variable="bob")
    assert verdict == PASSED, out
    assert target == "carol", f"expected carol, got {target}\n{out}"


@test("the PR_ASSIGNEE variable wins over the author")
def _():
    verdict, out, target = decide(author="alice", variable="bob")
    assert verdict == PASSED, out
    assert target == "bob", f"expected bob, got {target}\n{out}"


@test("with neither set, the author is assigned")
def _():
    verdict, out, target = decide(author="alice")
    assert verdict == PASSED, out
    assert target == "alice", f"expected alice, got {target}\n{out}"
    assert "PR_ASSIGNEE is unset" in out, out


@test("the log names which source the login came from")
def _():
    # Without this the run log cannot distinguish "the variable is set correctly" from
    # "the variable is empty and we happened to fall back to the same person".
    _, out, _ = decide(author="alice", variable="bob")
    assert "PR_ASSIGNEE variable" in out, out


@test("whitespace around a configured login is ignored")
def _():
    verdict, out, target = decide(author="alice", variable="  bob  ")
    assert verdict == PASSED, out
    assert target == "bob", f"expected bob, got {target!r}\n{out}"


# ── Skipping, and not over-skipping ─────────────────────────────────────────────


@test("a skip_actors author is skipped")
def _():
    verdict, out, target = decide(author="dependabot[bot]")
    assert verdict == PASSED, out
    assert target is None, f"should have skipped, chose {target}\n{out}"


@test("NEGATIVE CONTROL: skip matching is exact, not substring")
def _():
    # `"dependabot[bot]" in "not-dependabot[bot]"` is True. A substring match would
    # silently drop a real contributor's PR, and nobody reports a PR that was quietly
    # not assigned.
    verdict, out, target = decide(author="not-dependabot[bot]")
    assert verdict == PASSED, out
    assert target == "not-dependabot[bot]", (
        f"a substring skip match dropped a real author's PR (target={target})\n{out}"
    )


@test("multiple skip actors are all honoured")
def _():
    _, _, a = decide(author="renovate[bot]", skip="dependabot[bot] renovate[bot]")
    _, _, b = decide(author="dependabot[bot]", skip="dependabot[bot] renovate[bot]")
    assert a is None and b is None, f"renovate={a} dependabot={b}"


@test("an empty skip list skips nobody")
def _():
    _, out, target = decide(author="dependabot[bot]", skip="")
    assert target == "dependabot[bot]", f"{target}\n{out}"


@test("an already-assigned PR is left alone")
def _():
    verdict, out, target = decide(author="alice", variable="bob", current="carol")
    assert verdict == PASSED, out
    assert target is None, f"clobbered a deliberate assignment with {target}\n{out}"


@test("overwrite reassigns an already-assigned PR")
def _():
    verdict, out, target = decide(
        author="alice", variable="bob", current="carol", overwrite="true"
    )
    assert verdict == PASSED, out
    assert target == "bob", f"{target}\n{out}"


@test("a PR already assigned to the target is not reassigned")
def _():
    # Re-firing on ready_for_review must not churn the assignee list.
    _, out, target = decide(author="alice", variable="bob", current="bob", overwrite="true")
    assert target is None, f"{target}\n{out}"


# ── The event payload must actually be a pull request ───────────────────────────


@test("NEGATIVE CONTROL: no author in the payload fails loudly")
def _():
    # A caller that wires the wrong trigger gets an empty author. Skipping silently would
    # be indistinguishable from a correctly-configured repo with nothing to assign.
    verdict, out, target = decide(author="")
    assert verdict != PASSED, f"accepted an empty payload, chose {target}\n{out}"
    assert verdict != CRASHED, f"crashed instead of reporting a config error\n{out}"
    assert "::error::" in out, out
    assert "pull_request" in out, out


@test("NEGATIVE CONTROL: a whitespace-only author fails loudly")
def _():
    verdict, out, _ = decide(author="   ")
    assert verdict == REJECTED, out
    assert "::error::" in out, out


# ── The workflow's own contract ─────────────────────────────────────────────────


@test("the workflow sets every variable the decision program reads")
def _():
    text = WORKFLOW.read_text(encoding="utf-8")
    for name in ENV_KEYS:
        assert f"{name}:" in text, f"{name} is read by the program but never set"


@test("the assignment is read back and asserted")
def _():
    # THE guard. `gh pr edit --add-assignee` exits 0 while assigning nobody when the login
    # is valid but not a collaborator, so the verify step is the only evidence.
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--json assignees" in text, (
        "the workflow never reads the assignees back — a silently-dropped assignment "
        "would report success"
    )
    assert "assignment did not take" in text, "no failure path for an assignment that did not take"


@test("the job requests pull-requests: write and nothing more")
def _():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull-requests: write" in text, text
    assert "contents: write" not in text, "this workflow has no business writing contents"


@test("no personal login is baked into this public repository")
def _():
    # The login lives in the PR_ASSIGNEE variable precisely so it is not committed here.
    text = WORKFLOW.read_text(encoding="utf-8")
    for token in ("kev", "rolliq", "cashbucket", "klsjapan"):
        assert token not in text.lower(), (
            f"{token!r} appears in a public workflow — CONTRIBUTING.md's golden rule. "
            "Configure the login through the PR_ASSIGNEE variable instead."
        )


def main():
    failures = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            label = "FAIL" if isinstance(exc, AssertionError) else "ERROR"
            print(f"  {label}  {name}\n        {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
