#!/usr/bin/env python3
"""Tests for the fork-PR exposure gate in register-ado-agent.sh.

Regression coverage for CRITICAL finding 11 (reviews/2026-08-17-tier1-devops-815.md,
lines 243-259): `register-ado-agent.sh` used to register unconditionally against
whatever --org/--pool it was given, with no visibility/exposure gate at all, unlike
its three GitHub-runner siblings.

Azure DevOps's fork-PR security model differs from GitHub's: fork-PR builds don't
get secrets by default regardless of project visibility (an admin must separately
opt in via "Make secrets available to builds of forks" /
enforceNoAccessToSecretsFromForks), so porting the GitHub siblings' visibility-only
gate verbatim would check a real signal but miss the actual attack-relevant one.
The decisive, queryable ADO-native field is `buildsEnabledForForks` on the
project's pipeline general settings
(https://dev.azure.com/{org}/{project}/_apis/build/generalsettings) — it gates
whether a fork PR can run code on the pool's agents AT ALL, independent of secrets.
The gate here checks both project visibility (must be "private") and
buildsEnabledForForks (must be "false"), failing closed on anything unreadable.

These tests execute the shipped script itself (via subprocess, with a mock `az` on
PATH), not a reimplementation, so they stay honest about what actually ships. No
real `az`/network calls; --apply is never passed, so nothing outside the sandboxed
PATH/HOME is touched.

Run: python3 scripts/test_ado_agent_gate.py
"""

import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER_DIR = REPO_ROOT / "self-hosted-runner"
SCRIPT = "register-ado-agent.sh"

# Everything the script's pre-gate/gate code path needs besides `az` itself.
# PATH is built hermetically (this dir ONLY) so a real `az` on the host's
# actual PATH can never leak in and mask an az_present=False test.
BASH = shutil.which("bash")
_NEEDED_TOOLS = ["bash", "cut", "tr", "hostname"]

# Mock `az`: branches on the first three words (`devops project show` vs
# `devops invoke`) and an AZ_MOCK_SCENARIO env var, mirroring test_self_hosted_
# runner_guards.py's MOCK_GH style. Each scenario name below describes the pair
# of reads the gate makes per --project, in order: (visibility, generalsettings).
MOCK_AZ = r"""#!/usr/bin/env bash
if [ "$1" = "devops" ] && [ "$2" = "project" ] && [ "$3" = "show" ]; then
  case "$AZ_MOCK_SCENARIO" in
    safe|safe-two)          echo "private"; exit 0 ;;
    forks-on)                echo "private"; exit 0 ;;
    public)                  echo "public"; exit 0 ;;
    vis-error)                echo "az: 403 forbidden" >&2; exit 1 ;;
    settings-error)           echo "private"; exit 0 ;;
    second-bad)
      # first --project call returns private (good), second returns public (bad)
      if [ -f "$AZ_MOCK_STATE/vis-calls" ]; then
        echo "public"; exit 0
      else
        : > "$AZ_MOCK_STATE/vis-calls"
        echo "private"; exit 0
      fi
      ;;
    *) echo "mock az: unhandled scenario '$AZ_MOCK_SCENARIO' (project show)" >&2; exit 9 ;;
  esac
elif [ "$1" = "devops" ] && [ "$2" = "invoke" ]; then
  case "$AZ_MOCK_SCENARIO" in
    safe|safe-two|second-bad) printf 'false\tfalse\n'; exit 0 ;;
    forks-on)                 printf 'true\tfalse\n'; exit 0 ;;
    settings-error)            echo "az: 403 forbidden" >&2; exit 1 ;;
    *) echo "mock az: unhandled scenario '$AZ_MOCK_SCENARIO' (invoke)" >&2; exit 9 ;;
  esac
else
  echo "mock az: unhandled args: $*" >&2; exit 9
fi
"""


def _run(args: list, scenario: str, az_present: bool = True) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        bindir = tmp / "bin"
        bindir.mkdir()
        for tool in _NEEDED_TOOLS:
            real = shutil.which(tool)
            assert real, f"test host is missing required tool: {tool}"
            (bindir / tool).symlink_to(real)
        if az_present:
            az = bindir / "az"
            az.write_text(MOCK_AZ)
            az.chmod(0o755)
        state = tmp / "state"
        state.mkdir()
        home = tmp / "home"
        home.mkdir()

        # PATH is ONLY this hermetic bindir — no fallback to the host's real
        # /usr/bin:/bin, so a real `az` installed on the box (as on this test
        # host) can never leak in and silently pass an az_present=False case.
        env = {
            "PATH": str(bindir),
            "AZ_MOCK_SCENARIO": scenario,
            "AZ_MOCK_STATE": str(state),
            "HOME": str(home),
        }

        return subprocess.run(
            [BASH, str(RUNNER_DIR / SCRIPT), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )


BASE_ARGS = ["--org", "acme", "--pool", "beelink-ado", "--user", "runner", "--token", "tok"]


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{': ' + detail if detail and not condition else ''}")
    return condition


def test_gate():
    print("register-ado-agent.sh — exposure gate")
    failures = []

    r = _run(BASE_ARGS + ["--project", "Widgets"], "safe")
    failures.append(not check(
        "private project with buildsEnabledForForks=false is ACCEPTED",
        r.returncode == 0 and "REFUSING" not in r.stderr,
        f"returncode={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS + ["--project", "Widgets"], "public")
    failures.append(not check(
        "a PUBLIC project is REFUSED",
        r.returncode == 3 and "PUBLIC" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS + ["--project", "Widgets"], "forks-on")
    failures.append(not check(
        "a private project with buildsEnabledForForks=true is REFUSED "
        "(the case a visibility-only port would have missed entirely)",
        r.returncode == 3 and "fork-PR builds disabled" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS + ["--project", "Widgets"], "vis-error")
    failures.append(not check(
        "a failed visibility read is REFUSED, not read as not-public",
        r.returncode == 3 and "could not confirm" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS + ["--project", "Widgets"], "settings-error")
    failures.append(not check(
        "a failed generalsettings read is REFUSED, not read as forks-disabled",
        r.returncode == 3 and "could not be confirmed" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS, "safe")
    failures.append(not check(
        "no --project given is REFUSED",
        r.returncode == 3 and "--project" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS + ["--project", "Widgets"], "public", az_present=False)
    failures.append(not check(
        "'az' missing from PATH is REFUSED",
        r.returncode == 3 and "'az' CLI is not on PATH" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS + ["--project", "Widgets", "--i-understand-public"], "public", az_present=False)
    failures.append(not check(
        "--i-understand-public skips the gate entirely (no 'az' needed, public accepted)",
        r.returncode == 0 and "gate SKIPPED" in r.stdout and "REFUSING" not in r.stderr,
        f"returncode={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}",
    ))

    r = _run(BASE_ARGS + ["--project", "Widgets", "--project", "Gadgets"], "second-bad")
    failures.append(not check(
        "a second --project that fails is REFUSED, not short-circuited past after the first passes",
        r.returncode == 3 and "PUBLIC" in r.stderr,
        f"returncode={r.returncode} stderr={r.stderr!r}",
    ))

    return not any(failures)


def main() -> int:
    ok = test_gate()
    if ok:
        print("\nAll register-ado-agent.sh exposure-gate tests passed.")
        return 0
    print("\nFAIL: at least one register-ado-agent.sh exposure-gate test failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
