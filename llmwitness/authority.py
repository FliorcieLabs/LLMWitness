"""Least-privilege authority policy evaluation and risk classification."""

from __future__ import annotations

from dataclasses import dataclass

from llmwitness.envelope import AuthorityDecision, RiskTier


@dataclass(frozen=True)
class AuthorityPolicy:
    policy_id: str
    allowed_effects: frozenset[str]
    allowed_principals: frozenset[str]
    maximum_risk: RiskTier = RiskTier.R2
    maximum_delegation_depth: int = 0


@dataclass(frozen=True)
class AuthorityResult:
    policy_id: str
    decision: AuthorityDecision
    risk_tier: RiskTier
    reason: str
    approval_id: str | None = None


class RiskClassifier:
    """Classify effect risk independently from policy enforcement."""

    def classify(
        self,
        *,
        read_only: bool = False,
        reversible: bool = False,
        irreversible: bool = False,
    ) -> RiskTier:
        if read_only:
            return RiskTier.R0
        if irreversible:
            return RiskTier.R3
        return RiskTier.R1 if reversible else RiskTier.R2


class AuthorityEngine:
    """Evaluate a closed authority policy without executing an effect."""

    _ORDER = {RiskTier.R0: 0, RiskTier.R1: 1, RiskTier.R2: 2, RiskTier.R3: 3}

    def evaluate(
        self,
        policy: AuthorityPolicy,
        *,
        effect_name: str,
        principal_id: str,
        risk_tier: RiskTier,
        delegation_depth: int = 0,
        approval_id: str | None = None,
    ) -> AuthorityResult:
        if (
            effect_name not in policy.allowed_effects
            or principal_id not in policy.allowed_principals
        ):
            return AuthorityResult(
                policy.policy_id,
                AuthorityDecision.DENY,
                risk_tier,
                "effect or principal is outside policy scope",
            )
        if delegation_depth > policy.maximum_delegation_depth:
            return AuthorityResult(
                policy.policy_id,
                AuthorityDecision.DENY,
                risk_tier,
                "delegation depth exceeds policy",
            )
        if self._ORDER[risk_tier] > self._ORDER[policy.maximum_risk]:
            return AuthorityResult(
                policy.policy_id,
                AuthorityDecision.DENY,
                risk_tier,
                "risk exceeds policy maximum",
            )
        if risk_tier == RiskTier.R3 and not approval_id:
            return AuthorityResult(
                policy.policy_id,
                AuthorityDecision.REQUIRE_APPROVAL,
                risk_tier,
                "R3 requires explicit approval",
            )
        return AuthorityResult(
            policy.policy_id,
            AuthorityDecision.ALLOW,
            risk_tier,
            "policy allowed",
            approval_id,
        )


__all__ = [
    "AuthorityEngine",
    "AuthorityPolicy",
    "AuthorityResult",
    "RiskClassifier",
]
