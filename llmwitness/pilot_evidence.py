"""Offline validation of declared external-pilot evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from llmwitness.utils import atomic_write_text as _atomic_write

MAX_PILOT_MANIFEST_BYTES = 256 * 1024
MAX_SETUP_DURATION_SECONDS = 600
_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_PACKAGE_VERSION = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?$"
)
_SUPPORTED_PYTHON = frozenset({"3.10", "3.11", "3.12", "3.13", "3.14"})
_LLMWITNESS_REPOSITORY = ("github.com", "/fliorcielabs/llmwitness")
EvidenceReference = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=512,
        pattern=r"^[a-z][a-z0-9._-]{0,31}:[A-Za-z0-9][A-Za-z0-9._/-]*$",
    ),
]
_LIMITATIONS = (
    "This offline result checks submitted structure and declarations only. It "
    "does not access or authenticate the operator, repository, CI run, evidence, "
    "or feedback; establish adoption; or close TASK-106 or TASK-306."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _validate_https_repository(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("must be an HTTPS repository URL without credentials")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.count("/") != 2:
        raise ValueError("must identify exactly one repository owner and name")
    if (parsed.netloc.lower(), path.lower()) == _LLMWITNESS_REPOSITORY:
        raise ValueError("must not identify the LLMWitness repository")
    return value


class PilotIdentity(_StrictModel):
    pilot_id: str = Field(min_length=3, max_length=128)
    package_name: Literal["llmwitness"]
    package_version: str = Field(min_length=5, max_length=64)
    source_commit_sha: str

    @field_validator("pilot_id")
    @classmethod
    def validate_pilot_id(cls, value: str) -> str:
        if not _SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError("must be a stable lowercase pilot identifier")
        return value

    @field_validator("package_version")
    @classmethod
    def validate_package_version(cls, value: str) -> str:
        if not _PACKAGE_VERSION.fullmatch(value):
            raise ValueError("must be a bounded PEP 440 release version")
        return value

    @field_validator("source_commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("must be a full lowercase Git commit SHA")
        return value


class IndependentOperator(_StrictModel):
    operator_role_reference: EvidenceReference
    independent_from_project: Literal[True]
    internal_automation_or_project_member: Literal[False]
    consent_reference: EvidenceReference


class ExternalRepository(_StrictModel):
    repository_url: str = Field(min_length=1, max_length=2048)
    repository_commit_sha: str
    unrelated_to_llmwitness: Literal[True]
    clean_before_pilot: Literal[True]
    customer_data_present: Literal[False]
    provider_credentials_present: Literal[False]
    production_effects_enabled: Literal[False]

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        return _validate_https_repository(value)

    @field_validator("repository_commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("must be a full lowercase Git commit SHA")
        return value


class PilotEnvironment(_StrictModel):
    operating_system: Literal["linux", "macos", "windows"]
    python_version: str
    dependency_lock_reference: EvidenceReference
    installation_method: Literal["reviewed_wheel", "pinned_source"]
    installation_evidence_reference: EvidenceReference

    @field_validator("python_version")
    @classmethod
    def validate_python_version(cls, value: str) -> str:
        if value not in _SUPPORTED_PYTHON:
            raise ValueError("must be a declared supported Python version")
        return value


class SetupTiming(_StrictModel):
    started_at: datetime
    finished_at: datetime
    measured_duration_seconds: int = Field(gt=0, le=MAX_SETUP_DURATION_SECONDS)
    timing_evidence_reference: EvidenceReference

    @model_validator(mode="after")
    def require_consistent_aware_timing(self) -> SetupTiming:
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise ValueError("timestamps must include UTC offsets")
        elapsed = (self.finished_at - self.started_at).total_seconds()
        if elapsed <= 0:
            raise ValueError("finished_at must be later than started_at")
        if elapsed != self.measured_duration_seconds:
            raise ValueError("measured duration must agree with timestamps")
        return self


class ReliabilityEvidence(_StrictModel):
    package_installed: Literal[True]
    reliability_action_configured: Literal[True]
    corpus_executed: Literal[True]
    scenario_count: int = Field(gt=0)
    failed_scenario_count: Literal[0]
    baseline_comparison_passed: Literal[True]
    json_report_preserved: Literal[True]
    junit_report_preserved: Literal[True]
    markdown_report_preserved: Literal[True]
    ci_run_reference: EvidenceReference
    artifact_reference: EvidenceReference


class PilotFeedback(_StrictModel):
    friction_reference: EvidenceReference
    comprehension_reference: EvidenceReference
    desired_changes_reference: EvidenceReference
    limitations_reference: EvidenceReference
    understandable_without_proprietary_tooling: Literal[True]


class IndependentReview(_StrictModel):
    reviewer_role_reference: EvidenceReference
    decision: Literal["pending", "accepted", "rejected"]
    decision_reference: EvidenceReference | None = None

    @model_validator(mode="after")
    def require_final_decision_reference(self) -> IndependentReview:
        if self.decision != "pending" and not self.decision_reference:
            raise ValueError("accepted and rejected decisions require a reference")
        return self


class ExternalPilotEvidenceManifest(_StrictModel):
    schema_version: Literal["0.1"]
    identity: PilotIdentity
    operator: IndependentOperator
    repository: ExternalRepository
    environment: PilotEnvironment
    timing: SetupTiming
    reliability: ReliabilityEvidence
    feedback: PilotFeedback
    independent_review: IndependentReview


class PilotEvidenceFinding(_StrictModel):
    path: str
    code: str
    message: str


class PilotEvidenceReport(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    pilot_id: str
    package_version: str
    source_commit_sha: str
    ready_for_independent_review: bool
    declared_review_decision: Literal["invalid", "pending", "accepted", "rejected"]
    external_adoption_established: Literal[False] = False
    pending_review_roles: tuple[Literal["independent_pilot_reviewer"], ...]
    findings: tuple[PilotEvidenceFinding, ...]
    limitations: str = _LIMITATIONS


def _safe_identity(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return "unknown", "unknown", "unknown"
    pilot_id = identity.get("pilot_id")
    package_version = identity.get("package_version")
    commit_sha = identity.get("source_commit_sha")
    return (
        (
            pilot_id
            if isinstance(pilot_id, str) and _SAFE_IDENTIFIER.fullmatch(pilot_id)
            else "unknown"
        ),
        (
            package_version
            if isinstance(package_version, str)
            and _PACKAGE_VERSION.fullmatch(package_version)
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
        return "required external-pilot evidence field is missing"
    if error_type == "extra_forbidden":
        return "unexpected field is forbidden"
    if error_type.startswith("literal_error"):
        return "value does not match the required fail-closed pilot policy"
    return "value violates the external-pilot evidence contract"


def _finding_path(location: tuple[int | str, ...], error_type: str) -> str:
    parts = list(location)
    if error_type == "extra_forbidden" and parts:
        parts[-1] = "<unexpected-field>"
    return ".".join(str(part) for part in parts) or "manifest"


def validate_external_pilot_evidence(
    payload: Mapping[str, Any],
) -> PilotEvidenceReport:
    """Validate declarations without establishing external adoption."""

    pilot_id, package_version, source_commit_sha = _safe_identity(payload)
    try:
        manifest = ExternalPilotEvidenceManifest.model_validate(payload)
    except ValidationError as exc:
        findings = tuple(
            sorted(
                (
                    PilotEvidenceFinding(
                        path=_finding_path(tuple(error["loc"]), str(error["type"])),
                        code=str(error["type"]),
                        message=_finding_message(str(error["type"])),
                    )
                    for error in exc.errors(include_input=False, include_url=False)
                ),
                key=lambda item: (item.path, item.code, item.message),
            )
        )
        return PilotEvidenceReport(
            pilot_id=pilot_id,
            package_version=package_version,
            source_commit_sha=source_commit_sha,
            ready_for_independent_review=False,
            declared_review_decision="invalid",
            pending_review_roles=(),
            findings=findings,
        )
    decision = manifest.independent_review.decision
    pending: tuple[Literal["independent_pilot_reviewer"], ...] = (
        ("independent_pilot_reviewer",) if decision == "pending" else ()
    )
    return PilotEvidenceReport(
        pilot_id=manifest.identity.pilot_id,
        package_version=manifest.identity.package_version,
        source_commit_sha=manifest.identity.source_commit_sha,
        ready_for_independent_review=True,
        declared_review_decision=decision,
        pending_review_roles=pending,
        findings=(),
    )


def load_external_pilot_evidence(path: str | Path) -> Mapping[str, Any]:
    manifest_path = Path(path)
    if manifest_path.stat().st_size > MAX_PILOT_MANIFEST_BYTES:
        raise ValueError(
            f"external-pilot manifest exceeds {MAX_PILOT_MANIFEST_BYTES} bytes"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("external-pilot manifest must be a JSON object")
    return payload


def render_pilot_evidence_markdown(report: PilotEvidenceReport) -> str:
    if not report.ready_for_independent_review:
        status = "INCOMPLETE"
    elif report.declared_review_decision == "pending":
        status = "READY FOR INDEPENDENT REVIEW; DECISION PENDING"
    else:
        status = "DECLARED REVIEW RECORDED; EXTERNAL GATE STILL REQUIRED"
    lines = [
        "# External Pilot Evidence Validation",
        "",
        f"- Pilot: `{report.pilot_id}`",
        f"- Package version: `{report.package_version}`",
        f"- Source commit: `{report.source_commit_sha}`",
        f"- Result: **{status}**",
        "- External adoption established by this report: **NO**",
        "",
        report.limitations,
        "",
        "## Pending review roles",
        "",
    ]
    lines.extend(
        (f"- `{role}`" for role in report.pending_review_roles)
        if report.pending_review_roles
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


def write_pilot_evidence_reports(
    report: PilotEvidenceReport, output_directory: str | Path
) -> tuple[Path, Path]:
    output = Path(output_directory)
    json_path = output / "pilot-evidence.json"
    markdown_path = output / "pilot-evidence.md"
    serialized = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    _atomic_write(json_path, serialized + "\n")
    _atomic_write(markdown_path, render_pilot_evidence_markdown(report))
    return json_path, markdown_path


__all__ = [
    "ExternalPilotEvidenceManifest",
    "MAX_PILOT_MANIFEST_BYTES",
    "MAX_SETUP_DURATION_SECONDS",
    "PilotEvidenceReport",
    "load_external_pilot_evidence",
    "render_pilot_evidence_markdown",
    "validate_external_pilot_evidence",
    "write_pilot_evidence_reports",
]
