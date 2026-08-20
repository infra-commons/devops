#!/usr/bin/env python3
"""Tests for .github/actions/closing-keyword-guard/closing_keyword_guard.py.

Imports the shipped module directly (same convention as
scripts/test_traceability_check.py), so this exercises the real guard, not a
reimplementation that can drift from what CI actually runs. "A gate that cannot fail is
indistinguishable from one that passes" — most of this suite is negative controls: a
code PR closing its own card, an empty diff, a two-dot diff standing in for the trap
this guard exists to close.

The two `test_three_dot_*` / `test_two_dot_*` cases are the reason this guard exists:
they build a real temporary git repo where the target branch gains an unrelated commit
AFTER the PR branch was cut, and assert the merge-base (`...`) form ignores it while
also asserting — as an explicit negative control — that the naive endpoint (`..`) form
would NOT have.

Run: python3 scripts/test_closing_keyword_guard.py
"""

import contextlib
import os
import pathlib
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "closing-keyword-guard"
sys.path.insert(0, str(ACTION_DIR))

import closing_keyword_guard as ckg  # noqa: E402

FAILURES = []


def test(label):
    def deco(fn):
        try:
            fn()
            print(f"  PASS  {label}")
        except AssertionError as exc:
            print(f"  FAIL  {label}: {exc}")
            FAILURES.append(label)
        return fn
    return deco


# ---------------------------------------------------------------------------
# git fixture helpers
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_out(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(cwd, path, content, message):
    p = pathlib.Path(cwd) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git("add", "-A", cwd=cwd)
    _git("commit", "-q", "-m", message, cwd=cwd)
    return _git_out("rev-parse", "HEAD", cwd=cwd)


@contextlib.contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


@contextlib.contextmanager
def _diverged_repo():
    """A `main` and a PR branch that diverge, where main moves on AFTER the branch is
    cut. Yields (root_sha, head_sha, base_sha): head_sha is the PR branch tip (docs
    only), base_sha is main's CURRENT tip (which is what a live PR's base.sha reports —
    it tracks main, not the commit the branch was cut from)."""
    with tempfile.TemporaryDirectory() as d:
        _git("init", "-q", "-b", "main", cwd=d)
        _git("config", "user.email", "test@example.invalid", cwd=d)
        _git("config", "user.name", "Test", cwd=d)
        root_sha = _commit(d, "README.md", "root\n", "root")
        _git("checkout", "-q", "-b", "docs-branch", cwd=d)
        head_sha = _commit(d, "docs/guide.md", "guide\n", "docs change")
        _git("checkout", "-q", "main", cwd=d)
        base_sha = _commit(d, "src/app.py", "code\n", "main moved on")
        yield d, root_sha, head_sha, base_sha


@test("three-dot file diff excludes a file the target branch gained after the branch point")
def _():
    with _diverged_repo() as (d, _root, head_sha, base_sha), _chdir(d):
        files = ckg.git_changed_files(base_sha, head_sha)
    assert files == ["docs/guide.md"], files


@test("negative control: a naive two-dot file diff WOULD wrongly include it")
def _():
    with _diverged_repo() as (d, _root, head_sha, base_sha), _chdir(d):
        naive = subprocess.run(
            ["git", "diff", "--name-only", f"{base_sha}..{head_sha}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    assert "src/app.py" in naive, "the trap this guard closes must be reproducible, or the fix isn't proven"


@test("two-dot commit enumeration lists only the PR branch's own commit")
def _():
    with _diverged_repo() as (d, _root, head_sha, base_sha), _chdir(d):
        commits = ckg.git_commit_messages(base_sha, head_sha)
    assert len(commits) == 1 and "docs change" in commits[0][1], commits


@test("negative control: three-dot rev-list WOULD pull in main's own commit too")
def _():
    with _diverged_repo() as (d, _root, head_sha, base_sha), _chdir(d):
        naive = _git_out("rev-list", f"{base_sha}...{head_sha}", cwd=d).split()
    assert len(naive) == 2, "the symmetric-difference trap must be reproducible: expected both branches' commits"


# ---------------------------------------------------------------------------
# keyword detection
# ---------------------------------------------------------------------------

ALL_KEYWORDS = ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"]


@test("every GitHub closing keyword is detected")
def _():
    for kw in ALL_KEYWORDS:
        refs = ckg.find_closing_references(f"{kw} #12", "x")
        assert len(refs) == 1 and refs[0].keyword.lower() == kw, (kw, refs)


@test("keyword matching is case-insensitive")
def _():
    assert len(ckg.find_closing_references("CLOSES #12", "x")) == 1


@test("the cross-repo owner/repo#N form is detected (the incident's exact form)")
def _():
    refs = ckg.find_closing_references("Closes cashbucket-com/app#74", "x")
    assert len(refs) == 1 and refs[0].ref == "cashbucket-com/app#74", refs


@test("several references in one body are all found")
def _():
    assert len(ckg.find_closing_references("Closes #1 and fixes #2", "x")) == 2


@test("a keyword embedded in a longer word is not flagged")
def _():
    assert ckg.find_closing_references("enclosed #12", "x") == []
    assert ckg.find_closing_references("closely #12", "x") == []


@test("a keyword not directly adjacent to a reference is not flagged")
def _():
    assert ckg.find_closing_references("This closes: the loop, see #12", "x") == []


@test("plain text with no keyword is clean")
def _():
    assert ckg.find_closing_references("See #12 for context.", "x") == []


# ---------------------------------------------------------------------------
# docs-only classification
# ---------------------------------------------------------------------------


@test("an empty file list classifies as empty, not docs-only")
def _():
    assert ckg.classify_files([]) == "empty"


@test("a single doc file classifies as docs-only")
def _():
    assert ckg.classify_files(["README.md"]) == "docs-only"


@test("a single code file classifies as mixed")
def _():
    assert ckg.classify_files(["src/app.py"]) == "mixed"


@test("a script living under docs/ is mixed — extension wins over directory")
def _():
    assert ckg.classify_files(["docs/generate.py"]) == "mixed"


@test("docs_patterns is caller-configurable")
def _():
    assert ckg.classify_files(["notes.txt"], docs_patterns=("*.txt",)) == "docs-only"


# ---------------------------------------------------------------------------
# escape hatch
# ---------------------------------------------------------------------------


@test("the doc-closes-card label is an escape hatch, case-insensitively")
def _():
    assert ckg.has_escape_hatch("body", ["Doc-Closes-Card"])


@test("the [doc-closes-card] body marker is an escape hatch")
def _():
    assert ckg.has_escape_hatch("please [doc-closes-card] merge", [])


@test("no label and no marker is not an escape hatch")
def _():
    assert not ckg.has_escape_hatch("just a normal PR body", [])


# ---------------------------------------------------------------------------
# evaluate(): the combined predicate
# ---------------------------------------------------------------------------


@test("empty diff is an error, never a pass, even with a closing keyword present")
def _():
    try:
        ckg.evaluate("Closes #1", [], [], [])
        raise AssertionError("expected EmptyDiffError")
    except ckg.EmptyDiffError:
        pass


@test("a CODE PR closing its own card must never fire")
def _():
    result = ckg.evaluate("Closes #1", [], ["src/app.py"], [])
    assert not result.triggered, result


@test("a docs-only PR with a closing keyword and no escape hatch fires")
def _():
    result = ckg.evaluate("Closes #1", [], ["README.md"], [])
    assert result.triggered, result


@test("the label escape hatch suppresses a docs-only + keyword match")
def _():
    result = ckg.evaluate("Closes #1", [], ["README.md"], ["doc-closes-card"])
    assert not result.triggered and result.escape_hatch_used, result


@test("a docs-only diff with no keyword never fires")
def _():
    result = ckg.evaluate("nothing to see here", [], ["README.md"], [])
    assert not result.triggered and not result.references, result


@test("end-to-end: the incident itself, reproduced, is flagged")
def _():
    body = "Follows the ADR. Closes cashbucket-com/app#74."
    commits = [("commit deadbeef", "docs(adr): record decision 0005\n")]
    result = ckg.evaluate(body, commits, ["docs/decisions/0005-adr.md"], [])
    assert result.triggered
    assert any("cashbucket-com/app#74" in r.ref for r in result.references), result.references


def main() -> int:
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}", file=sys.stderr)
        return 1
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
