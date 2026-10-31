"""Metadata-only observation of plain OpenAI Responses create calls."""

from __future__ import annotations

import hashlib
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

from .utils import normalize_uuidv7
from .workflow_capture import TelemetrySink

logger = logging.getLogger(__name__)
_STATUSES = {"completed", "failed", "in_progress", "cancelled", "queued", "incomplete"}


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def wrap_responses_client(
    sink: TelemetrySink, client: Any, correlation_id_factory: Callable[[], str]
) -> Any:
    """Wrap create only; streams pass through untouched and are not captured."""
    original = client.responses.create
    owner = getattr(original, "__llmwitness_responses_owner__", None)
    if owner is sink:
        return client
    if owner is not None:
        raise ValueError("Responses client already has a different telemetry owner")

    def prepare(kwargs: dict[str, Any]) -> tuple[str, float]:
        correlation_id = normalize_uuidv7(correlation_id_factory())
        headers = dict(kwargs.get("extra_headers") or {})
        # Header names are case insensitive: avoid conflicting duplicate IDs.
        headers = {
            key: value
            for key, value in headers.items()
            if key.lower() != "x-correlation-id"
        }
        headers["x-correlation-id"] = correlation_id
        kwargs["extra_headers"] = headers
        return correlation_id, time.perf_counter()

    def record(
        correlation_id: str, start: float, response: Any = None, failed: bool = False
    ) -> None:
        try:
            status = "failed" if failed else _field(response, "status")
            status = (
                status if isinstance(status, str) and status in _STATUSES else "unknown"
            )
            state = {"schema_version": "0.1", "status": status}
            identifier = _field(response, "id")
            if isinstance(identifier, str) and 0 < len(identifier) <= 512:
                state["response_ref"] = (
                    "sha256:" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()
                )
            usage = _field(response, "usage")
            counts = []
            for name in ("input_tokens", "output_tokens"):
                count = _field(usage, name)
                counts.append(
                    count if type(count) is int and 0 <= count <= 2**53 - 1 else 0
                )
            sink.record_event(
                correlation_id=correlation_id,
                provider="openai-responses",
                latency_ms=(time.perf_counter() - start) * 1000,
                prompt_tokens=counts[0],
                completion_tokens=counts[1],
                agent_state=state,
            )
        except Exception:
            logger.warning(
                "Responses metadata observation failed; application result preserved"
            )

    if inspect.iscoroutinefunction(original):

        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            correlation_id, start = prepare(kwargs)
            if kwargs.get("stream") is True:
                return await original(*args, **kwargs)
            try:
                response = await original(*args, **kwargs)
            except BaseException:
                record(correlation_id, start, failed=True)
                raise
            record(correlation_id, start, response)
            return response

        wrapped: Any = async_wrapped

    else:

        def sync_wrapped(*args: Any, **kwargs: Any) -> Any:
            correlation_id, start = prepare(kwargs)
            if kwargs.get("stream") is True:
                return original(*args, **kwargs)
            try:
                response = original(*args, **kwargs)
            except BaseException:
                record(correlation_id, start, failed=True)
                raise
            record(correlation_id, start, response)
            return response

        wrapped = sync_wrapped

    wrapped.__llmwitness_responses_owner__ = sink
    client.responses.create = wrapped
    return client
