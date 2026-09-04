#!/usr/bin/env python3
"""Executable tests for azure-deploy-reusable.yml's load-matrix program (`#39`).

The program is executed **out of the workflow file itself**, not out of a copy — as
test_auto_assign_reusable.py, test_signed_dpa_gate.py and test_python_tests_reusable.py all do,
for the same reason: a test that reasons about a re-implementation stays green while the shipped
workflow rots. The price is that this extraction is coupled to the block's shape; it fails loudly,
never silently, if that shape changes.

It stays embedded rather than moving to scripts/ because this is a `workflow_call` reusable:
`actions/checkout` with no `repository:` checks out the CALLER's repo, so a file living here would
not be on disk when the job runs. Lifting it out costs a third SHA-pinned checkout of this repo on
the deploy path — a cost this file already pays for its two composite actions.

Covers the three behaviours `#38` shipped and verified only by hand. The first is the subtle one:
`yaml.safe_load` returns None for a blank/comment-only file and a list/scalar for a non-mapping
document — none of which raise, so the `try/except` around the load never saw them, and since the
loop is per-client ONE blank file aborted the deploy for EVERY client. That blast radius, not the
warning text, is what the second test pins.

Needs PyYAML (the program under test imports it). Run: python3 scripts/test_azure_deploy_matrix.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import textwrap
from unittest import mock

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard, not a code path
    sys.exit("PyYAML is required: pip install pyyaml")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "azure-deploy-reusable.yml"

# The delimiter is named rather than `EOF` because this file has more than one heredoc'd python3
# block — that is what makes the count in program_source a real check. LANDMARK is unique to this
# program, so a reshaped block fails loudly instead of testing the wrong one.
SENTINEL = "PYEOF_MATRIX"
LANDMARK = "ZDR_PROVIDERS"

SOLUTION = "example-solution"
SUBSCRIPTION_ID = "00000000-0000-4000-8000-000000000001"

# Asserted by value, not as "something non-empty": a stale default no client could override is the
# exact defect `#37` was filed for.
DEFAULT_PRIMARY = "claude-sonnet-5"
DEFAULT_FAST = "claude-haiku-4-5-20251001"

PASSED, REJECTED, CRASHED = "passed", "rejected", "crashed"


def program_source():
    text = WORKFLOW.read_text(encoding="utf-8")
    opener = f"python3 << '{SENTINEL}'"
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
    if LANDMARK not in body:
        raise AssertionError(
            f"the block extracted for {SENTINEL} does not contain {LANDMARK!r} — it is not the "
            "matrix program, or that program changed shape. Re-point this extraction."
        )
    return compile(body, f"{WORKFLOW}:{SENTINEL}", "exec")


LOAD_MATRIX = program_source()


def minimal_client(slug, **config_overrides):
    """The smallest client config that survives the whole load-matrix gauntlet.

    `environment_class: test` (run with ALLOWED_CLASS=test) keeps the zero-retention promote gate
    out of the way — it fires only for production-class clients and would otherwise hard-fail every
    non-Azure provider case below, since only azure_openai is ZDR-designated. `rolliq_managed`
    avoids needing a tenant_id. Region, tier, status and operations take their defaults, so
    anything set here is set because a behaviour under test needs it.
    """
    return {
        "client_slug": slug,
        "environment_class": "test",
        "deployment": {"model": "rolliq_managed", "azure": {"subscription_id": SUBSCRIPTION_ID}},
        "solutions": [{
            "name": SOLUTION,
            "enabled": True,
            "enabled_environments": ["staging"],
            "config_overrides": config_overrides,
        }],
    }


def run_matrix(files):
    """Run the shipped program over `files` ({filename: dict to dump, or raw text}).

    Returns (verdict, combined output, matrix entries). A rejected run's message is folded into the
    output: sys.exit's argument is printed only by the interpreter at top level, and catching
    SystemExit here means nothing else would ever surface it.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="load-matrix-"))
    clients = tmp / "clients-config" / "clients"
    clients.mkdir(parents=True)
    for name, content in files.items():
        text = content if isinstance(content, str) else yaml.safe_dump(content)
        (clients / name).write_text(text, encoding="utf-8")

    env = {
        "ONLY_CLIENT": "",
        "ALLOWED_CLASS": "test",
        "TARGET_ENVIRONMENT": "staging",
        "SOLUTION_NAME": SOLUTION,
        "GITHUB_OUTPUT": str(tmp / "github_output"),
    }
    out, err = io.StringIO(), io.StringIO()
    message = ""
    saved_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        with mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(LOAD_MATRIX, {"__name__": "__main__"})  # noqa: S102 - the artefact under test
                verdict = PASSED
            except SystemExit as exc:
                verdict = PASSED if exc.code in (0, None) else REJECTED
                message = "" if verdict == PASSED else str(exc.code)
            except Exception as exc:  # noqa: BLE001
                verdict, message = CRASHED, f"{type(exc).__name__}: {exc}"

        entries = []
        written = tmp / "github_output"
        if written.exists():
            for line in written.read_text(encoding="utf-8").splitlines():
                if line.startswith("matrix="):
                    entries = json.loads(line[len("matrix="):])["include"]
        return verdict, out.getvalue() + err.getvalue() + message, entries
    finally:
        os.chdir(saved_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


TESTS = []


def test(name):
    def register(fn):
        TESTS.append((name, fn))
        return fn

    return register


def check(condition, message):
    if not condition:
        raise AssertionError(message)


# ── 1. The non-mapping guard ────────────────────────────────────────────────────

@test("a non-mapping client file is skipped with a warning, whatever it parsed to")
def _():
    verdict, output, _entries = run_matrix({
        "alpha.yaml": minimal_client("alpha", llm_provider="anthropic"),
        "blank.yaml": "",
        "commented.yaml": "# a comment and nothing else\n",
        "alist.yaml": "- one\n- two\n",
        "ascalar.yaml": "just-a-string\n",
    })
    check(verdict == PASSED, f"expected the load to survive four malformed files, got {verdict}: {output}")
    for name, parsed in (("blank.yaml", "NoneType"), ("commented.yaml", "NoneType"),
                         ("alist.yaml", "list"), ("ascalar.yaml", "str")):
        check(f"skipping {name} — not a YAML mapping (parsed as {parsed})" in output,
              f"expected a non-mapping skip warning for {name} (parsed as {parsed}); got:\n{output}")


@test("one malformed file does not take the healthy clients down with it")
def _():
    # The pre-#38 defect was not a missing warning — it was AttributeError on the next line,
    # aborting the matrix build so a single blank file cancelled every client's deploy.
    verdict, output, entries = run_matrix({
        "alpha.yaml": minimal_client("alpha", llm_provider="anthropic"),
        "beta.yaml": minimal_client("beta", llm_provider="anthropic"),
        "blank.yaml": "",
    })
    check(verdict == PASSED, f"a blank file must not abort the load, got {verdict}: {output}")
    check(sorted(e["client_slug"] for e in entries) == ["alpha", "beta"],
          "both healthy clients must still reach the matrix alongside a malformed file; got "
          f"{sorted(e['client_slug'] for e in entries)}")


# ── 2. azure_openai has no safe default ─────────────────────────────────────────

@test("azure_openai with neither pin set fails the load, naming both keys")
def _():
    verdict, output, entries = run_matrix({
        "alpha.yaml": minimal_client("alpha", llm_provider="azure_openai"),
    })
    check(verdict == REJECTED, f"expected a hard fail on an unpinned azure_openai client, got {verdict}: {output}")
    check("sets no llm_model_primary and no llm_model_fast" in output,
          f"the error must name both missing pins; got:\n{output}")
    check(entries == [], f"a rejected load must emit no matrix, got {entries}")


@test("azure_openai with only one pin set still fails, naming just the missing one")
def _():
    verdict, output, _entries = run_matrix({
        "alpha.yaml": minimal_client("alpha", llm_provider="azure_openai", llm_model_fast="fast-deployment"),
    })
    check(verdict == REJECTED, f"a half-pinned azure_openai client must still fail, got {verdict}: {output}")
    check("sets no llm_model_primary in its solution config_overrides" in output,
          f"the error must name llm_model_primary alone, not both; got:\n{output}")


@test("azure_openai with both pins set lands, and the pins reach the matrix verbatim")
def _():
    verdict, output, entries = run_matrix({
        "alpha.yaml": minimal_client("alpha", llm_provider="azure_openai",
                                     llm_model_primary="primary-deployment",
                                     llm_model_fast="fast-deployment"),
    })
    check(verdict == PASSED, f"a fully pinned azure_openai client must load, got {verdict}: {output}")
    check(len(entries) == 1, f"expected exactly one matrix entry, got {entries}")
    check(entries[0]["llm_model_primary"] == "primary-deployment",
          f"llm_model_primary must pass through unchanged; got {entries[0]['llm_model_primary']!r}")
    check(entries[0]["llm_model_fast"] == "fast-deployment",
          f"llm_model_fast must pass through unchanged; got {entries[0]['llm_model_fast']!r}")


# ── 3. anthropic / foundry resolve the declared defaults ────────────────────────

@test("anthropic and foundry resolve the fleet defaults when no pin is set")
def _():
    for provider in ("anthropic", "foundry"):
        verdict, output, entries = run_matrix({
            "alpha.yaml": minimal_client("alpha", llm_provider=provider),
        })
        check(verdict == PASSED, f"an unpinned {provider} client must load, got {verdict}: {output}")
        check(len(entries) == 1, f"expected exactly one matrix entry for {provider}, got {entries}")
        check(entries[0]["llm_model_primary"] == DEFAULT_PRIMARY,
              f"{provider} llm_model_primary must default to {DEFAULT_PRIMARY!r}; "
              f"got {entries[0]['llm_model_primary']!r}")
        check(entries[0]["llm_model_fast"] == DEFAULT_FAST,
              f"{provider} llm_model_fast must default to {DEFAULT_FAST!r}; "
              f"got {entries[0]['llm_model_fast']!r}")


@test("an explicit pin on anthropic overrides the default")
def _():
    # `#37`'s actual defect: the reusable read key names no client ever wrote, so `or <default>`
    # was not a fallback, it was the value. A default that cannot be overridden is the bug.
    verdict, output, entries = run_matrix({
        "alpha.yaml": minimal_client("alpha", llm_provider="anthropic", llm_model_primary="pinned-primary"),
    })
    check(verdict == PASSED, f"expected the client to load, got {verdict}: {output}")
    check(entries[0]["llm_model_primary"] == "pinned-primary",
          f"an explicit pin must beat the default; got {entries[0]['llm_model_primary']!r}")
    check(entries[0]["llm_model_fast"] == DEFAULT_FAST,
          f"the unpinned key must still default; got {entries[0]['llm_model_fast']!r}")


def main() -> int:
    failures = 0
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {name}\n      {exc}")
        else:
            print(f"ok:   {name}")
    subject = f"{WORKFLOW.name}'s {SENTINEL} program"
    if failures:
        print(f"\n{failures} of {len(TESTS)} checks failed against {subject}.")
        return 1
    print(f"\nAll {len(TESTS)} checks passed against {subject}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
