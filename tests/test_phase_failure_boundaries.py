"""Exercise independently owned Community phase failure boundaries."""

import asyncio
from types import SimpleNamespace

import pytest

from llmwitness import artifacts
from llmwitness.artifacts import FileArtifactStore
from llmwitness.envelope import EffectStatus
from llmwitness.replay import ReplayEngine, ReplayMode
from llmwitness.state import (
    InMemoryStateAdapter,
    StateKind,
    StateManager,
    StateSnapshot,
)
from llmwitness.transactions import TransactionCoordinator, TransactionHalted


def test_artifact_roundtrip_deduplication_and_tamper_detection(tmp_path):
    store = FileArtifactStore(tmp_path, max_bytes=4)
    reference, path = store.put(b"data")
    assert store.get(reference) == b"data"
    assert store.put(b"data") == (reference, path)
    with pytest.raises(ValueError, match="size limit"):
        store.put(b"larger")
    with pytest.raises(ValueError, match="invalid"):
        store.get("../../private")
    path.write_bytes(b"evil")
    with pytest.raises(RuntimeError, match="integrity"):
        store.get(reference)
    with pytest.raises(RuntimeError, match="collision"):
        store.put(b"data")


def test_artifact_publication_race_validates_existing_content(tmp_path, monkeypatch):
    store = FileArtifactStore(tmp_path)

    def competing_writer(source, destination):
        destination.write_bytes(b"different content")
        raise FileExistsError("another publisher won")

    monkeypatch.setattr(artifacts.os, "link", competing_writer)
    with pytest.raises(RuntimeError, match="collision"):
        store.put(b"expected")
    assert list(tmp_path.rglob("*.tmp")) == []


def test_state_snapshot_diff_and_restore_are_isolated():
    async def exercise():
        state = {"nested": {"count": 1}, "removed": None}
        manager = StateManager(InMemoryStateAdapter(state))
        before = await manager.snapshot({})
        state["nested"]["count"] = 2
        state.pop("removed")
        state["added"] = None
        after = await manager.snapshot({})
        difference = await manager.diff(before, after)
        assert set(difference.changes) == {"nested", "removed", "added"}
        assert before.value["nested"]["count"] == 1
        assert await manager.restore(before, {})
        state["nested"]["count"] = 3
        assert before.value["nested"]["count"] == 1

    asyncio.run(exercise())


def test_failed_state_copy_does_not_destroy_current_state():
    state = {"keep": "original"}
    snapshot = StateSnapshot(
        "unused", StateKind.INTERNAL_RESTORABLE, {"bad": object()}, "now"
    )
    with pytest.raises(TypeError):
        asyncio.run(InMemoryStateAdapter(state).restore(snapshot, {}))
    assert state == {"keep": "original"}


def test_state_restore_rejects_external_state():
    state = {"keep": True}
    snapshot = StateSnapshot("unused", StateKind.EXTERNAL_EFFECT, {}, "now")
    with pytest.raises(ValueError, match="only internal"):
        asyncio.run(InMemoryStateAdapter(state).restore(snapshot, {}))
    assert state == {"keep": True}


def test_caught_runner_exception_leaves_transaction_in_manual_review():
    async def fail(*args):
        raise OSError("journal unavailable")

    async def exercise():
        transaction = TransactionCoordinator(SimpleNamespace(run=fail)).transaction(
            "test", None
        )
        async with transaction:
            with pytest.raises(OSError, match="journal unavailable"):
                await transaction.effect(None, {})
        assert transaction.final_state == "manual_review"

    asyncio.run(exercise())


def test_compensation_exception_marks_manual_review():
    async def run(*args):
        return SimpleNamespace(state=EffectStatus.VERIFIED)

    async def fail(*args):
        raise OSError("recovery journal unavailable")

    async def exercise():
        transaction = TransactionCoordinator(
            SimpleNamespace(run=run, compensate=fail)
        ).transaction("test", None)
        with pytest.raises(OSError, match="recovery journal unavailable"):
            async with transaction:
                await transaction.effect(None, {})
                raise ValueError("application failure")
        assert transaction.final_state == "manual_review"

    asyncio.run(exercise())


@pytest.mark.parametrize("terminal", [EffectStatus.UNKNOWN, EffectStatus.FAILED])
def test_catching_transaction_halt_does_not_report_verified(terminal):
    dispatches = []

    async def run(*args):
        dispatches.append(args)
        return SimpleNamespace(state=terminal)

    async def exercise():
        transaction = TransactionCoordinator(SimpleNamespace(run=run)).transaction(
            "test", None
        )
        async with transaction:
            with pytest.raises(TransactionHalted):
                await transaction.effect(None, {})
            with pytest.raises(TransactionHalted, match="transaction is"):
                await transaction.effect(None, {})
        assert transaction.final_state == (
            "manual_review" if terminal == EffectStatus.UNKNOWN else "failed"
        )
        assert len(dispatches) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("unresolved", [False, True])
def test_transaction_compensates_in_reverse_order_and_preserves_failure(unresolved):
    recovered = []

    async def run(adapter, request, context):
        return SimpleNamespace(state=EffectStatus.VERIFIED)

    async def compensate(adapter, execution, context):
        recovered.append(adapter)
        return SimpleNamespace(
            state=EffectStatus.UNKNOWN if unresolved else EffectStatus.COMPENSATED
        )

    async def exercise():
        transaction = TransactionCoordinator(
            SimpleNamespace(run=run, compensate=compensate)
        ).transaction("test", None)
        with pytest.raises(ValueError, match="original failure"):
            async with transaction:
                await transaction.effect("first", {})
                await transaction.effect("second", {})
                raise ValueError("original failure")
        assert recovered == ["second", "first"]
        assert transaction.final_state == (
            "manual_review" if unresolved else "compensated"
        )

    asyncio.run(exercise())


def test_replay_is_copy_isolated_and_live_execution_remains_guarded():
    entry = SimpleNamespace(
        envelope={"effect": {"adapter": "local.fixture"}, "nested": {"value": 1}}
    )
    engine = ReplayEngine()
    simulated = engine.replay([entry], ReplayMode.SIMULATED)
    simulated[0]["nested"]["value"] = 2
    assert entry.envelope["nested"]["value"] == 1
    with pytest.raises(ValueError, match="determinism"):
        engine.replay([entry], ReplayMode.EXACT)
    assert engine.replay([entry], ReplayMode.EXACT, deterministic_boundary=True)
    assert engine.replay([entry], ReplayMode.COUNTERFACTUAL, changes={"test": True})[0][
        "counterfactual"
    ] == {"test": True}
    with pytest.raises(PermissionError, match="authorization"):
        engine.replay([entry], ReplayMode.LIVE)
    with pytest.raises(PermissionError, match="allowlist"):
        engine.replay([entry], ReplayMode.LIVE, allow_side_effects=True)
    with pytest.raises(NotImplementedError, match="application-supplied"):
        engine.replay(
            [entry],
            ReplayMode.LIVE,
            allow_side_effects=True,
            effect_allowlist=frozenset({"local.fixture"}),
        )
