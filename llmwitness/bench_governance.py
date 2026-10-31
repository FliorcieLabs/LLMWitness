"""Offline governance validation for a future Agent Evidence Bench dataset."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from llmwitness.utils import atomic_write_text as _atomic_write

MAX_INTAKE_MANIFEST_BYTES = 256 * 1024
_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]$")
_SHA256_REFERENCE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LIMITATIONS = (
    "This offline result checks manifest structure and declared governance only. "
    "It does not read records or holdout content, verify reviewers or references, "
    "admit a dataset, run an evaluation, or establish legal or privacy approval."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DatasetIdentity(_StrictModel):
    dataset_id: str = Field(min_length=3, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_card_reference: str = Field(min_length=1, max_length=512)

    @field_validator("dataset_id")
    @classmethod
    def validate_dataset_id(cls, value: str) -> str:
        if not _SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError("must be a stable lowercase dataset identifier")
        return value


class ProvenanceReview(_StrictModel):
    source_categories: tuple[str, ...] = Field(min_length=1, max_length=32)
    collection_method_reference: str = Field(min_length=1, max_length=512)
    consent_or_basis_review_reference: str = Field(min_length=1, max_length=512)
    provenance_review_reference: str = Field(min_length=1, max_length=512)

    @field_validator("source_categories")
    @classmethod
    def validate_source_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 128 for item in value):
            raise ValueError("source categories must be non-empty and bounded")
        if len(set(value)) != len(value):
            raise ValueError("source categories must be unique")
        return value


class LicenseReview(_StrictModel):
    license_identifiers: tuple[str, ...] = Field(min_length=1, max_length=32)
    review_reference: str = Field(min_length=1, max_length=512)
    redistribution_policy: Literal["public", "restricted", "prohibited"]

    @field_validator("license_identifiers")
    @classmethod
    def validate_license_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 128 for item in value):
            raise ValueError("license identifiers must be non-empty and bounded")
        if len(set(value)) != len(value):
            raise ValueError("license identifiers must be unique")
        return value


class PrivacyReview(_StrictModel):
    classification: Literal["public", "restricted"]
    sensitive_data_excluded: Literal[True]
    raw_personal_data_in_manifest: Literal[False]
    minimization_reference: str = Field(min_length=1, max_length=512)
    retention_reference: str = Field(min_length=1, max_length=512)
    removal_procedure_reference: str = Field(min_length=1, max_length=512)
    privacy_review_reference: str = Field(min_length=1, max_length=512)


class AdjudicationReview(_StrictModel):
    protocol_reference: str = Field(min_length=1, max_length=512)
    independent_labeling: Literal[True]
    human_labels_used: bool
    inter_rater_evidence_reference: str | None = Field(default=None, max_length=512)
    dispute_process_reference: str = Field(min_length=1, max_length=512)
    holdout_adjudication_blinded: Literal[True]

    @model_validator(mode="after")
    def require_inter_rater_evidence(self) -> AdjudicationReview:
        if self.human_labels_used and not self.inter_rater_evidence_reference:
            raise ValueError("human labels require inter-rater evidence")
        return self


class SplitGovernance(_StrictModel):
    development_record_count: int = Field(gt=0)
    holdout_record_count: int = Field(gt=0)
    holdout_integrity_digest: str
    leakage_controls_reference: str = Field(min_length=1, max_length=512)
    holdout_access_role: str = Field(min_length=1, max_length=128)
    holdout_content_in_manifest: Literal[False]

    @field_validator("holdout_integrity_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_REFERENCE.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 reference")
        return value


class RecordContractReview(_StrictModel):
    record_schema_version: Literal["0.1"]
    record_manifest_digest: str
    all_records_schema_validated: Literal[True]
    raw_records_in_intake_manifest: Literal[False]

    @field_validator("record_manifest_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_REFERENCE.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 reference")
        return value


class ReviewDecision(_StrictModel):
    accountable_role_reference: str = Field(min_length=1, max_length=512)
    decision: Literal["pending", "accepted"]
    decision_reference: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def require_accepted_decision_reference(self) -> ReviewDecision:
        if self.decision == "accepted" and not self.decision_reference:
            raise ValueError("accepted decisions require a decision reference")
        return self


class BenchDatasetIntakeManifest(_StrictModel):
    schema_version: Literal["0.1"]
    identity: DatasetIdentity
    provenance: ProvenanceReview
    license: LicenseReview
    privacy: PrivacyReview
    adjudication: AdjudicationReview
    splits: SplitGovernance
    records: RecordContractReview
    data_owner_review: ReviewDecision
    independent_review: ReviewDecision

    @model_validator(mode="after")
    def require_independent_review_roles(self) -> BenchDatasetIntakeManifest:
        owner = self.data_owner_review.accountable_role_reference.casefold()
        reviewer = self.independent_review.accountable_role_reference.casefold()
        if owner == reviewer:
            raise ValueError("data owner and independent reviewer must be distinct")
        return self


class IntakeFinding(_StrictModel):
    path: str
    code: str
    message: str


class BenchDatasetIntakeReport(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    dataset_id: str
    ready_for_human_acceptance: bool
    declared_reviews_accepted: bool
    pending_review_roles: tuple[str, ...]
    findings: tuple[IntakeFinding, ...]
    limitations: str = _LIMITATIONS


def _safe_dataset_id(payload: Mapping[str, Any]) -> str:
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return "unknown"
    dataset_id = identity.get("dataset_id")
    if isinstance(dataset_id, str) and _SAFE_IDENTIFIER.fullmatch(dataset_id):
        return dataset_id
    return "unknown"


def _finding_message(error_type: str) -> str:
    if error_type == "missing":
        return "required intake field is missing"
    if error_type == "extra_forbidden":
        return "unexpected or raw-content field is forbidden"
    if error_type.startswith("literal_error"):
        return "value does not match the required fail-closed policy"
    return "value violates the dataset-intake contract"


def _finding_path(location: tuple[int | str, ...], error_type: str) -> str:
    parts = list(location)
    if error_type == "extra_forbidden" and parts:
        parts[-1] = "<unexpected-field>"
    return ".".join(str(part) for part in parts) or "manifest"


def validate_bench_dataset_intake(
    payload: Mapping[str, Any],
) -> BenchDatasetIntakeReport:
    """Validate governance declarations without reading any dataset records."""

    dataset_id = _safe_dataset_id(payload)
    try:
        manifest = BenchDatasetIntakeManifest.model_validate(payload)
    except ValidationError as exc:
        findings = tuple(
            sorted(
                (
                    IntakeFinding(
                        path=_finding_path(tuple(error["loc"]), str(error["type"])),
                        code=str(error["type"]),
                        message=_finding_message(str(error["type"])),
                    )
                    for error in exc.errors(include_input=False, include_url=False)
                ),
                key=lambda item: (item.path, item.code, item.message),
            )
        )
        return BenchDatasetIntakeReport(
            dataset_id=dataset_id,
            ready_for_human_acceptance=False,
            declared_reviews_accepted=False,
            pending_review_roles=(),
            findings=findings,
        )
    pending_roles = tuple(
        role
        for role, review in (
            ("data_owner", manifest.data_owner_review),
            ("independent_reviewer", manifest.independent_review),
        )
        if review.decision == "pending"
    )
    return BenchDatasetIntakeReport(
        dataset_id=manifest.identity.dataset_id,
        ready_for_human_acceptance=True,
        declared_reviews_accepted=not pending_roles,
        pending_review_roles=pending_roles,
        findings=(),
    )


def load_bench_dataset_intake(path: str | Path) -> Mapping[str, Any]:
    manifest_path = Path(path)
    if manifest_path.stat().st_size > MAX_INTAKE_MANIFEST_BYTES:
        raise ValueError(
            f"Bench intake manifest exceeds {MAX_INTAKE_MANIFEST_BYTES} bytes"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Bench intake manifest must be a JSON object")
    return payload


def render_bench_dataset_intake_markdown(report: BenchDatasetIntakeReport) -> str:
    if not report.ready_for_human_acceptance:
        status = "INCOMPLETE"
    elif report.declared_reviews_accepted:
        status = "DECLARED REVIEWS ACCEPTED; EXTERNAL ADMISSION STILL REQUIRED"
    else:
        status = "READY FOR HUMAN ACCEPTANCE; DECISIONS PENDING"
    lines = [
        "# Bench Dataset-Intake Validation",
        "",
        f"- Dataset: `{report.dataset_id}`",
        f"- Result: **{status}**",
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


def write_bench_dataset_intake_reports(
    report: BenchDatasetIntakeReport, output_directory: str | Path
) -> tuple[Path, Path]:
    output = Path(output_directory)
    json_path = output / "bench-intake.json"
    markdown_path = output / "bench-intake.md"
    serialized = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    _atomic_write(json_path, serialized + "\n")
    _atomic_write(markdown_path, render_bench_dataset_intake_markdown(report))
    return json_path, markdown_path


__all__ = [
    "BenchDatasetIntakeManifest",
    "BenchDatasetIntakeReport",
    "MAX_INTAKE_MANIFEST_BYTES",
    "load_bench_dataset_intake",
    "render_bench_dataset_intake_markdown",
    "validate_bench_dataset_intake",
    "write_bench_dataset_intake_reports",
]
