"""Conformance tests for narrow Safe Effect and recovery journal ports."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from llmwitness.authority import AuthorityResult
from llmwitness.contracts import AgentContract
from llmwitness.effects import EffectContext, SafeEffectRunner
from llmwitness.envelope import (
    Actor,
    Authority,
    AuthorityDecision,
    ContractReference,
    EffectDescriptor,
    EffectStatus,
    ExecutionEnvelope,
    Intent,
    Observation,
    RiskTier,
    sha256_ref,
)
from llmwitness.journal import (
    EffectPreparation,
    JournalEntry,
    JournalVerification,
    ReadOnlyJournal,
    SQLiteJournalStore,
    WritableEffectJournal,
)
from llmwitness.recovery import RecoveryAction, RecoveryPlanner
from llmwitness.reference_effects import InMemoryRefundEffect
from llmwitness.utils import generate_uuidv7


class FakeWritableEffectJournal:
    """Small in-memory port fake; it is not a journal integrity implementation."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.records: dict[str, dict[str, Any]] = {}

    def append(
        self,
        envelope: ExecutionEnvelope,
        event_type: str,
        *,
        transaction_id: str | None = None,
        expected_head: str | None = None,
    ) -> JournalEntry:
        del expected_head
        self.events.append(event_type)
        return JournalEntry(
            entry_id=generate_uuidv7(),
            run_id=envelope.run_id,
            run_sequence=len(self.events),
            transaction_id=transaction_id,
            event_type=event_type,
            envelope=envelope.model_dump(mode="json"),
            previous_hash=sha256_ref("previous"),
            entry_hash=sha256_ref(event_type),
            created_at="2026-09-08T00:00:00+00:00",
        )

    def prepare_effect(
        self,
        envelope: ExecutionEnvelope,
        *,
        key: str,
        parameters_hash: str,
        scope_hash: str,
        effect_id: str,
        run_id: str,
        execution: dict[str, Any],
        transaction_id: str | None = None,
    ) -> EffectPreparation:
        existing = self.records.get(key)
        if existing is not None:
            return EffectPreparation(existing, None)
        entry = self.append(envelope, "effect.prepared", transaction_id=transaction_id)
        record = {
            "idempotency_key": key,
            "parameters_hash": parameters_hash,
            "scope_hash": scope_hash,
            "effect_id": effect_id,
            "run_id": run_id,
            "state": EffectStatus.PREPARED.value,
            "execution": dict(execution),
        }
        self.records[key] = record
        return EffectPreparation(record, entry)

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        return self.records.get(key)

    def update_idempotency(
        self, key: str, state: str, execution: dict[str, Any] | None
    ) -> None:
        self.records[key]["state"] = state
        self.records[key]["execution"] = dict(execution or {})


class FakeReadOnlyJournal:
    read_only = True

    def __init__(self, entries: Mapping[str, list[JournalEntry]]) -> None:
        self.entries = entries

    def run_ids(self) -> tuple[str, ...]:
        return tuple(self.entries)

    def verify(self, run_id: str) -> JournalVerification:
        entries = self.entries[run_id]
        return JournalVerification(True, len(entries), sha256_ref("head"))

    def scan(
        self, run_id: str, transaction_id: str | None = None
    ) -> list[JournalEntry]:
        entries = self.entries[run_id]
        if transaction_id is None:
            return list(entries)
        return [entry for entry in entries if entry.transaction_id == transaction_id]


def _context() -> EffectContext:
    return EffectContext(
        run_id=generate_uuidv7(),
        trace_id=generate_uuidv7().replace("-", ""),
        actor=Actor(agent_id="agent", principal_id="local-user"),
        authority=AuthorityResult(
            "policy", AuthorityDecision.ALLOW, RiskTier.R1, "allowed"
        ),
        contract=AgentContract("contract"),
    )


def _recovery_entry(
    run_id: str, effect_id: str, status: EffectStatus, sequence: int
) -> JournalEntry:
    envelope = ExecutionEnvelope.create(
        run_id=run_id,
        event_id=effect_id if status == EffectStatus.PLANNED else None,
        parent_event_id=None if status == EffectStatus.PLANNED else effect_id,
        actor=Actor(agent_id="agent", principal_id="local-user"),
        intent=Intent(name="reference.refund"),
        authority=Authority(
            policy_id="policy",
            decision=AuthorityDecision.ALLOW,
            risk_tier=RiskTier.R1,
        ),
        contract=ContractReference(contract_id="contract"),
        effect=EffectDescriptor(
            adapter="reference.refund",
            idempotency_key="payment:refund",
            parameters_hash=sha256_ref("request"),
        ),
        observation=Observation(status=status),
    )
    return JournalEntry(
        entry_id=generate_uuidv7(),
        run_id=run_id,
        run_sequence=sequence,
        transaction_id=None,
        event_type=f"effect.{status.value}",
        envelope=envelope.model_dump(mode="json"),
        previous_hash=sha256_ref(f"previous-{sequence}"),
        entry_hash=sha256_ref(f"entry-{sequence}"),
        created_at="2026-09-08T00:00:00+00:00",
    )


def test_safe_effect_runner_accepts_writable_protocol_fake():
    journal = FakeWritableEffectJournal()
    adapter = InMemoryRefundEffect()
    context = _context()
    request = {"payment_id": "payment-1", "amount": 25}

    first = asyncio.run(SafeEffectRunner(journal).run(adapter, request, context))
    second = asyncio.run(SafeEffectRunner(journal).run(adapter, request, context))

    assert isinstance(journal, WritableEffectJournal)
    assert first.state == second.state == EffectStatus.VERIFIED
    assert second.deduplicated
    assert adapter.execute_count == 1
    assert journal.events[-1] == "effect.verified"


def test_recovery_planner_accepts_read_only_protocol_fake():
    run_id = generate_uuidv7()
    effect_id = generate_uuidv7()
    journal = FakeReadOnlyJournal(
        {
            run_id: [
                _recovery_entry(run_id, effect_id, EffectStatus.PLANNED, 1),
                _recovery_entry(run_id, effect_id, EffectStatus.UNKNOWN, 2),
            ]
        }
    )

    items = RecoveryPlanner(journal).list_items()

    assert isinstance(journal, ReadOnlyJournal)
    assert not hasattr(journal, "append")
    assert len(items) == 1
    assert items[0].effect_id == effect_id
    assert items[0].action == RecoveryAction.RECONCILE


def test_sqlite_structurally_conforms_to_both_journal_protocols(tmp_path):
    path = tmp_path / "protocols.db"
    with SQLiteJournalStore(path) as writable:
        assert isinstance(writable, WritableEffectJournal)
    with SQLiteJournalStore(path, read_only=True) as readable:
        assert isinstance(readable, ReadOnlyJournal)


def test_recovery_planner_rejects_writable_protocol_implementation(tmp_path):
    with SQLiteJournalStore(tmp_path / "writable.db") as journal:
        with pytest.raises(ValueError, match="read-only"):
            RecoveryPlanner(journal)
