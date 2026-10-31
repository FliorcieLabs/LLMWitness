from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from llmwitness.cli import main
from llmwitness.release_readiness import (
    MAX_RELEASE_MANIFEST_BYTES,
    load_release_readiness_manifest,
    render_release_readiness_markdown,
    validate_release_readiness,
    write_release_readiness_reports,
)

COMMIT_SHA = "a" * 40


def valid_pending_manifest() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "repository": {
            "canonical_repository_url": "https://github.com/FliorcieLabs/LLMWitness",
            "package_metadata_repository_url": "https://github.com/FliorcieLabs/LLMWitness.git",
            "default_branch": "main",
            "public_repository": True,
            "repository_identity_review_reference": "review:repository-identity-001",
        },
        "candidate": {
            "package_name": "llmwitness",
            "version": "0.2.0a1",
            "version_uniqueness_check_reference": "review:version-001",
            "commit_sha": COMMIT_SHA,
            "working_tree_clean": True,
            "release_notes_reference": "review:release-notes-001",
            "migration_notes_reference": "review:migration-001",
        },
        "hosted_ci": {
            "run_reference": "ci:run-001",
            "conclusion": "success",
            "python_versions": ["3.10", "3.11", "3.12", "3.13", "3.14"],
            "operating_systems": ["linux", "macos", "windows"],
            "installed_package_smoke_passed": True,
            "reliability_baseline_passed": True,
            "package_metadata_check_passed": True,
        },
        "repository_controls": {
            "ownership_review_reference": "review:ownership-001",
            "organization_2fa_required": True,
            "recovery_procedure_reference": "review:recovery-001",
            "protected_branch_reference": "review:ruleset-001",
            "codeowners_reference": "review:codeowners-001",
            "required_pull_request_reviews": True,
            "required_status_checks": True,
            "private_vulnerability_reporting_enabled": True,
            "secret_scanning_enabled": True,
        },
        "publishing_controls": {
            "package_namespace_ownership_reference": "review:namespace-001",
            "trusted_publishing_configured": True,
            "manual_environment_approval_required": True,
            "long_lived_upload_token_present": False,
            "publication_requires_explicit_approval": True,
            "publication_workflow_reference": "review:workflow-001",
        },
        "artifacts": {
            "build_input_lock_review_reference": "review:build-inputs-001",
            "dependency_review_reference": "review:dependencies-001",
            "license_review_reference": "review:licenses-001",
            "installed_package_test_reference": "review:install-001",
            "package_contents_review_reference": "review:contents-001",
            "sbom_reference": "artifact:sbom-001",
            "provenance_reference": "artifact:provenance-001",
            "public_claim_review_reference": "review:claims-001",
        },
        "incident_readiness": {
            "security_contact_reference": "review:security-contact-001",
            "incident_process_reference": "review:incident-process-001",
        },
        "maintainer_decision": {
            "accountable_role_reference": "role:release-maintainer",
            "decision": "pending",
            "decision_reference": None,
        },
    }


def test_complete_manifest_prepares_but_never_authorizes_release() -> None:
    report = validate_release_readiness(valid_pending_manifest())

    assert report.ready_for_maintainer_decision is True
    assert report.declared_decision == "pending"
    assert report.publication_authorized is False
    assert report.pending_control_roles == ("maintainer_ship_decision",)
    assert report.findings == ()
    assert "does not" in report.limitations
    assert "publish an artifact" in report.limitations


@pytest.mark.parametrize("decision", ["ship", "no_ship"])
def test_final_decision_requires_reference_and_still_does_not_authorize(
    decision: str,
) -> None:
    payload = valid_pending_manifest()
    maintainer_decision = payload["maintainer_decision"]
    assert isinstance(maintainer_decision, dict)
    maintainer_decision["decision"] = decision

    incomplete = validate_release_readiness(payload)
    assert incomplete.ready_for_maintainer_decision is False

    maintainer_decision["decision_reference"] = "decision:release-001"
    complete = validate_release_readiness(payload)
    assert complete.ready_for_maintainer_decision is True
    assert complete.declared_decision == decision
    assert complete.publication_authorized is False
    assert complete.pending_control_roles == ()


def test_canonical_and_package_repository_urls_must_agree() -> None:
    payload = deepcopy(valid_pending_manifest())
    repository = payload["repository"]
    assert isinstance(repository, dict)
    repository["package_metadata_repository_url"] = (
        "https://github.com/llmwitness/LLMWitness"
    )

    report = validate_release_readiness(payload)

    assert report.ready_for_maintainer_decision is False
    assert any(item.path == "repository" for item in report.findings)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://user:secret@github.com/FliorcieLabs/LLMWitness",
        "https://github.com/FliorcieLabs/LLMWitness?token=secret",
        "https://github.com/FliorcieLabs/LLMWitness#secret",
    ],
)
def test_repository_urls_reject_embedded_or_hidden_credentials(
    unsafe_url: str,
) -> None:
    payload = deepcopy(valid_pending_manifest())
    repository = payload["repository"]
    assert isinstance(repository, dict)
    repository["canonical_repository_url"] = unsafe_url

    report = validate_release_readiness(payload)

    assert report.ready_for_maintainer_decision is False
    assert "secret" not in report.model_dump_json()


@pytest.mark.parametrize(
    ("section", "field", "unsafe_value"),
    [
        ("candidate", "working_tree_clean", False),
        ("hosted_ci", "conclusion", "failure"),
        ("hosted_ci", "installed_package_smoke_passed", False),
        ("hosted_ci", "reliability_baseline_passed", False),
        ("repository_controls", "organization_2fa_required", False),
        ("repository_controls", "required_pull_request_reviews", False),
        ("repository_controls", "required_status_checks", False),
        ("publishing_controls", "trusted_publishing_configured", False),
        ("publishing_controls", "manual_environment_approval_required", False),
        ("publishing_controls", "long_lived_upload_token_present", True),
        ("publishing_controls", "publication_requires_explicit_approval", False),
    ],
)
def test_release_controls_fail_closed(
    section: str, field: str, unsafe_value: object
) -> None:
    payload = deepcopy(valid_pending_manifest())
    values = payload[section]
    assert isinstance(values, dict)
    values[field] = unsafe_value

    report = validate_release_readiness(payload)

    assert report.ready_for_maintainer_decision is False
    assert any(item.path == f"{section}.{field}" for item in report.findings)
    assert report.publication_authorized is False


def test_ci_must_cover_each_declared_version_and_operating_system_once() -> None:
    payload = deepcopy(valid_pending_manifest())
    hosted_ci = payload["hosted_ci"]
    assert isinstance(hosted_ci, dict)
    hosted_ci["python_versions"] = ["3.11", "3.11", "3.12", "3.13", "3.14"]
    hosted_ci["operating_systems"] = ["linux", "windows"]

    report = validate_release_readiness(payload)

    assert report.ready_for_maintainer_decision is False
    assert any(item.path == "hosted_ci" for item in report.findings)


def test_invalid_release_identity_is_minimized_in_report() -> None:
    payload = deepcopy(valid_pending_manifest())
    candidate = payload["candidate"]
    assert isinstance(candidate, dict)
    candidate["package_name"] = "secret-package-name"
    candidate["version"] = "not a version"
    candidate["commit_sha"] = "not a commit"

    report = validate_release_readiness(payload)

    assert report.package_name == "unknown"
    assert report.version == "unknown"
    assert report.commit_sha == "unknown"
    assert "secret-package-name" not in report.model_dump_json()


def test_unknown_field_is_rejected_without_echoing_name_or_value() -> None:
    payload = deepcopy(valid_pending_manifest())
    publishing = payload["publishing_controls"]
    assert isinstance(publishing, dict)
    publishing["pypi_token"] = "pypi-secret-value"

    report = validate_release_readiness(payload)
    serialized = report.model_dump_json()

    assert report.ready_for_maintainer_decision is False
    assert any(
        item.path == "publishing_controls.<unexpected-field>"
        for item in report.findings
    )
    assert "pypi_token" not in serialized
    assert "pypi-secret-value" not in serialized


def test_reports_are_deterministic_and_exclude_evidence_references(
    tmp_path: Path,
) -> None:
    report = validate_release_readiness(valid_pending_manifest())

    json_path, markdown_path = write_release_readiness_reports(report, tmp_path)
    expected_json = json_path.read_text(encoding="utf-8")
    expected_markdown = markdown_path.read_text(encoding="utf-8")
    write_release_readiness_reports(report, tmp_path)

    assert json_path.read_text(encoding="utf-8") == expected_json
    assert markdown_path.read_text(encoding="utf-8") == expected_markdown
    assert expected_markdown == render_release_readiness_markdown(report)
    assert "review:ownership-001" not in expected_json
    assert "artifact:sbom-001" not in expected_markdown


def test_loader_rejects_non_object_and_oversized_input(tmp_path: Path) -> None:
    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_release_readiness_manifest(non_object)

    oversized = tmp_path / "large.json"
    oversized.write_text(" " * (MAX_RELEASE_MANIFEST_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        load_release_readiness_manifest(oversized)


def test_cli_writes_pending_decision_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps(valid_pending_manifest()), encoding="utf-8")
    output = tmp_path / "reports"

    main(["release-readiness", str(manifest), "--output-dir", str(output)])

    console = capsys.readouterr().out
    assert "[OK] Release-readiness report" in console
    assert "[PENDING] Explicit maintainer" in console
    assert "never authorizes or performs publication" in console
    assert (output / "release-readiness.json").is_file()
    assert (output / "release-readiness.md").is_file()


def test_cli_fails_closed_and_preserves_findings(tmp_path: Path) -> None:
    manifest = tmp_path / "release.json"
    manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "reports"

    with pytest.raises(SystemExit) as exc:
        main(["release-readiness", str(manifest), "--output-dir", str(output)])

    assert exc.value.code == 1
    report = json.loads((output / "release-readiness.json").read_text(encoding="utf-8"))
    assert report["ready_for_maintainer_decision"] is False
    assert report["publication_authorized"] is False
    assert report["findings"]
