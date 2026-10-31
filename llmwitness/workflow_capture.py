"""Metadata-only translation for local AI workflow observations.

This module deliberately does not execute workflows, inspect nested payloads, or
turn framework events into runtime decisions.  It translates a small,
versioned allow-list into the existing SDK's local telemetry shape.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .utils import normalize_uuidv7

SUPPORTED_SOURCES = ("codex-jsonl", "claude-code-hook", "generic")

_CODEX_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)
_CODEX_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "agent_message",
        "reasoning",
        "web_search",
        "todo_list",
        "error",
    }
)
_CODEX_STATUS_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "mcp_tool_call"}
)
_CODEX_COMPLETED_ITEM_STATUSES = frozenset({"completed", "failed"})
_CLAUDE_HOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
        "StopFailure",
    }
)
_GENERIC_EVENTS = frozenset(
    {
        "workflow.started",
        "workflow.completed",
        "workflow.failed",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "usage",
    }
)
_MAX_NATIVE_IDENTIFIER_LENGTH = 512
_MAX_TOKEN_COUNT = 2**53 - 1
_FAILED_EVENT_ERROR = "workflow source reported a failure"


class CaptureError(ValueError):
    """A validation error that intentionally never includes source payload data."""


class TelemetrySink(Protocol):
    """The narrow SDK dependency required to record a capture observation."""

    def record_event(
        self,
        task_name: str | None = None,
        correlation_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        completion_string: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        agent_state: dict[str, Any] | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: float | None = None,
        input_messages: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> str:
        """Record a local telemetry event using the SDK's public contract."""


@dataclass(frozen=True)
class WorkflowObservation:
    """An immutable, deliberately minimal workflow metadata observation."""

    source: str
    source_event: str
    correlation_id: str
    observed_at: float
    prompt_tokens: int
    completion_tokens: int
    outcome: str
    native_references: tuple[tuple[str, str], ...]
    item_type: str | None = None
    error: str | None = None

    def to_sdk_payload(self) -> dict[str, Any]:
        """Return a fresh ingestion-compatible payload without raw source content."""
        agent_state: dict[str, Any] = {
            "schema_version": "0.1",
            "source": self.source,
            "source_event": self.source_event,
            "outcome": self.outcome,
            "native_references": dict(self.native_references),
        }
        if self.item_type is not None:
            agent_state["item_type"] = self.item_type
        return {
            "correlation_id": self.correlation_id,
            "task_name": f"workflow:{self.source}",
            "timestamp": self.observed_at,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "completion_string": None,
            "tool_calls": [],
            "agent_state": agent_state,
            "provider": self.source,
            "model": None,
            "latency_ms": None,
            "input_messages": [],
            "estimated_cost_usd": None,
            "error": self.error,
        }


class WorkflowCapture:
    """Records normalized metadata through a caller-provided SDK-compatible sink."""

    def __init__(self, sink: TelemetrySink) -> None:
        self._sink = sink

    def capture(
        self,
        source: str,
        event: Mapping[str, object],
        correlation_id: str,
        *,
        observed_at: float | None = None,
    ) -> str:
        """Normalize and record metadata through the caller's telemetry sink.

        ``observed_at`` is retained by :meth:`WorkflowObservation.to_sdk_payload`
        for offline consumers.  The SDK ``record_event`` contract owns its own
        telemetry timestamp, so this method deliberately does not present the
        caller-supplied observation time as a persisted SDK event timestamp.
        """
        observation = normalize_workflow_event(
            source, event, correlation_id, observed_at=observed_at
        )
        payload = observation.to_sdk_payload()
        try:
            returned_id = self._sink.record_event(
                task_name=payload["task_name"],
                correlation_id=payload["correlation_id"],
                prompt_tokens=payload["prompt_tokens"],
                completion_tokens=payload["completion_tokens"],
                completion_string=None,
                tool_calls=[],
                agent_state=payload["agent_state"],
                provider=payload["provider"],
                model=None,
                latency_ms=None,
                input_messages=[],
                error=payload["error"],
            )
            if normalize_uuidv7(returned_id) != observation.correlation_id:
                raise CaptureError("telemetry sink returned a different correlation id")
        except CaptureError:
            raise
        except Exception:
            raise CaptureError("telemetry sink rejected workflow observation") from None
        return observation.correlation_id


def normalize_workflow_event(
    source: str,
    event: Mapping[str, object],
    correlation_id: str,
    *,
    observed_at: float | None = None,
) -> WorkflowObservation:
    """Normalize one allow-listed native event without reading content-bearing fields."""
    normalized_correlation_id = _normalized_correlation_id(correlation_id)
    normalized_time = _normalized_timestamp(observed_at)
    if source not in SUPPORTED_SOURCES:
        raise CaptureError("workflow capture source is not supported")
    if not isinstance(event, Mapping):
        raise CaptureError("workflow event must be a mapping")

    if source == "codex-jsonl":
        source_event, item_type, outcome, references, tokens = _normalize_codex(event)
    elif source == "claude-code-hook":
        source_event, item_type, outcome, references, tokens = _normalize_claude(event)
    else:
        source_event, item_type, outcome, references, tokens = _normalize_generic(event)

    return WorkflowObservation(
        source=source,
        source_event=source_event,
        correlation_id=normalized_correlation_id,
        observed_at=normalized_time,
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        outcome=outcome,
        native_references=tuple(sorted(references.items())),
        item_type=item_type,
        error=_FAILED_EVENT_ERROR if outcome == "failed" else None,
    )


def _normalized_correlation_id(value: str) -> str:
    try:
        return normalize_uuidv7(value)
    except (TypeError, ValueError, AttributeError):
        raise CaptureError("correlation_id must be an RFC 9562 UUIDv7") from None


def _normalized_timestamp(value: float | None) -> float:
    timestamp = time.time() if value is None else value
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise CaptureError("observed_at must be a finite nonnegative number")
    try:
        numeric_timestamp = float(timestamp)
    except (OverflowError, TypeError, ValueError):
        raise CaptureError("observed_at must be a finite nonnegative number") from None
    if not math.isfinite(numeric_timestamp) or numeric_timestamp < 0:
        raise CaptureError("observed_at must be a finite nonnegative number")
    return numeric_timestamp


def _normalize_codex(
    event: Mapping[str, object],
) -> tuple[str, str | None, str, dict[str, str], tuple[int, int]]:
    event_type = _allowlisted_event_type(event, "type", _CODEX_EVENTS)
    references = _identifier_references(event, "codex-jsonl", ("thread_id",))
    item_type: str | None = None
    outcome = _event_outcome(event_type)
    if event_type.startswith("item."):
        item = event.get("item")
        if not isinstance(item, Mapping):
            raise CaptureError("workflow event shape is not supported")
        item_type = _allowlisted_event_type(item, "type", _CODEX_ITEM_TYPES)
        outcome = _codex_item_outcome(event_type, item_type, item)
        references.update(_identifier_references(item, "codex-jsonl", ("id",)))
        if item_type == "mcp_tool_call":
            references.update(_identifier_references(item, "codex-jsonl", ("tool",)))
    tokens = _usage_tokens(event.get("usage"))
    return event_type, item_type, outcome, references, tokens


def _codex_item_outcome(
    event_type: str, item_type: str, item: Mapping[str, object]
) -> str:
    """Derive a terminal outcome without treating a failed native item as success.

    Codex's command-execution, file-change, and MCP-tool item types carry a
    native status.  Those statuses are authoritative only for the narrow
    metadata observation: they never establish effect verification or provider
    truth.  Other item types have no portable terminal-status contract here.
    """
    outcome = _event_outcome(event_type)
    if item_type == "error":
        return "failed"
    if item_type not in _CODEX_STATUS_ITEM_TYPES:
        return outcome

    status = item.get("status")
    if event_type == "item.completed":
        if not isinstance(status, str) or status not in _CODEX_COMPLETED_ITEM_STATUSES:
            raise CaptureError("workflow native item status is not supported")
        return status
    if event_type == "item.started":
        if status != "in_progress":
            raise CaptureError("workflow native item status is not supported")
    elif event_type == "item.updated":
        if not isinstance(status, str) or status not in {
            "in_progress",
            "completed",
            "failed",
        }:
            raise CaptureError("workflow native item status is not supported")
        if status == "failed":
            return "failed"
    return outcome


def _normalize_claude(
    event: Mapping[str, object],
) -> tuple[str, str | None, str, dict[str, str], tuple[int, int]]:
    event_type = _allowlisted_event_type(event, "hook_event_name", _CLAUDE_HOOK_EVENTS)
    references = _identifier_references(
        event,
        "claude-code-hook",
        ("session_id", "tool_use_id", "tool_name", "agent_id"),
    )
    return event_type, None, _event_outcome(event_type), references, (0, 0)


def _normalize_generic(
    event: Mapping[str, object],
) -> tuple[str, str | None, str, dict[str, str], tuple[int, int]]:
    event_type = _allowlisted_event_type(event, "type", _GENERIC_EVENTS)
    references = _identifier_references(event, "generic", ("session_id", "event_id"))
    return (
        event_type,
        None,
        _event_outcome(event_type),
        references,
        _usage_tokens(event.get("usage")),
    )


def _allowlisted_event_type(
    event: Mapping[str, object], field_name: str, allowed_values: frozenset[str]
) -> str:
    candidate = event.get(field_name)
    if not isinstance(candidate, str) or candidate not in allowed_values:
        raise CaptureError("workflow event type is not supported")
    return candidate


def _identifier_references(
    event: Mapping[str, object], source: str, field_names: tuple[str, ...]
) -> dict[str, str]:
    references: dict[str, str] = {}
    for field_name in field_names:
        if field_name in event:
            value = event[field_name]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > _MAX_NATIVE_IDENTIFIER_LENGTH
            ):
                raise CaptureError("workflow native identifier is invalid")
            references[f"{field_name}_ref"] = _hashed_reference(
                source, field_name, value
            )
    return references


def _hashed_reference(source: str, field_name: str, value: str) -> str:
    material = f"llmwitness/workflow-capture/0.1:{source}:{field_name}:".encode()
    try:
        identifier = value.encode("utf-8")
    except UnicodeEncodeError:
        raise CaptureError("workflow native identifier is invalid") from None
    return "sha256:" + hashlib.sha256(material + identifier).hexdigest()


def _usage_tokens(value: object) -> tuple[int, int]:
    if value is None:
        return 0, 0
    if not isinstance(value, Mapping):
        raise CaptureError("workflow usage is invalid")
    return _token_count(value, "input_tokens"), _token_count(value, "output_tokens")


def _token_count(usage: Mapping[str, object], field_name: str) -> int:
    if field_name not in usage:
        return 0
    value = usage[field_name]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_TOKEN_COUNT
    ):
        raise CaptureError("workflow token count is invalid")
    return value


def _event_outcome(event_type: str) -> str:
    if event_type.endswith(".failed") or event_type in {
        "error",
        "PostToolUseFailure",
        "StopFailure",
    }:
        return "failed"
    if event_type.endswith(".completed") or event_type in {
        "PostToolUse",
        "Stop",
        "SessionEnd",
        "SubagentStop",
    }:
        return "completed"
    if event_type.endswith(".started") or event_type in {
        "SessionStart",
        "PreToolUse",
        "SubagentStart",
    }:
        return "started"
    if event_type == "item.updated":
        return "updated"
    return "observed"
