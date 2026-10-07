"""Offline provider-dossier validation for human adapter review.

This module validates metadata and review references only. It never reads a
credential, calls a provider, approves an adapter, or changes Runtime state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from llmwitness.utils import atomic_write_text as _atomic_write

MAX_DOSSIER_BYTES = 256 * 1024
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]$")
_LIMITATIONS = (
    "This offline result checks dossier structure and declared safety semantics only. "
    "It does not approve an adapter, inspect credentials, call a provider, verify "
    "external truth, or establish production readiness."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _require_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("must be an HTTPS URL without embedded credentials")
    return value


class ProviderIdentity(_StrictModel):
    provider_id: str = Field(min_length=3, max_length=128)
    api_version: str = Field(min_length=1, max_length=128)
    documentation_url: str = Field(min_length=1, max_length=2048)
    license_review_reference: str = Field(min_length=1, max_length=512)
    retention_review_reference: str = Field(min_length=1, max_length=512)

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("must be a stable lowercase provider identifier")
        return value

    @field_validator("documentation_url")
    @classmethod
    def validate_documentation_url(cls, value: str) -> str:
        return _require_https_url(value)


class SandboxAuthorization(_StrictModel):
    credential_environment_variable: str = Field(min_length=3, max_length=128)
    authorized_for_sandbox_tests: Literal[True]
    authorization_reference: str = Field(min_length=1, max_length=512)

    @field_validator("credential_environment_variable")
    @classmethod
    def validate_environment_variable(cls, value: str) -> str:
        if not _ENVIRONMENT_NAME.fullmatch(value):
            raise ValueError(
                "must name an environment variable, not contain a credential"
            )
        return value


class IdempotencyContract(_StrictModel):
    provider_supports_idempotency: Literal[True]
    key_scope: str = Field(min_length=1, max_length=512)
    key_lifetime_seconds: int = Field(gt=0, le=31_536_000)
    collision_behavior: str = Field(min_length=1, max_length=512)
    duplicate_behavior: str = Field(min_length=1, max_length=512)
    unknown_outcome_policy: Literal["reconcile_before_retry"]


class VerificationContract(_StrictModel):
    authoritative: Literal[True]
    endpoint_url: str = Field(min_length=1, max_length=2048)
    verifier_identity: str = Field(min_length=1, max_length=256)
    consistency_model: str = Field(min_length=1, max_length=512)
    maximum_freshness_seconds: int = Field(ge=0, le=86_400)
    stale_result_policy: Literal["not_verified"]
    exception_policy: Literal["unknown"]

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str) -> str:
        return _require_https_url(value)


class FailureTaxonomy(_StrictModel):
    definite_failure: str = Field(min_length=1, max_length=512)
    response_loss: str = Field(min_length=1, max_length=512)
    timeout: str = Field(min_length=1, max_length=512)
    rate_limit: str = Field(min_length=1, max_length=512)
    partial_success: str = Field(min_length=1, max_length=512)
    ambiguous_outcome_state: Literal["unknown_until_reconciled"]


class CompensationContract(_StrictModel):
    mode: Literal["supported", "irreversible"]
    semantics: str = Field(min_length=1, max_length=1024)
    endpoint_url: str | None = Field(default=None, max_length=2048)
    verification_endpoint_url: str | None = Field(default=None, max_length=2048)
    irreversible_cases: tuple[str, ...] = ()
    failed_compensation_state: Literal["unknown"]

    @field_validator("endpoint_url", "verification_endpoint_url")
    @classmethod
    def validate_optional_endpoint_url(cls, value: str | None) -> str | None:
        return None if value is None else _require_https_url(value)

    @field_validator("irreversible_cases")
    @classmethod
    def validate_irreversible_cases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 512 for item in value):
            raise ValueError("entries must be non-empty and at most 512 characters")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> CompensationContract:
        if self.mode == "supported" and (
            self.endpoint_url is None or self.verification_endpoint_url is None
        ):
            raise ValueError(
                "supported compensation requires execute and verification endpoints"
            )
        if self.mode == "irreversible" and not self.irreversible_cases:
            raise ValueError(
                "irreversible compensation requires documented irreversible cases"
            )
        return self


class PrivacyAssessment(_StrictModel):
    review_reference: str = Field(min_length=1, max_length=512)
    request_data_handling: str = Field(min_length=1, max_length=1024)
    response_data_handling: str = Field(min_length=1, max_length=1024)
    identifier_handling: str = Field(min_length=1, max_length=1024)
    evidence_handling: str = Field(min_length=1, max_length=1024)
    log_scrubbing_claim: Literal["best_effort_pattern_scrubbing"]


class ProviderAdapterDossier(_StrictModel):
    schema_version: Literal["0.1"]
    identity: ProviderIdentity
    sandbox: SandboxAuthorization
    idempotency: IdempotencyContract
    verification: VerificationContract
    failures: FailureTaxonomy
    compensation: CompensationContract
    privacy: PrivacyAssessment


class DossierFinding(_StrictModel):
    path: str
    code: str
    message: str


class ProviderReadinessReport(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    provider_id: str
    ready_for_human_review: bool
    findings: tuple[DossierFinding, ...]
    limitations: str = _LIMITATIONS


def _safe_provider_id(payload: Mapping[str, Any]) -> str:
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return "unknown"
    provider_id = identity.get("provider_id")
    if isinstance(provider_id, str) and _IDENTIFIER.fullmatch(provider_id):
        return provider_id
    return "unknown"


def _finding_message(error_type: str) -> str:
    if error_type == "missing":
        return "required dossier field is missing"
    if error_type == "extra_forbidden":
        return "unexpected field is forbidden"
    if error_type.startswith("literal_error"):
        return "value does not match the required fail-closed policy"
    if "string_pattern" in error_type:
        return "value has an invalid safe format"
    return "value violates the provider-dossier contract"


def _finding_path(location: tuple[int | str, ...], error_type: str) -> str:
    parts = list(location)
    if error_type == "extra_forbidden" and parts:
        parts[-1] = "<unexpected-field>"
    return ".".join(str(part) for part in parts) or "dossier"


def validate_provider_dossier(payload: Mapping[str, Any]) -> ProviderReadinessReport:
    """Validate a dossier without echoing submitted values into the report."""

    provider_id = _safe_provider_id(payload)
    try:
        dossier = ProviderAdapterDossier.model_validate(payload)
    except ValidationError as exc:
        findings = tuple(
            sorted(
                (
                    DossierFinding(
                        path=_finding_path(tuple(error["loc"]), str(error["type"])),
                        code=str(error["type"]),
                        message=_finding_message(str(error["type"])),
                    )
                    for error in exc.errors(include_input=False, include_url=False)
                ),
                key=lambda item: (item.path, item.code, item.message),
            )
        )
        return ProviderReadinessReport(
            provider_id=provider_id,
            ready_for_human_review=False,
            findings=findings,
        )
    return ProviderReadinessReport(
        provider_id=dossier.identity.provider_id,
        ready_for_human_review=True,
        findings=(),
    )


def load_provider_dossier(path: str | Path) -> Mapping[str, Any]:
    dossier_path = Path(path)
    size = dossier_path.stat().st_size
    if size > MAX_DOSSIER_BYTES:
        raise ValueError(f"provider dossier exceeds {MAX_DOSSIER_BYTES} bytes")
    payload = json.loads(dossier_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provider dossier must be a JSON object")
    return payload


def render_provider_readiness_markdown(report: ProviderReadinessReport) -> str:
    status = "READY FOR HUMAN REVIEW" if report.ready_for_human_review else "INCOMPLETE"
    lines = [
        "# Provider Dossier Validation",
        "",
        f"- Provider: `{report.provider_id}`",
        f"- Result: **{status}**",
        "",
        report.limitations,
        "",
        "## Findings",
        "",
    ]
    if report.findings:
        lines.extend(
            f"- `{finding.path}` (`{finding.code}`): {finding.message}"
            for finding in report.findings
        )
    else:
        lines.append("- No structural or declared-policy findings.")
    return "\n".join(lines) + "\n"


def write_provider_readiness_reports(
    report: ProviderReadinessReport, output_directory: str | Path
) -> tuple[Path, Path]:
    output = Path(output_directory)
    json_path = output / "provider-readiness.json"
    markdown_path = output / "provider-readiness.md"
    serialized = (
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    _atomic_write(json_path, serialized)
    _atomic_write(markdown_path, render_provider_readiness_markdown(report))
    return json_path, markdown_path


__all__ = [
    "MAX_DOSSIER_BYTES",
    "ProviderAdapterDossier",
    "ProviderReadinessReport",
    "load_provider_dossier",
    "render_provider_readiness_markdown",
    "validate_provider_dossier",
    "write_provider_readiness_reports",
]
