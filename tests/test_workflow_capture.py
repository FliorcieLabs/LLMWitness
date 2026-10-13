from __future__ import annotations

import math

import pytest

from llmwitness.utils import generate_uuidv7
from llmwitness.workflow_capture import (
    CaptureError,
    WorkflowCapture,
    normalize_workflow_event,
)


def test_codex_translation_is_deterministic_and_omits_raw_content():
    correlation_id = generate_uuidv7()
    event = {
        "type": "item.completed",
        "thread_id": "thread-private",
        "usage": {"input_tokens": 3, "output_tokens": 5},
        "item": {
            "id": "item-private",
            "type": "mcp_tool_call",
            "status": "completed",
            "tool": "private-tool-name",
            "arguments": {"password": "not recorded"},
            "result": "not recorded",
            "path": "C:/private.txt",
        },
    }

    observation = normalize_workflow_event(
        "codex-jsonl", event, correlation_id, observed_at=123.0
    )
    payload = observation.to_sdk_payload()

    assert payload["timestamp"] == 123.0
    assert payload["prompt_tokens"] == 3
    assert payload["completion_tokens"] == 5
    assert payload["completion_string"] is None
    assert payload["tool_calls"] == []
    assert payload["input_messages"] == []
    assert payload["agent_state"]["item_type"] == "mcp_tool_call"
    assert "thread-private" not in repr(payload)
    assert "private-tool-name" not in repr(payload)
    assert "password" not in repr(payload)
    assert "C:/private.txt" not in repr(payload)
    assert all(
        value.startswith("sha256:")
        for value in payload["agent_state"]["native_references"].values()
    )
    assert payload == observation.to_sdk_payload()
    assert payload is not observation.to_sdk_payload()


@pytest.mark.parametrize(
    ("source", "event", "expected_outcome"),
    [
        ("codex-jsonl", {"type": "turn.completed"}, "completed"),
        ("claude-code-hook", {"hook_event_name": "PreToolUse"}, "started"),
        ("generic", {"type": "usage"}, "observed"),
    ],
)
def test_supported_workflow_sources_have_metadata_only_observations(
    source, event, expected_outcome
):
    observation = normalize_workflow_event(
        source, event, generate_uuidv7(), observed_at=1
    )
    assert observation.source == source
    assert observation.outcome == expected_outcome
    assert observation.to_sdk_payload()["task_name"] == f"workflow:{source}"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.unknown"},
        {"type": "item.started", "item": {"type": "untrusted"}},
        {"type": "item.started", "item": "not-a-mapping"},
        {"type": "turn.started", "thread_id": ""},
        {"type": "turn.started", "thread_id": 7},
        {"type": "turn.started", "thread_id": "x" * 513},
        {"type": "turn.started", "usage": {"input_tokens": True}},
        {"type": "turn.started", "usage": {"input_tokens": -1}},
        {"type": "turn.started", "usage": {"output_tokens": 2**53}},
        {"type": "turn.started", "usage": {"input_tokens": 1.0}},
        {"type": "turn.started", "usage": "untrusted"},
    ],
)
def test_codex_rejects_malformed_or_unallowlisted_shapes(event):
    with pytest.raises(CaptureError) as exc_info:
        normalize_workflow_event("codex-jsonl", event, generate_uuidv7(), observed_at=1)
    assert "untrusted" not in str(exc_info.value)


@pytest.mark.parametrize("observed_at", [float("nan"), float("inf"), -1, True, "1"])
def test_capture_rejects_invalid_timestamps(observed_at):
    with pytest.raises(CaptureError):
        normalize_workflow_event(
            "generic",
            {"type": "workflow.started"},
            generate_uuidv7(),
            observed_at=observed_at,
        )


def test_capture_rejects_huge_integer_timestamp_without_overflowing():
    with pytest.raises(CaptureError, match="observed_at"):
        normalize_workflow_event(
            "generic",
            {"type": "workflow.started"},
            generate_uuidv7(),
            observed_at=10**400,
        )


@pytest.mark.parametrize(
    "item_type", ["command_execution", "mcp_tool_call", "file_change"]
)
def test_failed_codex_item_completion_is_never_normalized_as_completed(item_type):
    observation = normalize_workflow_event(
        "codex-jsonl",
        {
            "type": "item.completed",
            "item": {"id": "item-id", "type": item_type, "status": "failed"},
        },
        generate_uuidv7(),
        observed_at=1,
    )

    assert observation.outcome == "failed"
    assert observation.error == "workflow source reported a failure"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "item.completed", "item": {"id": "item", "type": "command_execution"}},
        {
            "type": "item.completed",
            "item": {"id": "item", "type": "mcp_tool_call", "status": "in_progress"},
        },
        {
            "type": "item.completed",
            "item": {"id": "item", "type": "file_change", "status": "declined"},
        },
        {
            "type": "item.started",
            "item": {"id": "item", "type": "command_execution", "status": "completed"},
        },
    ],
)
def test_codex_status_bearing_items_reject_unsupported_lifecycle_shapes(event):
    with pytest.raises(CaptureError, match="status"):
        normalize_workflow_event("codex-jsonl", event, generate_uuidv7(), observed_at=1)


def test_capture_canonicalizes_uppercase_uuidv7_without_reading_event_first():
    correlation_id = generate_uuidv7()
    observation = normalize_workflow_event(
        "generic", {"type": "workflow.started"}, correlation_id.upper(), observed_at=1
    )
    assert observation.correlation_id == correlation_id


@pytest.mark.parametrize(
    "correlation_id", ["not-a-uuid", "00000000-0000-4000-8000-000000000000"]
)
def test_capture_rejects_non_v7_correlation_ids(correlation_id):
    with pytest.raises(CaptureError) as exc_info:
        normalize_workflow_event(
            "generic", {"type": "private_event"}, correlation_id, observed_at=1
        )
    assert "private_event" not in str(exc_info.value)


def test_hashing_is_stable_and_domain_separated():
    correlation_id = generate_uuidv7()
    codex = normalize_workflow_event(
        "codex-jsonl",
        {"type": "thread.started", "thread_id": "same-id"},
        correlation_id,
        observed_at=1,
    )
    generic = normalize_workflow_event(
        "generic",
        {"type": "workflow.started", "session_id": "same-id"},
        correlation_id,
        observed_at=1,
    )
    assert (
        codex.native_references
        == normalize_workflow_event(
            "codex-jsonl",
            {"type": "thread.started", "thread_id": "same-id"},
            correlation_id,
            observed_at=1,
        ).native_references
    )
    assert codex.native_references[0][1] != generic.native_references[0][1]


class _Sink:
    def __init__(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id
        self.calls: list[dict[str, object]] = []

    def record_event(self, **kwargs):
        self.calls.append(kwargs)
        return self.correlation_id


def test_capture_calls_only_record_event_and_excludes_sdk_generated_values():
    correlation_id = generate_uuidv7()
    sink = _Sink(correlation_id)
    returned = WorkflowCapture(sink).capture(
        "generic",
        {"type": "workflow.completed", "usage": {"input_tokens": 1}},
        correlation_id,
        observed_at=1,
    )
    assert returned == correlation_id
    assert len(sink.calls) == 1
    assert "timestamp" not in sink.calls[0]
    assert "estimated_cost_usd" not in sink.calls[0]
    assert sink.calls[0]["completion_string"] is None
    assert sink.calls[0]["tool_calls"] == []
    assert sink.calls[0]["input_messages"] == []


def test_capture_rejects_sink_mismatch_and_hides_sink_exception():
    correlation_id = generate_uuidv7()
    mismatch = WorkflowCapture(_Sink(generate_uuidv7()))
    with pytest.raises(CaptureError, match="different correlation"):
        mismatch.capture(
            "generic", {"type": "workflow.started"}, correlation_id, observed_at=1
        )

    class SecretFailingSink:
        def record_event(self, **kwargs):
            raise RuntimeError("secret-token-value")

    with pytest.raises(CaptureError) as exc_info:
        WorkflowCapture(SecretFailingSink()).capture(
            "generic", {"type": "workflow.started"}, correlation_id, observed_at=1
        )
    assert "secret-token-value" not in str(exc_info.value)


def test_metadata_only_payload_uses_finite_timestamp():
    observation = normalize_workflow_event(
        "generic", {"type": "workflow.started"}, generate_uuidv7(), observed_at=0
    )
    assert math.isfinite(observation.to_sdk_payload()["timestamp"])


@pytest.mark.parametrize("status", [[], {}, True, None])
def test_codex_updated_status_is_type_checked(status):
    with pytest.raises(CaptureError, match="status"):
        normalize_workflow_event(
            "codex-jsonl",
            {
                "type": "item.updated",
                "item": {"type": "command_execution", "status": status},
            },
            generate_uuidv7(),
        )


def test_invalid_unicode_reference_has_content_free_error():
    with pytest.raises(CaptureError) as failure:
        normalize_workflow_event(
            "generic",
            {"type": "workflow.started", "session_id": "secret-\ud800"},
            generate_uuidv7(),
        )
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "item.completed",
            "item": {"type": "error", "message": "private failure"},
        },
        {
            "type": "item.updated",
            "item": {"type": "command_execution", "status": "failed"},
        },
    ],
)
def test_codex_error_items_and_failed_updates_remain_failed_observations(event):
    observation = normalize_workflow_event("codex-jsonl", event, generate_uuidv7())
    assert observation.outcome == "failed"
    assert observation.error == "workflow source reported a failure"
    assert "private failure" not in repr(observation)


@pytest.mark.parametrize(
    ("source", "event"),
    [
        ("generic", {"type": "workflow.completed"}),
        (
            "codex-jsonl",
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        ),
        (
            "claude-code-hook",
            {"hook_event_name": "PostToolUseFailure", "tool_use_id": "private"},
        ),
    ],
)
def test_normalized_observation_satisfies_existing_ingestion_schema(source, event):
    from llmwitness.ingest import SDKTelemetryPayload

    payload = normalize_workflow_event(
        source, event, generate_uuidv7(), observed_at=1
    ).to_sdk_payload()
    validated = SDKTelemetryPayload.model_validate(payload)
    assert validated.correlation_id == payload["correlation_id"]
    assert validated.completion_string is None
    assert validated.input_messages == []
