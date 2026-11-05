from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from llmwitness.cli import main
from llmwitness.naming_readiness import (
    MAX_NAMING_DOSSIER_BYTES,
    load_naming_clearance_dossier,
    render_naming_readiness_markdown,
    validate_naming_clearance_dossier,
    write_naming_readiness_reports,
)


def valid_pending_dossier() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "candidate": {
            "candidate_id": "fliorcie-house-mark",
            "working_display_name": "Fliorcie",
            "product_category": "house_mark",
        },
        "scope": {
            "target_jurisdictions": ["IN", "US"],
            "trademark_classes": [9, 42],
            "intended_uses_reference": "review:intended-uses-001",
        },
        "searches": {
            "professional_search_reference": "legal:trademark-search-001",
            "exact_and_similar_marks_checked": True,
            "wipo_global_brand_database_checked": True,
            "national_trademark_databases_checked": True,
            "company_registries_checked": True,
            "domains_checked": True,
            "app_stores_checked": True,
            "github_checked": True,
            "pypi_checked": True,
            "npm_checked": True,
            "crates_io_checked": True,
            "maven_checked": True,
            "nuget_checked": True,
            "signed_in_social_handles_checked": True,
            "logo_and_design_marks_checked": True,
            "copyright_reviewed": True,
            "conflicts_review_reference": "legal:conflicts-001",
        },
        "ownership": {
            "domain_control_confirmed": True,
            "package_names_controlled": True,
            "github_names_controlled": True,
            "social_handles_controlled": True,
            "accountable_owner_recorded": True,
            "recovery_method_recorded": True,
            "two_factor_authentication_enabled": True,
            "ownership_review_reference": "review:ownership-001",
        },
        "counsel": {
            "accountable_counsel_role_reference": "role:legal-counsel",
            "decision": "pending",
            "decision_reference": None,
            "legal_advice_retained_outside_repository": True,
            "legal_opinion_present_in_manifest": False,
        },
        "founder": {
            "accountable_founder_role_reference": "role:founder",
            "decision": "pending",
            "decision_reference": None,
            "public_rename_required": False,
            "migration_plan_reference": None,
        },
    }


def _set(payload: dict[str, object], section: str, field: str, value: object) -> None:
    values = payload[section]
    assert isinstance(values, dict)
    values[field] = value


def test_complete_dossier_is_reviewable_but_never_establishes_clearance() -> None:
    report = validate_naming_clearance_dossier(valid_pending_dossier())

    assert report.ready_for_founder_legal_review is True
    assert report.declared_counsel_decision == "pending"
    assert report.declared_founder_decision == "pending"
    assert report.legal_clearance_established is False
    assert report.pending_review_roles == ("legal_counsel", "founder")
    assert report.findings == ()


@pytest.mark.parametrize(
    ("section", "decision"),
    [
        ("counsel", "cleared"),
        ("counsel", "rejected"),
        ("founder", "approve_name"),
        ("founder", "reject_name"),
    ],
)
def test_final_decisions_require_evidence_references(
    section: str, decision: str
) -> None:
    payload = valid_pending_dossier()
    _set(payload, section, "decision", decision)

    report = validate_naming_clearance_dossier(payload)

    assert report.ready_for_founder_legal_review is False
    assert any(item.path == section for item in report.findings)
    assert report.legal_clearance_established is False


def test_declared_approval_requires_counsel_clearance() -> None:
    payload = valid_pending_dossier()
    _set(payload, "founder", "decision", "approve_name")
    _set(payload, "founder", "decision_reference", "decision:founder-001")

    report = validate_naming_clearance_dossier(payload)

    assert report.ready_for_founder_legal_review is False
    assert any(item.path == "dossier" for item in report.findings)


def test_approved_public_rename_requires_migration_plan() -> None:
    payload = valid_pending_dossier()
    _set(payload, "counsel", "decision", "cleared")
    _set(payload, "counsel", "decision_reference", "decision:counsel-001")
    _set(payload, "founder", "decision", "approve_name")
    _set(payload, "founder", "decision_reference", "decision:founder-001")
    _set(payload, "founder", "public_rename_required", True)

    incomplete = validate_naming_clearance_dossier(payload)
    assert incomplete.ready_for_founder_legal_review is False
    assert any(item.path == "founder" for item in incomplete.findings)

    _set(payload, "founder", "migration_plan_reference", "plan:migration-001")
    complete = validate_naming_clearance_dossier(payload)
    assert complete.ready_for_founder_legal_review is True
    assert complete.declared_counsel_decision == "cleared"
    assert complete.declared_founder_decision == "approve_name"
    assert complete.pending_review_roles == ()
    assert complete.legal_clearance_established is False


@pytest.mark.parametrize(
    "field",
    [
        "exact_and_similar_marks_checked",
        "wipo_global_brand_database_checked",
        "national_trademark_databases_checked",
        "company_registries_checked",
        "domains_checked",
        "app_stores_checked",
        "github_checked",
        "pypi_checked",
        "npm_checked",
        "crates_io_checked",
        "maven_checked",
        "nuget_checked",
        "signed_in_social_handles_checked",
        "logo_and_design_marks_checked",
        "copyright_reviewed",
    ],
)
def test_every_required_search_surface_fails_closed(field: str) -> None:
    payload = valid_pending_dossier()
    _set(payload, "searches", field, False)

    report = validate_naming_clearance_dossier(payload)

    assert report.ready_for_founder_legal_review is False
    assert any(item.path == f"searches.{field}" for item in report.findings)


@pytest.mark.parametrize(
    "field",
    [
        "domain_control_confirmed",
        "package_names_controlled",
        "github_names_controlled",
        "social_handles_controlled",
        "accountable_owner_recorded",
        "recovery_method_recorded",
        "two_factor_authentication_enabled",
    ],
)
def test_every_ownership_control_fails_closed(field: str) -> None:
    payload = valid_pending_dossier()
    _set(payload, "ownership", field, False)

    report = validate_naming_clearance_dossier(payload)

    assert report.ready_for_founder_legal_review is False
    assert any(item.path == f"ownership.{field}" for item in report.findings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_jurisdictions", ["US", "US"]),
        ("target_jurisdictions", ["USA"]),
        ("trademark_classes", [9, 9]),
        ("trademark_classes", [0, 46]),
    ],
)
def test_scope_values_are_unique_and_bounded(field: str, value: object) -> None:
    payload = valid_pending_dossier()
    _set(payload, "scope", field, value)

    report = validate_naming_clearance_dossier(payload)

    assert report.ready_for_founder_legal_review is False
    assert any(item.path == f"scope.{field}" for item in report.findings)


@pytest.mark.parametrize(
    "unsafe_reference",
    [
        "pasted legal opinion",
        "https://legal.example/review?token=secret",
        "legal:search result",
    ],
)
def test_references_reject_free_text_urls_and_secret_queries(
    unsafe_reference: str,
) -> None:
    payload = valid_pending_dossier()
    _set(payload, "searches", "professional_search_reference", unsafe_reference)

    report = validate_naming_clearance_dossier(payload)

    assert report.ready_for_founder_legal_review is False
    assert unsafe_reference not in report.model_dump_json()
    assert any(
        item.path == "searches.professional_search_reference"
        for item in report.findings
    )


def test_unknown_field_and_invalid_candidate_are_minimized() -> None:
    payload = deepcopy(valid_pending_dossier())
    _set(payload, "candidate", "candidate_id", "Secret Candidate")
    _set(payload, "candidate", "working_display_name", "Unreleased Secret Name")
    _set(payload, "counsel", "raw_legal_opinion", "top-secret advice")

    report = validate_naming_clearance_dossier(payload)
    serialized = report.model_dump_json()

    assert report.candidate_id == "unknown"
    assert "Secret Candidate" not in serialized
    assert "Unreleased Secret Name" not in serialized
    assert "raw_legal_opinion" not in serialized
    assert "top-secret advice" not in serialized
    assert any(item.path == "counsel.<unexpected-field>" for item in report.findings)


def test_reports_are_deterministic_and_exclude_submitted_references(
    tmp_path: Path,
) -> None:
    report = validate_naming_clearance_dossier(valid_pending_dossier())

    json_path, markdown_path = write_naming_readiness_reports(report, tmp_path)
    expected_json = json_path.read_text(encoding="utf-8")
    expected_markdown = markdown_path.read_text(encoding="utf-8")
    write_naming_readiness_reports(report, tmp_path)

    assert json_path.read_text(encoding="utf-8") == expected_json
    assert markdown_path.read_text(encoding="utf-8") == expected_markdown
    assert expected_markdown == render_naming_readiness_markdown(report)
    assert "legal:trademark-search-001" not in expected_json
    assert "review:ownership-001" not in expected_markdown
    assert "role:legal-counsel" not in expected_json


def test_loader_rejects_non_object_and_oversized_input(tmp_path: Path) -> None:
    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_naming_clearance_dossier(non_object)

    oversized = tmp_path / "large.json"
    oversized.write_text(" " * (MAX_NAMING_DOSSIER_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        load_naming_clearance_dossier(oversized)


def test_cli_writes_pending_review_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dossier = tmp_path / "naming.json"
    dossier.write_text(json.dumps(valid_pending_dossier()), encoding="utf-8")
    output = tmp_path / "reports"

    main(["naming-readiness", str(dossier), "--output-dir", str(output)])

    console = capsys.readouterr().out
    assert "[OK] Naming-readiness report" in console
    assert "[PENDING] Founder/legal decisions" in console
    assert "never establishes legal clearance" in console
    assert (output / "naming-readiness.json").is_file()
    assert (output / "naming-readiness.md").is_file()


def test_cli_fails_closed_and_preserves_sanitized_findings(tmp_path: Path) -> None:
    dossier = tmp_path / "naming.json"
    dossier.write_text("{}", encoding="utf-8")
    output = tmp_path / "reports"

    with pytest.raises(SystemExit) as exc:
        main(["naming-readiness", str(dossier), "--output-dir", str(output)])

    assert exc.value.code == 1
    report = json.loads((output / "naming-readiness.json").read_text(encoding="utf-8"))
    assert report["ready_for_founder_legal_review"] is False
    assert report["legal_clearance_established"] is False
    assert report["findings"]
