"""Risk-aware contract definitions and deterministic evaluation."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from llmwitness.envelope import RiskTier


class EvaluationClass(str, Enum):
    DETERMINISTIC = "deterministic"
    EXTERNAL = "external"
    MODEL_ASSISTED = "model_assisted"


class CheckStatus(str, Enum):
    PASS = "pass"  # noqa: S105 - contract result vocabulary, not a credential
    FAIL = "fail"
    UNKNOWN = "unknown"


CheckFunction = Callable[[Mapping[str, Any]], bool | None | Awaitable[bool | None]]


@dataclass(frozen=True)
class ContractCheck:
    name: str
    evaluation_class: EvaluationClass
    evaluate: CheckFunction
    blocking: bool = True


@dataclass(frozen=True)
class AgentContract:
    contract_id: str
    preconditions: tuple[ContractCheck, ...] = ()
    invariants: tuple[ContractCheck, ...] = ()
    postconditions: tuple[ContractCheck, ...] = ()
    budgets: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(limit < 0 for limit in self.budgets.values()):
            raise ValueError("contract budgets must be non-negative")


@dataclass(frozen=True)
class CheckResult:
    name: str
    evaluation_class: EvaluationClass
    status: CheckStatus
    blocking: bool
    detail: str | None = None


@dataclass(frozen=True)
class ContractEvaluation:
    contract_id: str
    results: tuple[CheckResult, ...]
    allowed: bool


class ContractEngine:
    """Evaluate one contract phase without knowing effect or storage details."""

    async def evaluate(
        self,
        contract: AgentContract,
        values: Mapping[str, Any],
        risk_tier: RiskTier,
        phase: str = "preconditions",
    ) -> ContractEvaluation:
        checks = tuple(getattr(contract, phase))

        results: list[CheckResult] = []
        if phase == "preconditions":
            for name, limit in contract.budgets.items():
                usage = values.get(name)
                if isinstance(usage, (int, float)) and not isinstance(usage, bool):
                    status = (
                        CheckStatus.PASS if float(usage) <= limit else CheckStatus.FAIL
                    )
                    budget_detail = f"usage={usage}; limit={limit}"
                else:
                    status = CheckStatus.FAIL
                    budget_detail = "budget usage is missing or non-numeric"
                results.append(
                    CheckResult(
                        f"budget:{name}",
                        EvaluationClass.DETERMINISTIC,
                        status,
                        True,
                        budget_detail,
                    )
                )

        for check in checks:
            try:
                outcome = check.evaluate(values)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                status = (
                    CheckStatus.PASS
                    if outcome is True
                    else CheckStatus.FAIL if outcome is False else CheckStatus.UNKNOWN
                )
                detail = None
            except Exception as exc:
                status = CheckStatus.UNKNOWN
                detail = type(exc).__name__
            results.append(
                CheckResult(
                    check.name, check.evaluation_class, status, check.blocking, detail
                )
            )

        blocking_failure = any(
            item.blocking and item.status == CheckStatus.FAIL for item in results
        )
        blocking_unknown = any(
            item.blocking and item.status == CheckStatus.UNKNOWN for item in results
        )
        model_only = bool(results) and all(
            item.evaluation_class == EvaluationClass.MODEL_ASSISTED for item in results
        )
        allowed = not blocking_failure
        if risk_tier in {RiskTier.R2, RiskTier.R3} and (blocking_unknown or model_only):
            allowed = False
        return ContractEvaluation(contract.contract_id, tuple(results), allowed)


__all__ = [
    "AgentContract",
    "CheckFunction",
    "CheckResult",
    "CheckStatus",
    "ContractCheck",
    "ContractEngine",
    "ContractEvaluation",
    "EvaluationClass",
]
