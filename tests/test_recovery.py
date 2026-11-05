import sqlite3

import pytest

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
from llmwitness.journal import JournalConflict, SQLiteJournalStore
from llmwitness.recovery import RecoveryAction, RecoveryPlanner
from llmwitness.utils import generate_uuidv7


def recovery_envelope(
    run_id: str,
    effect_id: str,
    status: EffectStatus,
) -> ExecutionEnvelope:
    return ExecutionEnvelope.create(
        run_id=run_id,
        event_id=effect_id if status == EffectStatus.PLANNED else None,
        parent_event_id=None if status == EffectStatus.PLANNED else effect_id,
        actor=Actor(agent_id="agent", principal_id="local-user"),
        intent=Intent(name="reference.refund"),
        authority=Authority(
            policy_id="policy",
            decision=AuthorityDecision.ALLOW,
            risk_tier=RiskTier.R2,
        ),
        contract=ContractReference(contract_id="contract"),
        effect=EffectDescriptor(
            adapter="reference.refund",
            idempotency_key="safe-local-key",
            parameters_hash=sha256_ref("request"),
        ),
        observation=Observation(status=status),
    )


def test_recovery_planner_is_read_only_and_lists_only_unresolved_effects(tmp_path):
    path = tmp_path / "recovery.db"
    unknown_run = generate_uuidv7()
    unknown_effect = generate_uuidv7()
    failed_run = generate_uuidv7()
    failed_effect = generate_uuidv7()
    with SQLiteJournalStore(path) as journal:
        journal.append(
            recovery_envelope(unknown_run, unknown_effect, EffectStatus.PLANNED),
            "effect.planned",
        )
        journal.append(
            recovery_envelope(unknown_run, unknown_effect, EffectStatus.UNKNOWN),
            "effect.unknown",
        )
        journal.append(
            recovery_envelope(failed_run, failed_effect, EffectStatus.PLANNED),
            "effect.planned",
        )
        journal.append(
            recovery_envelope(failed_run, failed_effect, EffectStatus.FAILED),
            "effect.failed",
        )
        journal.flush()
        with pytest.raises(ValueError, match="read-only"):
            RecoveryPlanner(journal)

    before = {
        item.name: (item.stat().st_size, item.stat().st_mtime_ns)
        for item in tmp_path.iterdir()
    }
    with SQLiteJournalStore(path, read_only=True) as journal:
        items = RecoveryPlanner(journal).list_items()
        all_items = RecoveryPlanner(journal).list_items(include_terminal=True)
    after = {
        item.name: (item.stat().st_size, item.stat().st_mtime_ns)
        for item in tmp_path.iterdir()
    }

    assert len(items) == 1
    assert items[0].effect_id == unknown_effect
    assert items[0].status == EffectStatus.UNKNOWN
    assert items[0].action == RecoveryAction.RECONCILE
    assert {item.effect_id for item in all_items} == {
        unknown_effect,
        failed_effect,
    }
    assert after == before


def test_tampered_chain_prevents_recovery_planning(tmp_path):
    path = tmp_path / "tampered.db"
    run_id = generate_uuidv7()
    effect_id = generate_uuidv7()
    with SQLiteJournalStore(path) as journal:
        journal.append(
            recovery_envelope(run_id, effect_id, EffectStatus.PLANNED),
            "effect.planned",
        )
        journal.flush()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE journal_entries SET event_type='tampered' WHERE run_id=?",
        (run_id,),
    )
    connection.commit()
    connection.close()

    with SQLiteJournalStore(path, read_only=True) as journal:
        with pytest.raises(JournalConflict, match="invalid run"):
            RecoveryPlanner(journal).list_items()
