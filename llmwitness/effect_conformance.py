"""Deterministic conformance harness for provider effect adapters."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llmwitness.effects import (
    EffectAdapter,
    EffectContext,
    EffectExecution,
    SafeEffectRunner,
    VerificationResult,
)
from llmwitness.envelope import EffectStatus
from llmwitness.journal import SQLiteJournalStore

AdapterFactory = Callable[[], EffectAdapter]
RequestFactory = Callable[[], Mapping[str, Any]]
ContextFactory = Callable[[], EffectContext]

_LIMITATIONS = (
    "Deterministic local protocol checks; passing is not production certification "
    "or proof of provider availability, durability, or external truth."
)


@dataclass(frozen=True)
class AdapterConformanceFixture:
    """Factories that isolate every conformance scenario from shared state."""

    adapter_factory: AdapterFactory
    request_factory: RequestFactory
    context_factory: ContextFactory


@dataclass(frozen=True)
class AdapterConformanceResult:
    scenario_id: str
    passed: bool
    invariant: str
    detail: str


@dataclass(frozen=True)
class AdapterConformanceReport:
    schema_version: str
    adapter_name: str
    results: tuple[AdapterConformanceResult, ...]
    limitations: str = _LIMITATIONS

    @property
    def sample_count(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(item.passed for item in self.results)

    @property
    def failed(self) -> int:
        return sum(not item.passed for item in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adapter_name": self.adapter_name,
            "sample_count": self.sample_count,
            "passed": self.passed,
            "failed": self.failed,
            "dimensions": [asdict(item) for item in self.results],
            "limitations": self.limitations,
        }


class _DelegatingAdapter:
    def __init__(self, delegate: EffectAdapter):
        self.delegate = delegate
        self.name = delegate.name
        self.execute_calls = 0

    def idempotency_key(self, request: Mapping[str, Any], ctx: EffectContext) -> str:
        return self.delegate.idempotency_key(request, ctx)

    async def execute(self, request: Mapping[str, Any], ctx: EffectContext) -> Any:
        self.execute_calls += 1
        return await self.delegate.execute(request, ctx)

    async def verify(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        return await self.delegate.verify(execution, ctx)

    async def compensate(self, execution: EffectExecution, ctx: EffectContext) -> Any:
        return await self.delegate.compensate(execution, ctx)

    async def verify_compensation(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        return await self.delegate.verify_compensation(execution, ctx)


class _ResponseLossAdapter(_DelegatingAdapter):
    async def execute(self, request: Mapping[str, Any], ctx: EffectContext) -> Any:
        await super().execute(request, ctx)
        raise TimeoutError("deterministic response loss after provider dispatch")


class _StaleEvidenceAdapter(_DelegatingAdapter):
    async def verify(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        result = await super().verify(execution, ctx)
        stale = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return replace(
            result,
            evidence=tuple(
                item.model_copy(update={"observed_at": stale})
                for item in result.evidence
            ),
        )


class _CompensationFailureAdapter(_DelegatingAdapter):
    async def compensate(self, execution: EffectExecution, ctx: EffectContext) -> Any:
        raise TimeoutError("deterministic compensation failure")


class EffectAdapterConformanceHarness:
    """Exercise one local/sandbox adapter through shared Runtime semantics."""

    async def run(self, fixture: AdapterConformanceFixture) -> AdapterConformanceReport:
        adapter_name = fixture.adapter_factory().name
        if not isinstance(adapter_name, str) or not adapter_name.strip():
            raise ValueError("adapter factory must produce a nonblank adapter name")
        scenarios = (
            (
                "provider.duplicate-dispatch.v0.1",
                "a duplicate scoped request dispatches once",
                lambda: self._duplicate_dispatch(fixture),
            ),
            (
                "provider.response-loss-reconciliation.v0.1",
                "response loss remains UNKNOWN until verification and never redispatches",
                lambda: self._response_loss(fixture),
            ),
            (
                "provider.stale-verification.v0.1",
                "stale evidence cannot produce VERIFIED",
                lambda: self._stale_verification(fixture),
            ),
            (
                "provider.verified-compensation.v0.1",
                "compensation is complete only after bound verification",
                lambda: self._verified_compensation(fixture),
            ),
            (
                "provider.compensation-failure.v0.1",
                "compensation failure remains visible as MANUAL_REVIEW",
                lambda: self._compensation_failure(fixture),
            ),
        )
        results = tuple(
            [
                await self._evaluate(scenario_id, invariant, evaluate)
                for scenario_id, invariant, evaluate in scenarios
            ]
        )
        return AdapterConformanceReport("0.1", adapter_name, results)

    @staticmethod
    async def _evaluate(
        scenario_id: str,
        invariant: str,
        evaluate: Callable[[], Awaitable[str]],
    ) -> AdapterConformanceResult:
        try:
            detail = await evaluate()
            return AdapterConformanceResult(scenario_id, True, invariant, detail)
        except Exception as exc:
            return AdapterConformanceResult(
                scenario_id,
                False,
                invariant,
                f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _parts(
        fixture: AdapterConformanceFixture,
        wrapper: type[_DelegatingAdapter] = _DelegatingAdapter,
    ) -> tuple[_DelegatingAdapter, Mapping[str, Any], EffectContext]:
        adapter = wrapper(fixture.adapter_factory())
        request = dict(fixture.request_factory())
        context = fixture.context_factory()
        return adapter, request, context

    async def _duplicate_dispatch(self, fixture: AdapterConformanceFixture) -> str:
        adapter, request, context = self._parts(fixture)
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteJournalStore(Path(directory) / "journal.db") as journal:
                runner = SafeEffectRunner(journal)
                first = await runner.run(adapter, request, context)
                second = await runner.run(adapter, request, context)
                if not (
                    first.state == second.state == EffectStatus.VERIFIED
                    and second.deduplicated
                    and adapter.execute_calls == 1
                    and journal.verify(context.run_id).valid
                ):
                    raise AssertionError(
                        "duplicate request was not safely deduplicated"
                    )
        return "one verified dispatch; duplicate reused durable identity"

    async def _response_loss(self, fixture: AdapterConformanceFixture) -> str:
        adapter, request, context = self._parts(fixture, _ResponseLossAdapter)
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteJournalStore(Path(directory) / "journal.db") as journal:
                runner = SafeEffectRunner(journal)
                first = await runner.run(adapter, request, context)
                second = await runner.run(adapter, request, context)
                if not (
                    first.state == EffectStatus.UNKNOWN
                    and second.state == EffectStatus.VERIFIED
                    and second.deduplicated
                    and adapter.execute_calls == 1
                    and journal.verify(context.run_id).valid
                ):
                    raise AssertionError("response loss was retried or not reconciled")
        return "UNKNOWN reconciled to VERIFIED without a second dispatch"

    async def _stale_verification(self, fixture: AdapterConformanceFixture) -> str:
        adapter, request, context = self._parts(fixture, _StaleEvidenceAdapter)
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteJournalStore(Path(directory) / "journal.db") as journal:
                execution = await SafeEffectRunner(journal).run(
                    adapter, request, context
                )
                if not (
                    execution.state == EffectStatus.UNKNOWN
                    and adapter.execute_calls == 1
                    and journal.verify(context.run_id).valid
                ):
                    raise AssertionError("stale evidence produced a terminal success")
        return "stale evidence remained UNKNOWN"

    async def _verified_compensation(self, fixture: AdapterConformanceFixture) -> str:
        adapter, request, context = self._parts(fixture)
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteJournalStore(Path(directory) / "journal.db") as journal:
                runner = SafeEffectRunner(journal)
                execution = await runner.run(adapter, request, context)
                compensated = await runner.compensate(adapter, execution, context)
                if not (
                    compensated.state == EffectStatus.COMPENSATED
                    and compensated.evidence
                    and compensated.verifier
                    and journal.verify(context.run_id).valid
                ):
                    raise AssertionError(
                        "compensation did not obtain bound verification"
                    )
        return "compensation reached COMPENSATED with evidence and verifier"

    async def _compensation_failure(self, fixture: AdapterConformanceFixture) -> str:
        adapter, request, context = self._parts(fixture, _CompensationFailureAdapter)
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteJournalStore(Path(directory) / "journal.db") as journal:
                runner = SafeEffectRunner(journal)
                execution = await runner.run(adapter, request, context)
                compensated = await runner.compensate(adapter, execution, context)
                if not (
                    compensated.state == EffectStatus.MANUAL_REVIEW
                    and journal.verify(context.run_id).valid
                ):
                    raise AssertionError("compensation failure was hidden")
        return "compensation exception remained MANUAL_REVIEW"


def write_adapter_conformance_report(
    report: AdapterConformanceReport,
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    """Write matching machine- and human-readable conformance evidence."""
    json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    lines = [
        "# Provider Adapter Conformance",
        "",
        f"Adapter: `{report.adapter_name}`.",
        f"Scenarios: {report.sample_count}; passed: {report.passed}; failed: {report.failed}.",
        "",
    ]
    lines.extend(
        f"- {'PASS' if item.passed else 'FAIL'} `{item.scenario_id}`: "
        f"{item.invariant} ({item.detail})"
        for item in report.results
    )
    lines.extend(["", report.limitations, ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")


__all__ = [
    "AdapterConformanceFixture",
    "AdapterConformanceReport",
    "AdapterConformanceResult",
    "EffectAdapterConformanceHarness",
    "write_adapter_conformance_report",
]
