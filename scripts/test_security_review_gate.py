#!/usr/bin/env python3
"""Tests for the "Security review gate" step in azure-deploy-reusable.yml.

Until now, the gate's parsing (comment/fence stripping, the `#472`-class table-boundary
split) had never been executed by anything but a real deploy, and the review that flagged
this file's actual defect (finding 10, `reviews/2026-08-17-tier1-devops-815.md`) put it
plainly: the gate authenticates a PASS row's *format*, never *who wrote it* — anyone able
to land the right markdown on the deployed branch satisfied it. The fix adds a `Reviewed-in`
PR reference the gate independently verifies via the GitHub API (merged, touches this exact
file, byte-identical to the approved version, carries an APPROVED review from someone other
than the PR's own author) instead of trusting the file's text.

These tests execute the verification program lifted **out of the workflow file itself**, not
out of a copy — same technique as `scripts/test_signed_dpa_gate.py`. A test that reasons
about a re-implementation is a test about the re-implementation: it stays green while the
shipped workflow rots. Most of what follows is negative controls, each driving a scenario
that SHOULD be rejected through the gate and asserting it is — the sibling finding in the
same review (finding 1: an identical bug survived unnoticed in two copies of a gate nobody
had executed) is exactly the failure mode this file exists to rule out here.

`urllib.request.urlopen` is monkeypatched so no test makes a real network call; each
GitHub API response is supplied as a fixture. Run: python3 scripts/test_security_review_gate.py
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import tempfile
import textwrap
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "azure-deploy-reusable.yml"

STEP_MARKER = "- name: Security review gate"
HEREDOC_OPEN = "python3 << 'EOF'"
HEREDOC_CLOSE = "\n          EOF"


def gate_source() -> "code":  # noqa: F821 - forward-ref only, no runtime import needed
    """The gate's verification program, lifted from the shipped workflow.

    This file has TWO `python3 << 'EOF'` heredocs (the other is "Build deployment
    matrix", covered by test_azure_deploy_reusable.py) — scope the search to the
    "Security review gate" step first so this can't silently start extracting the
    wrong one.
    """
    text = WORKFLOW.read_text()
    if text.count(STEP_MARKER) != 1:
        raise AssertionError(
            f"expected exactly one {STEP_MARKER!r} step in {WORKFLOW.name}; the "
            "extraction below is no longer unambiguous"
        )
    after_step = text.split(STEP_MARKER, 1)[1]
    if after_step.count(HEREDOC_OPEN) < 1:
        raise AssertionError(f"no {HEREDOC_OPEN!r} found after {STEP_MARKER!r}")
    body = after_step.split(HEREDOC_OPEN, 1)[1].split(HEREDOC_CLOSE, 1)[0]
    body = textwrap.dedent(body)
    if "docs/SECURITY-REVIEW.md" not in body or "reviewed-in" not in body:
        raise AssertionError("extracted block does not look like the security review gate")
    return compile(body, str(WORKFLOW), "exec")


GATE = gate_source()


PASSED, REJECTED, CRASHED = "passed", "rejected", "crashed"

DEFAULT_REPO = "acme/acme-repo"
DEFAULT_TOKEN = "test-token"  # noqa: S105 - fixture value, not a real credential
DEFAULT_API = "https://api.github.test"


def blob_sha(text: str) -> str:
    """The git blob SHA1 of `text`, matching what the gate computes and what
    GitHub's `pulls/{n}/files` endpoint reports for a changed file — cross-checked
    against a real merged PR (infra-commons/devops#24) while designing the gate:
    `git rev-parse <merge-sha>:<path>` == the files-endpoint `.sha` == this formula.
    """
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def _make_urlopen(fixtures: dict):
    """A stand-in for urllib.request.urlopen that never touches the network.

    Routes on whether the requested URL is the PR itself, its files, or its
    reviews (rather than requiring exact query strings) so fixtures stay simple.
    A fixture value that is an Exception instance is raised instead of returned,
    for simulating HTTPError/URLError.
    """
    import json

    def _urlopen(req, timeout=30):  # noqa: ARG001 - signature must match real urlopen
        url = req.full_url
        if "/files" in url:
            key = "files"
        elif "/reviews" in url:
            key = "reviews"
        elif "/pulls/" in url:
            key = "pr"
        else:
            raise AssertionError(f"unexpected GitHub API call in test: {url}")
        if key not in fixtures:
            raise AssertionError(f"no fixture supplied for {key!r} ({url})")
        value = fixtures[key]
        if isinstance(value, BaseException):
            raise value
        return _FakeResponse(json.dumps(value).encode("utf-8"))

    return _urlopen


def run_gate(review_md: str, solution_name: str = "acme-solution",
             fixtures: dict | None = None, missing_token: bool = False):
    """Run the gate against one docs/SECURITY-REVIEW.md + SOLUTION.yaml.

    Returns (verdict, output). CRASHED is kept distinct from REJECTED — see
    scripts/test_signed_dpa_gate.py's run_gate() docstring for why that
    distinction matters and has caught a real regression before.
    """
    tmp = tempfile.mkdtemp(prefix="security-review-gate-test-")
    old_cwd = os.getcwd()
    env_backup = dict(os.environ)
    real_stdout = sys.stdout
    try:
        (Path(tmp) / "docs").mkdir(parents=True, exist_ok=True)
        (Path(tmp) / "docs" / "SECURITY-REVIEW.md").write_text(review_md, encoding="utf-8")
        (Path(tmp) / "SOLUTION.yaml").write_text(f'name: "{solution_name}"\n', encoding="utf-8")
        os.chdir(tmp)

        os.environ["GITHUB_REPOSITORY"] = DEFAULT_REPO
        os.environ["GITHUB_API_URL"] = DEFAULT_API
        if missing_token:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = DEFAULT_TOKEN

        captured = io.StringIO()
        sys.stdout = captured

        verdict = CRASHED
        try:
            with mock.patch("urllib.request.urlopen", _make_urlopen(fixtures or {})):
                exec(GATE, {"__name__": "__main__"})
            # The gate always calls sys.exit(); falling through is itself a bug.
        except SystemExit as e:
            verdict = PASSED if e.code in (0, None) else REJECTED
        except Exception:  # noqa: BLE001 - deliberately broad, see CRASHED above
            verdict = CRASHED
        return verdict, captured.getvalue()
    finally:
        sys.stdout = real_stdout
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(env_backup)
        shutil.rmtree(tmp, ignore_errors=True)


def review_md(*, pr="42", result="**PASS**", solution="acme-solution",
              include_reviewed_in=True, extra_row: str | None = None) -> str:
    reviewed_in = f"| Reviewed-in | #{pr} |\n" if include_reviewed_in else ""
    extra = f"{extra_row}\n" if extra_row else ""
    return (
        "# Security review\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Solution | `{solution}` |\n"
        f"| Result | {result} |\n"
        f"{reviewed_in}"
        f"{extra}"
    )


def happy_fixtures(review_text: str, *, pr_author="author1", approver="reviewer1") -> dict:
    return {
        "pr": {"merged": True, "user": {"login": pr_author}},
        "files": [
            {"filename": "docs/SECURITY-REVIEW.md", "status": "modified",
             "sha": blob_sha(review_text)},
        ],
        "reviews": [{"user": {"login": approver}, "state": "APPROVED"}],
    }


def _check(name, condition, detail=""):
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    return condition


def main() -> int:
    problems = 0

    # ── Positive control ────────────────────────────────────────────────────
    text = review_md()
    verdict, out = run_gate(text, fixtures=happy_fixtures(text))
    if not _check("happy path: merged + touches file + hash matches + distinct approver "
                   "→ PASSED", verdict == PASSED, f"got {verdict}: {out}"):
        problems += 1
    if not _check("happy path output names the approver", "reviewer1" in out, out):
        problems += 1

    # ── New required field ──────────────────────────────────────────────────
    text = review_md(include_reviewed_in=False)
    verdict, out = run_gate(text, fixtures={})
    if not _check("no Reviewed-in field → REJECTED", verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1
    if not _check("rejection names the missing field", "Reviewed-in" in out, out):
        problems += 1

    text = review_md(pr="see-PR-42", include_reviewed_in=False,
                      extra_row="| Reviewed-in | see PR 42 |")
    verdict, out = run_gate(text, fixtures={})
    if not _check("malformed Reviewed-in value → REJECTED", verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    # ── PR must actually be merged ──────────────────────────────────────────
    text = review_md()
    fixtures = happy_fixtures(text)
    fixtures["pr"] = {"merged": False, "user": {"login": "author1"}}
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("unmerged PR → REJECTED", verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    # ── PR must touch this exact file (the laundering-prevention check) ────
    text = review_md()
    fixtures = happy_fixtures(text)
    fixtures["files"] = [{"filename": "README.md", "status": "modified", "sha": "deadbeef"}]
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("PR merged+approved but never touches docs/SECURITY-REVIEW.md → REJECTED "
                   "(citing an unrelated approved PR must not launder a direct-pushed row)",
                   verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    text = review_md()
    fixtures = happy_fixtures(text)
    fixtures["files"] = [{"filename": "docs/SECURITY-REVIEW.md", "status": "removed",
                           "sha": blob_sha(text)}]
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("PR removed the file rather than adding/modifying it → REJECTED",
                   verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    # ── Content must be byte-identical to what the PR approved ─────────────
    text = review_md()
    fixtures = happy_fixtures(text)
    fixtures["files"][0]["sha"] = blob_sha("this is not the approved content")
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("checkout content edited after the cited PR's approval → REJECTED",
                   verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    # ── Approval must come from someone other than the PR's own author ─────
    text = review_md()
    fixtures = happy_fixtures(text, pr_author="attacker", approver="attacker")
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("only the author's own name in the reviews list → REJECTED "
                   "(self-approval-only)", verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    text = review_md()
    fixtures = happy_fixtures(text)
    fixtures["reviews"] = [{"user": {"login": "author1"}, "state": "COMMENTED"}]
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("no APPROVED review at all (only a comment) → REJECTED",
                   verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    # ── The API itself must be fail-closed, not fail-open ───────────────────
    text = review_md()
    fixtures = happy_fixtures(text)
    fixtures["pr"] = urllib.error.HTTPError(url="x", code=404, msg="Not Found", hdrs=None, fp=None)
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("GitHub API 404 on the PR lookup → REJECTED, not silently accepted",
                   verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    text = review_md()
    fixtures = happy_fixtures(text)
    fixtures["pr"] = urllib.error.URLError("network unreachable")
    verdict, out = run_gate(text, fixtures=fixtures)
    if not _check("GitHub API unreachable → REJECTED, not silently accepted",
                   verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    text = review_md()
    verdict, out = run_gate(text, fixtures=happy_fixtures(text), missing_token=True)
    if not _check("GITHUB_TOKEN not wired to the step → REJECTED, not silently accepted",
                   verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    # ── The untouched #472 table-boundary hardening still composes correctly ─
    # Two tables with no blank line between them: the first names another
    # solution and passes, the second names THIS solution and fails. A
    # winner must never straddle the boundary between them, exactly the
    # defect #472 fixed — this is a regression check that the new code above
    # `if winner:` did not reopen it (it can't: this scenario never reaches
    # that code, since no winner is ever found).
    straddling = (
        "| Field | Value |\n"
        "|---|---|\n"
        "| Solution | `other-solution` |\n"
        "| Result | **PASS** |\n"
        "| Field | Value |\n"
        "|---|---|\n"
        "| Solution | `acme-solution` |\n"
        "| Result | **FAIL** |\n"
        "| Reviewed-in | #42 |\n"
    )
    verdict, out = run_gate(straddling, fixtures={})
    if not _check("table-straddling trick (#472-class) still refused, unaffected by the "
                   "new field", verdict == REJECTED, f"got {verdict}: {out}"):
        problems += 1

    print()
    if problems:
        print(f"FAIL: {problems} problem(s) found.", file=sys.stderr)
        return 1
    print("All security review gate checks passed. OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
