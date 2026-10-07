import contextvars
import os
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from llmwitness.utils import generate_uuidv7, redact_pii

# Thread-local / Context-local correlation ID tracker
_current_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llmwitness_correlation_id", default=None
)
_current_task_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llmwitness_task_name", default=None
)


def get_current_correlation_id() -> str | None:
    """Retrieve active X-LLMWitness-Correlation-ID from current execution context."""
    return _current_correlation_id.get()


def _normalize_uuidv7(value: str) -> str:
    """Validate and return the canonical text form of an RFC 9562 UUIDv7."""
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("correlation_id must be an RFC 9562 UUIDv7") from exc
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise ValueError("correlation_id must be an RFC 9562 UUIDv7")
    return str(parsed)


@contextmanager
def trace_session(task_name: str, correlation_id: str | None = None) -> Iterator[str]:
    """Bind a task and correlation ID without requiring a tracker instance."""
    cid = (
        generate_uuidv7()
        if correlation_id is None
        else _normalize_uuidv7(correlation_id)
    )
    token_cid = _current_correlation_id.set(cid)
    token_task = _current_task_name.set(task_name)
    try:
        yield cid
    finally:
        _current_correlation_id.reset(token_cid)
        _current_task_name.reset(token_task)


class LLMWitnessTracker:
    """
    Python SDK telemetry client for LLMWitness.
    Captures model interactions, tool calls, and state variables using a
    non-blocking, best-effort background queue.
    """

    def __init__(
        self,
        ingestion_url: str = "http://localhost:8000",
        max_queue_size: int = 10000,
        flush_interval_sec: float = 0.2,
    ):
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be greater than zero")
        if flush_interval_sec <= 0:
            raise ValueError("flush_interval_sec must be greater than zero")
        self.ingestion_url = ingestion_url.rstrip("/")
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=max_queue_size
        )
        self.running = True
        self.dropped_events = 0
        self.delivery_failures = 0
        self._flush_interval_sec = flush_interval_sec
        self._shutdown_lock = threading.Lock()
        self.http_client = httpx.Client(timeout=10.0)

        # Start the background worker for best-effort telemetry delivery.
        self.worker_thread = threading.Thread(
            target=self._telemetry_worker, daemon=True, name="LLMWitnessTelemetryWorker"
        )
        self.worker_thread.start()

    def _telemetry_worker(self):
        """Worker loop reading telemetry items from queue and dispatching to Ingestion Engine."""
        while self.running or not self.queue.empty():
            try:
                payload = self.queue.get(timeout=self._flush_interval_sec)
                if payload is None:
                    self.queue.task_done()
                    break
                try:
                    headers = {"Content-Type": "application/json"}
                    ingest_token = os.getenv("LLMWITNESS_INGEST_TOKEN")
                    if ingest_token:
                        headers["Authorization"] = f"Bearer {ingest_token}"
                    response = self.http_client.post(
                        f"{self.ingestion_url}/ingest/sdk",
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                except Exception as err:
                    self.delivery_failures += 1
                    print(f"[LLMWitness SDK Warning] Failed to stream telemetry: {err}")
                finally:
                    self.queue.task_done()
            except queue.Empty:
                if not self.running:
                    break

    def record_event(
        self,
        task_name: str | None = None,
        correlation_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        completion_string: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        agent_state: dict[str, Any] | None = None,
    ) -> str:
        """
        Pushes a telemetry payload into the background queue without blocking caller execution.
        """
        active_cid = (
            _normalize_uuidv7(correlation_id)
            if correlation_id is not None
            else (_current_correlation_id.get() or generate_uuidv7())
        )
        active_task = task_name or _current_task_name.get() or "default_task"

        payload = {
            "correlation_id": active_cid,
            "task_name": active_task,
            "timestamp": time.time(),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "completion_string": (
                redact_pii(completion_string) if completion_string else None
            ),
            "tool_calls": redact_pii(tool_calls) if tool_calls else [],
            "agent_state": redact_pii(agent_state) if agent_state else {},
        }
        with self._shutdown_lock:
            if not self.running:
                raise RuntimeError("LLMWitnessTracker has been shut down")
            try:
                self.queue.put_nowait(payload)
            except queue.Full:
                # Telemetry must never delay the application it observes.
                self.dropped_events += 1
        return active_cid

    @contextmanager
    def trace_session(self, task_name: str, correlation_id: str | None = None):
        """
        Automated context manager generating or binding a unique UUIDv7 correlation ID.
        """
        with trace_session(task_name, correlation_id) as cid:
            yield cid

    def wrap_openai_client(self, client: Any) -> Any:
        """
        Wraps an OpenAI client instance to automatically record prompt tokens, completion strings,
        and function/tool calls with the current session correlation ID.
        """
        tracker = self

        original_create = client.chat.completions.create

        def intercepted_create(*args, **kwargs):
            cid = get_current_correlation_id() or generate_uuidv7()
            extra_headers = dict(kwargs.get("extra_headers") or {})
            extra_headers["X-LLMWitness-Correlation-ID"] = cid
            kwargs["extra_headers"] = extra_headers

            response = original_create(*args, **kwargs)

            try:
                usage = getattr(response, "usage", None)
                p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                c_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

                choices = getattr(response, "choices", [])
                completion_str = ""
                tool_calls_data = []

                if choices:
                    msg = getattr(choices[0], "message", None)
                    if msg:
                        completion_str = getattr(msg, "content", "") or ""
                        raw_tool_calls = getattr(msg, "tool_calls", None)
                        if raw_tool_calls:
                            for tc in raw_tool_calls:
                                tool_calls_data.append(
                                    {
                                        "id": getattr(tc, "id", ""),
                                        "type": getattr(tc, "type", "function"),
                                        "function": {
                                            "name": getattr(
                                                getattr(tc, "function", None),
                                                "name",
                                                "",
                                            ),
                                            "arguments": getattr(
                                                getattr(tc, "function", None),
                                                "arguments",
                                                "",
                                            ),
                                        },
                                    }
                                )

                tracker.record_event(
                    correlation_id=cid,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    completion_string=completion_str,
                    tool_calls=tool_calls_data,
                )
            except Exception as ex:
                print(
                    f"[LLMWitness SDK Warning] Failed to parse OpenAI completion metadata: {ex}"
                )

            return response

        client.chat.completions.create = intercepted_create
        return client

    def flush(self):
        """Block until all queued telemetry items are dispatched."""
        self.queue.join()

    def shutdown(self, timeout_sec: float = 15.0) -> bool:
        """Request shutdown and return whether the worker stopped before the timeout."""
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be greater than zero")
        with self._shutdown_lock:
            if not self.running:
                return not self.worker_thread.is_alive()
            self.running = False
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                # The worker will drain the queue and exit because running is false.
                pass
        self.worker_thread.join(timeout=timeout_sec)
        stopped = not self.worker_thread.is_alive()
        if stopped:
            self.http_client.close()
        return stopped
