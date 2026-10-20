"""Static presentation boundary: no repository browsing or outbound assets."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dashboard_preview", ROOT / "scripts/preview-community-dashboard.py"
)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


@pytest.mark.parametrize(
    "route", ["/", "/index.html", "/assets/dashboard.css", "/assets/dashboard.mjs"]
)
def test_fixed_asset_routes(route):
    target = preview.resolve_asset(route)
    assert target is not None
    assert target.is_relative_to(preview.DASHBOARD_ROOT)


@pytest.mark.parametrize(
    "route",
    [
        "/assets/",
        "/.git/config",
        "/.fliorcie-labs/README.md",
        "/../README.md",
        "/%2e%2e/README.md",
        "/assets/%2e%2e/index.html",
        "/tests/report-model.test.mjs",
        "/README.md",
        "/index.html/",
        "//index.html",
    ],
)
def test_non_asset_paths_are_rejected(route):
    assert preview.resolve_asset(route) is None


def test_symlinked_asset_is_rejected(tmp_path):
    target = tmp_path / "private.txt"
    target.write_text("private", encoding="utf-8")
    root = tmp_path / "dashboard"
    root.mkdir()
    link = root / "index.html"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not available on this Windows host")
    assert preview.resolve_asset("/", root) is None


def test_loopback_and_content_policy():
    assert preview.LOOPBACK == "127.0.0.1"
    assert "connect-src 'none'" in preview.CSP
    assert "script-src 'self'" in preview.CSP
    assert "frame-ancestors 'none'" in preview.CSP
    assert "unsafe-inline" not in preview.CSP


def test_html_has_accessible_controls_and_no_external_assets():
    html = (preview.DASHBOARD_ROOT / "index.html").read_text(encoding="utf-8")
    assert '<html lang="en"' in html
    assert 'aria-live="polite"' in html
    assert 'aria-label="Search observations"' in html
    assert "SYNTHETIC" not in html  # initially empty; demo mode set by user action
    assert 'id="demo"' in html
    assert 'id="export" class="button secondary" disabled' in html
    assert "http://" not in html and "https://" not in html
    assert "onclick=" not in html


def test_browser_app_has_no_network_or_raw_html_sinks():
    source = (preview.DASHBOARD_ROOT / "assets/dashboard.mjs").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "innerHTML",
        "outerHTML",
        "eval(",
        "localStorage",
        "sessionStorage",
        "sendBeacon",
    ):
        assert forbidden not in source
    assert "generation !== state.generation" in source
