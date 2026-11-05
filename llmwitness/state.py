"""State classification, snapshot, diff, and internal restoration ports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from llmwitness.envelope import sha256_ref
from llmwitness.utils import canonical_json


class StateKind(str, Enum):
    INTERNAL_RESTORABLE = "internal_restorable"
    EXTERNAL_OBSERVED = "external_observed"
    EXTERNAL_EFFECT = "external_effect"


@dataclass(frozen=True)
class StateSnapshot:
    reference: str
    kind: StateKind
    value: Any
    created_at: str


@dataclass(frozen=True)
class StateDiff:
    before: str
    after: str
    changes: Mapping[str, Any]


class StateAdapter(Protocol):
    async def snapshot(self, ctx: Mapping[str, Any]) -> StateSnapshot: ...

    async def diff(self, before: StateSnapshot, after: StateSnapshot) -> StateDiff: ...

    async def restore(
        self, snapshot: StateSnapshot, ctx: Mapping[str, Any]
    ) -> bool: ...


def json_safe_copy(value: Any) -> Any:
    """Copy JSON-compatible state through the canonical serialization boundary."""

    return json.loads(canonical_json(value))


class InMemoryStateAdapter:
    """Small reference adapter limited to internally restorable dictionary state."""

    def __init__(self, state: dict[str, Any]):
        self.state = state

    async def snapshot(self, ctx: Mapping[str, Any]) -> StateSnapshot:
        value = json_safe_copy(self.state)
        return StateSnapshot(
            sha256_ref(canonical_json(value)),
            StateKind.INTERNAL_RESTORABLE,
            value,
            datetime.now(timezone.utc).isoformat(),
        )

    async def diff(self, before: StateSnapshot, after: StateSnapshot) -> StateDiff:
        left = before.value if isinstance(before.value, dict) else {}
        right = after.value if isinstance(after.value, dict) else {}
        keys = set(left) | set(right)
        changes = {
            key: {"before": left.get(key), "after": right.get(key)}
            for key in sorted(keys)
            if key not in left or key not in right or left[key] != right[key]
        }
        return StateDiff(before.reference, after.reference, changes)

    async def restore(self, snapshot: StateSnapshot, ctx: Mapping[str, Any]) -> bool:
        if snapshot.kind != StateKind.INTERNAL_RESTORABLE or not isinstance(
            snapshot.value, dict
        ):
            raise ValueError("only internal restorable state can be restored")
        restored = json_safe_copy(snapshot.value)
        self.state.clear()
        self.state.update(restored)
        return True


class StateManager:
    """Depend on the state protocol, not a concrete persistence implementation."""

    def __init__(self, adapter: StateAdapter):
        self.adapter = adapter

    async def snapshot(self, context: Mapping[str, Any]) -> StateSnapshot:
        return await self.adapter.snapshot(context)

    async def diff(self, before: StateSnapshot, after: StateSnapshot) -> StateDiff:
        return await self.adapter.diff(before, after)

    async def restore(
        self, snapshot: StateSnapshot, context: Mapping[str, Any]
    ) -> bool:
        return await self.adapter.restore(snapshot, context)


__all__ = [
    "InMemoryStateAdapter",
    "StateAdapter",
    "StateDiff",
    "StateKind",
    "StateManager",
    "StateSnapshot",
    "json_safe_copy",
]
