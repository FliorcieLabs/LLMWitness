import contextvars
import inspect
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from llmwitness.spool import (
    EventSpool,
    classify_delivery,
    spool_directory_from_env,
)
from llmwitness.utils import generate_uuidv7, normalize_uuidv7, redact_pii

logger = logging.getLogger("llmwitness.sdk")

_SPOOL_STREAM = "sdk"
_PROVIDER_LABELS = {"openai": "OpenAI", "anthropic": "Anthropic"}
# Captured prompts are bounded so one event stays under the ingestion size limit.
MAX_INPUT_CAPTURE_BYTES = 100_000

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
        return normalize_uuidv7(value)
    except ValueError as exc:
        raise ValueError("correlation_id must be an RFC 9562 UUIDv7") from exc


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

    An event that cannot be delivered is retried briefly and then written to a
    local spool so it can be re-sent once the ingestion service is reachable.
    Delivery is still best-effort: events are dropped when the queue is full.
    """

    def __init__(
        self,
        ingestion_url: str = "http://localhost:8000",
        max_queue_size: int = 10000,
        flush_interval_sec: float = 0.2,
        *,
        max_retries: int = 2,
        retry_backoff_sec: float = 0.1,
        spool_dir: str | os.PathLike[str] | None = None,
        durable: bool = True,
        capture_inputs: bool = True,
        pricing: dict[str, dict[str, float]] | None = None,
    ):
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be greater than zero")
        if flush_interval_sec <= 0:
            raise ValueError("flush_interval_sec must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_backoff_sec < 0:
            raise ValueError("retry_backoff_sec must not be negative")
        self.ingestion_url = ingestion_url.rstrip("/")
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=max_queue_size
        )
        self.running = True
        self.dropped_events = 0
        self.delivery_failures = 0
        self.spooled_events = 0
        self.replayed_events = 0
        self.last_receipt: dict[str, Any] | None = None
        self.capture_inputs = capture_inputs
        self.pricing = pricing if pricing is not None else _load_pricing_from_env()
        self._flush_interval_sec = flush_interval_sec
        self._max_retries = max_retries
        self._retry_backoff_sec = retry_backoff_sec
        self._consecutive_failures = 0
        self._last_error = ""
        self._stop_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self.http_client = httpx.Client(timeout=10.0)

        spool_path = (
            None
            if not durable
            else (
                Path(spool_dir) if spool_dir is not None else spool_directory_from_env()
            )
        )
        self.spool: EventSpool | None = (
            EventSpool(spool_path) if spool_path is not None else None
        )
        self._spool_pending = (
            self.spool.has_pending(_SPOOL_STREAM) if self.spool is not None else False
        )

        # Start the background worker for best-effort telemetry delivery.
        self.worker_thread = threading.Thread(
            target=self._telemetry_worker, daemon=True, name="LLMWitnessTelemetryWorker"
        )
        self.worker_thread.start()

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        ingest_token = os.getenv("LLMWITNESS_INGEST_TOKEN")
        if ingest_token:
            headers["Authorization"] = f"Bearer {ingest_token}"
        return headers

    def _post_event(self, payload: dict[str, Any]) -> str:
        """Send one event; returns ``delivered``, ``retry`` or ``discard``."""
        try:
            response = self.http_client.post(
                f"{self.ingestion_url}/ingest/sdk",
                json=payload,
                headers=self._auth_headers(),
            )
        except Exception as err:
            self._last_error = str(err)
            return "retry"
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            # A transport that exposes no status: fall back to its own check.
            try:
                response.raise_for_status()
            except Exception as err:
                self._last_error = str(err)
                return "retry"
            return "delivered"
        outcome = classify_delivery(status_code)
        if outcome != "delivered":
            self._last_error = f"HTTP {status_code}"
        return outcome

    def _deliver(self, payload: dict[str, Any]) -> None:
        # Only the first failure of an outage is retried, so a long outage costs
        # one backoff series rather than one per queued event.
        attempts = 1 + (self._max_retries if self._consecutive_failures == 0 else 0)
        outcome = "retry"
        for attempt in range(attempts):
            outcome = self._post_event(payload)
            if outcome != "retry" or attempt + 1 >= attempts:
                break
            # During shutdown the wait returns at once, so retries still happen
            # but no longer delay the exit.
            self._stop_event.wait(self._retry_backoff_sec * (2**attempt))

        if outcome == "delivered":
            self._consecutive_failures = 0
            if self._spool_pending or (
                self.spool is not None and self.spool.has_pending(_SPOOL_STREAM)
            ):
                self._replay_spool()
            return

        self.delivery_failures += 1
        if outcome == "discard":
            logger.warning(
                "Failed to stream telemetry: ingestion rejected the event (%s)",
                self._last_error,
            )
            return
        self._consecutive_failures += 1
        spooled = self.spool is not None and self.spool.append(_SPOOL_STREAM, payload)
        if spooled:
            self.spooled_events += 1
            self._spool_pending = True
        log = logger.warning if self._consecutive_failures == 1 else logger.debug
        log(
            "Failed to stream telemetry (%s); event %s",
            self._last_error,
            "spooled for retry" if spooled else "lost",
        )

    def _replay_spool(self) -> int:
        """Re-send spooled events; stops at the first one that still cannot be sent."""
        if self.spool is None:
            return 0
        claim = self.spool.claim(_SPOOL_STREAM)
        if claim is None:
            self._spool_pending = False
            return 0
        delivered = 0
        remaining: list[dict[str, Any]] = []
        for index, payload in enumerate(claim.payloads):
            outcome = self._post_event(payload)
            if outcome == "retry":
                remaining = claim.payloads[index:]
                break
            if outcome == "delivered":
                delivered += 1
        self.spool.release(_SPOOL_STREAM, claim, remaining)
        self._spool_pending = bool(remaining) or self.spool.has_pending(_SPOOL_STREAM)
        self.replayed_events += delivered
        if delivered:
            logger.info("LLMWitness re-sent %d spooled telemetry event(s)", delivered)
        return delivered

    def _telemetry_worker(self):
        """Worker loop reading telemetry items from queue and dispatching to Ingestion Engine."""
        while self.running or not self.queue.empty():
            try:
                payload = self.queue.get(timeout=self._flush_interval_sec)
                if payload is None:
                    self.queue.task_done()
                    break
                try:
                    self._deliver(payload)
                except Exception as err:
                    self.delivery_failures += 1
                    logger.warning("Failed to stream telemetry: %s", err)
                finally:
                    self.queue.task_done()
            except queue.Empty:
                if not self.running:
                    break

    def stats(self) -> dict[str, int]:
        """Delivery counters, so lost telemetry is visible to the application."""
        return {
            "queued": self.queue.qsize(),
            "dropped_events": self.dropped_events,
            "delivery_failures": self.delivery_failures,
            "spooled_events": self.spooled_events,
            "replayed_events": self.replayed_events,
        }

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
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "input_messages": (
                _bounded_messages(redact_pii(input_messages))
                if input_messages and self.capture_inputs
                else []
            ),
            "estimated_cost_usd": estimate_cost(
                self.pricing, model, prompt_tokens, completion_tokens
            ),
            "error": redact_pii(error[:2000]) if error else None,
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
    def trace_session(
        self,
        task_name: str,
        correlation_id: str | None = None,
        auto_seal: bool = False,
    ):
        """
        Automated context manager generating or binding a unique UUIDv7 correlation ID.

        With ``auto_seal`` the run is sealed into a receipt when the block ends.
        A failed seal is logged rather than raised, so it cannot break the caller.
        """
        with trace_session(task_name, correlation_id) as cid:
            try:
                yield cid
            finally:
                if auto_seal:
                    try:
                        self.seal(cid)
                    except Exception as err:
                        logger.warning("LLMWitness could not seal run %s: %s", cid, err)

    def seal(self, correlation_id: str | None = None) -> dict[str, Any]:
        """Flush queued events and seal one run into a signed local receipt.

        Returns the ingestion service's response, including ``receipt_file``.
        """
        active = correlation_id or _current_correlation_id.get()
        if active is None:
            raise ValueError("seal() needs a correlation_id outside a trace_session")
        cid = _normalize_uuidv7(active)
        self.flush()
        response = self.http_client.post(
            f"{self.ingestion_url}/ingest/seal",
            json={"correlation_id": cid},
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        self.last_receipt = response.json()
        return self.last_receipt

    def _record_call(
        self, cid: str, provider: str, started: float, **fields: Any
    ) -> None:
        try:
            self.record_event(
                correlation_id=cid,
                provider=provider,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                **fields,
            )
        except Exception as ex:
            logger.warning(
                "LLMWitness failed to record %s call metadata: %s", provider, ex
            )

    def _wrap_create(
        self,
        original_create: Callable[..., Any],
        provider: str,
        request_fields: Callable[[dict[str, Any]], dict[str, Any]],
        response_fields: Callable[[Any], dict[str, Any]],
        new_accumulator: Callable[[], Any],
    ) -> Callable[..., Any]:
        """Build a recording replacement for a provider's ``create`` call.

        Handles the four call shapes: sync or async, plain response or stream.
        """
        tracker = self

        def prepare(kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            cid = get_current_correlation_id() or generate_uuidv7()
            extra_headers = dict(kwargs.get("extra_headers") or {})
            extra_headers["X-LLMWitness-Correlation-ID"] = cid
            kwargs["extra_headers"] = extra_headers
            try:
                fields = request_fields(kwargs)
            except Exception:
                fields = {}
            return cid, fields

        def finish(cid: str, started: float, request: dict[str, Any], response: Any):
            if request.pop("_stream", False):
                accumulator = new_accumulator()

                def done() -> None:
                    tracker._record_call(
                        cid, provider, started, **{**request, **accumulator.fields()}
                    )

                return _RecordingStream(response, accumulator.add, done)
            try:
                observed = response_fields(response)
            except Exception as ex:
                logger.warning(
                    "Failed to parse %s completion metadata: %s",
                    _PROVIDER_LABELS[provider],
                    ex,
                )
                return response
            tracker._record_call(cid, provider, started, **{**request, **observed})
            return response

        def failed(cid: str, started: float, request: dict[str, Any], exc: Exception):
            request.pop("_stream", None)
            tracker._record_call(
                cid, provider, started, **request, error=f"{type(exc).__name__}: {exc}"
            )

        if inspect.iscoroutinefunction(original_create):

            async def intercepted_async(*args, **kwargs):
                cid, request = prepare(kwargs)
                started = time.perf_counter()
                try:
                    response = await original_create(*args, **kwargs)
                except Exception as exc:
                    failed(cid, started, request, exc)
                    raise
                return finish(cid, started, request, response)

            return intercepted_async

        def intercepted(*args, **kwargs):
            cid, request = prepare(kwargs)
            started = time.perf_counter()
            try:
                response = original_create(*args, **kwargs)
            except Exception as exc:
                failed(cid, started, request, exc)
                raise
            return finish(cid, started, request, response)

        return intercepted

    def wrap_openai_client(self, client: Any) -> Any:
        """
        Wraps an OpenAI client instance to automatically record the prompt, model,
        latency, token counts, completion text and tool calls with the current
        session correlation ID. Works with ``OpenAI`` and ``AsyncOpenAI`` and with
        ``stream=True``.
        """
        client.chat.completions.create = self._wrap_create(
            client.chat.completions.create,
            "openai",
            _openai_request_fields,
            _openai_response_fields,
            _OpenAIStreamAccumulator,
        )
        return client

    def wrap_anthropic_client(self, client: Any) -> Any:
        """
        Wraps an Anthropic client's ``messages.create`` the same way, for
        ``Anthropic`` and ``AsyncAnthropic`` and with ``stream=True``. The
        ``messages.stream()`` helper is not intercepted.
        """
        client.messages.create = self._wrap_create(
            client.messages.create,
            "anthropic",
            _anthropic_request_fields,
            _anthropic_response_fields,
            _AnthropicStreamAccumulator,
        )
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
            self._stop_event.set()
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


def _bounded_messages(messages: Any) -> list[dict[str, Any]]:
    """Keep the most recent messages that fit in the capture budget."""
    if not isinstance(messages, list):
        return []
    kept: list[dict[str, Any]] = []
    used = 0
    for message in reversed(messages[-500:]):
        if not isinstance(message, dict):
            message = {"content": str(message)}
        size = len(json.dumps(message, default=str, ensure_ascii=False).encode("utf-8"))
        if used + size > MAX_INPUT_CAPTURE_BYTES:
            break
        kept.append(message)
        used += size
    kept.reverse()
    omitted = len(messages) - len(kept)
    if omitted:
        kept.insert(
            0,
            {
                "role": "llmwitness",
                "content": f"[{omitted} earlier message(s) omitted from capture]",
            },
        )
    return kept[:500]


def _load_pricing_from_env() -> dict[str, dict[str, float]]:
    """Read ``LLMWITNESS_PRICING_FILE``; no prices are built in."""
    path = os.getenv("LLMWITNESS_PRICING_FILE")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            table = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.error("Ignoring LLMWITNESS_PRICING_FILE %s: %s", path, exc)
        return {}
    return table if isinstance(table, dict) else {}


def estimate_cost(
    pricing: dict[str, dict[str, float]] | None,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Estimate cost in USD from a caller-supplied per-million-token price table.

    The table maps a model name, or a prefix of one, to ``input_per_mtok`` and
    ``output_per_mtok``. Returns ``None`` when the model has no price.
    """
    if not pricing or not model:
        return None
    entry = pricing.get(model)
    if entry is None:
        prefixes = [name for name in pricing if model.startswith(name)]
        if not prefixes:
            return None
        entry = pricing[max(prefixes, key=len)]
    try:
        cost = (
            prompt_tokens * float(entry.get("input_per_mtok", 0))
            + completion_tokens * float(entry.get("output_per_mtok", 0))
        ) / 1_000_000
    except (AttributeError, TypeError, ValueError):
        return None
    return round(cost, 8) if cost >= 0 else None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from an SDK object or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class _RecordingStream:
    """Pass a provider stream through unchanged while observing each chunk."""

    def __init__(
        self, stream: Any, on_chunk: Callable[[Any], None], on_done: Callable[[], None]
    ):
        self._stream = stream
        self._on_chunk = on_chunk
        self._on_done = on_done
        self._finished = False

    def _observe(self, chunk: Any) -> None:
        try:
            self._on_chunk(chunk)
        except Exception as ex:
            logger.debug("LLMWitness could not read a stream chunk: %s", ex)

    def _finish(self) -> None:
        if not self._finished:
            self._finished = True
            try:
                self._on_done()
            except Exception as exc:
                # Recording must not replace provider errors or break a caller
                # that successfully consumed the response.
                logger.warning(
                    "LLMWitness stream recording failed (%s)", type(exc).__name__
                )

    def __iter__(self):
        try:
            for chunk in self._stream:
                self._observe(chunk)
                yield chunk
        finally:
            self._finish()

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                self._observe(chunk)
                yield chunk
        finally:
            self._finish()

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc_info):
        try:
            return self._stream.__exit__(*exc_info)
        finally:
            self._finish()

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        try:
            return await self._stream.__aexit__(*exc_info)
        finally:
            self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _openai_request_fields(kwargs: dict[str, Any]) -> dict[str, Any]:
    messages = kwargs.get("messages")
    return {
        "model": kwargs.get("model"),
        "input_messages": (
            list(messages) if isinstance(messages, (list, tuple)) else None
        ),
        "_stream": bool(kwargs.get("stream")),
    }


def _openai_tool_call(tool_call: Any) -> dict[str, Any]:
    function = _get(tool_call, "function")
    return {
        "id": _get(tool_call, "id", "") or "",
        "type": _get(tool_call, "type", "function") or "function",
        "function": {
            "name": _get(function, "name", "") or "",
            "arguments": _get(function, "arguments", "") or "",
        },
    }


def _openai_response_fields(response: Any) -> dict[str, Any]:
    usage = _get(response, "usage")
    choices = _get(response, "choices") or []
    message = _get(choices[0], "message") if choices else None
    fields: dict[str, Any] = {
        "prompt_tokens": _get(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": _get(usage, "completion_tokens", 0) or 0,
        "completion_string": (_get(message, "content", "") or "") if message else "",
        "tool_calls": (
            [
                _openai_tool_call(tool_call)
                for tool_call in (_get(message, "tool_calls") or [])
            ]
            if message
            else []
        ),
    }
    model = _get(response, "model")
    if isinstance(model, str) and model:
        fields["model"] = model
    return fields


class _OpenAIStreamAccumulator:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model: str | None = None

    def add(self, chunk: Any) -> None:
        model = _get(chunk, "model")
        if isinstance(model, str) and model:
            self.model = model
        usage = _get(chunk, "usage")
        if usage is not None:
            self.prompt_tokens = _get(usage, "prompt_tokens", 0) or 0
            self.completion_tokens = _get(usage, "completion_tokens", 0) or 0
        choices = _get(chunk, "choices") or []
        if not choices:
            return
        delta = _get(choices[0], "delta")
        content = _get(delta, "content")
        if isinstance(content, str):
            self.text.append(content)
        for fragment in _get(delta, "tool_calls") or []:
            index = _get(fragment, "index", 0) or 0
            entry = self.tool_calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            entry["id"] = _get(fragment, "id") or entry["id"]
            function = _get(fragment, "function")
            entry["function"]["name"] += _get(function, "name", "") or ""
            entry["function"]["arguments"] += _get(function, "arguments", "") or ""

    def fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "completion_string": "".join(self.text),
            "tool_calls": [self.tool_calls[index] for index in sorted(self.tool_calls)],
        }
        if self.model:
            fields["model"] = self.model
        return fields


def _anthropic_request_fields(kwargs: dict[str, Any]) -> dict[str, Any]:
    messages = kwargs.get("messages")
    captured = list(messages) if isinstance(messages, (list, tuple)) else []
    system = kwargs.get("system")
    if system:
        captured.insert(0, {"role": "system", "content": system})
    return {
        "model": kwargs.get("model"),
        "input_messages": captured or None,
        "_stream": bool(kwargs.get("stream")),
    }


def _anthropic_response_fields(response: Any) -> dict[str, Any]:
    usage = _get(response, "usage")
    text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in _get(response, "content") or []:
        block_type = _get(block, "type")
        if block_type == "text":
            text.append(_get(block, "text", "") or "")
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": _get(block, "id", "") or "",
                    "type": "tool_use",
                    "function": {
                        "name": _get(block, "name", "") or "",
                        "arguments": json.dumps(_get(block, "input", {}), default=str),
                    },
                }
            )
    fields: dict[str, Any] = {
        "prompt_tokens": _get(usage, "input_tokens", 0) or 0,
        "completion_tokens": _get(usage, "output_tokens", 0) or 0,
        "completion_string": "".join(text),
        "tool_calls": tool_calls,
    }
    model = _get(response, "model")
    if isinstance(model, str) and model:
        fields["model"] = model
    return fields


class _AnthropicStreamAccumulator:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model: str | None = None

    def add(self, event: Any) -> None:
        event_type = _get(event, "type")
        if event_type == "message_start":
            message = _get(event, "message")
            model = _get(message, "model")
            if isinstance(model, str) and model:
                self.model = model
            usage = _get(message, "usage")
            self.prompt_tokens = _get(usage, "input_tokens", 0) or 0
            self.completion_tokens = _get(usage, "output_tokens", 0) or 0
        elif event_type == "content_block_start":
            block = _get(event, "content_block")
            if _get(block, "type") == "tool_use":
                self.tool_calls[_get(event, "index", 0) or 0] = {
                    "id": _get(block, "id", "") or "",
                    "type": "tool_use",
                    "function": {
                        "name": _get(block, "name", "") or "",
                        "arguments": "",
                    },
                }
        elif event_type == "content_block_delta":
            delta = _get(event, "delta")
            delta_type = _get(delta, "type")
            if delta_type == "text_delta":
                self.text.append(_get(delta, "text", "") or "")
            elif delta_type == "input_json_delta":
                entry = self.tool_calls.get(_get(event, "index", 0) or 0)
                if entry is not None:
                    entry["function"]["arguments"] += (
                        _get(delta, "partial_json", "") or ""
                    )
        elif event_type == "message_delta":
            usage = _get(event, "usage")
            output_tokens = _get(usage, "output_tokens")
            if isinstance(output_tokens, int):
                self.completion_tokens = output_tokens

    def fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "completion_string": "".join(self.text),
            "tool_calls": [self.tool_calls[index] for index in sorted(self.tool_calls)],
        }
        if self.model:
            fields["model"] = self.model
        return fields
