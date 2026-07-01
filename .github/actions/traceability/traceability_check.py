#!/usr/bin/env python3
"""
Traceability check (canonical, infra-commons/devops).

Reads REQUIREMENTS.md, the ADRs in docs/decisions/, and a summary of the
codebase, then asks Claude to identify:
  - Forward gaps: requirements with no corresponding code
  - Backward gaps: code that has no corresponding requirement (scope creep)
  - Misalignments: code that contradicts or weakens a stated requirement

ADR-awareness: a deviation between a requirement and the code that is explained
by an accepted ADR is INTENTIONAL, not drift — the model is told to treat it as
such. This is what stops an ADR-justified decision (e.g. "provider changed from
Foundry to Anthropic, see ADR-0001") from being re-reported as a violation on
every PR.

Findings are posted as a PR comment. The check is advisory — it never blocks
merge. Reviewers use the report to catch drift between intent and implementation.

Required env vars:
  ANTHROPIC_API_KEY  — Anthropic API key (passed by the reusable workflow)
  GITHUB_TOKEN       — GitHub token (set automatically by Actions)
  PR_NUMBER          — Pull request number
  REPO               — owner/repo slug (e.g. rolliq-com/invoice-extractor)
"""

import os
import sys
import time
from pathlib import Path

import anthropic
import httpx

# ── Constants ──────────────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"
MODEL = "claude-sonnet-4-6"
COMMENT_MARKER = "<!-- traceability-check-bot -->"

# Caps to keep the context window manageable.
# Keeping the codebase summary well under 10k tokens avoids hitting the
# 10k-input-tokens/minute org rate limit when other CI jobs run concurrently.
MAX_FILE_CHARS = 2_000    # per file in the codebase summary
MAX_TOTAL_CHARS = 25_000  # total codebase summary (~6k tokens)
MAX_ADR_CHARS = 12_000    # total ADR text included

SYSTEM_PROMPT = """\
You are a requirements traceability analyst reviewing a software solution.
You will receive:
  1. A REQUIREMENTS.md describing what the solution must (and must not) do.
  2. The accepted ADRs (architecture decision records) from docs/decisions/.
  3. A summary of the codebase grouped by layer (API, workflows, LLM client,
     prompts, storage, infrastructure, config).

Your job is to identify three categories of issues:

**Forward gaps** — requirements that have no visible implementation in the
codebase. These are features or constraints that were specified but appear not
to be built yet.

**Backward gaps** — behaviour or capabilities in the codebase that have no
corresponding requirement. This is scope creep: the code does something the
requirements never asked for. Scope creep is where security holes live.

**Misalignments** — code that contradicts or weakens a stated requirement.
For example: a requirement says "MUST validate LLM output against schema" but
the code skips validation on a code path, or a "MUST NOT log document content"
requirement exists but a logger call logs the raw document.

CRITICAL — respect the ADRs. An accepted ADR is a deliberate, governing
decision. If a requirement and the code diverge but an ADR explains the
divergence (for example the code uses a different LLM provider than an older
requirement line, and an ADR records that change), that is INTENTIONAL — do NOT
report it as a misalignment or gap. Instead, at most note that the requirement
text should be refreshed to match the accepted ADR, as a low-priority doc nit.
Only report a misalignment when the code contradicts BOTH the requirements AND
the ADRs, or when no ADR governs the divergence.

The codebase is provided as a TRUNCATED summary (each file capped, some files
omitted). Do NOT report something as a "forward gap" or "missing" solely because
it is not visible in the truncated summary — say you cannot confirm it from the
summary instead. Prefer false negatives over false positives.

Format your response exactly as follows:

## Traceability Report

### Forward gaps — requirements with no corresponding code
For each gap: cite the specific requirement and explain what is missing.
_(or "None identified")_

### Backward gaps — code with no corresponding requirement
For each gap: cite the file and describe what the code does that was not
specified. Flag HIGH if it introduces external calls, data persistence, or
new input handling.
_(or "None identified")_

### Misalignments — code that contradicts a requirement (and is NOT ADR-governed)
For each misalignment: cite both the requirement text and the specific code
location. Do not list anything an ADR already justifies.
_(or "None identified")_

### Summary
Two to three sentences: overall alignment quality, the single most important
finding, and the recommended action.

Be precise. If you are uncertain whether something is a gap or just not visible
in the summary, say so — do not invent findings. Focus on substance: ignore
boilerplate, comments, and standard framework patterns unless they directly
relate to a stated requirement.\
"""


# ── Codebase summary ───────────────────────────────────────────────────────────

_CATEGORIES: list[tuple[str, list[str]]] = [
    ("SOLUTION.yaml", ["SOLUTION.yaml"]),
    ("API routes", ["src/api"]),
    ("Workflows", ["src/workflows"]),
    ("LLM client", ["src/llm"]),
    ("Prompts", ["prompts"]),
    ("Storage", ["src/storage"]),
    ("Infrastructure", ["infra"]),
]

_SKIP_SUFFIXES = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                  ".lock", ".sum", ".tfstate", ".tfstate.backup"}


def _read_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(unreadable)"
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n... (truncated at {MAX_FILE_CHARS} chars)"
    return text


def build_codebase_summary() -> str:
    cwd = Path(".").resolve()
    root = Path(".")
    sections: list[str] = []
    total = 0

    for label, paths in _CATEGORIES:
        files: list[str] = []
        for rel in paths:
            p = (root / rel).resolve()
            if not p.is_relative_to(cwd):
                continue
            if not p.exists():
                continue
            candidates = [p] if p.is_file() else sorted(p.rglob("*"))
            for fp in candidates:
                if not fp.resolve().is_relative_to(cwd):
                    continue
                if not fp.is_file():
                    continue
                if fp.suffix in _SKIP_SUFFIXES:
                    continue
                content = _read_file(fp)
                entry = f"--- {fp} ---\n{content}"
                files.append(entry)
                total += len(entry)
                if total >= MAX_TOTAL_CHARS:
                    files.append("... (codebase summary truncated — remaining files omitted)")
                    break
            if total >= MAX_TOTAL_CHARS:
                break

        if files:
            sections.append(f"=== {label} ===\n" + "\n\n".join(files))

    return "\n\n".join(sections) if sections else "(no codebase files found)"


# ── Requirements + ADRs ──────────────────────────────────────────────────────

def read_requirements() -> str:
    p = Path("REQUIREMENTS.md")
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def read_adrs() -> str:
    """Concatenate the accepted ADRs from docs/decisions/ (best-effort).

    Absent directory → empty string (the check still runs, just without ADR
    context). README/template files in the directory are skipped.
    """
    d = Path("docs/decisions")
    if not d.is_dir():
        return ""
    parts: list[str] = []
    total = 0
    for fp in sorted(d.glob("*.md")):
        if fp.name.lower() in {"readme.md", "template.md"}:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entry = f"--- {fp} ---\n{text}"
        parts.append(entry)
        total += len(entry)
        if total >= MAX_ADR_CHARS:
            parts.append("... (ADRs truncated — remaining omitted)")
            break
    return "\n\n".join(parts)


# ── Anthropic call ─────────────────────────────────────────────────────────────

def run_traceability(api_key: str, requirements: str, adrs: str, codebase: str) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    adr_section = adrs if adrs else "(no ADRs found in docs/decisions/)"
    user_content = (
        f"## REQUIREMENTS.md\n\n{requirements}\n\n"
        f"## Accepted ADRs (docs/decisions/)\n\n{adr_section}\n\n"
        f"## Codebase summary\n\n{codebase}\n\n"
        "Perform the traceability analysis. Remember: ADR-governed divergences "
        "are intentional, not misalignments."
    )
    for attempt in range(3):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            return message.content[0].text
        except anthropic.RateLimitError:
            if attempt == 2:
                raise
            wait = 70
            print(f"Rate limit hit — waiting {wait}s before retry {attempt + 2}/3 …", flush=True)
            time.sleep(wait)


# ── GitHub comment ─────────────────────────────────────────────────────────────

def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def delete_previous_comments(token: str, repo: str, pr_number: int) -> None:
    with httpx.Client() as client:
        resp = client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
            headers=_gh_headers(token),
            params={"per_page": 100},
        )
        resp.raise_for_status()
        for comment in resp.json():
            if COMMENT_MARKER in comment.get("body", ""):
                client.delete(
                    f"{GITHUB_API}/repos/{repo}/issues/comments/{comment['id']}",
                    headers=_gh_headers(token),
                ).raise_for_status()


def post_comment(token: str, repo: str, pr_number: int, body: str) -> None:
    with httpx.Client() as client:
        resp = client.post(
            f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
            headers=_gh_headers(token),
            json={"body": body},
        )
        resp.raise_for_status()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    pr_number_str = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("REPO", "")

    # No API key (e.g. Dependabot / fork PRs) → skip cleanly, do not fail the job.
    if not api_key:
        print("No ANTHROPIC_API_KEY available — skipping traceability check.")
        return

    missing = [k for k, v in {
        "GITHUB_TOKEN": token,
        "PR_NUMBER": pr_number_str,
        "REPO": repo,
    }.items() if not v]

    if missing:
        print(f"ERROR: missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    requirements = read_requirements()
    if not requirements:
        print("No REQUIREMENTS.md found — skipping traceability check.")
        return

    pr_number = int(pr_number_str)

    adrs = read_adrs()
    print(f"ADRs: {len(adrs)} chars")

    print("Building codebase summary …")
    codebase = build_codebase_summary()
    print(f"Codebase summary: {len(codebase)} chars")

    print(f"Running traceability check (model={MODEL}) …")
    report = run_traceability(api_key, requirements, adrs, codebase)

    comment_body = (
        f"{COMMENT_MARKER}\n"
        f"## Traceability Check\n\n"
        f"> **AI-generated** — advisory only, does not block merge. "
        f"Use this report to catch drift between `REQUIREMENTS.md` and the "
        f"implementation. ADR-governed decisions are treated as intentional. "
        f"Dismiss findings only after confirming they are intentional or "
        f"already mitigated.\n"
        f"> Model: `{MODEL}` | PR: #{pr_number}\n\n"
        f"{report}\n\n"
        f"---\n"
        f"*Canonical check — infra-commons/devops "
        f"(.github/actions/traceability)*"
    )

    MAX_COMMENT_CHARS = 60_000  # GitHub PR comments are capped at 65536 bytes
    if len(comment_body) > MAX_COMMENT_CHARS:
        comment_body = comment_body[:MAX_COMMENT_CHARS] + "\n\n... _(output truncated — see Actions log for full report)_"

    print(f"Posting comment to PR #{pr_number} in {repo} …")
    delete_previous_comments(token, repo, pr_number)
    post_comment(token, repo, pr_number, comment_body)
    print("Done.")


if __name__ == "__main__":
    main()
