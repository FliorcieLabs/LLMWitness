"""Framework integrations: a ``@witness`` decorator and a LangChain callback handler."""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Callable
from typing import Any

from llmwitness.sdk import LLMWitnessTracker


def witness(
    tracker: LLMWitnessTracker,
    task_name: str | None = None,
    *,
    auto_seal: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Run a function inside its own traced session and record its outcome.

    Works on sync and async functions. Arguments and return values are not
    recorded; only the task name, duration and any exception type and message.
    """

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        name = task_name or function.__qualname__

        def record(started: float, error: BaseException | None) -> None:
            tracker.record_event(
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                agent_state={
                    "witness": "function",
                    "outcome": "error" if error else "ok",
                },
                error=f"{type(error).__name__}: {error}" if error else None,
            )

        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with tracker.trace_session(name, auto_seal=auto_seal):
                    started = time.perf_counter()
                    try:
                        result = await function(*args, **kwargs)
                    except BaseException as exc:
                        record(started, exc)
                        raise
                    record(started, None)
                    return result

            return async_wrapper

        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with tracker.trace_session(name, auto_seal=auto_seal):
                started = time.perf_counter()
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    record(started, exc)
                    raise
                record(started, None)
                return result

        return wrapper

    return decorate


def _text_of(prompt: Any) -> str:
    return prompt if isinstance(prompt, str) else str(prompt)


def make_langchain_handler(tracker: LLMWitnessTracker) -> Any:
    """Return a LangChain callback handler that records model and tool calls.

    Requires ``langchain-core``. Pass the result in ``config={"callbacks": [...]}``.
    Events use the correlation ID of the surrounding ``trace_session``.
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError as exc:
        raise ImportError(
            "make_langchain_handler needs langchain-core: pip install langchain-core"
        ) from exc

    class LLMWitnessCallbackHandler(BaseCallbackHandler):
        def __init__(self) -> None:
            self._runs: dict[Any, dict[str, Any]] = {}

        def _start(self, run_id: Any, **details: Any) -> None:
            self._runs[run_id] = {"started": time.perf_counter(), **details}

        def _latency(self, run: dict[str, Any]) -> float:
            return round((time.perf_counter() - run["started"]) * 1000, 3)

        def on_llm_start(self, serialized, prompts, *, run_id=None, **kwargs):
            params = kwargs.get("invocation_params") or {}
            self._start(
                run_id,
                model=params.get("model") or params.get("model_name"),
                input_messages=[
                    {"role": "user", "content": _text_of(p)} for p in prompts
                ],
            )

        def on_chat_model_start(self, serialized, messages, *, run_id=None, **kwargs):
            params = kwargs.get("invocation_params") or {}
            flat = [
                {
                    "role": getattr(message, "type", "user"),
                    "content": _text_of(getattr(message, "content", message)),
                }
                for batch in messages
                for message in batch
            ]
            self._start(
                run_id,
                model=params.get("model") or params.get("model_name"),
                input_messages=flat,
            )

        def on_llm_end(self, response, *, run_id=None, **kwargs):
            run = self._runs.pop(run_id, None)
            if run is None:
                return
            output = getattr(response, "llm_output", None) or {}
            usage = output.get("token_usage") or output.get("usage") or {}
            generations = getattr(response, "generations", None) or []
            text = ""
            if generations and generations[0]:
                text = getattr(generations[0][0], "text", "") or ""
            tracker.record_event(
                provider="langchain",
                model=run.get("model") or output.get("model_name"),
                latency_ms=self._latency(run),
                input_messages=run.get("input_messages"),
                prompt_tokens=int(
                    usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                ),
                completion_tokens=int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                ),
                completion_string=text,
            )

        def on_llm_error(self, error, *, run_id=None, **kwargs):
            run = self._runs.pop(run_id, None)
            if run is None:
                return
            tracker.record_event(
                provider="langchain",
                model=run.get("model"),
                latency_ms=self._latency(run),
                input_messages=run.get("input_messages"),
                error=f"{type(error).__name__}: {error}",
            )

        def on_tool_start(self, serialized, input_str, *, run_id=None, **kwargs):
            self._start(
                run_id,
                tool=(serialized or {}).get("name", "tool"),
                tool_input=input_str,
            )

        def on_tool_end(self, output, *, run_id=None, **kwargs):
            run = self._runs.pop(run_id, None)
            if run is None:
                return
            tracker.record_event(
                provider="langchain",
                latency_ms=self._latency(run),
                tool_calls=[
                    {
                        "type": "function",
                        "function": {
                            "name": str(run.get("tool")),
                            "arguments": _text_of(run.get("tool_input")),
                        },
                    }
                ],
                completion_string=_text_of(output)[:100_000],
            )

        def on_tool_error(self, error, *, run_id=None, **kwargs):
            run = self._runs.pop(run_id, None)
            if run is None:
                return
            tracker.record_event(
                provider="langchain",
                latency_ms=self._latency(run),
                tool_calls=[
                    {
                        "type": "function",
                        "function": {"name": str(run.get("tool")), "arguments": ""},
                    }
                ],
                error=f"{type(error).__name__}: {error}",
            )

    return LLMWitnessCallbackHandler()
