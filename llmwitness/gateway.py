"""Localhost JSON and SSE proxy with best-effort audit scrubbing."""

import asyncio
import hmac
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from llmwitness.spool import (
    EventSpool,
    classify_delivery,
    spool_directory_from_env,
)
from llmwitness.utils import generate_uuidv7, normalize_uuidv7, redact_payload

logger = logging.getLogger("llmwitness.gateway")

UPSTREAM_OPENAI_URL = os.getenv("UPSTREAM_OPENAI_URL", "https://api.openai.com")
UPSTREAM_ANTHROPIC_URL = os.getenv(
    "UPSTREAM_ANTHROPIC_URL", "https://api.anthropic.com"
)
INGESTION_SERVER_URL = os.getenv("INGESTION_SERVER_URL", "http://localhost:8000")
INGEST_TOKEN = os.getenv("LLMWITNESS_INGEST_TOKEN")
GATEWAY_TOKEN = os.getenv("LLMWITNESS_GATEWAY_TOKEN")
MAX_REQUEST_BYTES = int(os.getenv("LLMWITNESS_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))
MAX_AUDIT_TEXT_BYTES = int(os.getenv("LLMWITNESS_MAX_AUDIT_TEXT_BYTES", "100000"))

app = FastAPI(
    title="LLMWitness Local Development Gateway",
    description="OpenAI and Anthropic JSON/SSE proxy with best-effort audit scrubbing",
    version="0.1.0",
)
http_client = httpx.AsyncClient(timeout=30.0)


def resolve_correlation_id(value: str | None) -> str:
    if value is None:
        return generate_uuidv7()
    try:
        return normalize_uuidv7(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-LLMWitness-Correlation-ID must be a UUIDv7"
        ) from exc


def _authorize_gateway(request: Request, token: str | None) -> None:
    if GATEWAY_TOKEN:
        if token is None or not hmac.compare_digest(token, GATEWAY_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid gateway token")
        return
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(
            status_code=403,
            detail="Set LLMWITNESS_GATEWAY_TOKEN for non-loopback access",
        )


_SPOOL_STREAM = "gateway"
_MAX_SSE_LINE_BYTES = 1024 * 1024
telemetry_stats = {"delivered": 0, "failed": 0, "spooled": 0, "replayed": 0}
_consecutive_failures = 0
_background_tasks: set[asyncio.Task] = set()


def _spool() -> EventSpool | None:
    directory = spool_directory_from_env()
    return EventSpool(directory) if directory is not None else None


async def _post_telemetry(payload: dict[str, Any]) -> str:
    """Submit one event; returns ``delivered``, ``retry`` or ``discard``."""
    headers = {"Authorization": f"Bearer {INGEST_TOKEN}"} if INGEST_TOKEN else {}
    try:
        response = await http_client.post(
            f"{INGESTION_SERVER_URL}/ingest/gateway", json=payload, headers=headers
        )
    except (httpx.HTTPError, OSError):
        return "retry"
    return classify_delivery(response.status_code)


async def _replay_spooled_telemetry(spool: EventSpool) -> None:
    claim = spool.claim(_SPOOL_STREAM)
    if claim is None:
        return
    remaining: list[dict[str, Any]] = []
    for index, payload in enumerate(claim.payloads):
        outcome = await _post_telemetry(payload)
        if outcome == "retry":
            remaining = claim.payloads[index:]
            break
        if outcome == "delivered":
            telemetry_stats["replayed"] += 1
    spool.release(_SPOOL_STREAM, claim, remaining)


async def send_gateway_telemetry(payload: dict[str, Any]) -> None:
    """Submit best-effort audit telemetry; proxy success does not imply delivery.

    A failed submission is retried once, then spooled to disk and re-sent after
    the next successful one. Failures are counted and logged, never raised.
    """
    global _consecutive_failures
    outcome = await _post_telemetry(payload)
    if outcome == "retry" and _consecutive_failures == 0:
        await asyncio.sleep(0.2)
        outcome = await _post_telemetry(payload)

    spool = _spool()
    if outcome == "delivered":
        telemetry_stats["delivered"] += 1
        _consecutive_failures = 0
        if spool is not None and spool.has_pending(_SPOOL_STREAM):
            await _replay_spooled_telemetry(spool)
        return

    telemetry_stats["failed"] += 1
    if outcome == "discard":
        logger.warning("Ingestion rejected gateway telemetry for a proxied request")
        return
    _consecutive_failures += 1
    spooled = spool is not None and spool.append(_SPOOL_STREAM, payload)
    if spooled:
        telemetry_stats["spooled"] += 1
    log = logger.warning if _consecutive_failures == 1 else logger.debug
    log(
        "Gateway telemetry could not be delivered; event %s",
        "spooled for retry" if spooled else "lost",
    )


def _send_in_background(payload: dict[str, Any]) -> None:
    """Deliver telemetry from a streaming response without tying it to the client."""
    task = asyncio.ensure_future(send_gateway_telemetry(payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _preview(content: bytes) -> dict[str, Any]:
    preview_limit = max(0, MAX_AUDIT_TEXT_BYTES)
    preview_bytes = content[:preview_limit]
    return {
        "body_preview": redact_payload(preview_bytes.decode("utf-8", errors="replace")),
        "truncated": len(content) > len(preview_bytes),
        "body_bytes": len(content),
        "preview_bytes": len(preview_bytes),
    }


def _audit_copy(response: httpx.Response) -> Any:
    content = response.content
    truncated = len(content) > max(0, MAX_AUDIT_TEXT_BYTES)
    content_type = response.headers.get("content-type", "")
    if "json" in content_type and not truncated:
        try:
            return redact_payload(response.json())
        except ValueError:
            pass
    return _preview(content)


@dataclass(frozen=True)
class _Provider:
    """What differs between the upstream APIs the gateway can proxy."""

    name: str
    path: str
    request_headers: frozenset[str]
    response_headers: frozenset[str]
    response_header_prefixes: tuple[str, ...]


_OPENAI = _Provider(
    name="openai",
    path="/v1/chat/completions",
    request_headers=frozenset(
        {
            "authorization",
            "content-type",
            "accept",
            "user-agent",
            "openai-organization",
            "openai-project",
            "idempotency-key",
        }
    ),
    response_headers=frozenset(
        {"content-type", "retry-after", "x-request-id", "openai-processing-ms"}
    ),
    response_header_prefixes=("x-ratelimit-",),
)
_ANTHROPIC = _Provider(
    name="anthropic",
    path="/v1/messages",
    request_headers=frozenset(
        {
            "authorization",
            "x-api-key",
            "anthropic-version",
            "anthropic-beta",
            "content-type",
            "accept",
            "user-agent",
            "idempotency-key",
        }
    ),
    response_headers=frozenset(
        {"content-type", "retry-after", "request-id", "x-request-id"}
    ),
    response_header_prefixes=("anthropic-ratelimit-", "x-ratelimit-"),
)


def _upstream_base(provider: _Provider) -> str:
    base = UPSTREAM_ANTHROPIC_URL if provider is _ANTHROPIC else UPSTREAM_OPENAI_URL
    return base.rstrip("/")


class _StreamAudit:
    """Builds the audit copy of a server-sent-event response as it passes through.

    Only the assembled text is kept, and it is scrubbed as a whole. Raw chunks
    are not stored, because a secret split across two chunks would slip past
    pattern scrubbing.
    """

    def __init__(self, provider: _Provider, is_event_stream: bool):
        self.provider = provider
        self.is_event_stream = is_event_stream
        self.body_bytes = 0
        self.events = 0
        self.text: list[str] = []
        self.text_bytes = 0
        self.truncated = False
        self.usage: dict[str, Any] = {}
        self.model: str | None = None
        self.finish_reason: str | None = None
        self.tool_names: list[str] = []
        self._pending = b""
        self._raw = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.body_bytes += len(chunk)
        if not self.is_event_stream:
            # An error body or plain JSON: keep a bounded preview instead.
            room = max(0, MAX_AUDIT_TEXT_BYTES) + 1 - len(self._raw)
            if room > 0:
                self._raw.extend(chunk[:room])
            return
        self._pending += chunk
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            self._line(line.rstrip(b"\r"))
        if len(self._pending) > _MAX_SSE_LINE_BYTES:
            self._pending = b""
            self.truncated = True

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        self.events += 1
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        try:
            if self.provider is _ANTHROPIC:
                self._anthropic_event(event)
            else:
                self._openai_event(event)
        except (AttributeError, IndexError, KeyError, TypeError):
            return  # an unexpected event shape is skipped, never fatal

    def _add_text(self, text: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        room = max(0, MAX_AUDIT_TEXT_BYTES) - self.text_bytes
        if room <= 0:
            self.truncated = True
            return
        piece = text[:room]
        if len(piece) < len(text):
            self.truncated = True
        self.text.append(piece)
        self.text_bytes += len(piece)

    def _openai_event(self, event: dict[str, Any]) -> None:
        if isinstance(event.get("model"), str):
            self.model = event["model"]
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            self._add_text(delta.get("content"))
            for tool_call in delta.get("tool_calls") or []:
                name = (tool_call.get("function") or {}).get("name")
                if isinstance(name, str) and name:
                    self.tool_names.append(name)
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])

    def _anthropic_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            if isinstance(message.get("model"), str):
                self.model = message["model"]
            if isinstance(message.get("usage"), dict):
                self.usage.update(message["usage"])
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                self.tool_names.append(block["name"])
        elif kind == "content_block_delta":
            self._add_text((event.get("delta") or {}).get("text"))
        elif kind == "message_delta":
            if isinstance(event.get("usage"), dict):
                self.usage.update(event["usage"])
            reason = (event.get("delta") or {}).get("stop_reason")
            if reason:
                self.finish_reason = str(reason)

    def summary(self, completed: bool) -> dict[str, Any]:
        if not self.is_event_stream:
            preview = _preview(bytes(self._raw))
            preview["body_bytes"] = self.body_bytes
            preview["truncated"] = self.body_bytes > preview["preview_bytes"]
            return preview
        return {
            "stream": True,
            "completed": completed,
            "events": self.events,
            "body_bytes": self.body_bytes,
            "text": redact_payload("".join(self.text)),
            "truncated": self.truncated,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "tool_names": redact_payload(self.tool_names[:100]),
            "usage": redact_payload(self.usage),
        }


def _mock_response(
    provider: _Provider, body: dict[str, Any], correlation_id: str
) -> tuple[bytes, str]:
    """A canned upstream reply for local runs without network access."""
    model = body.get("model", "mock-model")
    if body.get("stream") is True:
        if provider is _ANTHROPIC:
            events: list[dict[str, Any]] = [
                {
                    "type": "message_start",
                    "message": {"model": model, "usage": {"input_tokens": 1}},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Mock response."},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
                {"type": "message_stop"},
            ]
            lines = [
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ]
        else:
            chunk = {
                "id": f"chatcmpl-{correlation_id[:8]}",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Mock response."},
                        "finish_reason": "stop",
                    }
                ],
            }
            lines = [f"data: {json.dumps(chunk)}\n\n", "data: [DONE]\n\n"]
        return "".join(lines).encode(), "text/event-stream"
    if provider is _ANTHROPIC:
        payload: dict[str, Any] = {
            "id": f"msg_{correlation_id[:8]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "Mock response."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    else:
        payload = {
            "id": f"chatcmpl-{correlation_id[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Mock response."},
                    "finish_reason": "stop",
                }
            ],
        }
    return json.dumps(payload).encode(), "application/json"


async def _relay_stream(
    upstream: httpx.Response,
    audit: "_StreamAudit",
    build_telemetry: Callable[[Any], dict[str, Any]],
) -> AsyncIterator[bytes]:
    """Yield the upstream stream unchanged and report its audit copy at the end."""
    completed = False
    try:
        async for chunk in upstream.aiter_bytes():
            audit.feed(chunk)
            yield chunk
        completed = True
    except httpx.HTTPError:
        # The stream broke after the response started; the client sees a
        # short body and the audit copy records it as incomplete.
        pass
    finally:
        await upstream.aclose()
        _send_in_background(build_telemetry(audit.summary(completed)))


async def _read_json_object(request: Request) -> tuple[bytes, dict[str, Any]]:
    """Read a bounded request body and require it to be a JSON object."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length"
            ) from exc
    raw_body = await request.body()
    if len(raw_body) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        body = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON payload must be an object")
    return raw_body, body


async def _proxy(
    provider: _Provider,
    request: Request,
    background_tasks: BackgroundTasks,
    correlation_header: str | None,
    gateway_token: str | None,
) -> Response:
    """Forward one bounded request and return the upstream body unchanged."""
    _authorize_gateway(request, gateway_token)
    correlation_id = resolve_correlation_id(correlation_header)
    raw_body, body = await _read_json_object(request)

    start_time = time.time()
    target_url = f"{_upstream_base(provider)}{provider.path}"
    wants_stream = body.get("stream") is True

    def telemetry(
        status_code: int, upstream_label: str, audit_response: Any
    ) -> dict[str, Any]:
        return {
            "correlation_id": correlation_id,
            "timestamp": start_time,
            "upstream_url": upstream_label,
            "status_code": status_code,
            "redacted_request": redact_payload(body),
            "redacted_response": audit_response,
            "optimization_meta": {
                "telemetry_delivery": "best_effort",
                "provider": provider.name,
                "stream": wants_stream,
                "duration_ms": round((time.time() - start_time) * 1000, 3),
            },
        }

    is_mock = os.getenv("LLMWITNESS_MOCK_UPSTREAM", "false").lower() in {
        "true",
        "1",
        "yes",
    }
    if is_mock:
        response_body, content_type = _mock_response(provider, body, correlation_id)
        if wants_stream:
            audit = _StreamAudit(provider, is_event_stream=True)
            audit.feed(response_body)
            audit_response: Any = audit.summary(completed=True)
        else:
            audit_response = redact_payload(json.loads(response_body))
        background_tasks.add_task(
            send_gateway_telemetry, telemetry(200, "MOCK_UPSTREAM", audit_response)
        )
        return Response(
            content=response_body,
            status_code=200,
            headers={
                "content-type": content_type,
                "X-LLMWitness-Correlation-ID": correlation_id,
            },
        )

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in provider.request_headers
    }
    headers["X-LLMWitness-Correlation-ID"] = correlation_id

    def failed_upstream(exc: Exception) -> JSONResponse:
        background_tasks.add_task(
            send_gateway_telemetry,
            telemetry(502, target_url, {"error_type": type(exc).__name__}),
        )
        return JSONResponse(
            {"error": "Upstream request failed"},
            status_code=status.HTTP_502_BAD_GATEWAY,
            headers={"X-LLMWitness-Correlation-ID": correlation_id},
        )

    def forwarded(upstream: httpx.Response) -> dict[str, str]:
        selected = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in provider.response_headers
            or key.lower().startswith(provider.response_header_prefixes)
        }
        selected["X-LLMWitness-Correlation-ID"] = correlation_id
        return selected

    if wants_stream:
        try:
            upstream = await http_client.send(
                http_client.build_request(
                    "POST", target_url, content=raw_body, headers=headers
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            return failed_upstream(exc)
        return StreamingResponse(
            _relay_stream(
                upstream,
                _StreamAudit(
                    provider,
                    is_event_stream="text/event-stream"
                    in upstream.headers.get("content-type", ""),
                ),
                lambda audit_response: telemetry(
                    upstream.status_code, target_url, audit_response
                ),
            ),
            status_code=upstream.status_code,
            headers=forwarded(upstream),
        )

    try:
        upstream = await http_client.post(target_url, content=raw_body, headers=headers)
    except httpx.HTTPError as exc:
        return failed_upstream(exc)
    background_tasks.add_task(
        send_gateway_telemetry,
        telemetry(upstream.status_code, target_url, _audit_copy(upstream)),
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=forwarded(upstream),
    )


@app.post("/v1/chat/completions")
async def chat_completions_proxy(
    request: Request,
    background_tasks: BackgroundTasks,
    x_llmwitness_correlation_id: str | None = Header(
        None, alias="X-LLMWitness-Correlation-ID"
    ),
    x_llmwitness_gateway_token: str | None = Header(
        None, alias="X-LLMWitness-Gateway-Token"
    ),
):
    """Proxy an OpenAI chat-completions request, JSON or ``stream: true``."""
    return await _proxy(
        _OPENAI,
        request,
        background_tasks,
        x_llmwitness_correlation_id,
        x_llmwitness_gateway_token,
    )


@app.post("/v1/messages")
async def anthropic_messages_proxy(
    request: Request,
    background_tasks: BackgroundTasks,
    x_llmwitness_correlation_id: str | None = Header(
        None, alias="X-LLMWitness-Correlation-ID"
    ),
    x_llmwitness_gateway_token: str | None = Header(
        None, alias="X-LLMWitness-Gateway-Token"
    ),
):
    """Proxy an Anthropic Messages request, JSON or ``stream: true``."""
    return await _proxy(
        _ANTHROPIC,
        request,
        background_tasks,
        x_llmwitness_correlation_id,
        x_llmwitness_gateway_token,
    )


@app.get("/health")
async def health_check():
    """Report liveness and how much audit telemetry was delivered or lost."""
    return {
        "status": "healthy",
        "service": "LLMWitness local gateway",
        "timestamp": time.time(),
        "telemetry": dict(telemetry_stats),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8011)
