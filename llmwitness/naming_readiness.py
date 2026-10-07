"""Offline validation of naming-clearance readiness declarations."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

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

MAX_NAMING_DOSSIER_BYTES = 256 * 1024
_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]$")
_JURISDICTION = re.compile(r"^[A-Z]{2}$")
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
    "does not perform or verify searches, ownership, legal analysis, filings, "
    "purchases, reservations, renames, or launch authorization and cannot close "
    "TASK-801, TASK-802, or TASK-803."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class NamingCandidate(_StrictModel):
    candidate_id: str = Field(min_length=3, max_length=128)
    working_display_name: str = Field(min_length=2, max_length=128)
    product_category: Literal[
        "house_mark", "developer_tool", "software_package", "hosted_service"
    ]

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if not _SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError("must be a stable lowercase candidate identifier")
        return value


class ClearanceScope(_StrictModel):
    target_jurisdictions: tuple[str, ...] = Field(min_length=1, max_length=16)
    trademark_classes: tuple[int, ...] = Field(min_length=1, max_length=45)
    intended_uses_reference: EvidenceReference

    @field_validator("target_jurisdictions")
    @classmethod
    def validate_jurisdictions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("target jurisdictions must be unique")
        if any(not _JURISDICTION.fullmatch(item) for item in value):
            raise ValueError("jurisdictions must use two-letter uppercase codes")
        return value

    @field_validator("trademark_classes")
    @classmethod
    def validate_trademark_classes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(value)) != len(value):
            raise ValueError("trademark classes must be unique")
        if any(item < 1 or item > 45 for item in value):
            raise ValueError("trademark classes must be between 1 and 45")
        return value


class SearchCoverage(_StrictModel):
    professional_search_reference: EvidenceReference
    exact_and_similar_marks_checked: Literal[True]
    wipo_global_brand_database_checked: Literal[True]
    national_trademark_databases_checked: Literal[True]
    company_registries_checked: Literal[True]
    domains_checked: Literal[True]
    app_stores_checked: Literal[True]
    github_checked: Literal[True]
    pypi_checked: Literal[True]
    npm_checked: Literal[True]
    crates_io_checked: Literal[True]
    maven_checked: Literal[True]
    nuget_checked: Literal[True]
    signed_in_social_handles_checked: Literal[True]
    logo_and_design_marks_checked: Literal[True]
    copyright_reviewed: Literal[True]
    conflicts_review_reference: EvidenceReference


class OwnershipControls(_StrictModel):
    domain_control_confirmed: Literal[True]
    package_names_controlled: Literal[True]
    github_names_controlled: Literal[True]
    social_handles_controlled: Literal[True]
    accountable_owner_recorded: Literal[True]
    recovery_method_recorded: Literal[True]
    two_factor_authentication_enabled: Literal[True]
    ownership_review_reference: EvidenceReference


class CounselDecision(_StrictModel):
    accountable_counsel_role_reference: EvidenceReference
    decision: Literal["pending", "cleared", "rejected"]
    decision_reference: EvidenceReference | None = None
    legal_advice_retained_outside_repository: Literal[True]
    legal_opinion_present_in_manifest: Literal[False]

    @model_validator(mode="after")
    def require_final_decision_reference(self) -> CounselDecision:
        if self.decision != "pending" and not self.decision_reference:
            raise ValueError("cleared and rejected decisions require a reference")
        return self


class FounderDecision(_StrictModel):
    accountable_founder_role_reference: EvidenceReference
    decision: Literal["pending", "approve_name", "reject_name"]
    decision_reference: EvidenceReference | None = None
    public_rename_required: bool
    migration_plan_reference: EvidenceReference | None = None

    @model_validator(mode="after")
    def require_final_decision_reference(self) -> FounderDecision:
        if self.decision != "pending" and not self.decision_reference:
            raise ValueError("approve and reject decisions require a reference")
        if (
            self.decision == "approve_name"
            and self.public_rename_required
            and not self.migration_plan_reference
        ):
            raise ValueError("approved public renames require a migration plan")
        return self


class NamingClearanceDossier(_StrictModel):
    schema_version: Literal["0.1"]
    candidate: NamingCandidate
    scope: ClearanceScope
    searches: SearchCoverage
    ownership: OwnershipControls
    counsel: CounselDecision
    founder: FounderDecision

    @model_validator(mode="after")
    def require_consistent_final_decisions(self) -> NamingClearanceDossier:
        if (
            self.founder.decision == "approve_name"
            and self.counsel.decision != "cleared"
        ):
            raise ValueError("founder approval requires a cleared counsel decision")
        return self


class NamingReadinessFinding(_StrictModel):
    path: str
    code: str
    message: str


class NamingReadinessReport(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    candidate_id: str
    ready_for_founder_legal_review: bool
    declared_counsel_decision: Literal["invalid", "pending", "cleared", "rejected"]
    declared_founder_decision: Literal[
        "invalid", "pending", "approve_name", "reject_name"
    ]
    legal_clearance_established: Literal[False] = False
    pending_review_roles: tuple[Literal["legal_counsel", "founder"], ...]
    findings: tuple[NamingReadinessFinding, ...]
    limitations: str = _LIMITATIONS


def _safe_candidate_id(payload: Mapping[str, Any]) -> str:
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping):
        return "unknown"
    candidate_id = candidate.get("candidate_id")
    if isinstance(candidate_id, str) and _SAFE_IDENTIFIER.fullmatch(candidate_id):
        return candidate_id
    return "unknown"


def _finding_message(error_type: str) -> str:
    if error_type == "missing":
        return "required naming-readiness field is missing"
    if error_type == "extra_forbidden":
        return "unexpected field is forbidden"
    if error_type.startswith("literal_error"):
        return "value does not match the required fail-closed naming policy"
    return "value violates the naming-readiness contract"


def _finding_path(location: tuple[int | str, ...], error_type: str) -> str:
    parts = list(location)
    if error_type == "extra_forbidden" and parts:
        parts[-1] = "<unexpected-field>"
    return ".".join(str(part) for part in parts) or "dossier"


def validate_naming_clearance_dossier(
    payload: Mapping[str, Any],
) -> NamingReadinessReport:
    """Validate declarations without establishing legal clearance."""

    candidate_id = _safe_candidate_id(payload)
    try:
        dossier = NamingClearanceDossier.model_validate(payload)
    except ValidationError as exc:
        findings = tuple(
            sorted(
                (
                    NamingReadinessFinding(
                        path=_finding_path(tuple(error["loc"]), str(error["type"])),
                        code=str(error["type"]),
                        message=_finding_message(str(error["type"])),
                    )
                    for error in exc.errors(include_input=False, include_url=False)
                ),
                key=lambda item: (item.path, item.code, item.message),
            )
        )
        return NamingReadinessReport(
            candidate_id=candidate_id,
            ready_for_founder_legal_review=False,
            declared_counsel_decision="invalid",
            declared_founder_decision="invalid",
            pending_review_roles=(),
            findings=findings,
        )
    pending_roles: list[Literal["legal_counsel", "founder"]] = []
    if dossier.counsel.decision == "pending":
        pending_roles.append("legal_counsel")
    if dossier.founder.decision == "pending":
        pending_roles.append("founder")
    return NamingReadinessReport(
        candidate_id=dossier.candidate.candidate_id,
        ready_for_founder_legal_review=True,
        declared_counsel_decision=dossier.counsel.decision,
        declared_founder_decision=dossier.founder.decision,
        pending_review_roles=tuple(pending_roles),
        findings=(),
    )


def load_naming_clearance_dossier(path: str | Path) -> Mapping[str, Any]:
    dossier_path = Path(path)
    if dossier_path.stat().st_size > MAX_NAMING_DOSSIER_BYTES:
        raise ValueError(
            f"naming-clearance dossier exceeds {MAX_NAMING_DOSSIER_BYTES} bytes"
        )
    payload = json.loads(dossier_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("naming-clearance dossier must be a JSON object")
    return payload


def render_naming_readiness_markdown(report: NamingReadinessReport) -> str:
    if not report.ready_for_founder_legal_review:
        status = "INCOMPLETE"
    elif report.pending_review_roles:
        status = "READY FOR FOUNDER/LEGAL REVIEW; DECISIONS PENDING"
    else:
        status = "DECLARED DECISIONS RECORDED; EXTERNAL GATE STILL REQUIRED"
    lines = [
        "# Naming-Clearance Readiness Validation",
        "",
        f"- Candidate: `{report.candidate_id}`",
        f"- Result: **{status}**",
        "- Legal clearance established by this report: **NO**",
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


def write_naming_readiness_reports(
    report: NamingReadinessReport, output_directory: str | Path
) -> tuple[Path, Path]:
    output = Path(output_directory)
    json_path = output / "naming-readiness.json"
    markdown_path = output / "naming-readiness.md"
    serialized = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    _atomic_write(json_path, serialized + "\n")
    _atomic_write(markdown_path, render_naming_readiness_markdown(report))
    return json_path, markdown_path


__all__ = [
    "MAX_NAMING_DOSSIER_BYTES",
    "NamingClearanceDossier",
    "NamingReadinessReport",
    "load_naming_clearance_dossier",
    "render_naming_readiness_markdown",
    "validate_naming_clearance_dossier",
    "write_naming_readiness_reports",
]
