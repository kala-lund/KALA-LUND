"""Guard tests for the Railway deployment configuration.

These tests exist because a real production deploy failed with
``couldn't locate the dockerfile at path Dockerfile.trader in code archive``:
the deploy config referenced a Dockerfile that was not present in the deployed
commit, and the config file itself carried an invalid restart policy plus a
``[deploy.environment]`` table that Railway cannot apply. The checks below make
those failure modes impossible to reintroduce silently.

No network access, no credentials, no broker calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

try:  # Python >= 3.11
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.7-3.10 fallback
    tomllib = pytest.importorskip("tomli")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Railway config-as-code accepts exactly these restart policy values.
VALID_RESTART_POLICIES = {"ON_FAILURE", "ALWAYS", "NEVER"}
# Only these top-level tables are part of the config-as-code schema.
VALID_TOP_LEVEL_KEYS = {"build", "deploy", "environments", "$schema"}

_COPY_RE = re.compile(r"^\s*COPY\s+(?!--from)(.+)$", re.IGNORECASE)
_ENV_RE = re.compile(r"^\s*ENV\s+(.+)$", re.IGNORECASE)
_ENV_PAIR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)")


def _railway_config() -> dict:
    path = os.path.join(REPO_ROOT, "railway.toml")
    assert os.path.isfile(path), "railway.toml is missing from the repository root"
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _dockerfile_lines(filename: str) -> list[str]:
    path = os.path.join(REPO_ROOT, filename)
    assert os.path.isfile(path), f"{filename} is missing from the repository root"
    with open(path, "r", encoding="utf-8") as fh:
        return fh.readlines()


def _dockerfile_env(filename: str) -> dict:
    """Parse every ``ENV KEY=VALUE`` pair on every ENV line (quotes stripped)."""
    env: dict = {}
    for line in _dockerfile_lines(filename):
        m = _ENV_RE.match(line)
        if not m:
            continue
        for key, value in _ENV_PAIR_RE.findall(m.group(1)):
            env[key] = value.strip("\"'")
    return env


def _dockerfile_copy_sources(filename: str) -> list[str]:
    sources: list[str] = []
    for line in _dockerfile_lines(filename):
        m = _COPY_RE.match(line)
        if not m:
            continue
        tokens = [t for t in m.group(1).split() if not t.startswith("--")]
        assert tokens, "malformed COPY instruction"
        sources.extend(tokens[:-1])  # last token is the destination
    return sources


def _git(*args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if not git:
        pytest.skip("git executable not available")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def test_railway_config_schema_is_valid():
    cfg = _railway_config()
    assert set(cfg).issubset(VALID_TOP_LEVEL_KEYS), (
        f"unsupported Railway config tables: {sorted(set(cfg) - VALID_TOP_LEVEL_KEYS)}")

    deploy = cfg.get("deploy", {})
    # Railway cannot set service variables from config-as-code; an "environment"
    # (or "variables") table would be silently ignored.
    assert "environment" not in deploy
    assert "variables" not in deploy
    assert deploy.get("restartPolicyType") in VALID_RESTART_POLICIES, (
        "restartPolicyType must be one of " + ", ".join(sorted(VALID_RESTART_POLICIES)))


def test_railway_dockerfile_path_is_tracked_and_exists():
    build = _railway_config().get("build", {})
    assert build.get("builder") == "DOCKERFILE", (
        "build.builder must be DOCKERFILE so Railway does not fall back to Railpack")
    dockerfile_path = build.get("dockerfilePath")
    assert dockerfile_path, "build.dockerfilePath must be set for Dockerfile.trader"
    assert os.path.isfile(os.path.join(REPO_ROOT, dockerfile_path)), (
        f"build.dockerfilePath={dockerfile_path!r} does not exist in the repository root")

    # The original incident: the config was committed while the Dockerfile was
    # not, so the deployed tree had a dangling reference. A working-tree file is
    # not enough - it must be tracked by git.
    tracked = _git("ls-files", "--error-unmatch", "--", dockerfile_path)
    assert tracked.returncode == 0, (
        f"{dockerfile_path} is referenced by railway.toml but is not tracked by git; "
        "the deploy archive would not contain it")
    ignored = _git("check-ignore", "--", dockerfile_path)
    assert ignored.returncode != 0, f"{dockerfile_path} is ignored by .gitignore"


def test_dockerfile_copies_existing_and_unignored_paths():
    copy_sources = _dockerfile_copy_sources("Dockerfile.trader")
    assert copy_sources, "Dockerfile.trader copies no application files"
    for src in copy_sources:
        assert os.path.exists(os.path.join(REPO_ROOT, src)), (
            f"Dockerfile.trader copies missing path: {src}")
        ignored = _git("check-ignore", "--", src)
        assert ignored.returncode != 0, (
            f"{src} is git-ignored, so it would be missing from the deploy archive")


def test_dockerfile_enforces_paper_only_and_durable_store():
    env = _dockerfile_env("Dockerfile.trader")
    assert env.get("TRADING_MODE") == "paper"
    assert env.get("ENABLE_LIVE_TRADING") == "false"
    assert env.get("ENABLE_PAPER_ORDERS") == "false"
    assert env.get("API_HOST") == "0.0.0.0"
    # Durable state is mandatory in production: never allow an in-memory fallback.
    assert env.get("REQUIRE_DURABLE_STORE") == "true"


def test_railway_json_is_absent_or_consistent():
    """A stray railway.json would compete with railway.toml; if present it must agree."""
    path = os.path.join(REPO_ROOT, "railway.json")
    if not os.path.isfile(path):
        pytest.skip("no railway.json - railway.toml is the single source of truth")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    dockerfile_path = cfg.get("build", {}).get("dockerfilePath")
    assert dockerfile_path, "railway.json build.dockerfilePath must be set"
    assert os.path.isfile(os.path.join(REPO_ROOT, dockerfile_path))
