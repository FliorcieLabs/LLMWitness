from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from llmwitness.cli import main
from llmwitness.provider_readiness import (
    MAX_DOSSIER_BYTES,
    load_provider_dossier,
    render_provider_readiness_markdown,
    validate_provider_dossier,
    write_provider_readiness_reports,
)


def valid_dossier() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "identity": {
            "provider_id": "example-refunds",
            "api_version": "2026-01",
            "documentation_url": "https://provider.example/docs/refunds",
            "license_review_reference": "review:license-001",
            "retention_review_reference": "review:retention-001",
        },
        "sandbox": {
            "credential_environment_variable": "EXAMPLE_SANDBOX_TOKEN",
            "authorized_for_sandbox_tests": True,
            "authorization_reference": "approval:sandbox-001",
        },
        "idempotency": {
            "provider_supports_idempotency": True,
            "key_scope": "one refund operation in one merchant sandbox",
            "key_lifetime_seconds": 86_400,
            "collision_behavior": "same key and different request is rejected",
            "duplicate_behavior": "same response is returned without another refund",
            "unknown_outcome_policy": "reconcile_before_retry",
        },
        "verification": {
            "authoritative": True,
            "endpoint_url": "https://provider.example/v1/refunds/status",
            "verifier_identity": "provider refund status API",
            "consistency_model": "eventually consistent within declared freshness",
            "maximum_freshness_seconds": 30,
            "stale_result_policy": "not_verified",
            "exception_policy": "unknown",
        },
        "failures": {
            "definite_failure": "validated rejection before provider mutation",
            "response_loss": "unknown until authoritative reconciliation",
            "timeout": "unknown unless the provider proves no mutation",
            "rate_limit": "failed only when rejected before dispatch",
            "partial_success": "unknown and routed to manual reconciliation",
            "ambiguous_outcome_state": "unknown_until_reconciled",
        },
        "compensation": {
            "mode": "supported",
            "semantics": "create an explicit reversal and retain both identifiers",
            "endpoint_url": "https://provider.example/v1/refunds/reverse",
            "verification_endpoint_url": "https://provider.example/v1/reversals/status",
            "irreversible_cases": [],
            "failed_compensation_state": "unknown",
        },
        "privacy": {
            "review_reference": "review:privacy-001",
            "request_data_handling": "store hashes and bounded provider references",
            "response_data_handling": "retain only evidence needed for reconciliation",
            "identifier_handling": "treat customer and transaction identifiers as sensitive",
            "evidence_handling": "keep local evidence deletable and access-controlled",
            "log_scrubbing_claim": "best_effort_pattern_scrubbing",
        },
    }


def test_complete_dossier_is_only_ready_for_human_review() -> None:
    report = validate_provider_dossier(valid_dossier())

    assert report.ready_for_human_review is True
    assert report.provider_id == "example-refunds"
    assert report.findings == ()
    assert "does not approve an adapter" in report.limitations
    assert "production readiness" in report.limitations


def test_missing_sections_are_reported_deterministically_without_input_values() -> None:
    payload = valid_dossier()
    del payload["verification"]
    report = validate_provider_dossier(payload)

    assert report.ready_for_human_review is False
    assert [(item.path, item.code) for item in report.findings] == [
        ("verification", "missing")
    ]
    assert "provider.example" not in report.model_dump_json()


def test_raw_credential_and_unknown_field_are_rejected_without_secret_leak() -> None:
    payload = deepcopy(valid_dossier())
    sandbox = payload["sandbox"]
    assert isinstance(sandbox, dict)
    sandbox["credential_environment_variable"] = "sk-live-super-secret"
    sandbox["api_key"] = "another-super-secret"

    report = validate_provider_dossier(payload)
    serialized = report.model_dump_json()

    assert report.ready_for_human_review is False
    assert {item.path for item in report.findings} == {
        "sandbox.<unexpected-field>",
        "sandbox.credential_environment_variable",
    }
    assert "sk-live" not in serialized
    assert "another-super-secret" not in serialized
    assert "api_key" not in serialized


@pytest.mark.parametrize(
    ("section", "field", "unsafe_value"),
    [
        ("idempotency", "unknown_outcome_policy", "retry_automatically"),
        ("verification", "stale_result_policy", "verified"),
        ("verification", "exception_policy", "failed"),
        ("failures", "ambiguous_outcome_state", "failed"),
        ("compensation", "failed_compensation_state", "compensated"),
    ],
)
def test_fail_closed_runtime_policies_are_required(
    section: str, field: str, unsafe_value: str
) -> None:
    payload = deepcopy(valid_dossier())
    values = payload[section]
    assert isinstance(values, dict)
    values[field] = unsafe_value

    report = validate_provider_dossier(payload)

    assert report.ready_for_human_review is False
    assert any(item.path == f"{section}.{field}" for item in report.findings)


def test_supported_compensation_requires_execution_and_verification_endpoints() -> None:
    payload = deepcopy(valid_dossier())
    compensation = payload["compensation"]
    assert isinstance(compensation, dict)
    compensation["verification_endpoint_url"] = None

    report = validate_provider_dossier(payload)

    assert report.ready_for_human_review is False
    assert any(item.path == "compensation" for item in report.findings)


def test_reports_are_deterministic_and_contain_no_dossier_body(tmp_path: Path) -> None:
    report = validate_provider_dossier(valid_dossier())

    first_json, first_markdown = write_provider_readiness_reports(report, tmp_path)
    first_json_content = first_json.read_text(encoding="utf-8")
    first_markdown_content = first_markdown.read_text(encoding="utf-8")
    write_provider_readiness_reports(report, tmp_path)

    assert first_json.read_text(encoding="utf-8") == first_json_content
    assert first_markdown.read_text(encoding="utf-8") == first_markdown_content
    assert first_markdown_content == render_provider_readiness_markdown(report)
    assert "EXAMPLE_SANDBOX_TOKEN" not in first_json_content
    assert "provider.example" not in first_markdown_content


def test_loader_rejects_non_object_and_oversized_input(tmp_path: Path) -> None:
    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_provider_dossier(non_object)

    oversized = tmp_path / "large.json"
    oversized.write_text(" " * (MAX_DOSSIER_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        load_provider_dossier(oversized)


def test_cli_writes_passing_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dossier = tmp_path / "dossier.json"
    dossier.write_text(json.dumps(valid_dossier()), encoding="utf-8")
    output = tmp_path / "reports"

    main(["provider-dossier", str(dossier), "--output-dir", str(output)])

    assert "[OK] Provider dossier report" in capsys.readouterr().out
    assert (output / "provider-readiness.json").is_file()
    assert (output / "provider-readiness.md").is_file()


def test_cli_fails_closed_but_preserves_findings_report(tmp_path: Path) -> None:
    dossier = tmp_path / "dossier.json"
    dossier.write_text("{}", encoding="utf-8")
    output = tmp_path / "reports"

    with pytest.raises(SystemExit) as exc:
        main(["provider-dossier", str(dossier), "--output-dir", str(output)])

    assert exc.value.code == 1
    report = json.loads(
        (output / "provider-readiness.json").read_text(encoding="utf-8")
    )
    assert report["ready_for_human_review"] is False
    assert report["findings"]
