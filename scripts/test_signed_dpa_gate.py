#!/usr/bin/env python3
"""Tests for .github/workflows/signed-dpa-gate.yml.

The gate had no tests, and had never been observed to fail. A gate that cannot fail is
indistinguishable from one that passes, so most of what follows is negative controls:
each drives a client record that SHOULD be rejected through the gate and asserts it is.

These tests execute the verification logic **out of the workflow file itself**, not out
of a copy. A test that reasons about a re-implementation of the gate is a test about the
re-implementation: it stays green while the shipped workflow rots. Extracting the heredoc
also keeps the reusable self-contained, which matters because a reusable workflow runs in
the caller's checkout and cannot rely on this repository's files being present.

Run: python3 scripts/test_signed_dpa_gate.py
"""

import copy
import datetime
import io
import os
import pathlib
import sys
import textwrap

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard, not a code path
    sys.exit("PyYAML is required: pip install pyyaml")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "signed-dpa-gate.yml"

HEREDOC_OPEN = "python3 - <<'PYEOF'"
HEREDOC_CLOSE = "PYEOF"


def gate_source():
    """The verification program, lifted from the shipped workflow."""
    text = WORKFLOW.read_text()
    if text.count(HEREDOC_OPEN) != 1:
        raise AssertionError(
            f"expected exactly one {HEREDOC_OPEN} block in {WORKFLOW.name}; the "
            "extraction below is no longer unambiguous, so these tests would be "
            "asserting against the wrong program"
        )
    body = text.split(HEREDOC_OPEN, 1)[1].split(HEREDOC_CLOSE, 1)[0]
    body = textwrap.dedent(body)
    if "signed_original_link" not in body:
        raise AssertionError("extracted block does not look like the DPA gate")
    return compile(body, str(WORKFLOW), "exec")


GATE = gate_source()


PASSED, REJECTED, CRASHED = "passed", "rejected", "crashed"


def run_gate(client, slug="acme", max_age_days=365):
    """Run the gate against one client document.

    Returns (verdict, output) where verdict is PASSED, REJECTED or CRASHED.

    CRASHED is kept distinct from REJECTED deliberately. In the workflow both fail the
    step, so both block the deploy and it is tempting to treat them as one — but a
    traceback gives the operator no ::error:: annotation and no idea what to fix, and a
    test that accepts "it blew up" as a pass would be satisfied by a gate that is simply
    broken. An earlier version of this file conflated them and a mutation that deleted a
    real rule read as green.
    """
    os.environ["SLUG"] = slug
    os.environ["MAX_AGE_DAYS"] = str(max_age_days)
    serialised = yaml.safe_dump(client)
    captured = io.StringIO()
    namespace = {
        "__name__": "__main__",
        "open": lambda *a, **k: io.StringIO(serialised),
    }
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        exec(GATE, namespace)  # noqa: S102 - executing the artefact under test is the point
        return PASSED, captured.getvalue()
    except SystemExit as exc:
        return (PASSED if exc.code in (0, None) else REJECTED), captured.getvalue()
    except Exception as exc:  # noqa: BLE001 - any failure mode is a finding here
        return CRASHED, captured.getvalue() + f"\n<uncaught {type(exc).__name__}: {exc}>"
    finally:
        sys.stdout = real_stdout


TODAY = datetime.date.today()
RECENT = (TODAY - datetime.timedelta(days=30)).isoformat()

# A record that passes, so every case below is one deliberate mutation away from green
# and no test can pass for an incidental reason.
SIGNED = {
    "tags": {"data_class": "confidential"},
    "contracts": {
        "dpa": {
            "status": "signed",
            "signed_date": "2026-01-15",
            "version": "1.0",
            "signed_original_link": "https://drive.example.com/open?id=EXAMPLE",
            "sub_processors_current_as_of": RECENT,
        }
    },
}

SELF_ATTESTED = {
    "tags": {"data_class": "internal"},
    "contracts": {
        "dpa": {
            "status": "self_attested",
            "signed_date": "2026-01-15",
            "version": "1.0",
            "sub_processors_current_as_of": RECENT,
        }
    },
}


def without(doc, *path):
    out = copy.deepcopy(doc)
    node = out
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return out


def with_dpa(doc, **fields):
    out = copy.deepcopy(doc)
    out["contracts"]["dpa"].update(fields)
    return out


TESTS = []


def test(name):
    def register(fn):
        TESTS.append((name, fn))
        return fn

    return register


def assert_rejected(client, because, slug="acme"):
    """Assert the gate rejects CLEANLY: a non-zero exit carrying an ::error:: line."""
    verdict, output = run_gate(client, slug=slug)
    assert verdict != PASSED, f"gate ACCEPTED a record it must reject ({because}).\n{output}"
    assert verdict != CRASHED, (
        f"gate CRASHED instead of rejecting ({because}). It blocks the deploy either way, "
        f"but a traceback tells the operator nothing about what to fix.\n{output}"
    )
    assert "::error::" in output, f"rejection carried no ::error:: annotation.\n{output}"
    return output


def assert_accepted(client, slug="acme"):
    verdict, output = run_gate(client, slug=slug)
    assert verdict == PASSED, f"gate did not accept a valid record ({verdict}).\n{output}"
    return output


# ── Baselines ───────────────────────────────────────────────────────────────────


@test("a fully evidenced signed DPA passes")
def _():
    out = assert_accepted(SIGNED)
    assert "DPA gate passed" in out, out


@test("a self_attested record on internal data passes")
def _():
    out = assert_accepted(SELF_ATTESTED)
    assert "DPA gate passed" in out, out


# ── The evidence rule: `signed` must point at the executed document ──────────────
#
# THE negative control. Before this rule the gate passed on exactly this input, which is
# why it had never been seen to go red.


@test("NEGATIVE CONTROL: signed with NO signed_original_link is rejected")
def _():
    out = assert_rejected(
        without(SIGNED, "contracts", "dpa", "signed_original_link"),
        "signed with no link is the defect this gate exists to catch",
    )
    assert "signed_original_link" in out, out


@test("NEGATIVE CONTROL: signed with an empty link is rejected")
def _():
    assert_rejected(with_dpa(SIGNED, signed_original_link="   "), "whitespace is not a link")


@test("NEGATIVE CONTROL: signed with a null link is rejected")
def _():
    assert_rejected(with_dpa(SIGNED, signed_original_link=None), "null is not a link")


@test("NEGATIVE CONTROL: signed with a non-URL string is rejected")
def _():
    out = assert_rejected(
        with_dpa(SIGNED, signed_original_link="ask Legal for a copy"),
        "prose is not a link",
    )
    assert "well-formed" in out, out


@test("NEGATIVE CONTROL: a scheme-less link is rejected")
def _():
    assert_rejected(
        with_dpa(SIGNED, signed_original_link="drive.example.com/open?id=X"),
        "a bare host is not an absolute URL",
    )


@test("NEGATIVE CONTROL: a non-http scheme is rejected")
def _():
    assert_rejected(
        with_dpa(SIGNED, signed_original_link="mailto:legal@example.com"),
        "an email address is not a retrievable document",
    )


@test("NEGATIVE CONTROL: signed with no signed_date is rejected")
def _():
    out = assert_rejected(
        without(SIGNED, "contracts", "dpa", "signed_date"), "no execution date"
    )
    assert "signed_date" in out, out


@test("NEGATIVE CONTROL: signed with no version is rejected")
def _():
    out = assert_rejected(without(SIGNED, "contracts", "dpa", "version"), "no version")
    assert "version" in out, out


@test("every missing evidence field is reported in one run, not just the first")
def _():
    bare = copy.deepcopy(SIGNED)
    for field in ("signed_original_link", "signed_date", "version"):
        del bare["contracts"]["dpa"][field]
    out = assert_rejected(bare, "incomplete record")
    for field in ("signed_original_link", "signed_date", "version"):
        assert field in out, f"{field} not reported\n{out}"


@test("an http link is accepted as well as https")
def _():
    assert_accepted(with_dpa(SIGNED, signed_original_link="http://docs.example.com/dpa.pdf"))


# ── self_attested is confined to the operator's own data ────────────────────────


@test("NEGATIVE CONTROL: self_attested on confidential data is rejected")
def _():
    out = assert_rejected(
        {"tags": {"data_class": "confidential"}, "contracts": {"dpa": SELF_ATTESTED["contracts"]["dpa"]}},
        "a counterparty's data needs a counterparty agreement",
    )
    assert "self_attested" in out and "confidential" in out, out


@test("NEGATIVE CONTROL: self_attested on restricted data is rejected")
def _():
    assert_rejected(
        {"tags": {"data_class": "restricted"}, "contracts": {"dpa": SELF_ATTESTED["contracts"]["dpa"]}},
        "restricted is not the operator's own data",
    )


@test("NEGATIVE CONTROL: self_attested with data_class unset fails CLOSED")
def _():
    out = assert_rejected(
        {"contracts": {"dpa": SELF_ATTESTED["contracts"]["dpa"]}},
        "an absent data_class must not be read as permission",
    )
    assert "unset" in out, out


@test("NEGATIVE CONTROL: self_attested with no tags block at all fails CLOSED")
def _():
    assert_rejected(
        {"tags": None, "contracts": {"dpa": SELF_ATTESTED["contracts"]["dpa"]}},
        "a null tags block must not crash or pass",
    )


@test("self_attested does NOT require a link (that is the whole point)")
def _():
    doc = copy.deepcopy(SELF_ATTESTED)
    assert "signed_original_link" not in doc["contracts"]["dpa"]
    assert_accepted(doc)


# ── Behaviour preserved: every status rejected before is still rejected ─────────
#
# Widening the accepted set from one value to two must change the verdict for
# `self_attested` and for nothing else.


@test("statuses rejected before this change are still rejected")
def _():
    for status in ("none", "sent", "superseded", "pending", ""):
        assert_rejected(with_dpa(SIGNED, status=status), f"status {status!r}")


@test("a missing status is still rejected")
def _():
    assert_rejected(without(SIGNED, "contracts", "dpa", "status"), "no status at all")


@test("a client with no contracts block is still rejected")
def _():
    out = assert_rejected({"tags": {"data_class": "confidential"}}, "no contracts")
    assert "contracts" in out, out


@test("a client with no dpa block is still rejected")
def _():
    out = assert_rejected(
        {"tags": {"data_class": "confidential"}, "contracts": {"msa": {"status": "signed"}}},
        "no dpa",
    )
    assert "contracts.dpa" in out, out


@test("a stale sub-processor disclosure is still rejected, for both statuses")
def _():
    stale = (TODAY - datetime.timedelta(days=400)).isoformat()
    assert_rejected(with_dpa(SIGNED, sub_processors_current_as_of=stale), "stale disclosure")
    assert_rejected(
        with_dpa(SELF_ATTESTED, sub_processors_current_as_of=stale),
        "self_attested does not exempt the disclosure age check",
    )


@test("a missing sub-processor disclosure is still rejected")
def _():
    assert_rejected(
        without(SIGNED, "contracts", "dpa", "sub_processors_current_as_of"), "no disclosure date"
    )


@test("max_sub_processor_age_days: 0 still disables the age check only")
def _():
    stale = (TODAY - datetime.timedelta(days=4000)).isoformat()
    verdict, out = run_gate(with_dpa(SIGNED, sub_processors_current_as_of=stale), max_age_days=0)
    assert verdict == PASSED, out
    # ...and does not become a way to skip the evidence rule.
    verdict, out = run_gate(
        without(SIGNED, "contracts", "dpa", "signed_original_link"), max_age_days=0
    )
    assert verdict == REJECTED, f"age check disabled also disabled the evidence rule\n{out}"


# ── The log has to distinguish the two ways of passing ──────────────────────────
#
# If a pass renders identically for an executed agreement and for a record with no
# counterparty, the operator reading the log is back where they started.


@test("a signed pass names the document; a self_attested pass says there is none")
def _():
    signed_out = assert_accepted(SIGNED)
    attested_out = assert_accepted(SELF_ATTESTED)
    assert "executed document" in signed_out, signed_out
    assert "https://drive.example.com/open?id=EXAMPLE" in signed_out, signed_out
    assert "WITHOUT an executed DPA" in attested_out, attested_out
    assert "executed document" not in attested_out, attested_out
    assert signed_out != attested_out


@test("no input shape makes the gate crash instead of deciding")
def _():
    # The gate reads a file from another repository, so it does not get to assume the
    # shape is sane. Every case here must end in a clean pass or a clean ::error::,
    # never a traceback: an unexplained step failure is indistinguishable from an
    # infrastructure problem and gets retried rather than fixed.
    hostile = [
        ("empty document", {}),
        ("null contracts", {"contracts": None}),
        ("contracts is a list", {"contracts": []}),
        ("dpa is a string", {"contracts": {"dpa": "signed"}}),
        ("status is a number", {"contracts": {"dpa": {"status": 1}}}),
        ("link is a list", with_dpa(SIGNED, signed_original_link=["https://x.example"])),
        ("link is a number", with_dpa(SIGNED, signed_original_link=42)),
        ("tags is a string", {"tags": "internal", "contracts": {"dpa": {"status": "self_attested"}}}),
        ("version is null", with_dpa(SIGNED, version=None)),
        ("signed_date is null", with_dpa(SIGNED, signed_date=None)),
        ("sub_processors_current_as_of is not a date", with_dpa(SIGNED, sub_processors_current_as_of="TBD")),
        ("sub_processors_current_as_of is wrong format", with_dpa(SIGNED, sub_processors_current_as_of="2024/01/01")),
    ]
    for label, doc in hostile:
        verdict, output = run_gate(doc)
        assert verdict != CRASHED, f"{label} crashed the gate:\n{output}"


@test("a malformed sub_processors_current_as_of is rejected, not crashed or ignored")
def _():
    out = assert_rejected(
        with_dpa(SIGNED, sub_processors_current_as_of="TBD"), "unparseable disclosure date"
    )
    assert "TBD" in out, out


def main():
    failures = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        # Catching Exception, not just AssertionError: an unexpected error inside one
        # test must not abort the run. When it did, every later test silently never ran
        # and the absence of FAIL lines read as a clean sheet.
        except Exception as exc:  # noqa: BLE001
            failures += 1
            label = "FAIL" if isinstance(exc, AssertionError) else "ERROR"
            print(f"  {label}  {name}\n        {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
