#!/usr/bin/env python3
"""Tests for the org-scope runner-group gate in the self-hosted-runner scripts.

Regression test for a real bug found by executing (not just reading) the fork-PR
RCE guard: `register-gh-runner.sh` and `gh-runner-light-register.sh` both stored
the `gh api .../runner-groups` response in a shell variable named `GROUPS`. Bash
reserves that name for a special, effectively read-only array holding the calling
process's own supplementary group IDs — assigning to it is silently discarded, so
`$GROUPS` kept reading back something like "1000" (a real GID) instead of the API
response. The match against the operator-supplied `--group` name could then never
succeed, so a correctly configured, safe runner group was refused exactly like an
unsafe one — permanently, not just on error paths. That is fail-closed in effect
(it never lets an unsafe config through), but it means org-scope registration can
never succeed at all, which is exactly the shape of guard an operator learns to
route around with `--i-understand-public` — disarming the real protection.
Reproduce the collision directly: `bash -c 'GROUPS="x"; echo "$GROUPS"'` prints a
GID, never "x".

These tests execute the shipped scripts themselves (via subprocess, with a mock
`gh` on PATH), not a reimplementation, so they stay honest about what actually
ships. No real `gh`/network calls; --apply is never passed, so nothing outside
the sandboxed PATH/HOME is touched.

Run: python3 scripts/test_self_hosted_runner_guards.py
"""

import pathlib
import re
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER_DIR = REPO_ROOT / "self-hosted-runner"

MOCK_GH = """#!/usr/bin/env bash
# Mock gh CLI: behavior selected via $GH_MOCK_SCENARIO, keyed by which
# runner-groups endpoint is hit (there's only one shape used by these tests).
[ "$1" = "api" ] || { echo "mock gh: unhandled verb $1" >&2; exit 9; }
case "$GH_MOCK_SCENARIO" in
  org-groups-good)          printf 'beelink-light\\tselected\\tfalse\\n'; exit 0 ;;
  org-groups-allowspublic)  printf 'beelink-light\\tselected\\ttrue\\n'; exit 0 ;;
  org-groups-notfound)      printf 'other-group\\tselected\\tfalse\\n'; exit 0 ;;
  org-groups-error)         echo "gh: 403 forbidden" >&2; exit 1 ;;
  *) echo "mock gh: unhandled scenario '$GH_MOCK_SCENARIO'" >&2; exit 9 ;;
esac
"""

MOCK_SYSTEMCTL = """#!/usr/bin/env bash
# Mock systemctl: only the one --user cat call gh-runner-light-register.sh's
# preflight makes needs to succeed.
exit 0
"""


def _run(script: str, args: list, scenario: str, env_extra: dict = None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        bindir = tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(MOCK_GH)
        gh.chmod(0o755)
        systemctl = bindir / "systemctl"
        systemctl.write_text(MOCK_SYSTEMCTL)
        systemctl.chmod(0o755)

        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "GH_MOCK_SCENARIO": scenario,
            "HOME": str(tmp / "home"),
        }
        (tmp / "home").mkdir()
        if env_extra:
            env.update(env_extra)

        return subprocess.run(
            ["bash", str(RUNNER_DIR / script), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )


_GROUPS_ASSIGNMENT_RE = re.compile(r"(?<![A-Za-z0-9_])GROUPS=")


def _assigns_to_groups(path: pathlib.Path) -> bool:
    """True if any non-comment line assigns to a variable literally named
    GROUPS (word-boundary match, so RUNNER_GROUPS= doesn't false-positive)."""
    for line in path.read_text().splitlines():
        if line.strip().startswith("#"):
            continue
        if _GROUPS_ASSIGNMENT_RE.search(line):
            return True
    return False


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{': ' + detail if detail and not condition else ''}")
    return condition


def test_register_gh_runner_org_scope():
    print("register-gh-runner.sh — org-scope runner-group gate")
    failures = []

    r = _run("register-gh-runner.sh", ["--org", "acme", "--group", "beelink-light",
                                        "--label", "x", "--user", "runner"], "org-groups-good")
    failures.append(not check(
        "a correctly configured (selected, allows_public_repositories=false) group is ACCEPTED",
        r.returncode == 0 and "REFUSING" not in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run("register-gh-runner.sh", ["--org", "acme", "--group", "beelink-light",
                                        "--label", "x", "--user", "runner"], "org-groups-allowspublic")
    failures.append(not check(
        "a group with allows_public_repositories=true is REFUSED",
        r.returncode == 3 and "allows public repositories" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run("register-gh-runner.sh", ["--org", "acme", "--group", "beelink-light",
                                        "--label", "x", "--user", "runner"], "org-groups-notfound")
    failures.append(not check(
        "a --group name absent from the API response is REFUSED",
        r.returncode == 3 and "could not confirm runner group" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run("register-gh-runner.sh", ["--org", "acme", "--group", "beelink-light",
                                        "--label", "x", "--user", "runner"], "org-groups-error")
    failures.append(not check(
        "a failed runner-groups API read is REFUSED, not read as absent",
        r.returncode == 3 and "could not confirm runner group" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    failures.append(not check(
        "no shell variable in this script is literally named GROUPS (the bug's root cause)",
        not _assigns_to_groups(RUNNER_DIR / "register-gh-runner.sh"),
    ))

    return not any(failures)


def test_gh_runner_light_register_org_scope():
    print("gh-runner-light-register.sh — org-scope runner-group gate")
    failures = []

    # _run's HOME is a fresh tempdir per call, but the slot needs to be staged
    # (config.sh/.env/job-cleanup.sh present) before the script's own preflight
    # will let it reach the gate under test, so pre-stage one here and point
    # HOME at it via env_extra rather than the one _run creates.
    with tempfile.TemporaryDirectory() as home_tmp:
        home = pathlib.Path(home_tmp)
        slot = home / "gh-runner-2"
        slot.mkdir()
        (slot / "config.sh").write_text("#!/usr/bin/env bash\n")
        (slot / "config.sh").chmod(0o755)
        (slot / ".env").write_text("PYTHONNOUSERSITE=1\n")
        (slot / "job-cleanup.sh").write_text("#!/usr/bin/env bash\n")
        (slot / "job-cleanup.sh").chmod(0o755)

        r = _run("gh-runner-light-register.sh",
                  ["--slot", "2", "--org", "acme", "--group", "beelink-light"],
                  "org-groups-good", env_extra={"HOME": str(home)})
        failures.append(not check(
            "a correctly configured group is ACCEPTED (preflight passes, gate passes)",
            r.returncode == 0 and "REFUSING" not in r.stderr,
            f"returncode={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}",
        ))

        r = _run("gh-runner-light-register.sh",
                  ["--slot", "2", "--org", "acme", "--group", "beelink-light"],
                  "org-groups-allowspublic", env_extra={"HOME": str(home)})
        failures.append(not check(
            "a group with allows_public_repositories=true is REFUSED",
            r.returncode == 3 and "allows public repositories" in r.stderr,
            f"returncode={r.returncode} stderr={r.stderr!r}",
        ))

    failures.append(not check(
        "no shell variable in this script is literally named GROUPS (the bug's root cause)",
        not _assigns_to_groups(RUNNER_DIR / "gh-runner-light-register.sh"),
    ))

    return not any(failures)


def main() -> int:
    ok = True
    ok &= test_register_gh_runner_org_scope()
    ok &= test_gh_runner_light_register_org_scope()
    if ok:
        print("\nAll self-hosted-runner org-scope gate tests passed.")
        return 0
    print("\nFAIL: at least one self-hosted-runner org-scope gate test failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
