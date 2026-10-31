"""Real optional LangGraph execution, without provider calls or network I/O."""

import asyncio
from typing import TypedDict

import httpx
import pytest

from llmwitness.integrations import make_langchain_handler
from llmwitness.metadata_callbacks import make_metadata_langchain_handler
from llmwitness.sdk import LLMWitnessTracker
from llmwitness.utils import normalize_uuidv7


class GraphState(TypedDict, total=False):
    prompt: str
    answer: str
    result: int


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_graph_metadata_only_callbacks(graph_tracker, asynchronous):
    tracker, events = graph_tracker
    with tracker.trace_session("metadata-graph") as run_id:
        callback = make_metadata_langchain_handler(tracker, run_id)
        graph = _graph()
        config = {"callbacks": [callback]}
        result = (
            asyncio.run(graph.ainvoke({"prompt": "PRIVATE INPUT"}, config))
            if asynchronous
            else graph.invoke({"prompt": "PRIVATE INPUT"}, config)
        )
    assert tracker.shutdown(timeout_sec=10)
    assert result["result"] == 2
    assert len(events) == 2
    assert {event["correlation_id"] for event in events} == {run_id}
    assert all(event["provider"] == "langchain-metadata" for event in events)
    assert "PRIVATE INPUT" not in str(events)
    assert "synthetic answer" not in str(events)
    assert callback.stats()["pending"] == 0


@pytest.fixture
def graph_tracker(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    events = []

    def receive(request):
        import json

        events.append(json.loads(request.content))
        return httpx.Response(201)

    tracker = LLMWitnessTracker(durable=False)
    tracker.http_client.close()
    tracker.http_client = httpx.Client(transport=httpx.MockTransport(receive))
    try:
        yield tracker, events
    finally:
        assert tracker.shutdown(timeout_sec=10)


def _graph(*, fail_tool=False):
    pytest.importorskip("langgraph.graph")
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    model = FakeListChatModel(responses=["synthetic answer"])

    @tool
    def add_one(number: int) -> int:
        """Add one locally, or raise a deterministic fixture error."""
        if fail_tool:
            raise ValueError("synthetic failure")
        return number + 1

    def answer(state: GraphState, config):
        response = model.invoke(state["prompt"], config=config)
        return {"answer": response.content}

    def calculate(state: GraphState, config):
        return {"result": add_one.invoke({"number": 1}, config=config)}

    builder = StateGraph(GraphState)
    builder.add_node("answer", answer)
    builder.add_node("calculate", calculate)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", "calculate")
    builder.add_edge("calculate", END)
    return builder.compile()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_graph_callbacks_preserve_uuid_and_application_result(
    graph_tracker, asynchronous
):
    tracker, events = graph_tracker
    graph = _graph()
    config = {"callbacks": [make_langchain_handler(tracker)]}
    with tracker.trace_session("real-graph") as run_id:
        if asynchronous:
            result = asyncio.run(graph.ainvoke({"prompt": "synthetic prompt"}, config))
        else:
            result = graph.invoke({"prompt": "synthetic prompt"}, config)
    assert tracker.shutdown(timeout_sec=10)

    assert result["answer"] == "synthetic answer"
    assert result["result"] == 2
    assert len(events) == 2
    assert {event["correlation_id"] for event in events} == {run_id}
    assert normalize_uuidv7(run_id) == run_id
    model_event, tool_event = events
    assert model_event["provider"] == "langchain"
    assert model_event["completion_string"] == "synthetic answer"
    assert tool_event["tool_calls"][0]["function"]["name"] == "add_one"
    assert tool_event["completion_string"] == "2"
    assert all("VERIFIED" not in str(event["agent_state"]) for event in events)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_graph_failure_is_observed_without_swallowing_error(
    graph_tracker, asynchronous
):
    tracker, events = graph_tracker
    graph = _graph(fail_tool=True)
    config = {"callbacks": [make_langchain_handler(tracker)]}
    with tracker.trace_session("failed-graph") as run_id:
        with pytest.raises(ValueError, match="synthetic failure"):
            if asynchronous:
                asyncio.run(graph.ainvoke({"prompt": "synthetic prompt"}, config))
            else:
                graph.invoke({"prompt": "synthetic prompt"}, config)
    assert tracker.shutdown(timeout_sec=10)

    assert len(events) == 2
    assert {event["correlation_id"] for event in events} == {run_id}
    assert events[-1]["error"] == "ValueError: synthetic failure"
    assert events[-1]["completion_string"] is None
