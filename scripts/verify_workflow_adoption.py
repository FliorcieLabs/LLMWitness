"""Offline installed-package smoke: metadata profiles, CLI and real LangGraph.

Run with the freshly installed environment's Python, using ``-I`` to exclude
the source checkout and PYTHONPATH. No provider model or live service is used.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict


def main() -> None:
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    import httpx
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.graph import END, START, StateGraph

    import llmwitness
    from llmwitness import LLMWitnessTracker, WorkflowCapture, generate_uuidv7
    from llmwitness.cli import main as capture_main
    from llmwitness.integrations import make_langchain_handler
    from llmwitness.metadata_callbacks import make_metadata_langchain_handler
    from llmwitness.proof_card import render_proof_card

    module_path = Path(llmwitness.__file__).resolve()
    if not module_path.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("smoke must use a separately installed distribution")

    def offline(*args, **kwargs):
        raise AssertionError("unexpected network call")

    events = []

    def receive(request):
        events.append(json.loads(request.content))
        return httpx.Response(201)

    class State(TypedDict, total=False):
        answer: str

    model = FakeListChatModel(responses=["synthetic"])

    def answer(state, config):
        return {"answer": model.invoke("synthetic", config=config).content}

    builder = StateGraph(State)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile()
    previous_connect = socket.socket.connect
    previous_connection = socket.create_connection
    previous_stdin = sys.stdin
    tracker = LLMWitnessTracker(durable=False)
    tracker.http_client.close()
    tracker.http_client = httpx.Client(transport=httpx.MockTransport(receive))
    run_id = generate_uuidv7()
    try:
        socket.socket.connect = offline
        socket.create_connection = offline
        with tracker.trace_session("installed-graph", run_id):
            result = graph.invoke({}, {"callbacks": [make_langchain_handler(tracker)]})
        assert result["answer"] == "synthetic"
        metadata_result = graph.invoke(
            {}, {"callbacks": [make_metadata_langchain_handler(tracker, run_id)]}
        )
        assert metadata_result["answer"] == "synthetic"
        response = {"id": "private", "status": "completed", "output": "private"}
        client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: response)
        )
        with tracker.trace_session("installed-responses", run_id):
            tracker.wrap_openai_responses_client(client)
            assert client.responses.create(input="private") is response
        capture = WorkflowCapture(tracker)
        for source, event in [
            ("codex-jsonl", {"type": "turn.completed"}),
            (
                "claude-code-hook",
                {"hook_event_name": "PostToolUse", "tool_name": "private"},
            ),
            ("generic", {"type": "workflow.completed"}),
        ]:
            assert capture.capture(source, event, run_id) == run_id
        assert tracker.shutdown(timeout_sec=10)
        assert len(events) == 6
        assert {event["correlation_id"] for event in events} == {run_id}
        assert "private" not in json.dumps(events[1:])
        output = io.StringIO()
        sys.stdin = io.TextIOWrapper(
            io.BytesIO(b'{"type":"workflow.completed","secret":"never kept"}\n')
        )
        with contextlib.redirect_stdout(output):
            capture_main(["capture", "--source", "generic", "--correlation-id", run_id])
        assert json.loads(output.getvalue())["correlation_id"] == run_id
        assert "never kept" not in output.getvalue()
        card = render_proof_card(
            {"authority": b'{"schema_version":"0.1","status":"unknown"}'}
        )
        assert "unknown" in card and "not authenticated" in card
        with tempfile.TemporaryDirectory(prefix="llmwitness-card-smoke-") as directory:
            source = Path(directory) / "authority.json"
            source.write_text(
                '{"schema_version":"0.1","status":"unknown"}', encoding="utf-8"
            )
            card_output = io.StringIO()
            with contextlib.redirect_stdout(card_output):
                capture_main(
                    ["proof-card", "--authority", str(source), "--format", "json"]
                )
            assert (
                json.loads(card_output.getvalue())["checks"]["authority"]["status"]
                == "unknown"
            )
    finally:
        tracker.shutdown(timeout_sec=10)
        sys.stdin = previous_stdin
        socket.socket.connect = previous_connect
        socket.create_connection = previous_connection
    print(
        json.dumps(
            {
                "profiles": 3,
                "installed_distribution": True,
                "real_graph": "passed",
                "offline_cli": "passed",
                "metadata_callbacks": "passed",
                "responses_wrapper_fixture": "passed",
                "proof_card": "passed",
                "proof_card_cli": "passed",
                "uuidv7": "preserved",
                "network_guard": "enabled during capture",
            }
        )
    )


if __name__ == "__main__":
    main()
