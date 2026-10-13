"""Opt-in bounded metadata-only LangChain/LangGraph observations."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from typing import Any
from uuid import UUID

from .utils import normalize_uuidv7
from .workflow_capture import TelemetrySink


def make_metadata_langchain_handler(
    sink: TelemetrySink,
    correlation_id: str,
    *,
    max_pending_runs: int = 1024,
    max_run_age_seconds: float = 300,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Record terminal model/tool metadata; never inspect callback content.

    Pending state is bounded, not a durable replay store. Diagnostics describe
    local losses, not complete framework telemetry.
    """
    correlation_id = normalize_uuidv7(correlation_id)
    if type(max_pending_runs) is not int or not 1 <= max_pending_runs <= 100_000:
        raise ValueError("invalid pending-run limit")
    if (
        isinstance(max_run_age_seconds, bool)
        or not math.isfinite(max_run_age_seconds)
        or max_run_age_seconds <= 0
    ):
        raise ValueError("invalid run age")
    from langchain_core.callbacks import BaseCallbackHandler

    class MetadataHandler(BaseCallbackHandler):
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._pending: OrderedDict[str, tuple[str, float]] = OrderedDict()
            self._counts: Counter[str] = Counter()

        def stats(self) -> dict[str, int]:
            with self._lock:
                return {**self._counts, "pending": len(self._pending)}

        def _key(self, run_id: Any) -> str | None:
            if not isinstance(run_id, (str, UUID)):
                return None
            value = str(run_id)
            if not value or len(value) > 512:
                return None
            try:
                return hashlib.sha256(value.encode("utf-8")).hexdigest()
            except UnicodeError:
                return None

        def _expire(self, now: float) -> None:
            expired = [
                key
                for key, (_, start) in self._pending.items()
                if now - start >= max_run_age_seconds
            ]
            for key in expired:
                del self._pending[key]
                self._counts["expired"] += 1

        def _observe(self, run_id: Any, kind: str, outcome: str | None) -> None:
            key = self._key(run_id)
            now = clock()
            if key is None or not math.isfinite(now):
                with self._lock:
                    self._counts["invalid"] += 1
                return
            with self._lock:
                self._expire(now)
                previous = self._pending.get(key)
                if outcome is None:
                    if previous is not None:
                        self._counts["duplicate_start"] += 1
                        return
                    if len(self._pending) >= max_pending_runs:
                        self._pending.popitem(last=False)
                        self._counts["evicted"] += 1
                    self._pending[key] = (kind, now)
                    return
                if previous is None or previous[0] != kind:
                    self._counts["unmatched_end"] += 1
                    return
                del self._pending[key]
            try:
                sink.record_event(
                    correlation_id=correlation_id,
                    provider="langchain-metadata",
                    latency_ms=max(0, now - previous[1]) * 1000,
                    agent_state={
                        "schema_version": "0.1",
                        "kind": kind,
                        "outcome": outcome,
                        "run_ref": "sha256:" + key,
                    },
                )
            except Exception:
                with self._lock:
                    self._counts["sink_failures"] += 1

        def on_llm_start(
            self, serialized: Any, prompts: Any, *, run_id: Any, **kwargs: Any
        ) -> None:
            self._observe(run_id, "model", None)

        def on_chat_model_start(
            self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any
        ) -> None:
            self._observe(run_id, "model", None)

        def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
            self._observe(run_id, "model", "completed")

        def on_llm_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
            self._observe(run_id, "model", "failed")

        def on_tool_start(
            self, serialized: Any, input_str: Any, *, run_id: Any, **kwargs: Any
        ) -> None:
            self._observe(run_id, "tool", None)

        def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
            self._observe(run_id, "tool", "completed")

        def on_tool_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
            self._observe(run_id, "tool", "failed")

    return MetadataHandler()
