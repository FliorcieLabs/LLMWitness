"""Framework-neutral adapters that translate native events into envelopes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, Protocol

from llmwitness.envelope import (
    Actor,
    Authority,
    AuthorityDecision,
    Compensation,
    ContractReference,
    EffectDescriptor,
    EffectStatus,
    EvidenceReference,
    ExecutionEnvelope,
    Intent,
    Observation,
    RiskTier,
    StateReference,
    new_trace_id,
    sha256_ref,
)
from llmwitness.utils import canonical_json, generate_uuidv7, redact_payload

SUPPORTED_MAPPING_ADAPTERS = (
    "python",
    "opentelemetry",
    "langchain",
    "langgraph",
    "openai-agents",
    "mcp",
    "crewai",
    "autogen",
    "llamaindex",
)


class EnvelopeAdapter(Protocol):
    name: str

    def translate(self, event: Mapping[str, Any]) -> ExecutionEnvelope: ...


class AdapterRegistry:
    """Lazy adapter registry; optional frameworks are never imported at module load."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], EnvelopeAdapter]] = {}

    def register(self, name: str, factory: Callable[[], EnvelopeAdapter]) -> None:
        if not name or name in self._factories:
            raise ValueError(f"adapter {name!r} is empty or already registered")
        self._factories[name] = factory

    def create(self, name: str) -> EnvelopeAdapter:
        try:
            return self._factories[name]()
        except KeyError as exc:
            raise KeyError(
                f"unknown adapter {name!r}; available: {', '.join(self.names())}"
            ) from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


class MappingEnvelopeAdapter:
    """Translate normalized mappings emitted by Python, OTel, or framework hooks."""

    def __init__(self, name: str):
        self.name = name

    def translate(self, event: Mapping[str, Any]) -> ExecutionEnvelope:
        raw_parameters = event.get("parameters", {})
        parameters = redact_payload(raw_parameters)
        evidence = [
            EvidenceReference.model_validate(item) for item in event.get("evidence", [])
        ]
        status = EffectStatus(event.get("status", EffectStatus.PLANNED.value))
        return ExecutionEnvelope.create(
            run_id=str(event.get("run_id") or generate_uuidv7()),
            event_id=str(event.get("event_id") or generate_uuidv7()),
            parent_event_id=event.get("parent_event_id"),
            trace_id=str(event.get("trace_id") or new_trace_id()),
            actor=Actor(
                agent_id=str(event.get("agent_id") or "unknown-agent"),
                principal_id=str(event.get("principal_id") or "local-user"),
            ),
            intent=Intent(
                name=str(event.get("intent") or event.get("name") or "observe"),
                reason=str(event.get("reason") or ""),
            ),
            authority=Authority(
                policy_id=str(event.get("policy_id") or "local-default"),
                decision=AuthorityDecision(event.get("authority_decision", "allow")),
                risk_tier=RiskTier(event.get("risk_tier", "R0")),
                approval_id=event.get("approval_id"),
            ),
            contract=ContractReference(
                contract_id=str(event.get("contract_id") or "observation-v0.1"),
                preconditions=list(event.get("preconditions", [])),
                invariants=list(event.get("invariants", [])),
                postconditions=list(event.get("postconditions", [])),
                budgets=dict(event.get("budgets", {})),
            ),
            pre_state=StateReference(
                snapshot_ref=str(event.get("pre_state_ref") or sha256_ref(b""))
            ),
            effect=EffectDescriptor(
                adapter=str(event.get("effect_adapter") or self.name),
                idempotency_key=str(
                    event.get("idempotency_key")
                    or event.get("event_id")
                    or generate_uuidv7()
                ),
                parameters_hash=str(
                    event.get("parameters_hash")
                    or sha256_ref(canonical_json(parameters))
                ),
            ),
            observation=Observation(
                status=status,
                external_refs=list(event.get("external_refs", [])),
                verifier=event.get("verifier"),
                detail=event.get("detail"),
            ),
            post_state=StateReference(
                snapshot_ref=str(event.get("post_state_ref") or sha256_ref(b""))
            ),
            compensation=Compensation(
                available=bool(event.get("compensation_available", False)),
                adapter=event.get("compensation_adapter"),
            ),
            evidence=evidence,
        )


class EnvelopeTranslator:
    """Small facade that keeps framework selection outside translation logic."""

    def __init__(self, registry: AdapterRegistry):
        self.registry = registry

    def translate(
        self, adapter_name: str, event: Mapping[str, Any]
    ) -> ExecutionEnvelope:
        return self.registry.create(adapter_name).translate(event)


def default_adapter_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    for name in SUPPORTED_MAPPING_ADAPTERS:
        registry.register(name, partial(MappingEnvelopeAdapter, name))
    return registry


def envelope_to_otel_attributes(envelope: ExecutionEnvelope) -> dict[str, Any]:
    """Export stable reliability fields without requiring the OTel package."""
    return {
        "gen_ai.operation.name": envelope.intent.name,
        "fliorcie.schema.version": envelope.schema_version,
        "fliorcie.run.id": envelope.run_id,
        "fliorcie.event.id": envelope.event_id,
        "fliorcie.effect.adapter": envelope.effect.adapter,
        "fliorcie.effect.status": envelope.observation.status.value,
        "fliorcie.authority.decision": envelope.authority.decision.value,
        "fliorcie.risk.tier": envelope.authority.risk_tier.value,
        "fliorcie.parameters.hash": envelope.effect.parameters_hash,
    }
