"""Saga-style transaction coordination and explicit recovery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from llmwitness.effects import (
    EffectAdapter,
    EffectContext,
    EffectExecution,
    SafeEffectRunner,
)
from llmwitness.envelope import EffectStatus


class TransactionHalted(RuntimeError):
    pass


class AgentTransaction:
    """Saga-like transaction; recovery uses compensation, never ACID rollback."""

    def __init__(self, name: str, runner: SafeEffectRunner, context: EffectContext):
        self.name = name
        self.runner = runner
        self.context = context
        self.completed: list[tuple[EffectAdapter, EffectExecution]] = []
        self.final_state = "open"

    async def __aenter__(self) -> AgentTransaction:
        return self

    async def effect(
        self, adapter: EffectAdapter, request: Mapping[str, Any]
    ) -> EffectExecution:
        if self.final_state != "open":
            raise TransactionHalted(f"transaction is {self.final_state}")
        try:
            execution = await self.runner.run(adapter, request, self.context)
        except Exception:
            self.final_state = "manual_review"
            raise
        if execution.state == EffectStatus.VERIFIED:
            self.completed.append((adapter, execution))
            return execution
        self.final_state = (
            "manual_review" if execution.state == EffectStatus.UNKNOWN else "failed"
        )
        raise TransactionHalted(f"effect ended in {execution.state.value}")

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc is None:
            if self.final_state == "open":
                self.final_state = "verified"
            return False
        unresolved = False
        for adapter, execution in reversed(self.completed):
            try:
                compensated = await self.runner.compensate(
                    adapter, execution, self.context
                )
            except Exception:
                self.final_state = "manual_review"
                raise
            unresolved = unresolved or compensated.state != EffectStatus.COMPENSATED
        self.final_state = (
            "manual_review"
            if unresolved or self.final_state == "manual_review"
            else "compensated"
        )
        return False


class RecoveryEngine:
    def __init__(self, runner: SafeEffectRunner):
        self.runner = runner

    async def compensate(
        self, adapter: EffectAdapter, execution: EffectExecution, context: EffectContext
    ) -> EffectExecution:
        return await self.runner.compensate(adapter, execution, context)


class TransactionCoordinator:
    def __init__(self, runner: SafeEffectRunner):
        self.runner = runner

    def transaction(self, name: str, context: EffectContext) -> AgentTransaction:
        return AgentTransaction(name, self.runner, context)


__all__ = [
    "AgentTransaction",
    "RecoveryEngine",
    "TransactionCoordinator",
    "TransactionHalted",
]
