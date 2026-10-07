import asyncio
import json
from pathlib import Path

import pytest

from llmwitness.authority import AuthorityResult
from llmwitness.contracts import AgentContract
from llmwitness.effect_conformance import (
    AdapterConformanceFixture,
    EffectAdapterConformanceHarness,
    write_adapter_conformance_report,
)
from llmwitness.effects import EffectContext, EffectExecution, VerificationResult
from llmwitness.envelope import (
    Actor,
    AuthorityDecision,
    EffectStatus,
    RiskTier,
    new_trace_id,
)
from llmwitness.reference_effects import InMemoryCRMEffect, InMemoryRefundEffect
from llmwitness.utils import generate_uuidv7


def _context() -> EffectContext:
    return EffectContext(
        run_id=generate_uuidv7(),
        trace_id=new_trace_id(),
        actor=Actor(agent_id="adapter-author", principal_id="local-user"),
        authority=AuthorityResult(
            "adapter-policy",
            AuthorityDecision.ALLOW,
            RiskTier.R2,
            "local conformance fixture",
        ),
        contract=AgentContract("adapter-contract"),
    )


@pytest.mark.parametrize(
    ("adapter_factory", "request_factory"),
    [
        (
            InMemoryRefundEffect,
            lambda: {"payment_id": "payment-1", "amount": 25},
        ),
        (
            InMemoryCRMEffect,
            lambda: {"record_id": "customer-1", "fields": {"stage": "won"}},
        ),
    ],
)
def test_reference_effects_pass_provider_conformance(adapter_factory, request_factory):
    report = asyncio.run(
        EffectAdapterConformanceHarness().run(
            AdapterConformanceFixture(
                adapter_factory=adapter_factory,
                request_factory=request_factory,
                context_factory=_context,
            )
        )
    )

    assert report.sample_count == 5
    assert report.passed == 5
    assert report.failed == 0
    assert {item.scenario_id for item in report.results} == {
        "provider.duplicate-dispatch.v0.1",
        "provider.response-loss-reconciliation.v0.1",
        "provider.stale-verification.v0.1",
        "provider.verified-compensation.v0.1",
        "provider.compensation-failure.v0.1",
    }
    assert "not production" in report.limitations


class BrokenCompensationRefund(InMemoryRefundEffect):
    async def verify_compensation(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        return VerificationResult(
            EffectStatus.UNKNOWN,
            verifier=self.name,
            detail="adapter cannot prove compensation",
        )


def test_broken_compensation_fails_only_verified_compensation_dimension():
    report = asyncio.run(
        EffectAdapterConformanceHarness().run(
            AdapterConformanceFixture(
                adapter_factory=BrokenCompensationRefund,
                request_factory=lambda: {"payment_id": "payment-2", "amount": 10},
                context_factory=_context,
            )
        )
    )

    failed = [item.scenario_id for item in report.results if not item.passed]
    assert failed == ["provider.verified-compensation.v0.1"]
    failure_visibility = next(
        item
        for item in report.results
        if item.scenario_id == "provider.compensation-failure.v0.1"
    )
    assert failure_visibility.passed


def test_conformance_report_is_machine_and_human_readable(tmp_path: Path):
    report = asyncio.run(
        EffectAdapterConformanceHarness().run(
            AdapterConformanceFixture(
                adapter_factory=InMemoryRefundEffect,
                request_factory=lambda: {"payment_id": "payment-3", "amount": 5},
                context_factory=_context,
            )
        )
    )
    json_path = tmp_path / "adapter-conformance.json"
    markdown_path = tmp_path / "adapter-conformance.md"

    write_adapter_conformance_report(
        report, json_path=json_path, markdown_path=markdown_path
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "0.1"
    assert payload["sample_count"] == 5
    assert payload["failed"] == 0
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Provider Adapter Conformance" in markdown
    assert "universal" not in markdown.lower()
    assert "production certification" in markdown
