#!/usr/bin/env python3
"""Tests for .github/workflows/python-tests-reusable.yml.

The reusable decides two things a green check mark cannot show you: whether a coverage
floor was actually applied, and whether coverage measured anything at all. Both fail
silently by construction if they fail at all —

  * an unrecognised `coverage` value that fell through to report-only would delete a gate
    a caller believed was enforced, and the job would still pass;
  * `--cov=<path-that-does-not-exist>` makes pytest-cov emit a warning, write no report,
    and exit ZERO. Under `floor:N` the floor catches it (0% < N); under `report` nothing
    does, so the hole opens only on the repos that deliberately chose not to gate.

So most of what follows is negative controls: an input that MUST be rejected, asserted to
be rejected with an actionable ::error:: rather than a traceback.

These tests execute the two programs **out of the workflow file itself**, not out of a
copy. A test that reasons about a re-implementation is a test about the re-implementation:
it stays green while the shipped workflow rots. Extracting the heredocs also keeps the
reusable self-contained, which matters because a reusable workflow runs in the caller's
checkout and cannot rely on this repository's files being present.

Run: python3 scripts/test_python_tests_reusable.py
"""

import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "python-tests-reusable.yml"

RESOLVE, CENSUS = "PYEOF_RESOLVE", "PYEOF_CENSUS"


def program_text(sentinel):
    """The raw source of one heredoc'd program, lifted from the shipped workflow.

    The terminator is indented in the file and flush-left by the time bash sees it: YAML
    strips a block scalar's common indentation before handing it to the shell. So the
    terminator is matched on its stripped content, and the body is dedented afterwards —
    the same two transformations, in the same order, that the runner applies.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    opener = f"python3 - <<'{sentinel}'"
    if text.count(opener) != 1:
        raise AssertionError(
            f"expected exactly one {opener} block in {WORKFLOW.name}; the extraction below "
            "is no longer unambiguous, so these tests would be asserting against the wrong "
            "program"
        )
    lines = text.split(opener, 1)[1].splitlines()
    for index, line in enumerate(lines):
        if line.strip() == sentinel:
            break
    else:
        raise AssertionError(f"no {sentinel} terminator found in {WORKFLOW.name}")
    return textwrap.dedent("\n".join(lines[:index]))


def program_source(sentinel, must_contain):
    """`program_text`, checked to be the program the caller thinks it is, and compiled."""
    body = program_text(sentinel)
    if must_contain not in body:
        raise AssertionError(
            f"the block extracted for {sentinel} does not contain {must_contain!r} — it is "
            "not the program these tests think it is"
        )
    return compile(body, f"{WORKFLOW}:{sentinel}", "exec")


RESOLVER = program_source(RESOLVE, "--cov-fail-under")
CENSUSER = program_source(CENSUS, "measured 0 files")


PASSED, REJECTED, CRASHED = "passed", "rejected", "crashed"

ENV_KEYS = ("COVERAGE", "SOURCE_DIR", "TEST_PATH", "PYTEST_EXTRA_ARGS", "ARGV_FILE", "COV_JSON")


def _run(program, env):
    """Execute one lifted program under `env`, returning (verdict, output).

    CRASHED is kept distinct from REJECTED deliberately. In the workflow both fail the
    step, so both stop the job and it is tempting to treat them as one — but a traceback
    gives the operator no ::error:: annotation and no idea which input to change, and a
    test that accepted "it blew up" as a pass would be satisfied by a step that is simply
    broken.
    """
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    captured = io.StringIO()
    real_stdout = sys.stdout
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(env)
        sys.stdout = captured
        exec(program, {"__name__": "__main__"})  # noqa: S102 - running the artefact under test is the point
        return PASSED, captured.getvalue()
    except SystemExit as exc:
        return (PASSED if exc.code in (0, None) else REJECTED), captured.getvalue()
    except Exception as exc:  # noqa: BLE001 - any failure mode is a finding here
        return CRASHED, captured.getvalue() + f"\n<uncaught {type(exc).__name__}: {exc}>"
    finally:
        sys.stdout = real_stdout
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Workspace:
    """A throwaway checkout the resolver's path assertions can be run against.

    `src` and `pipeline` exist; `nope` deliberately does not. The resolver resolves
    source paths relative to the process working directory, exactly as it does in the
    runner's workspace, so the tests chdir rather than passing absolute paths — a test
    that only ever passed absolute paths would not notice the relative-path contract
    breaking.
    """

    def __enter__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="python-tests-reusable-"))
        self.cwd = os.getcwd()
        (self.dir / "src").mkdir()
        (self.dir / "pipeline").mkdir()
        (self.dir / "tests").mkdir()
        os.chdir(self.dir)
        return self

    def __exit__(self, *_):
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)


def resolve(coverage="report", source_dir="src", test_path="tests", extra_args="-q"):
    """Run the resolver in a throwaway workspace. Returns (verdict, output, argv)."""
    with Workspace() as workspace:
        argv_file = workspace.dir / "argv"
        verdict, output = _run(
            RESOLVER,
            {
                "COVERAGE": coverage,
                "SOURCE_DIR": source_dir,
                "TEST_PATH": test_path,
                "PYTEST_EXTRA_ARGS": extra_args,
                "ARGV_FILE": str(argv_file),
                "COV_JSON": str(workspace.dir / "coverage.json"),
            },
        )
        argv = argv_file.read_text(encoding="utf-8").splitlines() if argv_file.exists() else None
    return verdict, output, argv


def census(report):
    """Run the census against `report` (a dict, or None to write no file at all)."""
    with Workspace() as workspace:
        cov_json = workspace.dir / "coverage.json"
        if report is not None:
            cov_json.write_text(json.dumps(report), encoding="utf-8")
        return _run(CENSUSER, {"COV_JSON": str(cov_json)})


TESTS = []


def test(name):
    def register(fn):
        TESTS.append((name, fn))
        return fn

    return register


def assert_rejected(verdict, output, because):
    """Assert a CLEAN rejection: non-zero exit carrying an ::error:: line."""
    assert verdict != PASSED, f"ACCEPTED an input it must reject ({because}).\n{output}"
    assert verdict != CRASHED, (
        f"CRASHED instead of rejecting ({because}). It fails the step either way, but a "
        f"traceback tells the operator nothing about which input to change.\n{output}"
    )
    assert "::error::" in output, f"rejection carried no ::error:: annotation.\n{output}"
    return output


def cov_flags(argv):
    return [arg for arg in argv if arg.startswith("--cov")]


# ── Baselines ───────────────────────────────────────────────────────────────────
#
# Every case below is one deliberate mutation away from one of these, so no test can
# pass for an incidental reason.


@test("report mode: --cov is set and no floor is imposed")
def _():
    verdict, output, argv = resolve(coverage="report")
    assert verdict == PASSED, output
    assert "--cov=src" in argv, argv
    assert "--cov-report=term-missing" in argv, argv
    assert not [a for a in argv if a.startswith("--cov-fail-under")], (
        f"report mode must impose no floor — that is the whole distinction from floor:N.\n{argv}"
    )


@test("floor mode: --cov-fail-under carries exactly the requested number")
def _():
    verdict, output, argv = resolve(coverage="floor:80")
    assert verdict == PASSED, output
    floors = [a for a in argv if a.startswith("--cov-fail-under")]
    assert floors == ["--cov-fail-under=80"], f"expected one exact floor, got {floors}\n{argv}"


@test("none mode: NOT ONE --cov flag reaches pytest")
def _():
    verdict, output, argv = resolve(coverage="none", source_dir="")
    assert verdict == PASSED, output
    assert cov_flags(argv) == [], (
        "coverage: none must produce no coverage flags at all. A --cov-fail-under that "
        f"survived here would be a different posture than the caller asked for.\n{argv}"
    )
    assert argv == ["tests", "-q"], argv


@test("test_path and pytest_args are passed through, in that order")
def _():
    verdict, output, argv = resolve(coverage="none", source_dir="", extra_args="-v --tb=short")
    assert verdict == PASSED, output
    assert argv[:3] == ["tests", "-v", "--tb=short"], argv


@test("an empty test_path leaves discovery to pytest's rootdir")
def _():
    verdict, output, argv = resolve(coverage="none", source_dir="", test_path="", extra_args="")
    assert verdict == PASSED, output
    assert argv == [], f"nothing should be passed positionally when test_path is empty\n{argv}"


@test("multiple source dirs each get their own --cov")
def _():
    verdict, output, argv = resolve(coverage="report", source_dir="src pipeline")
    assert verdict == PASSED, output
    assert [a for a in argv if a.startswith("--cov=")] == ["--cov=src", "--cov=pipeline"], argv


@test("multiple test paths are each passed through")
def _():
    verdict, output, argv = resolve(coverage="none", source_dir="", test_path="tests src")
    assert verdict == PASSED, output
    assert argv[:2] == ["tests", "src"], argv


# ── The coverage grammar must FAIL CLOSED ────────────────────────────────────────
#
# THE negative control. A value that fell through to report-only would remove a floor the
# caller believed was enforced, and the job would report success — the failure and the
# success are the same green check.


@test("NEGATIVE CONTROL: a bare number is rejected, not read as a floor")
def _():
    verdict, output, _argv = resolve(coverage="80")
    out = assert_rejected(verdict, output, "'80' is not the declared grammar")
    assert "floor:N" in out, out


@test("NEGATIVE CONTROL: floor with no number is rejected")
def _():
    verdict, output, _ = resolve(coverage="floor:")
    assert_rejected(verdict, output, "an empty floor is not a floor")


@test("NEGATIVE CONTROL: a non-numeric floor is rejected")
def _():
    verdict, output, _ = resolve(coverage="floor:abc")
    assert_rejected(verdict, output, "'abc' is not a percentage")


@test("NEGATIVE CONTROL: a floor above 100 is rejected")
def _():
    verdict, output, _ = resolve(coverage="floor:101")
    assert_rejected(verdict, output, "101% is unreachable, so the gate could never pass")


@test("NEGATIVE CONTROL: a floor with a negative sign is rejected")
def _():
    verdict, output, _ = resolve(coverage="floor:-1")
    assert_rejected(verdict, output, "a negative floor is not a percentage")


@test("NEGATIVE CONTROL: wrong case is rejected rather than guessed at")
def _():
    verdict, output, _ = resolve(coverage="Report")
    assert_rejected(verdict, output, "the grammar is exact; guessing invites silent drift")


@test("NEGATIVE CONTROL: an empty coverage value is rejected")
def _():
    verdict, output, _ = resolve(coverage="")
    assert_rejected(verdict, output, "empty is not a posture")


@test("NEGATIVE CONTROL: a plausible-but-wrong synonym is rejected")
def _():
    verdict, output, _ = resolve(coverage="off")
    assert_rejected(verdict, output, "'off' reads like 'none' and is not it")


@test("floor:0 is accepted — it is a legitimate, if inert, declaration")
def _():
    verdict, output, argv = resolve(coverage="floor:0")
    assert verdict == PASSED, output
    assert "--cov-fail-under=0" in argv, argv


@test("floor:100 is accepted")
def _():
    verdict, output, argv = resolve(coverage="floor:100")
    assert verdict == PASSED, output
    assert "--cov-fail-under=100" in argv, argv


@test("surrounding whitespace on the coverage value does not change the posture")
def _():
    verdict, output, argv = resolve(coverage="  floor:80  ")
    assert verdict == PASSED, output
    assert "--cov-fail-under=80" in argv, argv


# ── Dead coverage: the pre-flight half ───────────────────────────────────────────
#
# Measured, not assumed: `pytest tests --cov=nosuchdir --cov-report=term-missing` exits 0
# and writes no report. Under `report` nothing downstream notices.


@test("NEGATIVE CONTROL: a source_dir that does not exist is rejected")
def _():
    verdict, output, _ = resolve(coverage="report", source_dir="nope")
    out = assert_rejected(
        verdict, output, "pytest-cov treats this as a warning and exits 0 — a green job measuring nothing"
    )
    assert "nope" in out, out


@test("NEGATIVE CONTROL: one bad dir among several good ones is rejected")
def _():
    verdict, output, _ = resolve(coverage="report", source_dir="src nope pipeline")
    assert_rejected(verdict, output, "a partially dead --cov set still reports a healthy-looking total")


@test("NEGATIVE CONTROL: report mode with no source_dir at all is rejected")
def _():
    verdict, output, _ = resolve(coverage="report", source_dir="")
    assert_rejected(verdict, output, "measuring nothing is not report-only, it is not measuring")


@test("NEGATIVE CONTROL: floor mode with no source_dir at all is rejected")
def _():
    verdict, output, _ = resolve(coverage="floor:80", source_dir="")
    assert_rejected(verdict, output, "a floor over nothing would fail every run for the wrong reason")


@test("NEGATIVE CONTROL: coverage:none with a source_dir set is rejected as contradictory")
def _():
    verdict, output, _ = resolve(coverage="none", source_dir="src")
    assert_rejected(
        verdict,
        output,
        "a repo that names a source dir believes it is measured; ignoring it quietly "
        "leaves that belief unchallenged",
    )


@test("NEGATIVE CONTROL: an argument containing a newline is rejected")
def _():
    # The argv file is line-delimited, so this would silently become two arguments.
    verdict, output, _ = resolve(coverage="none", source_dir="", test_path="'tests\nrm -rf /'")
    assert_rejected(verdict, output, "a newline would be read back as an argument boundary")


# ── Dead coverage: the post-run census ───────────────────────────────────────────


@test("census: a report naming at least one file passes")
def _():
    verdict, output = census(
        {"files": {"src/m.py": {}}, "totals": {"percent_covered": 75.0}}
    )
    assert verdict == PASSED, output
    assert "1 file" in output, output


@test("NEGATIVE CONTROL: census rejects a report that measured zero files")
def _():
    verdict, output = census({"files": {}, "totals": {"percent_covered": 0.0}})
    assert_rejected(
        verdict, output, "0 measured files is the exact shape of a --cov pointed at the wrong tree"
    )


@test("NEGATIVE CONTROL: census rejects a MISSING report")
def _():
    # pytest-cov writes no file at all when it collected no data, so absence is the
    # loudest symptom of the quietest failure — and the easiest one to mistake for a
    # tidy run that simply had nothing to say.
    verdict, output = census(None)
    assert_rejected(verdict, output, "no report at all means nothing was measured")


@test("census survives a report with no totals block")
def _():
    verdict, output = census({"files": {"src/m.py": {}}})
    assert verdict == PASSED, output
    assert "unknown" in output, output


@test("census does not crash on hostile report shapes")
def _():
    # The census reads a file written by another tool, so it does not get to assume the
    # shape is sane. Every case must end in a clean pass or a clean ::error::, never a
    # traceback: an unexplained step failure is indistinguishable from an infrastructure
    # problem and gets retried rather than fixed.
    hostile = [
        ("empty object", {}),
        ("files is null", {"files": None}),
        ("files is a list", {"files": []}),
        ("report is a list", []),
        ("report is a string", "no data"),
        ("totals is null", {"files": {"a.py": {}}, "totals": None}),
        ("totals is a list", {"files": {"a.py": {}}, "totals": []}),
        ("totals is missing percent", {"files": {"a.py": {}}, "totals": {}}),
        ("percent is a string", {"files": {"a.py": {}}, "totals": {"percent_covered": "n/a"}}),
        ("percent is null", {"files": {"a.py": {}}, "totals": {"percent_covered": None}}),
    ]
    for label, report in hostile:
        verdict, output = census(report)
        assert verdict != CRASHED, f"{label} crashed the census:\n{output}"


# ── The workflow's own contract ──────────────────────────────────────────────────


@test("the workflow declares every input the lifted programs read")
def _():
    # A rename on either side of the env: block is invisible to the tests above, because
    # they set the environment themselves. This binds them back to the shipped file.
    text = WORKFLOW.read_text(encoding="utf-8")
    for name in ("COVERAGE", "SOURCE_DIR", "TEST_PATH", "PYTEST_EXTRA_ARGS"):
        assert f"{name}: ${{{{ inputs." in text, (
            f"{name} is read by a lifted program but never set from an input in {WORKFLOW.name}"
        )
    for name in ("VENV", "ARGV_FILE", "COV_JSON"):
        assert f'echo "{name}=$RUNNER_TEMP/' in text, (
            f"{name} is read by a lifted program or a run step but never exported in "
            f"{WORKFLOW.name}"
        )


@test("the census step is skipped exactly when coverage is none")
def _():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "if: ${{ inputs.coverage != 'none' }}" in text, (
        "the census must not run under coverage: none (there is no report to read), and "
        "must run under every other posture"
    )


@test("third-party actions are SHA-pinned")
def _():
    import re

    text = WORKFLOW.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- uses:") and not stripped.startswith("uses:"):
            continue
        ref = stripped.split("uses:", 1)[1].split("#", 1)[0].strip()
        assert re.search(r"@[0-9a-f]{40}$", ref), f"not SHA-pinned: {ref}"


def main():
    # `--extract <sentinel>` prints one lifted program to stdout. The self-test workflow
    # uses it to drive the census against a real (failed) coverage run, so there is still
    # exactly one definition anywhere of how a program is lifted out of the workflow.
    if len(sys.argv) == 3 and sys.argv[1] == "--extract":
        print(program_text(sys.argv[2]))
        return 0
    if len(sys.argv) > 1:
        print(f"usage: {sys.argv[0]} [--extract {RESOLVE}|{CENSUS}]", file=sys.stderr)
        return 2

    failures = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        # Catching Exception, not just AssertionError: an unexpected error inside one test
        # must not abort the run. When it does, every later test silently never runs and
        # the absence of FAIL lines reads as a clean sheet.
        except Exception as exc:  # noqa: BLE001
            failures += 1
            label = "FAIL" if isinstance(exc, AssertionError) else "ERROR"
            print(f"  {label}  {name}\n        {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
