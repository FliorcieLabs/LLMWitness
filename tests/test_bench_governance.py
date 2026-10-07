from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from llmwitness.bench_governance import (
    MAX_INTAKE_MANIFEST_BYTES,
    load_bench_dataset_intake,
    render_bench_dataset_intake_markdown,
    validate_bench_dataset_intake,
    write_bench_dataset_intake_reports,
)
from llmwitness.cli import main

SHA256_A = "sha256:" + "a" * 64
SHA256_B = "sha256:" + "b" * 64


def valid_pending_manifest() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "identity": {
            "dataset_id": "fliorcie-evidence-pilot",
            "dataset_version": "2026-10-review-1",
            "dataset_card_reference": "review:dataset-card-001",
        },
        "provenance": {
            "source_categories": ["authorized-local-runtime-evidence"],
            "collection_method_reference": "review:collection-001",
            "consent_or_basis_review_reference": "review:basis-001",
            "provenance_review_reference": "review:provenance-001",
        },
        "license": {
            "license_identifiers": ["Apache-2.0"],
            "review_reference": "review:license-001",
            "redistribution_policy": "restricted",
        },
        "privacy": {
            "classification": "restricted",
            "sensitive_data_excluded": True,
            "raw_personal_data_in_manifest": False,
            "minimization_reference": "review:minimization-001",
            "retention_reference": "review:retention-001",
            "removal_procedure_reference": "review:removal-001",
            "privacy_review_reference": "review:privacy-001",
        },
        "adjudication": {
            "protocol_reference": "review:adjudication-001",
            "independent_labeling": True,
            "human_labels_used": True,
            "inter_rater_evidence_reference": "review:inter-rater-001",
            "dispute_process_reference": "review:disputes-001",
            "holdout_adjudication_blinded": True,
        },
        "splits": {
            "development_record_count": 80,
            "holdout_record_count": 20,
            "holdout_integrity_digest": SHA256_A,
            "leakage_controls_reference": "review:leakage-001",
            "holdout_access_role": "independent holdout custodian",
            "holdout_content_in_manifest": False,
        },
        "records": {
            "record_schema_version": "0.1",
            "record_manifest_digest": SHA256_B,
            "all_records_schema_validated": True,
            "raw_records_in_intake_manifest": False,
        },
        "data_owner_review": {
            "accountable_role_reference": "role:data-owner",
            "decision": "pending",
            "decision_reference": None,
        },
        "independent_review": {
            "accountable_role_reference": "role:independent-reviewer",
            "decision": "pending",
            "decision_reference": None,
        },
    }


def test_complete_manifest_is_ready_for_human_acceptance_but_not_admitted() -> None:
    report = validate_bench_dataset_intake(valid_pending_manifest())

    assert report.ready_for_human_acceptance is True
    assert report.declared_reviews_accepted is False
    assert report.pending_review_roles == ("data_owner", "independent_reviewer")
    assert report.findings == ()
    assert "does not" in report.limitations
    assert "admit a dataset" in report.limitations


def test_declared_acceptance_requires_references_and_still_has_limitations() -> None:
    payload = valid_pending_manifest()
    for key, reference in (
        ("data_owner_review", "decision:data-owner-001"),
        ("independent_review", "decision:independent-001"),
    ):
        review = payload[key]
        assert isinstance(review, dict)
        review["decision"] = "accepted"
        review["decision_reference"] = reference

    report = validate_bench_dataset_intake(payload)

    assert report.ready_for_human_acceptance is True
    assert report.declared_reviews_accepted is True
    assert report.pending_review_roles == ()
    assert "verify reviewers or references" in report.limitations


@pytest.mark.parametrize(
    ("section", "field", "unsafe_value"),
    [
        ("privacy", "sensitive_data_excluded", False),
        ("privacy", "raw_personal_data_in_manifest", True),
        ("adjudication", "independent_labeling", False),
        ("adjudication", "holdout_adjudication_blinded", False),
        ("splits", "holdout_content_in_manifest", True),
        ("records", "all_records_schema_validated", False),
        ("records", "raw_records_in_intake_manifest", True),
    ],
)
def test_fail_closed_governance_declarations_are_required(
    section: str, field: str, unsafe_value: object
) -> None:
    payload = deepcopy(valid_pending_manifest())
    values = payload[section]
    assert isinstance(values, dict)
    values[field] = unsafe_value

    report = validate_bench_dataset_intake(payload)

    assert report.ready_for_human_acceptance is False
    assert any(item.path == f"{section}.{field}" for item in report.findings)


def test_holdout_must_be_nonempty_and_integrity_bound() -> None:
    payload = deepcopy(valid_pending_manifest())
    splits = payload["splits"]
    assert isinstance(splits, dict)
    splits["holdout_record_count"] = 0
    splits["holdout_integrity_digest"] = "sha256:not-a-digest"

    report = validate_bench_dataset_intake(payload)

    assert report.ready_for_human_acceptance is False
    assert {item.path for item in report.findings} == {
        "splits.holdout_integrity_digest",
        "splits.holdout_record_count",
    }


def test_human_labels_require_inter_rater_evidence() -> None:
    payload = deepcopy(valid_pending_manifest())
    adjudication = payload["adjudication"]
    assert isinstance(adjudication, dict)
    adjudication["inter_rater_evidence_reference"] = None

    report = validate_bench_dataset_intake(payload)

    assert report.ready_for_human_acceptance is False
    assert any(item.path == "adjudication" for item in report.findings)


def test_data_owner_and_independent_reviewer_must_be_distinct() -> None:
    payload = deepcopy(valid_pending_manifest())
    independent = payload["independent_review"]
    assert isinstance(independent, dict)
    independent["accountable_role_reference"] = "ROLE:DATA-OWNER"

    report = validate_bench_dataset_intake(payload)

    assert report.ready_for_human_acceptance is False
    assert any(item.path == "manifest" for item in report.findings)


def test_unknown_raw_content_is_rejected_without_echoing_its_name_or_value() -> None:
    payload = deepcopy(valid_pending_manifest())
    payload["raw_customer_prompts"] = "secret customer content"

    report = validate_bench_dataset_intake(payload)
    serialized = report.model_dump_json()

    assert report.ready_for_human_acceptance is False
    assert any(item.path == "<unexpected-field>" for item in report.findings)
    assert "raw_customer_prompts" not in serialized
    assert "secret customer content" not in serialized


def test_reports_are_deterministic_and_do_not_copy_manifest_values(
    tmp_path: Path,
) -> None:
    report = validate_bench_dataset_intake(valid_pending_manifest())

    json_path, markdown_path = write_bench_dataset_intake_reports(report, tmp_path)
    expected_json = json_path.read_text(encoding="utf-8")
    expected_markdown = markdown_path.read_text(encoding="utf-8")
    write_bench_dataset_intake_reports(report, tmp_path)

    assert json_path.read_text(encoding="utf-8") == expected_json
    assert markdown_path.read_text(encoding="utf-8") == expected_markdown
    assert expected_markdown == render_bench_dataset_intake_markdown(report)
    assert "review:privacy-001" not in expected_json
    assert SHA256_A not in expected_markdown


def test_loader_rejects_non_object_and_oversized_input(tmp_path: Path) -> None:
    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_bench_dataset_intake(non_object)

    oversized = tmp_path / "large.json"
    oversized.write_text(" " * (MAX_INTAKE_MANIFEST_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        load_bench_dataset_intake(oversized)


def test_cli_writes_pending_human_review_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "intake.json"
    manifest.write_text(json.dumps(valid_pending_manifest()), encoding="utf-8")
    output = tmp_path / "reports"

    main(["bench-intake", str(manifest), "--output-dir", str(output)])

    console = capsys.readouterr().out
    assert "[OK] Bench intake report" in console
    assert "[PENDING] Human acceptance is still required" in console
    assert (output / "bench-intake.json").is_file()
    assert (output / "bench-intake.md").is_file()


def test_cli_fails_closed_and_preserves_findings(tmp_path: Path) -> None:
    manifest = tmp_path / "intake.json"
    manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "reports"

    with pytest.raises(SystemExit) as exc:
        main(["bench-intake", str(manifest), "--output-dir", str(output)])

    assert exc.value.code == 1
    report = json.loads((output / "bench-intake.json").read_text(encoding="utf-8"))
    assert report["ready_for_human_acceptance"] is False
    assert report["findings"]
