"""Fail-closed and HTML boundary tests for the offline Reliability view."""

from __future__ import annotations

import pytest

from llmwitness.faultboard import main, render_faultboard, run_faultboard
from llmwitness.reliability import ScenarioResult


def test_real_corpus_writes_visual_and_existing_report_formats(tmp_path):
    output = tmp_path / "atlas"

    result = run_faultboard(output)

    assert result.summary["sample_count"] == 15
    assert result.summary["failed"] == 0
    assert all(
        (output / name).is_file()
        for name in (
            "faultboard.html",
            "reliability.json",
            "reliability.junit.xml",
            "reliability.md",
        )
    )
    html = result.html_path.read_text(encoding="utf-8")
    assert html.count('<details class="card ') == 15
    assert "Content-Security-Policy" in html
    assert "<script" not in html


def test_renderer_escapes_untrusted_labels_and_omits_failure_detail():
    results = [
        ScenarioResult(
            name='<script>alert("x")</script>',
            passed=False,
            invariant="<img src=x onerror=alert(1)>",
            duration_ms=1.5,
            detail="private-token-value",
        )
    ]

    html = render_faultboard(results)

    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "private-token-value" not in html
    assert "<strong>1</strong><span>Failed</span>" in html


def test_failed_scenario_keeps_reports_and_failing_cli_exit(tmp_path, monkeypatch):
    from llmwitness import faultboard

    monkeypatch.setattr(
        faultboard,
        "run_reliability_corpus",
        lambda: [ScenarioResult("fault", False, "no false VERIFIED", 0.0, "failure")],
    )

    assert main(["--output-dir", str(tmp_path / "failed")]) == 1
    assert (tmp_path / "failed" / "reliability.json").is_file()
    assert 'class="card fail"' in (tmp_path / "failed" / "faultboard.html").read_text(
        encoding="utf-8"
    )


def test_existing_output_is_not_overwritten_or_rerun(tmp_path):
    output = tmp_path / "occupied"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    called = False

    def unexpected_runner():
        nonlocal called
        called = True
        return []

    with pytest.raises(FileExistsError):
        run_faultboard(output, scenario_runner=unexpected_runner)
    assert not called
    assert marker.read_text(encoding="utf-8") == "keep"


def test_invalid_scenario_shape_fails_before_report_render(tmp_path):
    bad = ScenarioResult("", True, "invariant", 0.0, "detail")

    with pytest.raises(ValueError, match="invalid display fields"):
        run_faultboard(tmp_path / "bad", scenario_runner=lambda: [bad])
    assert not (tmp_path / "bad" / "faultboard.html").exists()


def test_symlinked_output_parent_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")

    with pytest.raises(ValueError, match="symlink"):
        run_faultboard(link / "report")
    assert not (target / "report").exists()
