#!/usr/bin/env python3
"""Regression test for the stop_reason/truncation guard in traceability_check.py.

Found by three independent review passes: `run_traceability()` returned
`message.content[0].text` with no check of `message.stop_reason`. A response
truncated at `max_tokens` (4096) was posted to the PR verbatim as a complete
`## Traceability Report` — a reviewer seeing no "Misalignments" section reads
that as "none found", not "cut off before reaching it". Advisory-only (never
blocks merge), but it's the exact "degraded result read as clean" defect class
this repo's other guards exist to prevent, reproduced in the check itself.

This imports the shipped module directly (it already depends on `anthropic` +
`httpx`, unlike this repo's stdlib-only test scripts, so importing it here adds
nothing new) and monkeypatches the Anthropic client, so it's the real
`run_traceability()` under test, not a reimplementation.

Run: python3 scripts/test_traceability_check.py
"""

import pathlib
import sys
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "traceability"
sys.path.insert(0, str(ACTION_DIR))

import traceability_check as tc  # noqa: E402

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


def fake_client(text: str, stop_reason: str):
    message = mock.Mock()
    message.content = [mock.Mock(text=text)]
    message.stop_reason = stop_reason
    client = mock.Mock()
    client.messages.create.return_value = message
    return client


@test("a complete response (stop_reason=end_turn) carries no truncation notice")
def _():
    with mock.patch.object(tc.anthropic, "Anthropic",
                            return_value=fake_client("## Report\n\nAll good.", "end_turn")):
        out = tc.run_traceability("key", "reqs", "adrs", "code")
    assert "cut off" not in out, out
    assert out == "## Report\n\nAll good.", out


@test("a truncated response (stop_reason=max_tokens) is flagged, not posted as complete")
def _():
    with mock.patch.object(tc.anthropic, "Anthropic",
                            return_value=fake_client("## Report\n\n### Misalignmen", "max_tokens")):
        out = tc.run_traceability("key", "reqs", "adrs", "code")
    assert "cut off" in out, f"truncated response has no truncation notice:\n{out}"
    assert out.startswith("## Report\n\n### Misalignmen"), "the truncation notice must not replace the partial report"


def main() -> int:
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}", file=sys.stderr)
        return 1
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
