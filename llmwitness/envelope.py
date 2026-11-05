"""Versioned, framework-neutral reliability envelopes."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llmwitness.utils import generate_uuidv7, redact_payload

SCHEMA_VERSION = "0.1"
GENESIS_HASH = "sha256:" + ("0" * 64)
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_UUIDV7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def sha256_ref(value: bytes | str) -> str:
    """Return a content-addressed SHA-256 reference."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def new_trace_id() -> str:
    """Create a non-zero OpenTelemetry-compatible trace identifier."""
    while True:
        value = os.urandom(16).hex()
        if value != "0" * 32:
            return value


class EnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskTier(str, Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"


class AuthorityDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"
    REQUIRE_APPROVAL = "require_approval"


class EffectStatus(str, Enum):
    PLANNED = "planned"
    AUTHORIZED = "authorized"
    PREPARED = "prepared"
    EXECUTING = "executing"
    SUCCEEDED_UNVERIFIED = "succeeded_unverified"
    FAILED = "failed"
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    MANUAL_REVIEW = "manual_review"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"


class Actor(EnvelopeModel):
    agent_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=256)


class Intent(EnvelopeModel):
    name: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=2048)


class Authority(EnvelopeModel):
    policy_id: str = Field(min_length=1, max_length=256)
    decision: AuthorityDecision
    risk_tier: RiskTier
    approval_id: str | None = Field(default=None, max_length=256)


class ContractReference(EnvelopeModel):
    contract_id: str = Field(min_length=1, max_length=256)
    preconditions: list[str] = Field(default_factory=list, max_length=100)
    invariants: list[str] = Field(default_factory=list, max_length=100)
    postconditions: list[str] = Field(default_factory=list, max_length=100)
    budgets: dict[str, float] = Field(default_factory=dict)

    @field_validator("budgets")
    @classmethod
    def validate_budgets(cls, value: dict[str, float]) -> dict[str, float]:
        if any(limit < 0 for limit in value.values()):
            raise ValueError("contract budgets must be non-negative")
        return value


class StateReference(EnvelopeModel):
    snapshot_ref: str = Field(pattern=_SHA256_REF.pattern)

    @field_validator("snapshot_ref")
    @classmethod
    def validate_snapshot_ref(cls, value: str) -> str:
        if not _SHA256_REF.fullmatch(value):
            raise ValueError("snapshot_ref must be a sha256:<hex> reference")
        return value


class EffectDescriptor(EnvelopeModel):
    adapter: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=512)
    parameters_hash: str = Field(pattern=_SHA256_REF.pattern)

    @field_validator("parameters_hash")
    @classmethod
    def validate_parameters_hash(cls, value: str) -> str:
        if not _SHA256_REF.fullmatch(value):
            raise ValueError("parameters_hash must be a sha256:<hex> reference")
        return value


class Observation(EnvelopeModel):
    status: EffectStatus
    external_refs: list[str] = Field(default_factory=list, max_length=100)
    verifier: str | None = Field(default=None, max_length=256)
    detail: str | None = Field(default=None, max_length=2048)


class Compensation(EnvelopeModel):
    available: bool
    adapter: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def require_adapter_when_available(self) -> Compensation:
        if self.available and not self.adapter:
            raise ValueError("available compensation requires an adapter")
        return self


class EvidenceReference(EnvelopeModel):
    kind: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=_SHA256_REF.pattern)
    uri: str | None = Field(default=None, max_length=2048)
    observed_at: datetime | None = None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_REF.fullmatch(value):
            raise ValueError("evidence sha256 must be a sha256:<hex> reference")
        return value

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("observed_at must be an RFC 3339 timestamp with timezone")
        return value


class ExecutionEnvelope(EnvelopeModel):
    """Canonical reliability record for one meaningful action or transition."""

    schema_version: Literal["0.1"] = "0.1"
    run_id: str = Field(pattern=_UUIDV7.pattern)
    event_id: str = Field(pattern=_UUIDV7.pattern)
    parent_event_id: str | None = Field(default=None, pattern=_UUIDV7.pattern)
    trace_id: str = Field(pattern=_TRACE_ID.pattern)
    actor: Actor
    intent: Intent
    authority: Authority
    contract: ContractReference
    pre_state: StateReference
    effect: EffectDescriptor
    observation: Observation
    post_state: StateReference
    compensation: Compensation
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=100)
    previous_journal_hash: str = Field(
        default=GENESIS_HASH, pattern=_SHA256_REF.pattern
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("run_id", "event_id", "parent_event_id")
    @classmethod
    def validate_uuidv7(cls, value: str | None) -> str | None:
        if value is None:
            return None
        import uuid

        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("identifier must be an RFC 9562 UUIDv7") from exc
        if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
            raise ValueError("identifier must be an RFC 9562 UUIDv7")
        return str(parsed)

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        normalized = value.lower()
        if not _TRACE_ID.fullmatch(normalized) or normalized == "0" * 32:
            raise ValueError("trace_id must be a non-zero 32-character hexadecimal id")
        return normalized

    @field_validator("previous_journal_hash")
    @classmethod
    def validate_previous_hash(cls, value: str) -> str:
        if not _SHA256_REF.fullmatch(value):
            raise ValueError("previous_journal_hash must be a sha256:<hex> reference")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("created_at must be an RFC 3339 timestamp with timezone")
        return value

    @model_validator(mode="after")
    def validate_evidence_and_secrets(self) -> ExecutionEnvelope:
        if self.observation.status in {
            EffectStatus.VERIFIED,
            EffectStatus.COMPENSATED,
        }:
            if not self.evidence or not self.observation.verifier:
                raise ValueError(
                    "VERIFIED requires evidence and verifier; COMPENSATED requires the same"
                )
        # Scan only human/provider-controlled content. UUIDs, trace IDs, timestamps,
        # and cryptographic digests are opaque structural fields and can otherwise
        # accidentally resemble payment-card patterns.
        scan_payload = {
            "actor": self.actor.model_dump(mode="json"),
            "intent": self.intent.model_dump(mode="json"),
            "authority": self.authority.model_dump(mode="json"),
            "contract": self.contract.model_dump(mode="json"),
            "effect": {
                "adapter": self.effect.adapter,
                "idempotency_key": self.effect.idempotency_key,
            },
            "observation": self.observation.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
        }
        if redact_payload(scan_payload) != scan_payload:
            raise ValueError(
                "execution envelope contains a recognized raw secret or sensitive value"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        actor: Actor,
        intent: Intent,
        authority: Authority,
        contract: ContractReference,
        effect: EffectDescriptor,
        observation: Observation,
        pre_state: StateReference | None = None,
        post_state: StateReference | None = None,
        compensation: Compensation | None = None,
        evidence: list[EvidenceReference] | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        parent_event_id: str | None = None,
        trace_id: str | None = None,
        previous_journal_hash: str = GENESIS_HASH,
    ) -> ExecutionEnvelope:
        empty_state = StateReference(snapshot_ref=sha256_ref(b""))
        return cls(
            run_id=run_id or generate_uuidv7(),
            event_id=event_id or generate_uuidv7(),
            parent_event_id=parent_event_id,
            trace_id=trace_id or new_trace_id(),
            actor=actor,
            intent=intent,
            authority=authority,
            contract=contract,
            pre_state=pre_state or empty_state,
            effect=effect,
            observation=observation,
            post_state=post_state or empty_state,
            compensation=compensation or Compensation(available=False),
            evidence=evidence or [],
            previous_journal_hash=previous_journal_hash,
        )


class UnsupportedEnvelopeVersion(ValueError):
    """Raised when no backwards reader or migration path is registered."""


def read_envelope(value: dict[str, Any]) -> ExecutionEnvelope:
    """Read a supported envelope or fail with an explicit migration error."""
    version = value.get("schema_version")
    if version != SCHEMA_VERSION:
        raise UnsupportedEnvelopeVersion(
            f"unsupported execution envelope schema {version!r}; no migration to {SCHEMA_VERSION} is registered"
        )
    return ExecutionEnvelope.model_validate(value)
