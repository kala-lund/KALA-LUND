from __future__ import annotations

"""Static frontend regression checks.

The dashboard is intentionally framework-free: plain HTML/CSS/ES2020 served by
FastAPI or Cloudflare Pages. These tests protect the invariants that make that
safe and deployable without a bundler:

* no inline ``<script>`` or inline event handlers (CSP ``script-src 'self'``),
* assets referenced through ``/static/`` (FastAPI mount + Pages redirects),
* Cloudflare ``_headers`` / ``_redirects`` exist and stay in sync,
* ``node --check`` accepts the shipped JavaScript (skipped when node is absent),
* the build copies runtime files and writes an API base without secrets.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "trader" / "frontend"
BUILD_SCRIPT = ROOT / "scripts" / "build_frontend.js"
NODE = shutil.which("node")


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_index_has_no_inline_script_or_event_handlers():
    html = _read("index.html")
    assert "<script>" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)


def test_index_references_versioned_static_assets():
    html = _read("index.html")
    for asset in ("config.js", "app.js", "styles.css"):
        assert re.search(rf"/static/{re.escape(asset)}\?v=\d+", html), asset
    assert 'href="#main"' in html  # skip link
    assert "aria-live" in html


def test_no_inline_handlers_in_generated_markup():
    js = _read("app.js")
    assert not re.search(r"\son(?:click|change|input|submit|load|error)\s*=", js, re.IGNORECASE)


def test_cloudflare_headers_define_strict_csp():
    headers = _read("_headers")
    assert "Content-Security-Policy:" in headers
    assert "script-src 'self'" in headers
    assert "object-src 'none'" in headers
    assert "frame-ancestors 'none'" in headers
    assert "X-Content-Type-Options: nosniff" in headers
    # Inline style attributes are generated for chart geometry only.
    assert "style-src 'self' 'unsafe-inline'" in headers


def test_cloudflare_redirects_map_static_assets():
    redirects = _read("_redirects")
    for asset in ("config.js", "app.js", "styles.css"):
        assert re.search(rf"^/static/{re.escape(asset)}\s+/{re.escape(asset)}\s+200$",
                         redirects, re.MULTILINE), asset
    assert re.search(r"^/\*\s+/index\.html\s+200$", redirects, re.MULTILINE)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_node_syntax_check():
    for script in (FRONTEND / "app.js", FRONTEND / "config.js", BUILD_SCRIPT):
        proc = subprocess.run([NODE, "--check", str(script)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_build_output_is_deployable_dist():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "dist"
        env = dict(os.environ)
        env.update({
            "API_BASE_URL": "https://api.example.invalid",
            "BUILD_OUTPUT_DIR": str(out),
            "ALLOW_SAME_ORIGIN": "0",
        })
        proc = subprocess.run([NODE, str(BUILD_SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        for name in ("index.html", "app.js", "styles.css", "_headers", "_redirects"):
            assert (out / name).is_file(), name
        config = (out / "config.js").read_text(encoding="utf-8")
        assert json.dumps("https://api.example.invalid") in config
        # The build pins connect-src to the real API origin (no arbitrary https:).
        headers = (out / "_headers").read_text(encoding="utf-8")
        assert "connect-src 'self' https://api.example.invalid;" in headers
        assert "connect-src 'self' https:;" not in headers
        # The bundle must never embed credentials.
        for marker in ("ALPACA", "DATABASE_URL", "postgres://", "sk-"):
            assert marker not in config


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_build_rejects_non_http_api_base():
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env.update({
            "API_BASE_URL": "ftp://nope",
            "BUILD_OUTPUT_DIR": str(Path(tmp) / "dist"),
        })
        proc = subprocess.run([NODE, str(BUILD_SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 1
        assert "API_BASE_URL" in proc.stderr
