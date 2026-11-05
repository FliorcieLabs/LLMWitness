"""Offline release-readiness validation for maintainer review."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from llmwitness.utils import atomic_write_text as _atomic_write

MAX_RELEASE_MANIFEST_BYTES = 256 * 1024
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_VERSION = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?$"
)
_SUPPORTED_PYTHON = frozenset({"3.10", "3.11", "3.12", "3.13", "3.14"})
_SUPPORTED_SYSTEMS = frozenset({"linux", "macos", "windows"})
_LIMITATIONS = (
    "This offline result checks submitted structure and declared controls only. "
    "It does not verify accounts or references, authorize shipment, configure a "
    "repository or package index, create a release, or publish an artifact."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _normalize_repository_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("must be an HTTPS URL without embedded credentials")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.count("/") != 2:
        raise ValueError("must identify exactly one repository owner and name")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, "", ""))


class CanonicalRepository(_StrictModel):
    canonical_repository_url: str = Field(min_length=1, max_length=2048)
    package_metadata_repository_url: str = Field(min_length=1, max_length=2048)
    default_branch: Literal["main"]
    public_repository: Literal[True]
    repository_identity_review_reference: str = Field(min_length=1, max_length=512)

    @field_validator("canonical_repository_url", "package_metadata_repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        _normalize_repository_url(value)
        return value

    @model_validator(mode="after")
    def require_one_canonical_repository(self) -> CanonicalRepository:
        canonical = _normalize_repository_url(self.canonical_repository_url)
        metadata = _normalize_repository_url(self.package_metadata_repository_url)
        if canonical != metadata:
            raise ValueError(
                "canonical and package-metadata repository URLs must agree"
            )
        return self


class ReleaseCandidate(_StrictModel):
    package_name: Literal["llmwitness"]
    version: str = Field(min_length=5, max_length=64)
    version_uniqueness_check_reference: str = Field(min_length=1, max_length=512)
    commit_sha: str
    working_tree_clean: Literal[True]
    release_notes_reference: str = Field(min_length=1, max_length=512)
    migration_notes_reference: str = Field(min_length=1, max_length=512)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _RELEASE_VERSION.fullmatch(value):
            raise ValueError("must be a bounded PEP 440 release version")
        return value

    @field_validator("commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("must be a full lowercase Git commit SHA")
        return value


class HostedCiEvidence(_StrictModel):
    run_reference: str = Field(min_length=1, max_length=512)
    conclusion: Literal["success"]
    python_versions: tuple[str, ...]
    operating_systems: tuple[str, ...]
    installed_package_smoke_passed: Literal[True]
    reliability_baseline_passed: Literal[True]
    package_metadata_check_passed: Literal[True]

    @model_validator(mode="after")
    def require_declared_support_matrix(self) -> HostedCiEvidence:
        if frozenset(self.python_versions) != _SUPPORTED_PYTHON:
            raise ValueError("CI must cover every declared Python version")
        if len(self.python_versions) != len(_SUPPORTED_PYTHON):
            raise ValueError("CI Python versions must be unique")
        if frozenset(self.operating_systems) != _SUPPORTED_SYSTEMS:
            raise ValueError("CI must cover Linux, macOS, and Windows")
        if len(self.operating_systems) != len(_SUPPORTED_SYSTEMS):
            raise ValueError("CI operating systems must be unique")
        return self


class RepositoryControls(_StrictModel):
    ownership_review_reference: str = Field(min_length=1, max_length=512)
    organization_2fa_required: Literal[True]
    recovery_procedure_reference: str = Field(min_length=1, max_length=512)
    protected_branch_reference: str = Field(min_length=1, max_length=512)
    codeowners_reference: str = Field(min_length=1, max_length=512)
    required_pull_request_reviews: Literal[True]
    required_status_checks: Literal[True]
    private_vulnerability_reporting_enabled: Literal[True]
    secret_scanning_enabled: Literal[True]


class PublishingControls(_StrictModel):
    package_namespace_ownership_reference: str = Field(min_length=1, max_length=512)
    trusted_publishing_configured: Literal[True]
    manual_environment_approval_required: Literal[True]
    long_lived_upload_token_present: Literal[False]
    publication_requires_explicit_approval: Literal[True]
    publication_workflow_reference: str = Field(min_length=1, max_length=512)


class ArtifactEvidence(_StrictModel):
    build_input_lock_review_reference: str = Field(min_length=1, max_length=512)
    dependency_review_reference: str = Field(min_length=1, max_length=512)
    license_review_reference: str = Field(min_length=1, max_length=512)
    installed_package_test_reference: str = Field(min_length=1, max_length=512)
    package_contents_review_reference: str = Field(min_length=1, max_length=512)
    sbom_reference: str = Field(min_length=1, max_length=512)
    provenance_reference: str = Field(min_length=1, max_length=512)
    public_claim_review_reference: str = Field(min_length=1, max_length=512)


class IncidentReadiness(_StrictModel):
    security_contact_reference: str = Field(min_length=1, max_length=512)
    incident_process_reference: str = Field(min_length=1, max_length=512)


class MaintainerDecision(_StrictModel):
    accountable_role_reference: str = Field(min_length=1, max_length=512)
    decision: Literal["pending", "ship", "no_ship"]
    decision_reference: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def require_final_decision_reference(self) -> MaintainerDecision:
        if self.decision != "pending" and not self.decision_reference:
            raise ValueError("ship and no-ship decisions require a decision reference")
        return self


class ReleaseReadinessManifest(_StrictModel):
    schema_version: Literal["0.1"]
    repository: CanonicalRepository
    candidate: ReleaseCandidate
    hosted_ci: HostedCiEvidence
    repository_controls: RepositoryControls
    publishing_controls: PublishingControls
    artifacts: ArtifactEvidence
    incident_readiness: IncidentReadiness
    maintainer_decision: MaintainerDecision


class ReleaseReadinessFinding(_StrictModel):
    path: str
    code: str
    message: str


class ReleaseReadinessReport(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    package_name: str
    version: str
    commit_sha: str
    ready_for_maintainer_decision: bool
    declared_decision: Literal["invalid", "pending", "ship", "no_ship"]
    publication_authorized: Literal[False] = False
    pending_control_roles: tuple[Literal["maintainer_ship_decision"], ...]
    findings: tuple[ReleaseReadinessFinding, ...]
    limitations: str = _LIMITATIONS


def _safe_candidate_identity(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping):
        return "unknown", "unknown", "unknown"
    package_name = candidate.get("package_name")
    version = candidate.get("version")
    commit_sha = candidate.get("commit_sha")
    return (
        "llmwitness" if package_name == "llmwitness" else "unknown",
        (
            version
            if isinstance(version, str) and _RELEASE_VERSION.fullmatch(version)
            else "unknown"
        ),
        (
            commit_sha
            if isinstance(commit_sha, str) and _COMMIT_SHA.fullmatch(commit_sha)
            else "unknown"
        ),
    )


def _finding_message(error_type: str) -> str:
    if error_type == "missing":
        return "required release-readiness field is missing"
    if error_type == "extra_forbidden":
        return "unexpected field is forbidden"
    if error_type.startswith("literal_error"):
        return "value does not match the required fail-closed release policy"
    return "value violates the release-readiness contract"


def _finding_path(location: tuple[int | str, ...], error_type: str) -> str:
    parts = list(location)
    if error_type == "extra_forbidden" and parts:
        parts[-1] = "<unexpected-field>"
    return ".".join(str(part) for part in parts) or "manifest"


def validate_release_readiness(
    payload: Mapping[str, Any],
) -> ReleaseReadinessReport:
    """Validate declared release controls without authorizing publication."""

    package_name, version, commit_sha = _safe_candidate_identity(payload)
    try:
        manifest = ReleaseReadinessManifest.model_validate(payload)
    except ValidationError as exc:
        findings = tuple(
            sorted(
                (
                    ReleaseReadinessFinding(
                        path=_finding_path(tuple(error["loc"]), str(error["type"])),
                        code=str(error["type"]),
                        message=_finding_message(str(error["type"])),
                    )
                    for error in exc.errors(include_input=False, include_url=False)
                ),
                key=lambda item: (item.path, item.code, item.message),
            )
        )
        return ReleaseReadinessReport(
            package_name=package_name,
            version=version,
            commit_sha=commit_sha,
            ready_for_maintainer_decision=False,
            declared_decision="invalid",
            pending_control_roles=(),
            findings=findings,
        )
    decision = manifest.maintainer_decision.decision
    pending_roles: tuple[Literal["maintainer_ship_decision"], ...] = (
        ("maintainer_ship_decision",) if decision == "pending" else ()
    )
    return ReleaseReadinessReport(
        package_name=manifest.candidate.package_name,
        version=manifest.candidate.version,
        commit_sha=manifest.candidate.commit_sha,
        ready_for_maintainer_decision=True,
        declared_decision=decision,
        pending_control_roles=pending_roles,
        findings=(),
    )


def load_release_readiness_manifest(path: str | Path) -> Mapping[str, Any]:
    manifest_path = Path(path)
    if manifest_path.stat().st_size > MAX_RELEASE_MANIFEST_BYTES:
        raise ValueError(
            f"release-readiness manifest exceeds {MAX_RELEASE_MANIFEST_BYTES} bytes"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("release-readiness manifest must be a JSON object")
    return payload


def render_release_readiness_markdown(report: ReleaseReadinessReport) -> str:
    if not report.ready_for_maintainer_decision:
        status = "INCOMPLETE"
    elif report.declared_decision == "pending":
        status = "READY FOR MAINTAINER DECISION; DECISION PENDING"
    else:
        status = "DECLARED DECISION RECORDED; EXTERNAL ACTION STILL REQUIRED"
    lines = [
        "# Release-Readiness Validation",
        "",
        f"- Candidate: `{report.package_name} {report.version}`",
        f"- Commit: `{report.commit_sha}`",
        f"- Result: **{status}**",
        "- Publication authorized by this report: **NO**",
        "",
        report.limitations,
        "",
        "## Pending control roles",
        "",
    ]
    lines.extend(
        (f"- `{role}`" for role in report.pending_control_roles)
        if report.pending_control_roles
        else ("- None declared pending.",)
    )
    lines.extend(["", "## Findings", ""])
    lines.extend(
        (
            f"- `{finding.path}` (`{finding.code}`): {finding.message}"
            for finding in report.findings
        )
        if report.findings
        else ("- No structural or declared-policy findings.",)
    )
    return "\n".join(lines) + "\n"


def write_release_readiness_reports(
    report: ReleaseReadinessReport, output_directory: str | Path
) -> tuple[Path, Path]:
    output = Path(output_directory)
    json_path = output / "release-readiness.json"
    markdown_path = output / "release-readiness.md"
    serialized = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    _atomic_write(json_path, serialized + "\n")
    _atomic_write(markdown_path, render_release_readiness_markdown(report))
    return json_path, markdown_path


__all__ = [
    "MAX_RELEASE_MANIFEST_BYTES",
    "ReleaseReadinessManifest",
    "ReleaseReadinessReport",
    "load_release_readiness_manifest",
    "render_release_readiness_markdown",
    "validate_release_readiness",
    "write_release_readiness_reports",
]
