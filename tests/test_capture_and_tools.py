"""Regression tests for SDK delivery and capture, the gateway routes, and CLI tools."""

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from llmwitness import cli, config, gateway, ingest
from llmwitness.integrations import make_langchain_handler, witness
from llmwitness.otel_export import post_otlp, receipt_to_otlp
from llmwitness.sdk import LLMWitnessTracker, estimate_cost
from llmwitness.timestamping import (
    build_timestamp_request,
    request_timestamp,
    response_status,
)
from llmwitness.utils import Ed25519KeyManager, generate_uuidv7, receipt_digest


class _Reply:
    def __init__(self, status_code=201, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")


def _tracker(monkeypatch, responses, **kwargs):
    """A tracker whose HTTP client replays ``responses`` (an exception is raised)."""
    tracker = LLMWitnessTracker(
        ingestion_url="http://ingest.invalid", retry_backoff_sec=0, **kwargs
    )
    calls: list[dict] = []
    script = list(responses)

    def post(url, **post_kwargs):
        calls.append({"url": url, **post_kwargs})
        outcome = script.pop(0) if script else _Reply()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(tracker.http_client, "post", post)
    return tracker, calls


# --- SDK delivery ------------------------------------------------------------


def test_a_transient_failure_is_retried_and_delivered(monkeypatch, tmp_path):
    tracker, calls = _tracker(
        monkeypatch, [OSError("refused"), _Reply(503), _Reply()], spool_dir=tmp_path
    )
    tracker.record_event(task_name="retry")
    assert tracker.shutdown(timeout_sec=10)
    assert len(calls) == 3
    assert tracker.delivery_failures == 0 and tracker.spooled_events == 0


def test_an_outage_spools_events_and_a_later_tracker_replays_them(
    monkeypatch, tmp_path, caplog
):
    down, calls = _tracker(monkeypatch, [OSError("refused")] * 10, spool_dir=tmp_path)
    for index in range(3):
        down.record_event(task_name=f"lost-{index}")
    assert down.shutdown(timeout_sec=10)
    assert down.delivery_failures == 3 and down.spooled_events == 3
    # Only the first failure of the outage is retried: 3 attempts, then 1 each.
    assert len(calls) == 5
    assert caplog.text.count("Failed to stream telemetry") == 1
    assert down.stats()["spooled_events"] == 3

    up, calls = _tracker(monkeypatch, [], spool_dir=tmp_path)
    up.record_event(task_name="fresh")
    assert up.shutdown(timeout_sec=10)
    delivered = [call["json"]["task_name"] for call in calls]
    assert delivered == ["fresh", "lost-0", "lost-1", "lost-2"]
    assert up.replayed_events == 3
    assert not up.spool.has_pending("sdk")


def test_a_rejected_event_is_discarded_not_spooled(monkeypatch, tmp_path, caplog):
    tracker, calls = _tracker(monkeypatch, [_Reply(409)], spool_dir=tmp_path)
    tracker.record_event(task_name="sealed-already")
    assert tracker.shutdown(timeout_sec=10)
    assert len(calls) == 1
    assert tracker.delivery_failures == 1 and tracker.spooled_events == 0
    assert "ingestion rejected the event" in caplog.text


def test_durable_false_keeps_nothing_on_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMWITNESS_SPOOL_DIR", str(tmp_path / "spool"))
    tracker, _ = _tracker(monkeypatch, [OSError("refused")] * 5, durable=False)
    tracker.record_event(task_name="gone")
    assert tracker.shutdown(timeout_sec=10)
    assert tracker.spool is None and not (tmp_path / "spool").exists()


def test_constructor_rejects_negative_retry_settings():
    with pytest.raises(ValueError, match="max_retries"):
        LLMWitnessTracker(max_retries=-1)
    with pytest.raises(ValueError, match="retry_backoff_sec"):
        LLMWitnessTracker(retry_backoff_sec=-0.1)


# --- SDK capture -------------------------------------------------------------


def _openai_response(text="hello", model="gpt-test"):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))
        ],
    )


def _client(create):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


def test_wrapper_records_prompt_model_latency_and_cost(monkeypatch, tmp_path):
    pricing = {"gpt-test": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}
    tracker, calls = _tracker(monkeypatch, [], spool_dir=tmp_path, pricing=pricing)
    client = tracker.wrap_openai_client(_client(lambda **kwargs: _openai_response()))
    with tracker.trace_session("capture") as correlation_id:
        client.chat.completions.create(
            model="gpt-test",
            messages=[{"role": "user", "content": "my SSN is 123-45-6789"}],
        )
    tracker.shutdown()
    event = calls[0]["json"]
    assert event["correlation_id"] == correlation_id
    assert event["provider"] == "openai" and event["model"] == "gpt-test"
    assert event["latency_ms"] >= 0
    assert event["input_messages"] == [
        {"role": "user", "content": "my SSN is [REDACTED_SSN]"}
    ]
    assert event["completion_string"] == "hello"
    assert event["estimated_cost_usd"] == pytest.approx(0.00002)


def test_capture_inputs_can_be_turned_off_and_is_bounded(monkeypatch, tmp_path):
    tracker, calls = _tracker(monkeypatch, [], spool_dir=tmp_path, capture_inputs=False)
    tracker.record_event(input_messages=[{"role": "user", "content": "secret plan"}])
    tracker.shutdown()
    assert calls[0]["json"]["input_messages"] == []

    tracker, calls = _tracker(monkeypatch, [], spool_dir=tmp_path)
    messages = [
        {"role": "user", "content": f"{index}:" + "word " * 8000} for index in range(6)
    ]
    tracker.record_event(input_messages=messages)
    tracker.shutdown()
    captured = calls[0]["json"]["input_messages"]
    assert captured[0]["role"] == "llmwitness" and "omitted" in captured[0]["content"]
    assert captured[-1]["content"].startswith("5:")
    assert len(json.dumps(captured)) < 110_000


def test_a_failed_provider_call_is_recorded_and_reraised(monkeypatch, tmp_path):
    tracker, calls = _tracker(monkeypatch, [], spool_dir=tmp_path)

    def create(**kwargs):
        raise TimeoutError("upstream timed out")

    client = tracker.wrap_openai_client(_client(create))
    with pytest.raises(TimeoutError):
        client.chat.completions.create(model="gpt-test", messages=[])
    tracker.shutdown()
    assert calls[0]["json"]["error"] == "TimeoutError: upstream timed out"


def _openai_chunks():
    def chunk(content=None, tool=None, usage=None):
        delta = SimpleNamespace(content=content, tool_calls=[tool] if tool else None)
        return SimpleNamespace(
            model="gpt-test", usage=usage, choices=[SimpleNamespace(delta=delta)]
        )

    tool_a = SimpleNamespace(
        index=0, id="call_1", function=SimpleNamespace(name="lookup", arguments='{"q":')
    )
    tool_b = SimpleNamespace(
        index=0, id=None, function=SimpleNamespace(name=None, arguments='"x"}')
    )
    return [
        chunk("Hel"),
        chunk("lo"),
        chunk(tool=tool_a),
        chunk(tool=tool_b),
        SimpleNamespace(
            model="gpt-test",
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
            choices=[],
        ),
    ]


def test_sync_stream_is_passed_through_and_recorded_once(monkeypatch, tmp_path):
    tracker, calls = _tracker(monkeypatch, [], spool_dir=tmp_path)
    chunks = _openai_chunks()
    client = tracker.wrap_openai_client(_client(lambda **kwargs: iter(chunks)))
    stream = client.chat.completions.create(model="gpt-test", messages=[], stream=True)
    assert list(stream) == chunks
    tracker.shutdown()
    assert len(calls) == 1
    event = calls[0]["json"]
    assert event["completion_string"] == "Hello"
    assert (event["prompt_tokens"], event["completion_tokens"]) == (7, 3)
    function = event["tool_calls"][0]["function"]
    assert function["name"] == "lookup" and json.loads(function["arguments"]) == {
        "q": "x"
    }


def test_async_client_and_async_stream_are_recorded(monkeypatch, tmp_path):
    tracker, calls = _tracker(monkeypatch, [], spool_dir=tmp_path)

    async def create(**kwargs):
        if kwargs.get("stream"):

            async def generate():
                for chunk in _openai_chunks():
                    yield chunk

            return generate()
        return _openai_response("async reply")

    client = tracker.wrap_openai_client(_client(create))

    async def run():
        plain = await client.chat.completions.create(model="gpt-test", messages=[])
        stream = await client.chat.completions.create(
            model="gpt-test", messages=[], stream=True
        )
        return plain, [chunk async for chunk in stream]

    plain, streamed = asyncio.run(run())
    assert plain.choices[0].message.content == "async reply" and len(streamed) == 5
    tracker.shutdown()
    assert [call["json"]["completion_string"] for call in calls] == [
        "async reply",
        "Hello",
    ]


def test_anthropic_client_response_and_stream_are_recorded(monkeypatch, tmp_path):
    tracker, calls = _tracker(monkeypatch, [], spool_dir=tmp_path)
    response = SimpleNamespace(
        model="claude-test",
        usage=SimpleNamespace(input_tokens=11, output_tokens=4),
        content=[
            SimpleNamespace(type="text", text="Sure."),
            SimpleNamespace(
                type="tool_use", id="toolu_1", name="search", input={"q": "x"}
            ),
        ],
    )
    events = [
        {
            "type": "message_start",
            "message": {"model": "claude-test", "usage": {"input_tokens": 9}},
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t", "name": "search"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hi"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "message_delta", "usage": {"output_tokens": 6}},
    ]

    def create(**kwargs):
        return iter(events) if kwargs.get("stream") else response

    client = tracker.wrap_anthropic_client(
        SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    client.messages.create(
        model="claude-test",
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
    )
    list(client.messages.create(model="claude-test", messages=[], stream=True))
    tracker.shutdown()
    plain, streamed = (call["json"] for call in calls)
    assert plain["provider"] == "anthropic" and plain["completion_string"] == "Sure."
    assert plain["input_messages"][0] == {"role": "system", "content": "be brief"}
    assert plain["tool_calls"][0]["function"]["name"] == "search"
    assert (plain["prompt_tokens"], plain["completion_tokens"]) == (11, 4)
    assert streamed["completion_string"] == "Hi"
    assert (streamed["prompt_tokens"], streamed["completion_tokens"]) == (9, 6)
    assert streamed["tool_calls"][0]["function"] == {
        "name": "search",
        "arguments": "{}",
    }


def test_cost_estimate_uses_only_supplied_prices():
    table = {"gpt-4o": {"input_per_mtok": 2.0, "output_per_mtok": 8.0}}
    assert estimate_cost(table, "gpt-4o-2026-01-01", 1_000_000, 500_000) == 6.0
    assert estimate_cost(table, "other-model", 10, 10) is None
    assert estimate_cost({}, "gpt-4o", 10, 10) is None
    assert estimate_cost({"m": "broken"}, "m", 1, 1) is None


# --- seal, decorator and LangChain ------------------------------------------


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A tracker wired straight into the in-process ingestion app."""
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path / "receipts")
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    monkeypatch.setattr(ingest, "key_manager", Ed25519KeyManager())
    monkeypatch.setattr(config, "_LOCAL_SESSION_SECRET", "y" * 32)
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    ingest._chain_heads.clear()
    app_client = TestClient(ingest.app)
    tracker = LLMWitnessTracker(
        ingestion_url="http://testserver", spool_dir=tmp_path / "spool"
    )
    monkeypatch.setattr(
        tracker.http_client,
        "post",
        lambda url, **kwargs: app_client.post(
            url.replace("http://testserver", ""),
            json=kwargs["json"],
            headers=kwargs.get("headers"),
        ),
    )
    yield tracker
    tracker.shutdown()
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()


def test_seal_and_auto_seal_produce_receipts(live):
    with live.trace_session("manual") as correlation_id:
        live.record_event(completion_string="done")
    result = live.seal(correlation_id)
    assert result["status"] == "receipt_created" and result["chain_index"] == 0

    with live.trace_session("automatic", auto_seal=True) as second:
        live.record_event(completion_string="done too")
    assert live.last_receipt["correlation_id"] == second
    assert live.last_receipt["chain_index"] == 1

    with pytest.raises(ValueError, match="correlation_id"):
        live.seal()


def test_auto_seal_failure_is_logged_not_raised(live, monkeypatch, caplog):
    def refuse(url, **kwargs):
        raise httpx.ConnectError("ingestion is down")

    with live.trace_session("unreachable", auto_seal=True):
        monkeypatch.setattr(live.http_client, "post", refuse)
    assert "could not seal run" in caplog.text


def test_witness_decorator_traces_sync_and_async_functions(live):
    @witness(live, auto_seal=True)
    def answer(question: str) -> str:
        return question.upper()

    @witness(live, "async-task")
    async def fail() -> None:
        raise RuntimeError("tool exploded")

    assert answer("hi") == "HI" and answer.__name__ == "answer"
    first = live.last_receipt["correlation_id"]
    with pytest.raises(RuntimeError):
        asyncio.run(fail())
    live.flush()
    sealed = ingest.audit_vault[first]["sdk_events"][0]
    assert sealed["agent_state"] == {"witness": "function", "outcome": "ok"}
    failed = [
        session["sdk_events"][0]
        for session in ingest.audit_vault.values()
        if session["sdk_events"][0]["task_name"] == "async-task"
    ]
    assert failed[0]["error"] == "RuntimeError: tool exploded"


def test_langchain_handler_records_model_and_tool_runs(live, monkeypatch):
    with pytest.raises(ImportError, match="langchain-core"):
        monkeypatch.setitem(sys.modules, "langchain_core", None)
        make_langchain_handler(live)

    callbacks = types.ModuleType("langchain_core.callbacks")
    callbacks.BaseCallbackHandler = type("BaseCallbackHandler", (), {})
    package = types.ModuleType("langchain_core")
    package.callbacks = callbacks
    monkeypatch.setitem(sys.modules, "langchain_core", package)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks)
    handler = make_langchain_handler(live)

    with live.trace_session("chain") as correlation_id:
        handler.on_chat_model_start(
            {},
            [[SimpleNamespace(type="human", content="what is 2+2?")]],
            run_id="r1",
            invocation_params={"model": "chat-model"},
        )
        handler.on_llm_end(
            SimpleNamespace(
                generations=[[SimpleNamespace(text="4")]],
                llm_output={
                    "token_usage": {"prompt_tokens": 6, "completion_tokens": 1}
                },
            ),
            run_id="r1",
        )
        handler.on_tool_start({"name": "calculator"}, "2+2", run_id="r2")
        handler.on_tool_end("4", run_id="r2")
        handler.on_llm_start({}, ["prompt"], run_id="r3")
        handler.on_llm_error(ValueError("rate limited"), run_id="r3")
        handler.on_llm_end(SimpleNamespace(), run_id="unknown")
    live.flush()
    model, tool, error = ingest.audit_vault[correlation_id]["sdk_events"]
    assert model["model"] == "chat-model" and model["completion_string"] == "4"
    assert model["input_messages"] == [{"role": "human", "content": "what is 2+2?"}]
    assert tool["tool_calls"][0]["function"] == {
        "name": "calculator",
        "arguments": "2+2",
    }
    assert error["error"] == "ValueError: rate limited"


# --- gateway -----------------------------------------------------------------


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    """The gateway app with a scripted upstream and ingestion service."""
    state = SimpleNamespace(upstream=None, requests=[], telemetry=[], ingest_status=201)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ingest.test":
            if state.ingest_status >= 400:
                return httpx.Response(state.ingest_status)
            state.telemetry.append(json.loads(request.content))
            return httpx.Response(201, json={"status": "accepted"})
        state.requests.append(request)
        return state.upstream(request)

    monkeypatch.setattr(
        gateway,
        "http_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(gateway, "UPSTREAM_OPENAI_URL", "https://openai.test")
    monkeypatch.setattr(gateway, "UPSTREAM_ANTHROPIC_URL", "https://anthropic.test")
    monkeypatch.setattr(gateway, "INGESTION_SERVER_URL", "http://ingest.test")
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", None)
    monkeypatch.setattr(gateway, "INGEST_TOKEN", None)
    monkeypatch.setattr(gateway, "_consecutive_failures", 0)
    monkeypatch.setattr(
        gateway, "telemetry_stats", dict.fromkeys(gateway.telemetry_stats, 0)
    )
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "false")
    monkeypatch.setenv("LLMWITNESS_SPOOL_DIR", str(tmp_path / "gateway-spool"))
    state.client = TestClient(gateway.app)
    return state


def test_anthropic_route_forwards_its_own_headers_and_audits_the_reply(proxy):
    body = {"id": "msg_1", "content": [{"type": "text", "text": "reach me at a@b.io"}]}
    proxy.upstream = lambda request: httpx.Response(
        200,
        json=body,
        headers={
            "request-id": "req_1",
            "anthropic-ratelimit-requests-remaining": "9",
            "set-cookie": "x=1",
        },
    )
    response = proxy.client.post(
        "/v1/messages",
        json={
            "model": "claude-test",
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={
            "x-api-key": "secret-key",
            "anthropic-version": "2023-06-01",
            "cookie": "session=1",
        },
    )
    assert response.status_code == 200 and response.json() == body
    assert response.headers["request-id"] == "req_1"
    assert response.headers["anthropic-ratelimit-requests-remaining"] == "9"
    assert "set-cookie" not in response.headers
    upstream = proxy.requests[0]
    assert str(upstream.url) == "https://anthropic.test/v1/messages"
    assert upstream.headers["x-api-key"] == "secret-key"
    assert upstream.headers["anthropic-version"] == "2023-06-01"
    assert "cookie" not in upstream.headers
    event = proxy.telemetry[0]
    assert event["optimization_meta"]["provider"] == "anthropic"
    assert "secret-key" not in json.dumps(event)
    assert (
        event["redacted_response"]["content"][0]["text"]
        == "reach me at [REDACTED_EMAIL]"
    )


def _sse(events) -> bytes:
    return (
        b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)
        + b"data: [DONE]\n\n"
    )


def test_openai_stream_is_relayed_unchanged_and_audited_as_whole_text(proxy):
    # The SSN is split across two chunks; only whole-text scrubbing can catch it.
    raw = _sse(
        [
            {"model": "gpt-test", "choices": [{"delta": {"content": "SSN 123-45"}}]},
            {"choices": [{"delta": {"content": "-6789 ok"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
        ]
    )
    proxy.upstream = lambda request: httpx.Response(
        200, content=raw, headers={"content-type": "text/event-stream"}
    )
    response = proxy.client.post(
        "/v1/chat/completions",
        json={"model": "gpt-test", "stream": True, "messages": []},
    )
    assert response.status_code == 200 and response.content == raw
    assert response.headers["content-type"].startswith("text/event-stream")
    audit = proxy.telemetry[0]["redacted_response"]
    assert audit["stream"] is True and audit["completed"] is True
    assert audit["text"] == "SSN [REDACTED_SSN] ok"
    assert audit["events"] == 3 and audit["finish_reason"] == "stop"
    assert audit["usage"] == {"prompt_tokens": 3, "completion_tokens": 4}
    assert "123-45" not in json.dumps(proxy.telemetry[0])


def test_anthropic_stream_and_stream_error_bodies_are_audited(proxy):
    raw = _sse(
        [
            {
                "type": "message_start",
                "message": {"model": "claude-test", "usage": {"input_tokens": 2}},
            },
            {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "search"},
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hi there"},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
        ]
    )
    proxy.upstream = lambda request: httpx.Response(
        200, content=raw, headers={"content-type": "text/event-stream"}
    )
    response = proxy.client.post(
        "/v1/messages", json={"model": "claude-test", "stream": True}
    )
    assert response.content == raw
    audit = proxy.telemetry[0]["redacted_response"]
    assert audit["text"] == "Hi there" and audit["tool_names"] == ["search"]
    assert audit["usage"] == {"input_tokens": 2, "output_tokens": 5}
    assert audit["finish_reason"] == "end_turn"

    proxy.upstream = lambda request: httpx.Response(
        429, json={"error": "slow down sk-abcdefghijklmnopqrstuvwx"}
    )
    refused = proxy.client.post(
        "/v1/messages", json={"model": "claude-test", "stream": True}
    )
    assert refused.status_code == 429
    preview = proxy.telemetry[1]["redacted_response"]
    assert "[REDACTED_API_TOKEN]" in preview["body_preview"] and "stream" not in preview


def test_mock_upstream_supports_both_providers_and_streams(proxy, monkeypatch):
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "true")
    message = proxy.client.post("/v1/messages", json={"model": "m"})
    assert message.json()["content"][0]["text"] == "Mock response."
    stream = proxy.client.post(
        "/v1/chat/completions", json={"model": "m", "stream": True}
    )
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert b"data: [DONE]" in stream.content
    assert proxy.telemetry[1]["redacted_response"]["text"] == "Mock response."
    assert proxy.requests == []


def test_undelivered_gateway_telemetry_is_spooled_counted_and_replayed(proxy, caplog):
    proxy.upstream = lambda request: httpx.Response(200, json={"ok": True})
    proxy.ingest_status = 503
    for _ in range(2):
        assert (
            proxy.client.post("/v1/chat/completions", json={"model": "m"}).status_code
            == 200
        )
    health = proxy.client.get("/health").json()["telemetry"]
    assert health["failed"] == 2 and health["spooled"] == 2 and health["delivered"] == 0
    assert "could not be delivered" in caplog.text

    proxy.ingest_status = 201
    proxy.client.post("/v1/chat/completions", json={"model": "m"})
    health = proxy.client.get("/health").json()["telemetry"]
    assert health["delivered"] == 1 and health["replayed"] == 2
    assert len(proxy.telemetry) == 3


# --- CLI tools ---------------------------------------------------------------


def _receipts(live, outputs):
    ids = []
    for output in outputs:
        with live.trace_session("step") as correlation_id:
            live.record_event(
                model="gpt-test",
                prompt_tokens=10,
                completion_tokens=2,
                latency_ms=40.0,
                completion_string=output,
                input_messages=[{"role": "user", "content": "<b>question</b>"}],
                tool_calls=[
                    {
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            )
        live.seal(correlation_id)
        ids.append(correlation_id)
    return ids


def test_list_show_diff_and_chain_commands(live, tmp_path, capsys):
    first, second = _receipts(live, ["the answer is 4", "the answer is 5"])
    directory = str(ingest.RECEIPT_DIR)

    cli.main(["list", "--receipt-dir", directory])
    listing = capsys.readouterr().out
    assert first in listing and second in listing and "valid" in listing
    cli.main(["list", "--receipt-dir", directory, "--json"])
    assert {item["correlation_id"] for item in json.loads(capsys.readouterr().out)} == {
        first,
        second,
    }
    cli.main(["list", "--receipt-dir", str(tmp_path / "empty")])
    assert "No receipts" in capsys.readouterr().out

    cli.main(["show", first, "--receipt-dir", directory])
    shown = capsys.readouterr().out
    assert (
        "Signature  valid" in shown and "gpt-test" in shown and "tools: lookup" in shown
    )
    page = tmp_path / "run.html"
    cli.main(["show", first, "--receipt-dir", directory, "--html", str(page)])
    html_text = page.read_text(encoding="utf-8")
    assert (
        "&lt;b&gt;question&lt;/b&gt;" in html_text
        and "<b>question</b>" not in html_text
    )

    with pytest.raises(SystemExit):
        cli.main(["diff", first, second, "--receipt-dir", directory])
    difference = capsys.readouterr().out
    assert "step 1 output differs" in difference and "+the answer is 5" in difference
    cli.main(["diff", first, first, "--receipt-dir", directory])
    assert "No differences" in capsys.readouterr().out

    cli.main(["verify-chain", "--receipt-dir", directory])
    assert "[OK] Receipt chain is unbroken." in capsys.readouterr().out
    (ingest.RECEIPT_DIR / f"{first}.json").unlink()
    with pytest.raises(SystemExit):
        cli.main(["verify-chain", "--receipt-dir", directory])
    assert "missing" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["show", generate_uuidv7(), "--receipt-dir", directory])
    assert "[ERROR]" in capsys.readouterr().out


def test_seal_command_reports_success_and_refusal(live, monkeypatch, capsys):
    app_client = TestClient(ingest.app)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: app_client.post("/ingest/seal", json=kwargs["json"]),
    )
    with live.trace_session("cli-seal") as correlation_id:
        live.record_event(completion_string="x")
    live.flush()
    cli.main(["seal", correlation_id, "--ingestion-url", "http://testserver"])
    assert "[OK] Receipt created" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["seal", correlation_id, "--ingestion-url", "http://testserver"])
    assert "HTTP 409" in capsys.readouterr().out

    def unreachable(url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", unreachable)
    with pytest.raises(SystemExit):
        cli.main(["seal", correlation_id])
    assert "Could not reach" in capsys.readouterr().out


def test_otel_export_builds_genai_spans_and_posts_them(live, tmp_path, capsys):
    (correlation_id,) = _receipts(live, ["ok"])
    receipt = json.loads(
        (ingest.RECEIPT_DIR / f"{correlation_id}.json").read_text(encoding="utf-8")
    )
    payload = receipt_to_otlp(receipt)
    root, span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert (
        span["traceId"] == correlation_id.replace("-", "") and len(span["spanId"]) == 16
    )
    assert span["parentSpanId"] == root["spanId"]
    attributes = {item["key"]: item["value"] for item in span["attributes"]}
    assert attributes["gen_ai.request.model"] == {"stringValue": "gpt-test"}
    assert attributes["gen_ai.usage.input_tokens"] == {"intValue": "10"}
    assert int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]) == 40_000_000

    output = tmp_path / "trace.json"
    cli.main(
        [
            "export-otel",
            correlation_id,
            "--receipt-dir",
            str(ingest.RECEIPT_DIR),
            "--output",
            str(output),
        ]
    )
    assert json.loads(output.read_text(encoding="utf-8")) == payload

    seen = []

    def collector(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(collector)) as client:
        assert post_otlp("http://collector.test:4318", payload, client) == 200
    assert seen == [("http://collector.test:4318/v1/traces", payload)]


def test_timestamp_request_encoding_and_token_storage(live):
    request = build_timestamp_request(bytes(range(32)), nonce=bytes([0xFF] * 8))
    assert request[:2] == b"\x30\x43" and len(request) == 69
    assert request[2:5] == b"\x02\x01\x01"  # version 1
    assert bytes(range(32)) in request and request.endswith(b"\x01\x01\xff")
    assert request[56:66] == b"\x02\x08\x7f" + b"\xff" * 7  # positive nonce
    with pytest.raises(ValueError):
        build_timestamp_request(b"short")
    granted = bytes.fromhex("30053003020100")  # TimeStampResp{PKIStatusInfo{status 0}}
    refused = bytes.fromhex("30053003020102")
    assert response_status(granted) == 0 and response_status(refused) == 2
    with pytest.raises(ValueError):
        response_status(b"\x30\x80")

    (correlation_id,) = _receipts(live, ["ok"])
    path = ingest.RECEIPT_DIR / f"{correlation_id}.json"
    digest = receipt_digest(json.loads(path.read_text(encoding="utf-8")))
    sent = []

    def authority(http_request: httpx.Request) -> httpx.Response:
        sent.append(http_request)
        return httpx.Response(200, content=granted if len(sent) == 1 else refused)

    with httpx.Client(transport=httpx.MockTransport(authority)) as client:
        token_path, digest_hex = request_timestamp(path, "https://tsa.test", client)
        assert token_path.read_bytes() == granted and digest_hex == digest.hex()
        assert digest in sent[0].content
        assert sent[0].headers["content-type"] == "application/timestamp-query"
        with pytest.raises(FileExistsError):
            request_timestamp(path, "https://tsa.test", client)
        token_path.unlink()
        with pytest.raises(ValueError, match="refused"):
            request_timestamp(path, "https://tsa.test", client)
        assert not token_path.exists()
