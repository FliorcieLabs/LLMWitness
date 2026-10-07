"""Export one receipt as OpenTelemetry trace data (OTLP/JSON).

The output follows the OTLP JSON encoding and the GenAI semantic conventions
for model calls, without depending on the OpenTelemetry SDK. Content fields are
whatever the receipt holds, which was scrubbed on a best-effort basis.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import httpx

from llmwitness.utils import as_mapping

SCOPE_NAME = "llmwitness"


def _attribute(key: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (_attribute(key, value) for key, value in values.items())
        if item
    ]


def _span_id(trace_id: str, label: str) -> str:
    return hashlib.sha256(f"{trace_id}:{label}".encode()).hexdigest()[:16]


def _nanos(timestamp: Any) -> int:
    seconds = float(timestamp) if isinstance(timestamp, (int, float)) else 0.0
    if seconds > 1e11:  # browser events are recorded in milliseconds
        seconds /= 1000
    return int(seconds * 1_000_000_000)


def receipt_to_otlp(
    receipt: dict[str, Any], service_name: str = "llmwitness"
) -> dict[str, Any]:
    """Build an OTLP ``ExportTraceServiceRequest`` for one sealed run."""
    correlation_id = str(receipt.get("correlation_id", ""))
    trace_id = uuid.UUID(correlation_id).hex
    events = receipt.get("events") or {}
    root_id = _span_id(trace_id, "root")
    spans: list[dict[str, Any]] = []

    def add(
        label: str,
        name: str,
        start: int,
        end: int,
        attributes: dict[str, Any],
        error: Any = None,
    ) -> None:
        span: dict[str, Any] = {
            "traceId": trace_id,
            "spanId": _span_id(trace_id, label),
            "parentSpanId": root_id,
            "name": name,
            "kind": 3,  # SPAN_KIND_CLIENT
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(max(start, end)),
            "attributes": _attributes(attributes),
        }
        if error:
            span["status"] = {"code": 2, "message": str(error)[:256]}
        spans.append(span)

    for index, event in enumerate(events.get("sdk") or []):
        if not isinstance(event, dict):
            continue
        end = _nanos(event.get("timestamp"))
        latency = event.get("latency_ms")
        duration = int(latency * 1_000_000) if isinstance(latency, (int, float)) else 0
        model = event.get("model")
        add(
            f"sdk:{index}",
            f"chat {model}" if model else str(event.get("task_name", "task")),
            end - duration,
            end,
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.system": event.get("provider"),
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": event.get("prompt_tokens"),
                "gen_ai.usage.output_tokens": event.get("completion_tokens"),
                "llmwitness.task_name": event.get("task_name"),
                "llmwitness.tool_call_count": len(event.get("tool_calls") or []),
                "llmwitness.estimated_cost_usd": event.get("estimated_cost_usd"),
            },
            event.get("error"),
        )
    for index, event in enumerate(events.get("gateway") or []):
        if not isinstance(event, dict):
            continue
        start = _nanos(event.get("timestamp"))
        meta = as_mapping(event.get("optimization_meta"))
        gateway_ms = meta.get("duration_ms")
        status_code = event.get("status_code")
        add(
            f"gateway:{index}",
            "gateway request",
            start,
            start
            + (
                int(gateway_ms * 1_000_000)
                if isinstance(gateway_ms, (int, float))
                else 0
            ),
            {
                "http.response.status_code": status_code,
                "url.full": event.get("upstream_url"),
                "gen_ai.system": meta.get("provider"),
                "llmwitness.stream": meta.get("stream"),
            },
            (
                f"HTTP {status_code}"
                if isinstance(status_code, int) and status_code >= 400
                else None
            ),
        )
    for index, event in enumerate(events.get("extension") or []):
        if not isinstance(event, dict):
            continue
        moment = _nanos(event.get("timestamp"))
        add(
            f"extension:{index}",
            f"browser {event.get('event_type', 'event')}",
            moment,
            moment,
            {
                "url.full": event.get("url"),
                "llmwitness.element_id": event.get("element_id"),
            },
        )

    starts = [int(span["startTimeUnixNano"]) for span in spans]
    ends = [int(span["endTimeUnixNano"]) for span in spans]
    root = {
        "traceId": trace_id,
        "spanId": root_id,
        "name": "llmwitness run",
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": str(min(starts, default=0)),
        "endTimeUnixNano": str(max(ends, default=0)),
        "attributes": _attributes(
            {
                "llmwitness.correlation_id": correlation_id,
                "llmwitness.sealed_at": receipt.get("sealed_at"),
                "llmwitness.signer_fingerprint": receipt.get("public_key_fingerprint"),
            }
        ),
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes({"service.name": service_name})},
                "scopeSpans": [
                    {"scope": {"name": SCOPE_NAME}, "spans": [root, *spans]}
                ],
            }
        ]
    }


def post_otlp(
    endpoint: str, payload: dict[str, Any], client: httpx.Client | None = None
) -> int:
    """Send trace data to an OTLP/HTTP collector, e.g. ``http://localhost:4318``."""
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    owned = client is None
    active = client or httpx.Client(timeout=10.0)
    try:
        response = active.post(url, json=payload)
        response.raise_for_status()
        return response.status_code
    finally:
        if owned:
            active.close()
