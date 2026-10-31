"""Regression tests for phase ownership and runtime import compatibility."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import llmwitness.authority as authority
import llmwitness.contracts as contracts
import llmwitness.effects as effects
import llmwitness.replay as replay
import llmwitness.runtime as runtime
import llmwitness.state as state
import llmwitness.transactions as transactions


@pytest.mark.parametrize(
    ("module", "names"),
    (
        (
            contracts,
            (
                "AgentContract",
                "CheckResult",
                "CheckStatus",
                "ContractCheck",
                "ContractEngine",
                "ContractEvaluation",
                "EvaluationClass",
            ),
        ),
        (
            authority,
            (
                "AuthorityEngine",
                "AuthorityPolicy",
                "AuthorityResult",
                "RiskClassifier",
            ),
        ),
        (
            effects,
            (
                "DefiniteEffectFailure",
                "EffectAdapter",
                "EffectContext",
                "EffectExecution",
                "EffectIdentity",
                "EffectRegistry",
                "EffectRejected",
                "IdempotencyCoordinator",
                "SafeEffectRunner",
                "VerificationEngine",
                "VerificationResult",
            ),
        ),
        (
            transactions,
            (
                "AgentTransaction",
                "RecoveryEngine",
                "TransactionCoordinator",
                "TransactionHalted",
            ),
        ),
        (
            state,
            (
                "InMemoryStateAdapter",
                "StateAdapter",
                "StateDiff",
                "StateKind",
                "StateManager",
                "StateSnapshot",
                "json_safe_copy",
            ),
        ),
        (replay, ("ReplayEngine", "ReplayMode")),
    ),
)
def test_runtime_facade_reexports_owning_module_symbols(module, names):
    for name in names:
        owned = getattr(module, name)
        assert getattr(runtime, name) is owned
        assert owned.__module__ == module.__name__


def test_runtime_facade_contains_no_workflow_implementation():
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = (ast.Expr, ast.ImportFrom, ast.Assign)
    assert all(isinstance(node, allowed) for node in tree.body)


def test_package_internals_do_not_depend_on_runtime_facade():
    package_root = Path(runtime.__file__).parent
    offenders: list[str] = []
    for path in package_root.glob("*.py"):
        if path.name == "runtime.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "llmwitness.runtime":
                offenders.append(path.name)
            if isinstance(node, ast.Import):
                if any(alias.name == "llmwitness.runtime" for alias in node.names):
                    offenders.append(path.name)
    assert offenders == []
