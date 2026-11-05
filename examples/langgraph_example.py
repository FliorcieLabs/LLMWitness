"""Real local LangGraph callbacks with a fake model; no paid provider required.

Install from this checkout: pip install -e ".[langgraph]"
In another terminal: llmwitness serve --no-gateway
Run: python examples/langgraph_example.py
Existing callbacks capture synthetic content with best-effort pattern scrubbing.
"""

import json
from typing import TypedDict

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from llmwitness import LLMWitnessTracker
from llmwitness.integrations import make_langchain_handler


class GraphState(TypedDict, total=False):
    prompt: str
    answer: str
    result: int


@tool
def add_one(number: int) -> int:
    """Add one locally; no provider or external side effect."""
    return number + 1


def run_graph() -> None:
    model = FakeListChatModel(responses=["synthetic answer"])

    def answer(state: GraphState, config: RunnableConfig) -> dict:
        return {"answer": model.invoke(state["prompt"], config=config).content}

    def calculate(state: GraphState, config: RunnableConfig) -> dict:
        return {"result": add_one.invoke({"number": 1}, config=config)}

    builder = StateGraph(GraphState)
    builder.add_node("answer", answer)
    builder.add_node("calculate", calculate)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", "calculate")
    builder.add_edge("calculate", END)
    graph = builder.compile()
    tracker = LLMWitnessTracker()
    try:
        with tracker.trace_session("langgraph-local-demo") as run_id:
            result = graph.invoke(
                {"prompt": "synthetic prompt"},
                config={"callbacks": [make_langchain_handler(tracker)]},
            )
        tracker.flush()
        print(
            json.dumps(
                {"run_id": run_id, "result": result, "delivery": tracker.stats()}
            )
        )
    finally:
        tracker.shutdown()


if __name__ == "__main__":
    run_graph()
