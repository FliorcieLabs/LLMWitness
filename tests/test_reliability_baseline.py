import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from llmwitness.cli import main
from llmwitness.reliability import (
    ReliabilityBaseline,
    ScenarioResult,
    compare_reliability_baseline,
    create_reliability_baseline,
    load_reliability_baseline,
    write_reliability_baseline,
    write_reliability_comparison,
)


def _result(name: str, invariant: str, passed: bool = True) -> ScenarioResult:
    return ScenarioResult(name, passed, invariant, 999.0, "fixture")


def test_baseline_serialization_is_deterministic_and_ignores_duration(tmp_path):
    first = create_reliability_baseline(
        [_result("b", "invariant b"), _result("a", "invariant a")]
    )
    second = create_reliability_baseline(
        [
            ScenarioResult("a", True, "invariant a", 0.001, "other"),
            ScenarioResult("b", True, "invariant b", 50000, "other"),
        ]
    )

    assert first == second
    assert first.sample_count == 2
    assert all(
        item.scenario_id.startswith("reliability.v0.1:") for item in first.scenarios
    )
    path = tmp_path / "baseline.json"
    write_reliability_baseline(first, path)
    assert load_reliability_baseline(path) == first


def test_comparison_reports_every_dimension_and_fails_closed(tmp_path):
    baseline = create_reliability_baseline(
        [
            _result("regressed", "must pass"),
            _result("changed", "original invariant"),
            _result("recovered", "may recover", False),
            _result("removed", "must remain"),
        ]
    )
    current = [
        _result("regressed", "must pass", False),
        _result("changed", "changed invariant"),
        _result("recovered", "may recover", True),
        _result("added-pass", "new passing dimension", True),
        _result("added-fail", "new failing dimension", False),
    ]

    comparison = compare_reliability_baseline(baseline, current)

    assert not comparison.policy_passed
    assert comparison.regressed == ("regressed",)
    assert comparison.removed == ("removed",)
    assert comparison.invariant_changed == ("changed",)
    assert comparison.recovered == ("recovered",)
    assert comparison.added == ("added-fail", "added-pass")
    assert comparison.newly_failing == ("added-fail",)
    json_path = tmp_path / "comparison.json"
    markdown_path = tmp_path / "comparison.md"
    write_reliability_comparison(
        comparison, json_path=json_path, markdown_path=markdown_path
    )
    assert json.loads(json_path.read_text(encoding="utf-8"))["policy_passed"] is False
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Regressed: `regressed`" in markdown
    assert "universal" in markdown.lower()


def test_baseline_validation_rejects_duplicate_and_tampered_identity():
    baseline = create_reliability_baseline([_result("a", "invariant")])
    payload = baseline.model_dump(mode="json")
    payload["scenario_set_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="scenario_set_hash"):
        ReliabilityBaseline.model_validate(payload)

    duplicate = baseline.model_dump(mode="json")
    duplicate["scenarios"].append(dict(duplicate["scenarios"][0]))
    duplicate["sample_count"] = 2
    with pytest.raises(ValidationError, match="unique"):
        ReliabilityBaseline.model_validate(duplicate)


def test_cli_writes_and_compares_baseline(tmp_path, monkeypatch, capsys):
    baseline_path = tmp_path / "baseline.json"
    output_dir = tmp_path / "reports"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llmwitness",
            "reliability",
            "--output-dir",
            str(output_dir),
            "--write-baseline",
            str(baseline_path),
        ],
    )
    main()
    assert "15/15" in capsys.readouterr().out
    assert baseline_path.is_file()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llmwitness",
            "reliability",
            "--output-dir",
            str(output_dir),
            "--baseline",
            str(baseline_path),
        ],
    )
    main()
    assert "baseline policy passed" in capsys.readouterr().out
    assert (output_dir / "reliability.comparison.json").is_file()
    assert (output_dir / "reliability.comparison.md").is_file()


def test_committed_baseline_matches_current_corpus():
    from llmwitness.reliability import run_reliability_corpus

    root = Path(__file__).resolve().parents[1]
    baseline = load_reliability_baseline(
        root / ".github" / "reliability-baseline-v0.1.json"
    )

    comparison = compare_reliability_baseline(baseline, run_reliability_corpus())

    assert comparison.policy_passed
    assert comparison.added == comparison.removed == ()
    assert comparison.invariant_changed == comparison.regressed == ()


def test_cli_fails_on_baseline_regression_and_preserves_comparison(
    tmp_path, monkeypatch
):
    baseline_path = tmp_path / "baseline.json"
    write_reliability_baseline(
        create_reliability_baseline([_result("scenario", "must pass")]),
        baseline_path,
    )
    monkeypatch.setattr(
        "llmwitness.reliability.run_reliability_corpus",
        lambda: [_result("scenario", "must pass", False)],
    )
    output_dir = tmp_path / "reports"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llmwitness",
            "reliability",
            "--output-dir",
            str(output_dir),
            "--baseline",
            str(baseline_path),
        ],
    )

    with pytest.raises(SystemExit) as result:
        main()

    assert result.value.code == 1
    comparison = json.loads(
        (output_dir / "reliability.comparison.json").read_text(encoding="utf-8")
    )
    assert comparison["policy_passed"] is False
    assert comparison["regressed"] == ["scenario"]


def test_cli_never_writes_failing_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llmwitness.reliability.run_reliability_corpus",
        lambda: [_result("scenario", "must pass", False)],
    )
    baseline_path = tmp_path / "baseline.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llmwitness",
            "reliability",
            "--output-dir",
            str(tmp_path / "reports"),
            "--write-baseline",
            str(baseline_path),
        ],
    )

    with pytest.raises(SystemExit) as result:
        main()

    assert result.value.code == 1
    assert not baseline_path.exists()
