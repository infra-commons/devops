#!/usr/bin/env python3
"""Regression test: cloudflare-deploy-reusable.yml / cloudflare-pages-deploy-reusable.yml
must reject an `environment` input that is neither "staging" nor "production",
not silently fall through to the production deploy path.

Both files previously used `if [ "$DEPLOY_ENV" = "staging" ]; then ... else ...`,
so ANY value other than the exact string "staging" — a typo, wrong case, a stray
space — took the else branch and deployed to production with no warning. This
runs the shipped `run:` block itself (extracted from the workflow file), with
`wrangler`/`npm` stubbed on PATH so nothing real is installed or deployed.

Run: python3 scripts/test_cloudflare_deploy_environment.py
"""

import pathlib
import subprocess
import sys
import tempfile

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


def deploy_run_block(path: pathlib.Path) -> str:
    """The last `run: |` block in the file (the "Deploy" step's body)."""
    text = path.read_text()
    start = text.rindex("run: |\n") + len("run: |\n")
    lines = text[start:].splitlines()
    body = [line[10:] for line in lines if line.startswith(" " * 10) or not line.strip()]
    return "\n".join(body)


def run_deploy_step(path: pathlib.Path, deploy_env: str) -> subprocess.CompletedProcess:
    script = deploy_run_block(path)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        wrangler = tmp / "wrangler"
        wrangler.write_text('#!/usr/bin/env bash\necho "wrangler called with: $*"\n')
        wrangler.chmod(0o755)
        env = {
            "PATH": f"{tmp}:/usr/bin:/bin",
            "DEPLOY_ENV": deploy_env,
            "PROJECT_NAME": "proj",
            "DEPLOY_DIR": "dist",
        }
        return subprocess.run(["bash", "-c", script], env=env,
                               capture_output=True, text=True, timeout=15)


WORKERS = WORKFLOW_DIR / "cloudflare-deploy-reusable.yml"
PAGES = WORKFLOW_DIR / "cloudflare-pages-deploy-reusable.yml"


@test("cloudflare-deploy-reusable: 'staging' deploys with --env staging")
def _():
    r = run_deploy_step(WORKERS, "staging")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--env staging" in r.stdout, r.stdout


@test("cloudflare-deploy-reusable: 'production' deploys with no --env flag")
def _():
    r = run_deploy_step(WORKERS, "production")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--env" not in r.stdout, r.stdout


@test("cloudflare-deploy-reusable: a typo'd environment is REJECTED, not silently deployed to prod")
def _():
    r = run_deploy_step(WORKERS, "Staging")
    assert r.returncode != 0, f"typo silently deployed:\n{r.stdout}{r.stderr}"
    assert "must be" in (r.stdout + r.stderr), r.stdout + r.stderr


@test("cloudflare-pages-deploy-reusable: 'staging' deploys to the staging branch")
def _():
    r = run_deploy_step(PAGES, "staging")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--branch staging" in r.stdout, r.stdout


@test("cloudflare-pages-deploy-reusable: 'production' deploys to the main branch")
def _():
    r = run_deploy_step(PAGES, "production")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--branch main" in r.stdout, r.stdout


@test("cloudflare-pages-deploy-reusable: a typo'd environment is REJECTED, not silently deployed to prod")
def _():
    r = run_deploy_step(PAGES, "prod")
    assert r.returncode != 0, f"typo silently deployed:\n{r.stdout}{r.stderr}"
    assert "must be" in (r.stdout + r.stderr), r.stdout + r.stderr


def main() -> int:
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}", file=sys.stderr)
        return 1
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
