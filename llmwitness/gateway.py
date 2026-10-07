"""Localhost JSON proxy with best-effort audit scrubbing."""

import hmac
import json
import os
import time
import uuid
from typing import Any

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from llmwitness.utils import generate_uuidv7, redact_payload

UPSTREAM_OPENAI_URL = os.getenv("UPSTREAM_OPENAI_URL", "https://api.openai.com")
INGESTION_SERVER_URL = os.getenv("INGESTION_SERVER_URL", "http://localhost:8000")
INGEST_TOKEN = os.getenv("LLMWITNESS_INGEST_TOKEN")
GATEWAY_TOKEN = os.getenv("LLMWITNESS_GATEWAY_TOKEN")
MAX_REQUEST_BYTES = int(os.getenv("LLMWITNESS_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))
MAX_AUDIT_TEXT_BYTES = int(os.getenv("LLMWITNESS_MAX_AUDIT_TEXT_BYTES", "100000"))

app = FastAPI(
    title="LLMWitness Local Development Gateway",
    description="OpenAI chat-completions JSON proxy with best-effort audit scrubbing",
    version="0.1.0",
)
http_client = httpx.AsyncClient(timeout=30.0)


def resolve_correlation_id(value: str | None) -> str:
    if value is None:
        return generate_uuidv7()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=400, detail="X-LLMWitness-Correlation-ID must be a UUIDv7"
        ) from exc
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise HTTPException(
            status_code=400, detail="X-LLMWitness-Correlation-ID must be a UUIDv7"
        )
    return str(parsed)


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


async def send_gateway_telemetry(payload: dict[str, Any]) -> None:
    """Submit best-effort audit telemetry; proxy success does not imply delivery."""
    headers = {"Authorization": f"Bearer {INGEST_TOKEN}"} if INGEST_TOKEN else {}
    try:
        response = await http_client.post(
            f"{INGESTION_SERVER_URL}/ingest/gateway", json=payload, headers=headers
        )
        response.raise_for_status()
    except (httpx.HTTPError, OSError):
        # Community delivery is explicitly best-effort with no completeness guarantee.
        return


def _audit_copy(response: httpx.Response) -> Any:
    content = response.content
    preview_limit = max(0, MAX_AUDIT_TEXT_BYTES)
    preview_bytes = content[:preview_limit]
    truncated = len(content) > len(preview_bytes)
    content_type = response.headers.get("content-type", "")
    if "json" in content_type and not truncated:
        try:
            return redact_payload(response.json())
        except ValueError:
            pass
    text = preview_bytes.decode("utf-8", errors="replace")
    return {
        "body_preview": redact_payload(text),
        "truncated": truncated,
        "body_bytes": len(content),
        "preview_bytes": len(preview_bytes),
    }


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
    """Forward one bounded JSON request and return the upstream body unchanged."""
    _authorize_gateway(request, x_llmwitness_gateway_token)
    correlation_id = resolve_correlation_id(x_llmwitness_correlation_id)
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

    start_time = time.time()
    target_url = f"{UPSTREAM_OPENAI_URL.rstrip('/')}/v1/chat/completions"
    is_mock = os.getenv("LLMWITNESS_MOCK_UPSTREAM", "false").lower() in {
        "true",
        "1",
        "yes",
    }
    if is_mock:
        response_body = json.dumps(
            {
                "id": f"chatcmpl-{correlation_id[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "mock-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Mock response."},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        upstream_status = 200
        upstream_headers = {"content-type": "application/json"}
        audit_response: Any = redact_payload(json.loads(response_body))
        upstream_label = "MOCK_UPSTREAM"
    else:
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            in {
                "authorization",
                "content-type",
                "accept",
                "user-agent",
                "openai-organization",
                "openai-project",
                "idempotency-key",
            }
        }
        headers["X-LLMWitness-Correlation-ID"] = correlation_id
        try:
            upstream = await http_client.post(
                target_url, content=raw_body, headers=headers
            )
        except httpx.HTTPError as exc:
            background_tasks.add_task(
                send_gateway_telemetry,
                {
                    "correlation_id": correlation_id,
                    "timestamp": start_time,
                    "upstream_url": target_url,
                    "status_code": 502,
                    "redacted_request": redact_payload(body),
                    "redacted_response": {"error_type": type(exc).__name__},
                    "optimization_meta": {"telemetry_delivery": "best_effort"},
                },
            )
            return JSONResponse(
                {"error": "Upstream request failed"},
                status_code=status.HTTP_502_BAD_GATEWAY,
                headers={"X-LLMWitness-Correlation-ID": correlation_id},
            )
        response_body = upstream.content
        upstream_status = upstream.status_code
        forwarded_response_headers = {
            "content-type",
            "retry-after",
            "x-request-id",
            "openai-processing-ms",
        }
        upstream_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in forwarded_response_headers
            or key.lower().startswith("x-ratelimit-")
        }
        audit_response = _audit_copy(upstream)
        upstream_label = target_url

    background_tasks.add_task(
        send_gateway_telemetry,
        {
            "correlation_id": correlation_id,
            "timestamp": start_time,
            "upstream_url": upstream_label,
            "status_code": upstream_status,
            "redacted_request": redact_payload(body),
            "redacted_response": audit_response,
            "optimization_meta": {"telemetry_delivery": "best_effort"},
        },
    )
    upstream_headers["X-LLMWitness-Correlation-ID"] = correlation_id
    return Response(
        content=response_body, status_code=upstream_status, headers=upstream_headers
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8011)
