"""Boundary behaviour of the tracker queue, lifecycle, and client wrapper."""

import json
import threading
from types import SimpleNamespace

import pytest

from llmwitness import LLMWitnessTracker, get_current_correlation_id, trace_session


class _Response:
    def raise_for_status(self):
        return None


def _recording_tracker(monkeypatch, **kwargs) -> tuple[LLMWitnessTracker, list, list]:
    """Build a tracker whose deliveries are captured instead of sent."""
    delivered: list = []
    headers: list = []
    tracker = LLMWitnessTracker(ingestion_url="http://ingest.invalid", **kwargs)

    def post(*args, **post_kwargs):
        delivered.append(post_kwargs["json"])
        headers.append(post_kwargs["headers"])
        return _Response()

    monkeypatch.setattr(tracker.http_client, "post", post)
    return tracker, delivered, headers


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_queue_size": 0}, "max_queue_size"),
        ({"max_queue_size": -1}, "max_queue_size"),
        ({"flush_interval_sec": 0}, "flush_interval_sec"),
        ({"flush_interval_sec": -0.5}, "flush_interval_sec"),
    ],
)
def test_constructor_rejects_non_positive_bounds(kwargs, message):
    with pytest.raises(ValueError, match=message):
        LLMWitnessTracker(**kwargs)


def test_trailing_slashes_are_stripped_from_the_ingestion_url(monkeypatch):
    tracker, _, _ = _recording_tracker(monkeypatch)
    tracker.shutdown()
    assert LLMWitnessTracker(ingestion_url="http://host:8000///").ingestion_url == (
        "http://host:8000"
    )


def test_shutdown_rejects_a_non_positive_timeout(monkeypatch):
    tracker, _, _ = _recording_tracker(monkeypatch)
    with pytest.raises(ValueError, match="timeout_sec"):
        tracker.shutdown(timeout_sec=0)
    tracker.shutdown()


def test_queue_overflow_drops_events_rather_than_blocking_the_caller(monkeypatch):
    """Telemetry must never delay the application it observes."""
    release = threading.Event()
    tracker = LLMWitnessTracker(ingestion_url="http://ingest.invalid", max_queue_size=2)

    def blocking_post(*args, **kwargs):
        release.wait(timeout=5)
        return _Response()

    monkeypatch.setattr(tracker.http_client, "post", blocking_post)
    for _ in range(50):
        tracker.record_event(task_name="overflow")

    assert tracker.dropped_events > 0
    release.set()
    assert tracker.shutdown(timeout_sec=10) is True


def test_delivery_failures_are_counted_and_never_raised(monkeypatch, capsys):
    tracker = LLMWitnessTracker(ingestion_url="http://ingest.invalid")

    def failing_post(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(tracker.http_client, "post", failing_post)
    tracker.record_event(task_name="unreachable")
    assert tracker.shutdown(timeout_sec=10) is True

    assert tracker.delivery_failures == 1
    assert "Failed to stream telemetry" in capsys.readouterr().out


def test_explicit_arguments_win_over_the_ambient_session(monkeypatch):
    tracker, delivered, _ = _recording_tracker(monkeypatch)
    override = "019fd93b-a06b-7799-947f-67f80b9edc04"

    with trace_session("ambient-task"):
        assert tracker.record_event(task_name="explicit", correlation_id=override) == (
            override
        )

    tracker.shutdown()
    assert delivered[0]["correlation_id"] == override
    assert delivered[0]["task_name"] == "explicit"


def test_events_recorded_outside_a_session_get_defaults(monkeypatch):
    tracker, delivered, _ = _recording_tracker(monkeypatch)
    correlation_id = tracker.record_event()
    tracker.shutdown()

    assert delivered[0]["correlation_id"] == correlation_id
    assert delivered[0]["task_name"] == "default_task"
    assert delivered[0]["completion_string"] is None
    assert delivered[0]["tool_calls"] == []
    assert delivered[0]["agent_state"] == {}


def test_recorded_payloads_are_redacted_before_leaving_the_process(monkeypatch):
    tracker, delivered, _ = _recording_tracker(monkeypatch)
    tracker.record_event(
        completion_string="ssn 123-45-6789",
        tool_calls=[{"function": {"arguments": '{"ssn": "123-45-6789"}'}}],
        agent_state={"authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
    )
    tracker.shutdown()

    assert "123-45-6789" not in json.dumps(delivered[0])
    assert delivered[0]["agent_state"]["authorization"] == "[REDACTED_SENSITIVE_FIELD]"


def test_ingest_token_is_attached_only_when_configured(monkeypatch):
    tracker, _, headers = _recording_tracker(monkeypatch)
    tracker.record_event()
    tracker.flush()
    assert "Authorization" not in headers[0]

    monkeypatch.setenv("LLMWITNESS_INGEST_TOKEN", "local-ingest-token")
    tracker.record_event()
    tracker.shutdown()
    assert headers[1]["Authorization"] == "Bearer local-ingest-token"


def test_flush_blocks_until_every_queued_event_is_dispatched(monkeypatch):
    tracker, delivered, _ = _recording_tracker(monkeypatch)
    for index in range(25):
        tracker.record_event(task_name=f"task-{index}")

    tracker.flush()
    assert len(delivered) == 25
    tracker.shutdown()


def test_shutdown_is_idempotent_and_closes_the_session(monkeypatch):
    tracker, _, _ = _recording_tracker(monkeypatch)
    assert tracker.shutdown(timeout_sec=10) is True
    assert tracker.shutdown(timeout_sec=10) is True
    with pytest.raises(RuntimeError, match="shut down"):
        tracker.record_event()


def test_nested_sessions_restore_the_enclosing_correlation_id():
    with trace_session("outer") as outer:
        with trace_session("inner") as inner:
            assert inner != outer
            assert get_current_correlation_id() == inner
        assert get_current_correlation_id() == outer
    assert get_current_correlation_id() is None


def test_session_context_is_cleared_when_the_body_raises():
    with pytest.raises(RuntimeError, match="agent failure"):
        with trace_session("failing-task"):
            raise RuntimeError("agent failure")
    assert get_current_correlation_id() is None


def test_session_context_does_not_leak_into_other_threads():
    observed: list = []

    with trace_session("main-thread-task"):
        worker = threading.Thread(
            target=lambda: observed.append(get_current_correlation_id())
        )
        worker.start()
        worker.join()

    assert observed == [None]


def _fake_openai_client(response):
    namespace = SimpleNamespace()
    namespace.chat = SimpleNamespace(
        completions=SimpleNamespace(create=lambda *args, **kwargs: response)
    )
    return namespace


def test_wrapper_preserves_caller_supplied_extra_headers(monkeypatch):
    tracker, _, _ = _recording_tracker(monkeypatch)
    seen: dict = {}
    response = SimpleNamespace(usage=None, choices=[])

    client = _fake_openai_client(response)
    original_create = client.chat.completions.create
    client.chat.completions.create = lambda *args, **kwargs: (
        seen.update(kwargs) or original_create(*args, **kwargs)
    )
    tracker.wrap_openai_client(client)

    with trace_session("wrapped-task") as correlation_id:
        client.chat.completions.create(
            model="test", extra_headers={"X-Existing": "kept"}
        )

    tracker.shutdown()
    assert seen["extra_headers"]["X-Existing"] == "kept"
    assert seen["extra_headers"]["X-LLMWitness-Correlation-ID"] == correlation_id


def test_wrapper_returns_the_response_even_when_metadata_cannot_be_parsed(
    monkeypatch, capsys
):
    tracker, delivered, _ = _recording_tracker(monkeypatch)

    class _Hostile:
        @property
        def usage(self):
            raise RuntimeError("provider changed the schema")

    response = _Hostile()
    client = tracker.wrap_openai_client(_fake_openai_client(response))
    assert client.chat.completions.create(model="test") is response

    tracker.shutdown()
    assert delivered == []
    assert "Failed to parse OpenAI completion metadata" in capsys.readouterr().out


def test_wrapper_tolerates_responses_without_usage_or_choices(monkeypatch):
    tracker, delivered, _ = _recording_tracker(monkeypatch)
    response = SimpleNamespace(usage=None, choices=[])
    client = tracker.wrap_openai_client(_fake_openai_client(response))

    client.chat.completions.create(model="test")
    tracker.shutdown()

    assert delivered[0]["prompt_tokens"] == 0
    assert delivered[0]["completion_tokens"] == 0
    assert delivered[0]["completion_string"] is None
