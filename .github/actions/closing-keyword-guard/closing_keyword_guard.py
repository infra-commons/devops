#!/usr/bin/env python3
"""
Closing-keyword guard — infra-commons/meta#899.

`cashbucket-com/operations` PR 94 merged a docs-only PR (a decision record plus a
handoff script that had not been run) whose body carried
`Closes cashbucket-com/app#74`. The card closed on merge with production untouched,
and closed reads as finished, so the gap survived two weeks. This guard fails a PR
when BOTH hold:

  1. its body or any commit message in the range carries a closing keyword — the
     full GitHub set (close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved),
     including the cross-repo `owner/repo#N` form, which is the form that fired;
  2. the diff between base and head is documentation-only.

A code PR closing its own card is correct and must never fire — flagging it trains
people to reach for the escape hatch reflexively, which disarms the guard for the one
case it exists for. The escape hatch itself is explicit and review-visible: the
`doc-closes-card` label, or the literal `[doc-closes-card]` marker in the PR body.

Two details decide whether this works or silently does not, both non-negotiable:

  * The file diff is taken against the MERGE BASE, `git diff A...B` (three dots), not
    the endpoints `A..B`. Comparing endpoints reports every file the target branch
    gained since the PR branched, which classifies a docs-only branch as "code" and
    disables the guard exactly when it is needed.
  * Commit enumeration is the opposite: `git rev-list A..B` (two dots). `A...B` for
    `rev-list` is a SYMMETRIC difference and pulls in commits that only ever landed on
    the target branch — not written by this PR at all.
  * An empty file diff is an error, never a pass. Reading "no files changed" as "all
    files are docs" is the same silent-pass defect the merge-base rule closes.

Standard library only — this runs in a job that installs nothing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

ESCAPE_LABEL = "doc-closes-card"
ESCAPE_MARKER_RE = re.compile(r"\[doc-closes-card\]", re.IGNORECASE)

DEFAULT_DOCS_PATTERNS = ("*.md", "*.mdx", "*.markdown", "*.rst", "*.adoc", "*.txt")

# keyword, word-bounded on both ends so `enclosed #12` or `closely #12` never match;
# then mandatory whitespace (no colon, no filler word — GitHub's own syntax is
# "KEYWORD #N" with nothing between); then an optional `owner/repo` prefix and `#N`,
# with a trailing (?!\w) so `#72nd` is not read as a truncated `#72`.
CLOSING_REF_RE = re.compile(
    r"\b(?P<keyword>close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b"
    r"\s+"
    r"(?P<owner_repo>[\w][\w.-]*/[\w][\w.-]*)?"
    r"#(?P<num>\d+)(?!\w)",
    re.IGNORECASE,
)


class EmptyDiffError(RuntimeError):
    """The file diff had zero entries. Never treated as a docs-only pass — see module
    docstring: "no files changed" is not evidence that "all files are docs"."""


@dataclass
class Reference:
    source: str
    keyword: str
    ref: str

    def __str__(self) -> str:
        return (
            f"{self.source}: closing keyword '{self.keyword}' targets {self.ref} in a "
            f"documentation-only diff. If this PR really is the whole deliverable and "
            f"should close the card, say so explicitly: add the '{ESCAPE_LABEL}' label "
            f"or the literal '[{ESCAPE_LABEL}]' marker to the PR body."
        )


@dataclass
class Result:
    triggered: bool
    references: list = field(default_factory=list)
    docs_only: bool = False
    escape_hatch_used: bool = False


def find_closing_references(text: str, source: str) -> list[Reference]:
    """Every closing-keyword reference in `text`, in document order.

    Deliberately NOT masked against code fences/inline code/URLs, unlike a bare-`#NNN`
    scan: under-detecting a real closing keyword reproduces the incident this guard
    exists to catch, while over-detecting one only costs an easy label add. The
    accuracy budget goes to the docs-only predicate instead (below), which is the side
    the spec requires to be accurate rather than conservative.
    """
    if not text:
        return []
    out = []
    for m in CLOSING_REF_RE.finditer(text):
        owner_repo = m.group("owner_repo") or ""
        out.append(Reference(source, m.group("keyword"), f"{owner_repo}#{m.group('num')}"))
    return out


def classify_files(files: list[str], docs_patterns=DEFAULT_DOCS_PATTERNS) -> str:
    """"empty" | "docs-only" | "mixed". Extension-based on each path's basename, not
    directory-based — a script living under docs/ must never be waved through."""
    if not files:
        return "empty"
    import fnmatch

    for path in files:
        basename = path.rsplit("/", 1)[-1]
        if not any(fnmatch.fnmatch(basename, pattern) for pattern in docs_patterns):
            return "mixed"
    return "docs-only"


def has_escape_hatch(pr_body: str, labels: list[str]) -> bool:
    if any(label.strip().lower() == ESCAPE_LABEL for label in labels):
        return True
    return bool(ESCAPE_MARKER_RE.search(pr_body or ""))


def evaluate(
    pr_body: str,
    commit_messages: list[tuple[str, str]],
    files: list[str],
    labels: list[str],
    docs_patterns=DEFAULT_DOCS_PATTERNS,
) -> Result:
    classification = classify_files(files, docs_patterns)
    if classification == "empty":
        raise EmptyDiffError(
            "the diff between base and head touched zero files. That is never read as "
            "'all files are docs' — check that base-sha/head-sha are correct and the "
            "PR actually contains commits."
        )

    references = find_closing_references(pr_body, "PR body")
    for label, message in commit_messages:
        references.extend(find_closing_references(message, label))

    docs_only = classification == "docs-only"
    escape = has_escape_hatch(pr_body, labels)
    triggered = bool(references) and docs_only and not escape
    return Result(triggered=triggered, references=references, docs_only=docs_only, escape_hatch_used=escape)


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def git_changed_files(base_sha: str, head_sha: str) -> list[str]:
    """Files changed by the PR, against the MERGE BASE — three dots. See module
    docstring for why two endpoints (`A..B`) would silently disable this guard."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def git_commit_messages(base_sha: str, head_sha: str) -> list[tuple[str, str]]:
    """(label, message) for every commit the PR branch added — two dots, the inverse
    of the file diff above. `rev-list A...B` is a symmetric difference and would pull
    in commits that only ever landed on the target branch."""
    shas = subprocess.run(
        ["git", "rev-list", f"{base_sha}..{head_sha}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    messages = []
    for sha in shas:
        body = subprocess.run(
            ["git", "log", "-1", "--format=%B", sha],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        messages.append((f"commit {sha[:8]}", body))
    return messages


def _docs_patterns_from_env() -> tuple:
    raw = os.environ.get("DOCS_PATTERNS", "").strip()
    if not raw:
        return DEFAULT_DOCS_PATTERNS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print(
            "closing-keyword-guard: GITHUB_EVENT_PATH is not set — this must run "
            "inside a GitHub Actions job.",
            file=sys.stderr,
        )
        return 2

    with open(event_path, encoding="utf-8") as f:
        event = json.load(f)

    pr = event.get("pull_request")
    if not pr:
        print(
            "closing-keyword-guard: event payload has no 'pull_request' — wrong "
            "trigger? This guard is meant to run on the pull_request event.",
            file=sys.stderr,
        )
        return 2

    base_sha = pr["base"]["sha"]
    head_sha = pr["head"]["sha"]
    body = pr.get("body") or ""
    labels = [entry.get("name", "") for entry in pr.get("labels", [])]

    try:
        files = git_changed_files(base_sha, head_sha)
        commit_messages = git_commit_messages(base_sha, head_sha)
    except subprocess.CalledProcessError as exc:
        print(f"closing-keyword-guard: git failed: {exc}", file=sys.stderr)
        return 2

    try:
        result = evaluate(body, commit_messages, files, labels, _docs_patterns_from_env())
    except EmptyDiffError as exc:
        print(f"::error::closing-keyword-guard: {exc}", file=sys.stderr)
        return 2

    if not result.triggered:
        if not result.references:
            print("closing-keyword-guard: no closing keyword found. OK.")
        elif not result.docs_only:
            print(
                f"closing-keyword-guard: {len(result.references)} closing keyword "
                "reference(s) found, but the diff is not documentation-only — a code "
                "PR closing its own card is expected behaviour. OK."
            )
        else:
            print(
                f"closing-keyword-guard: {len(result.references)} closing keyword "
                f"reference(s) found in a documentation-only diff, but the "
                f"'{ESCAPE_LABEL}' escape hatch is present. OK."
            )
        return 0

    for ref in result.references:
        print(f"::error::{ref}")
    print(
        f"\n{len(result.references)} closing keyword reference(s) in a "
        "documentation-only diff — a doc PR delivers the decision, not the capability "
        "the card asks for. See infra-commons/meta#899.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
