"""Compatibility facade for the phase-specific Community reliability modules.

New code should import the narrow module it needs: contracts, authority, effects,
transactions, state, or replay. Existing imports from llmwitness.runtime remain
supported.
"""

from llmwitness.authority import (
    AuthorityEngine,
    AuthorityPolicy,
    AuthorityResult,
    RiskClassifier,
)
from llmwitness.contracts import (
    AgentContract,
    CheckFunction,
    CheckResult,
    CheckStatus,
    ContractCheck,
    ContractEngine,
    ContractEvaluation,
    EvaluationClass,
)
from llmwitness.effects import (
    DefiniteEffectFailure,
    EffectAdapter,
    EffectContext,
    EffectExecution,
    EffectIdentity,
    EffectRegistry,
    EffectRejected,
    IdempotencyCoordinator,
    SafeEffectRunner,
    VerificationEngine,
    VerificationResult,
)
from llmwitness.planning import EffectPlan, EffectPlanner, NextSafeAction, PlanCheck
from llmwitness.replay import ReplayEngine, ReplayMode
from llmwitness.state import (
    InMemoryStateAdapter,
    StateAdapter,
    StateDiff,
    StateKind,
    StateManager,
    StateSnapshot,
    json_safe_copy,
)
from llmwitness.transactions import (
    AgentTransaction,
    RecoveryEngine,
    TransactionCoordinator,
    TransactionHalted,
)

__all__ = [
    "AgentContract",
    "AgentTransaction",
    "AuthorityEngine",
    "AuthorityPolicy",
    "AuthorityResult",
    "CheckFunction",
    "CheckResult",
    "CheckStatus",
    "ContractCheck",
    "ContractEngine",
    "ContractEvaluation",
    "DefiniteEffectFailure",
    "EffectAdapter",
    "EffectContext",
    "EffectExecution",
    "EffectIdentity",
    "EffectPlan",
    "EffectPlanner",
    "EffectRegistry",
    "EffectRejected",
    "EvaluationClass",
    "IdempotencyCoordinator",
    "InMemoryStateAdapter",
    "NextSafeAction",
    "PlanCheck",
    "RecoveryEngine",
    "ReplayEngine",
    "ReplayMode",
    "RiskClassifier",
    "SafeEffectRunner",
    "StateAdapter",
    "StateDiff",
    "StateKind",
    "StateManager",
    "StateSnapshot",
    "TransactionCoordinator",
    "TransactionHalted",
    "VerificationEngine",
    "VerificationResult",
    "json_safe_copy",
]
