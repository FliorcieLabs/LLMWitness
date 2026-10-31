"""Side-effect-free authority and contract planning explanations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from llmwitness.authority import AuthorityResult
from llmwitness.contracts import (
    AgentContract,
    CheckResult,
    CheckStatus,
    ContractEngine,
    EvaluationClass,
)
from llmwitness.envelope import AuthorityDecision, RiskTier, sha256_ref
from llmwitness.utils import canonical_json, redact_payload

NextSafeAction = Literal[
    "execute", "request_approval", "revise_authority", "revise_contract"
]
_LIMITATIONS = (
    "Planning evaluated local authority and contract inputs only; it did not "
    "open a journal, reserve idempotency, load a provider, execute an effect, "
    "or verify external truth."
)


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    scrubbed = redact_payload(value)
    return scrubbed if isinstance(scrubbed, str) else "[REDACTED]"


class PlanCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    evaluation_class: EvaluationClass
    status: CheckStatus
    blocking: bool
    detail: str | None = None

    @classmethod
    def from_result(cls, result: CheckResult) -> PlanCheck:
        return cls(
            name=_safe_text(result.name) or "[REDACTED]",
            evaluation_class=result.evaluation_class,
            status=result.status,
            blocking=result.blocking,
            # Check details may contain raw inputs or resource usage. Keep the
            # decision and status, but do not export those values in a plan.
            detail=None,
        )


class EffectPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1"] = "0.1"
    effect_name: str
    risk_tier: RiskTier
    policy_id: str
    authority_decision: AuthorityDecision
    authority_reason: str
    approval_present: bool
    contract_id: str
    preconditions: tuple[PlanCheck, ...]
    invariants: tuple[PlanCheck, ...]
    postconditions_pending: tuple[str, ...]
    request_hash: str
    values_hash: str
    allowed: bool
    next_safe_action: NextSafeAction
    limitations: str = _LIMITATIONS


class EffectPlanner:
    """Explain current policy gates without accepting execution dependencies."""

    def __init__(self, contract_engine: ContractEngine | None = None):
        self.contract_engine = contract_engine or ContractEngine()

    async def plan(
        self,
        *,
        effect_name: str,
        authority: AuthorityResult,
        contract: AgentContract,
        values: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> EffectPlan:
        if not effect_name.strip():
            raise ValueError("effect_name must not be blank")
        request_hash = sha256_ref(canonical_json(dict(request)))
        values_hash = sha256_ref(canonical_json(dict(values)))
        preconditions = await self.contract_engine.evaluate(
            contract, values, authority.risk_tier, phase="preconditions"
        )
        invariants = await self.contract_engine.evaluate(
            contract, values, authority.risk_tier, phase="invariants"
        )
        approval_present = bool(authority.approval_id)
        approval_missing = authority.risk_tier == RiskTier.R3 and not approval_present
        authority_allowed = (
            authority.decision == AuthorityDecision.ALLOW and not approval_missing
        )
        allowed = authority_allowed and preconditions.allowed and invariants.allowed
        if allowed:
            next_action: NextSafeAction = "execute"
        elif (
            authority.decision == AuthorityDecision.REQUIRE_APPROVAL or approval_missing
        ):
            next_action = "request_approval"
        elif not authority_allowed:
            next_action = "revise_authority"
        else:
            next_action = "revise_contract"
        return EffectPlan(
            effect_name=_safe_text(effect_name) or "[REDACTED]",
            risk_tier=authority.risk_tier,
            policy_id=_safe_text(authority.policy_id) or "[REDACTED]",
            authority_decision=authority.decision,
            authority_reason=_safe_text(authority.reason) or "[REDACTED]",
            approval_present=approval_present,
            contract_id=_safe_text(contract.contract_id) or "[REDACTED]",
            preconditions=tuple(
                PlanCheck.from_result(item) for item in preconditions.results
            ),
            invariants=tuple(
                PlanCheck.from_result(item) for item in invariants.results
            ),
            postconditions_pending=tuple(
                _safe_text(item.name) or "[REDACTED]"
                for item in contract.postconditions
            ),
            request_hash=request_hash,
            values_hash=values_hash,
            allowed=allowed,
            next_safe_action=next_action,
        )


__all__ = ["EffectPlan", "EffectPlanner", "NextSafeAction", "PlanCheck"]
