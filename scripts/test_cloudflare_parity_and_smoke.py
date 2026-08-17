#!/usr/bin/env python3
"""Tests for three reusables with no prior test coverage: cloudflare-env-parity.yml,
cloudflare-worker-env-parity.yml, and smoke-test-cloudflare.yml.

All three shared the same defect-class-1 shape: a Cloudflare API call (or a JSON
decode) that returns an unexpected empty/malformed result was read as a *matching*
or *clean* result rather than an unconfirmed one, because "zero items on both
sides" and "zero items because nothing was actually read" produce the identical
comparison. Concretely: `success: true` with a response shape the script doesn't
recognize (renamed/restructured `result`/`deployment_configs`) collapsed both env
comparisons to the empty set and printed "OK — 0 vars/secrets match"; an empty or
malformed `pages` input made the smoke-test loop run zero iterations and print
"All smoke tests passed" having curled nothing. These are negative controls for
the fix, run against the shipped heredoc/script text itself (extracted from the
workflow files), not a reimplementation.

Run: python3 scripts/test_cloudflare_parity_and_smoke.py
"""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

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


# ── cloudflare-env-parity.yml / cloudflare-worker-env-parity.yml ────────────────
#
# Extract the python heredoc from each workflow and exec it in-process with
# urllib.request.urlopen patched, so the same code that ships is what's tested.

def heredoc_source(path: pathlib.Path) -> str:
    text = path.read_text()
    start = text.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n")
    end = text.index("\n          PY", start)
    block = text[start:end]
    return textwrap.dedent(block)


def run_heredoc(path: pathlib.Path, env: dict, fake_response: dict):
    src = heredoc_source(path)
    fake_bytes = json.dumps(fake_response).encode()

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        return FakeResp(fake_bytes)

    out = io.StringIO()
    old_environ = dict(os.environ)
    os.environ.update(env)
    exit_code = 0
    try:
        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             contextlib.redirect_stdout(out):
            try:
                exec(compile(src, str(path), "exec"), {"__name__": "__main__"})
            except SystemExit as exc:
                exit_code = exc.code or 0
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
    return exit_code, out.getvalue()


ENV_PARITY = WORKFLOW_DIR / "cloudflare-env-parity.yml"
WORKER_ENV_PARITY = WORKFLOW_DIR / "cloudflare-worker-env-parity.yml"

BASE_ENV = {
    "CLOUDFLARE_API_TOKEN": "tok",
    "CLOUDFLARE_ACCOUNT_ID": "acct",
    "PROJECT_NAME": "proj",
    "ENVS_JSON": '["preview", "production"]',
}

WORKER_BASE_ENV = {
    "CLOUDFLARE_API_TOKEN": "tok",
    "CLOUDFLARE_ACCOUNT_ID": "acct",
    "SCRIPTS_JSON": '["svc-preview", "svc-production"]',
}


@test("cloudflare-env-parity: a matching, non-empty var set on both sides passes")
def _():
    resp = {"success": True, "result": {"deployment_configs": {
        "preview": {"env_vars": {"A": {}, "B": {}}},
        "production": {"env_vars": {"A": {}, "B": {}}},
    }}}
    code, out = run_heredoc(ENV_PARITY, BASE_ENV, resp)
    assert code == 0, out


@test("cloudflare-env-parity: a genuine mismatch still fails")
def _():
    resp = {"success": True, "result": {"deployment_configs": {
        "preview": {"env_vars": {"A": {}}},
        "production": {"env_vars": {"A": {}, "B": {}}},
    }}}
    code, out = run_heredoc(ENV_PARITY, BASE_ENV, resp)
    assert code != 0, out


@test("cloudflare-env-parity: success=true with missing deployment_configs is REJECTED, not read as a match")
def _():
    resp = {"success": True, "result": {}}
    code, out = run_heredoc(ENV_PARITY, BASE_ENV, resp)
    assert code != 0, f"schema drift silently passed as a match:\n{out}"
    assert "ZERO env vars" in out, out


@test("cloudflare-worker-env-parity: a matching, non-empty secret set on both sides passes")
def _():
    resp = {"success": True, "result": [{"name": "X"}, {"name": "Y"}]}
    code, out = run_heredoc(WORKER_ENV_PARITY, WORKER_BASE_ENV, resp)
    assert code == 0, out


@test("cloudflare-worker-env-parity: success=true with missing result is REJECTED, not read as a match")
def _():
    resp = {"success": True}
    code, out = run_heredoc(WORKER_ENV_PARITY, WORKER_BASE_ENV, resp)
    assert code != 0, f"schema drift silently passed as a match:\n{out}"
    assert "ZERO secrets" in out, out


# ── smoke-test-cloudflare.yml ────────────────────────────────────────────────
#
# This one is a bash `run:` block, not python — extract and execute it directly
# under bash with a stubbed `curl` on PATH so no real network call happens.

def run_smoke_test(pages_json: str, curl_status: str = "200") -> subprocess.CompletedProcess:
    text = (WORKFLOW_DIR / "smoke-test-cloudflare.yml").read_text()
    start = text.index("run: |\n") + len("run: |\n")
    lines = text[start:].splitlines()
    # The run: block is indented 10 spaces (step body under `run: |`); collect
    # until the indentation drops back to the step-list level.
    body = []
    for line in lines:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line[10:] if line.startswith(" " * 10) else line)
    script = "\n".join(body)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        curl = tmp / "curl"
        curl.write_text(f'#!/usr/bin/env bash\necho -n "{curl_status}"\n')
        curl.chmod(0o755)
        env = {
            "PATH": f"{tmp}:/usr/bin:/bin",
            "BASE_URL": "https://example.invalid",
            "PAGES_JSON": pages_json,
            "MAX_RETRIES": "1",
        }
        return subprocess.run(["bash", "-c", script], env=env,
                               capture_output=True, text=True, timeout=30)


@test("smoke-test-cloudflare: a non-empty pages array with all-200s passes")
def _():
    r = run_smoke_test('["/", "/about.html"]', curl_status="200")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All smoke tests passed" in r.stdout, r.stdout


@test("smoke-test-cloudflare: an empty pages array is REJECTED, not read as a pass")
def _():
    r = run_smoke_test("[]")
    assert r.returncode != 0, f"empty pages array silently passed:\n{r.stdout}{r.stderr}"
    assert "No pages were tested" in r.stdout + r.stderr, r.stdout + r.stderr


@test("smoke-test-cloudflare: a genuine failing page still fails")
def _():
    r = run_smoke_test('["/"]', curl_status="500")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "smoke tests failed" in r.stdout, r.stdout


def main() -> int:
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}", file=sys.stderr)
        return 1
    print(f"\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
