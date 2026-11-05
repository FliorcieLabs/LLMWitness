"""Side-effect-safe journal replay modes and guardrails."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from llmwitness.journal import JournalEntry
from llmwitness.state import json_safe_copy


class ReplayMode(str, Enum):
    EXACT = "exact"
    SIMULATED = "simulated"
    COUNTERFACTUAL = "counterfactual"
    LIVE = "live"


class ReplayEngine:
    def replay(
        self,
        entries: Sequence[JournalEntry],
        mode: ReplayMode,
        *,
        deterministic_boundary: bool = False,
        changes: Mapping[str, Any] | None = None,
        allow_side_effects: bool = False,
        effect_allowlist: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        if mode == ReplayMode.EXACT and not deterministic_boundary:
            raise ValueError("exact replay requires a declared determinism boundary")
        if mode == ReplayMode.LIVE:
            if not allow_side_effects:
                raise PermissionError(
                    "live re-execution is disabled without explicit side-effect authorization"
                )
            adapters = {entry.envelope["effect"]["adapter"] for entry in entries}
            if not adapters.issubset(effect_allowlist):
                raise PermissionError(
                    "live replay contains an effect outside the allowlist"
                )
            raise NotImplementedError(
                "live execution requires an application-supplied executor and policy hook"
            )
        replayed = [json_safe_copy(entry.envelope) for entry in entries]
        if mode == ReplayMode.COUNTERFACTUAL and changes:
            for envelope in replayed:
                envelope.setdefault("counterfactual", {}).update(changes)
        return replayed


__all__ = ["ReplayEngine", "ReplayMode"]
