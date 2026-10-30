"""Regressions for halted transactions, evidence freshness and spool retention."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from llmwitness.effects import VerificationEngine, VerificationResult
from llmwitness.envelope import EffectStatus, EvidenceReference
from llmwitness.spool import EventSpool
from llmwitness.transactions import TransactionCoordinator, TransactionHalted


def _transaction(run, compensate=None):
    runner = SimpleNamespace(run=run, compensate=compensate)
    return TransactionCoordinator(runner).transaction("test", None)


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [(EffectStatus.UNKNOWN, "manual_review"), (EffectStatus.FAILED, "failed")],
    ids=["unknown-effect", "failed-effect"],
)
def test_caught_halt_is_never_reported_as_verified(terminal, expected):
    dispatched = []

    async def run(adapter, request, context):
        dispatched.append(adapter)
        return SimpleNamespace(state=terminal)

    async def exercise():
        transaction = _transaction(run)
        async with transaction:
            with pytest.raises(TransactionHalted):
                await transaction.effect("first", {})
        return transaction

    transaction = asyncio.run(exercise())
    assert transaction.final_state == expected
    assert dispatched == ["first"]


def test_halted_transaction_refuses_further_dispatch():
    dispatched = []

    async def run(adapter, request, context):
        dispatched.append(adapter)
        return SimpleNamespace(state=EffectStatus.UNKNOWN)

    async def exercise():
        transaction = _transaction(run)
        async with transaction:
            with pytest.raises(TransactionHalted):
                await transaction.effect("first", {})
            with pytest.raises(TransactionHalted, match="transaction is manual_review"):
                await transaction.effect("second", {})
        return transaction

    transaction = asyncio.run(exercise())
    assert dispatched == ["first"]
    assert transaction.final_state == "manual_review"


def test_caught_halt_after_completed_effects_requires_manual_review():
    async def run(adapter, request, context):
        state = EffectStatus.FAILED if adapter == "second" else EffectStatus.VERIFIED
        return SimpleNamespace(state=state)

    async def exercise():
        transaction = _transaction(run)
        async with transaction:
            await transaction.effect("first", {})
            with pytest.raises(TransactionHalted):
                await transaction.effect("second", {})
        return transaction

    assert asyncio.run(exercise()).final_state == "manual_review"


def test_compensation_exception_does_not_strand_earlier_effects():
    recovered = []

    async def run(adapter, request, context):
        return SimpleNamespace(state=EffectStatus.VERIFIED)

    async def compensate(adapter, execution, context):
        if adapter == "second":
            raise OSError("refund API unavailable")
        recovered.append(adapter)
        return SimpleNamespace(state=EffectStatus.COMPENSATED)

    async def exercise():
        transaction = _transaction(run, compensate)
        with pytest.raises(OSError, match="refund API unavailable") as raised:
            async with transaction:
                await transaction.effect("first", {})
                await transaction.effect("second", {})
                await transaction.effect("third", {})
                raise ValueError("original failure")
        assert isinstance(raised.value.__context__, ValueError)
        return transaction

    transaction = asyncio.run(exercise())
    assert recovered == ["third", "first"]
    assert transaction.final_state == "manual_review"


def _evidence(observed_at):
    return EvidenceReference(
        kind="fixture", sha256="sha256:" + "a" * 64, observed_at=observed_at
    )


_FRESH = datetime.now(timezone.utc) + timedelta(days=1)


@pytest.mark.parametrize(
    "status",
    [EffectStatus.VERIFIED, EffectStatus.COMPENSATED],
    ids=["verify", "compensate"],
)
@pytest.mark.parametrize(
    "observed",
    [(None,), (_FRESH, None)],
    ids=["missing-timestamp", "mixed-timestamps"],
)
def test_evidence_without_observation_time_is_not_fresh(status, observed):
    result = VerificationResult(
        status, evidence=tuple(_evidence(item) for item in observed), verifier="fixture"
    )

    checked = VerificationEngine(max_evidence_age_seconds=0).validate_freshness(result)

    assert checked.status == EffectStatus.UNKNOWN
    assert "no observation time" in checked.detail


@pytest.mark.parametrize(
    "status",
    [EffectStatus.VERIFIED, EffectStatus.COMPENSATED],
    ids=["verify", "compensate"],
)
def test_disabled_freshness_keeps_untimestamped_evidence(status):
    result = VerificationResult(status, evidence=(_evidence(None),), verifier="fixture")

    assert (
        VerificationEngine(max_evidence_age_seconds=None).validate_freshness(result)
        is result
    )


def test_fresh_and_stale_timestamps_keep_their_existing_outcomes():
    fresh = VerificationResult(
        EffectStatus.VERIFIED,
        evidence=(_evidence(datetime.now(timezone.utc)),),
        verifier="fixture",
    )
    stale = VerificationResult(
        EffectStatus.VERIFIED,
        evidence=(_evidence(datetime.now(timezone.utc) - timedelta(hours=1)),),
        verifier="fixture",
    )
    engine = VerificationEngine(max_evidence_age_seconds=60)

    assert engine.validate_freshness(fresh).status == EffectStatus.VERIFIED
    checked = engine.validate_freshness(stale)
    assert checked.status == EffectStatus.UNKNOWN
    assert "freshness window" in checked.detail


def _fill_near_capacity(spool, count=4):
    for n in range(count):
        assert spool.append("sdk", {"n": n, "pad": "x" * 20})


def _spooled(spool):
    claim = spool.claim("sdk")
    return [] if claim is None else [payload["n"] for payload in claim.payloads]


def test_failed_replay_of_a_near_capacity_spool_keeps_every_event(tmp_path):
    spool = EventSpool(tmp_path, max_bytes=200)
    _fill_near_capacity(spool)
    claim = spool.claim("sdk")

    spool.release("sdk", claim, claim.payloads)

    assert _spooled(spool) == [0, 1, 2, 3]


def test_partial_replay_keeps_exactly_the_undelivered_events(tmp_path):
    spool = EventSpool(tmp_path, max_bytes=200)
    _fill_near_capacity(spool)
    claim = spool.claim("sdk")

    spool.release("sdk", claim, claim.payloads[2:])

    assert _spooled(spool) == [2, 3]


def test_restoration_write_failure_preserves_the_original_claim(tmp_path, monkeypatch):
    spool = EventSpool(tmp_path, max_bytes=200)
    _fill_near_capacity(spool)
    claim = spool.claim("sdk")
    original = claim.path.read_bytes()

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("llmwitness.spool.os.replace", refuse)
    spool.release("sdk", claim, claim.payloads[1:])
    monkeypatch.undo()

    assert claim.path.read_bytes() == original
    assert [json.loads(line)["n"] for line in original.splitlines()] == [0, 1, 2, 3]
    assert not [item for item in os.listdir(tmp_path) if item.endswith(".tmp")]
    # The kept claim is published for lease recovery once it goes stale.
    old = os.path.getmtime(claim.path) - 600
    os.utime(claim.path, (old, old))
    assert _spooled(spool) == [0, 1, 2, 3]
