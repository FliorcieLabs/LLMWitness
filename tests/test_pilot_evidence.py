from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from llmwitness.cli import main
from llmwitness.pilot_evidence import (
    MAX_PILOT_MANIFEST_BYTES,
    load_external_pilot_evidence,
    render_pilot_evidence_markdown,
    validate_external_pilot_evidence,
    write_pilot_evidence_reports,
)

SOURCE_COMMIT = "a" * 40
REPOSITORY_COMMIT = "b" * 40


def valid_pending_manifest() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "identity": {
            "pilot_id": "outside-repository-001",
            "package_name": "llmwitness",
            "package_version": "0.1.0",
            "source_commit_sha": SOURCE_COMMIT,
        },
        "operator": {
            "operator_role_reference": "role:independent-maintainer",
            "independent_from_project": True,
            "internal_automation_or_project_member": False,
            "consent_reference": "review:operator-consent-001",
        },
        "repository": {
            "repository_url": "https://github.com/example/outside-project",
            "repository_commit_sha": REPOSITORY_COMMIT,
            "unrelated_to_llmwitness": True,
            "clean_before_pilot": True,
            "customer_data_present": False,
            "provider_credentials_present": False,
            "production_effects_enabled": False,
        },
        "environment": {
            "operating_system": "linux",
            "python_version": "3.12",
            "dependency_lock_reference": "artifact:lock-001",
            "installation_method": "reviewed_wheel",
            "installation_evidence_reference": "artifact:install-001",
        },
        "timing": {
            "started_at": "2026-10-02T10:00:00+00:00",
            "finished_at": "2026-10-02T10:10:00+00:00",
            "measured_duration_seconds": 600,
            "timing_evidence_reference": "artifact:timing-001",
        },
        "reliability": {
            "package_installed": True,
            "reliability_action_configured": True,
            "corpus_executed": True,
            "scenario_count": 15,
            "failed_scenario_count": 0,
            "baseline_comparison_passed": True,
            "json_report_preserved": True,
            "junit_report_preserved": True,
            "markdown_report_preserved": True,
            "ci_run_reference": "ci:outside-run-001",
            "artifact_reference": "artifact:reports-001",
        },
        "feedback": {
            "friction_reference": "feedback:friction-001",
            "comprehension_reference": "feedback:comprehension-001",
            "desired_changes_reference": "feedback:changes-001",
            "limitations_reference": "feedback:limitations-001",
            "understandable_without_proprietary_tooling": True,
        },
        "independent_review": {
            "reviewer_role_reference": "role:pilot-reviewer",
            "decision": "pending",
            "decision_reference": None,
        },
    }


def _set(payload: dict[str, object], section: str, field: str, value: object) -> None:
    values = payload[section]
    assert isinstance(values, dict)
    values[field] = value


def test_complete_manifest_is_reviewable_but_never_establishes_adoption() -> None:
    report = validate_external_pilot_evidence(valid_pending_manifest())

    assert report.ready_for_independent_review is True
    assert report.declared_review_decision == "pending"
    assert report.external_adoption_established is False
    assert report.pending_review_roles == ("independent_pilot_reviewer",)
    assert report.findings == ()
    assert "does not" in report.limitations


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_final_review_requires_reference_and_never_establishes_adoption(
    decision: str,
) -> None:
    payload = valid_pending_manifest()
    _set(payload, "independent_review", "decision", decision)

    incomplete = validate_external_pilot_evidence(payload)
    assert incomplete.ready_for_independent_review is False

    _set(payload, "independent_review", "decision_reference", "review:decision-001")
    complete = validate_external_pilot_evidence(payload)
    assert complete.ready_for_independent_review is True
    assert complete.declared_review_decision == decision
    assert complete.external_adoption_established is False
    assert complete.pending_review_roles == ()


@pytest.mark.parametrize(
    ("section", "field", "unsafe_value"),
    [
        ("operator", "independent_from_project", False),
        ("operator", "internal_automation_or_project_member", True),
        ("repository", "unrelated_to_llmwitness", False),
        ("repository", "clean_before_pilot", False),
        ("repository", "customer_data_present", True),
        ("repository", "provider_credentials_present", True),
        ("repository", "production_effects_enabled", True),
        ("reliability", "package_installed", False),
        ("reliability", "reliability_action_configured", False),
        ("reliability", "corpus_executed", False),
        ("reliability", "failed_scenario_count", 1),
        ("reliability", "baseline_comparison_passed", False),
        ("reliability", "json_report_preserved", False),
        ("reliability", "junit_report_preserved", False),
        ("reliability", "markdown_report_preserved", False),
        ("feedback", "understandable_without_proprietary_tooling", False),
    ],
)
def test_safety_and_evidence_declarations_fail_closed(
    section: str, field: str, unsafe_value: object
) -> None:
    payload = valid_pending_manifest()
    _set(payload, section, field, unsafe_value)

    report = validate_external_pilot_evidence(payload)

    assert report.ready_for_independent_review is False
    assert any(item.path == f"{section}.{field}" for item in report.findings)
    assert report.external_adoption_established is False


@pytest.mark.parametrize(
    "repository_url",
    [
        "https://github.com/FliorcieLabs/LLMWitness",
        "https://user:secret@github.com/example/outside-project",
        "https://github.com/example/outside-project?token=secret",
        "https://github.com/example/outside-project#secret",
    ],
)
def test_repository_must_be_external_and_contain_no_credentials(
    repository_url: str,
) -> None:
    payload = valid_pending_manifest()
    _set(payload, "repository", "repository_url", repository_url)

    report = validate_external_pilot_evidence(payload)

    assert report.ready_for_independent_review is False
    assert "secret" not in report.model_dump_json()


@pytest.mark.parametrize(
    ("started", "finished", "seconds"),
    [
        ("2026-10-02T10:00:00", "2026-10-02T10:09:00", 540),
        ("2026-10-02T10:10:00+00:00", "2026-10-02T10:00:00+00:00", 600),
        ("2026-10-02T10:00:00+00:00", "2026-10-02T10:10:00+00:00", 599),
        ("2026-10-02T10:00:00+00:00", "2026-10-02T10:10:01+00:00", 601),
    ],
)
def test_timing_requires_aware_consistent_timestamps_under_ten_minutes(
    started: str, finished: str, seconds: int
) -> None:
    payload = valid_pending_manifest()
    _set(payload, "timing", "started_at", started)
    _set(payload, "timing", "finished_at", finished)
    _set(payload, "timing", "measured_duration_seconds", seconds)

    report = validate_external_pilot_evidence(payload)

    assert report.ready_for_independent_review is False
    assert any(item.path.startswith("timing") for item in report.findings)


def test_supported_environment_is_required() -> None:
    payload = valid_pending_manifest()
    _set(payload, "environment", "python_version", "3.9")

    report = validate_external_pilot_evidence(payload)

    assert report.ready_for_independent_review is False
    assert any(item.path == "environment.python_version" for item in report.findings)


def test_unknown_field_and_invalid_identity_are_minimized() -> None:
    payload = deepcopy(valid_pending_manifest())
    _set(payload, "identity", "pilot_id", "Secret Pilot")
    _set(payload, "identity", "package_version", "private-version")
    _set(payload, "identity", "source_commit_sha", "secret-commit")
    _set(payload, "repository", "access_token", "top-secret")

    report = validate_external_pilot_evidence(payload)
    serialized = report.model_dump_json()

    assert report.pilot_id == "unknown"
    assert report.package_version == "unknown"
    assert report.source_commit_sha == "unknown"
    assert "access_token" not in serialized
    assert "top-secret" not in serialized
    assert "Secret Pilot" not in serialized
    assert any(item.path == "repository.<unexpected-field>" for item in report.findings)


@pytest.mark.parametrize(
    "unsafe_reference",
    [
        "plain pasted secret",
        "https://evidence.example/run?token=secret",
        "artifact:reports 001",
    ],
)
def test_evidence_references_reject_free_text_urls_and_secret_queries(
    unsafe_reference: str,
) -> None:
    payload = valid_pending_manifest()
    _set(payload, "reliability", "artifact_reference", unsafe_reference)

    report = validate_external_pilot_evidence(payload)

    assert report.ready_for_independent_review is False
    assert unsafe_reference not in report.model_dump_json()
    assert any(
        item.path == "reliability.artifact_reference" for item in report.findings
    )


def test_reports_are_deterministic_and_exclude_submitted_references(
    tmp_path: Path,
) -> None:
    report = validate_external_pilot_evidence(valid_pending_manifest())

    json_path, markdown_path = write_pilot_evidence_reports(report, tmp_path)
    expected_json = json_path.read_text(encoding="utf-8")
    expected_markdown = markdown_path.read_text(encoding="utf-8")
    write_pilot_evidence_reports(report, tmp_path)

    assert json_path.read_text(encoding="utf-8") == expected_json
    assert markdown_path.read_text(encoding="utf-8") == expected_markdown
    assert expected_markdown == render_pilot_evidence_markdown(report)
    assert "https://github.com/example/outside-project" not in expected_json
    assert "feedback:friction-001" not in expected_markdown
    assert "role:independent-maintainer" not in expected_json


def test_loader_rejects_non_object_and_oversized_input(tmp_path: Path) -> None:
    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_external_pilot_evidence(non_object)

    oversized = tmp_path / "large.json"
    oversized.write_text(" " * (MAX_PILOT_MANIFEST_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        load_external_pilot_evidence(oversized)


def test_cli_writes_pending_review_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "pilot.json"
    manifest.write_text(json.dumps(valid_pending_manifest()), encoding="utf-8")
    output = tmp_path / "reports"

    main(["pilot-evidence", str(manifest), "--output-dir", str(output)])

    console = capsys.readouterr().out
    assert "[OK] External-pilot evidence report" in console
    assert "[PENDING] Independent human review" in console
    assert "never establishes external adoption" in console
    assert (output / "pilot-evidence.json").is_file()
    assert (output / "pilot-evidence.md").is_file()


def test_cli_fails_closed_and_preserves_sanitized_findings(tmp_path: Path) -> None:
    manifest = tmp_path / "pilot.json"
    manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "reports"

    with pytest.raises(SystemExit) as exc:
        main(["pilot-evidence", str(manifest), "--output-dir", str(output)])

    assert exc.value.code == 1
    report = json.loads((output / "pilot-evidence.json").read_text(encoding="utf-8"))
    assert report["ready_for_independent_review"] is False
    assert report["external_adoption_established"] is False
    assert report["findings"]
