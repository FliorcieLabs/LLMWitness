"""Local development telemetry ingestion and tamper-evident receipts."""

import datetime
import hmac
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from llmwitness.config import get_secret_key
from llmwitness.utils import (
    Ed25519KeyManager,
    canonical_json,
    compute_hmac_signature,
    redact_payload,
)

RECEIPT_DIR = Path(os.getenv("LLMWITNESS_RECEIPT_DIR", ".llmwitness/receipts"))
MAX_SESSIONS = int(os.getenv("LLMWITNESS_MAX_SESSIONS", "256"))
MAX_EVENTS_PER_STREAM = int(os.getenv("LLMWITNESS_MAX_EVENTS_PER_STREAM", "1000"))
MAX_EVENT_BYTES = int(os.getenv("LLMWITNESS_MAX_EVENT_BYTES", "262144"))
INGEST_TOKEN = os.getenv("LLMWITNESS_INGEST_TOKEN")

key_manager = Ed25519KeyManager()

app = FastAPI(
    title="LLMWitness Local Ingestion Service",
    description="Localhost telemetry collector with tamper-evident receipts",
    version="0.1.0",
)


@app.exception_handler(RequestValidationError)
async def handle_invalid_payload(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Reject invalid telemetry without echoing input FastAPI cannot serialize.

    The default handler returns the offending value, so a bare JSON ``NaN``
    literal raises inside the error response and the request fails as a 500.
    """
    detail = [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.middleware("http")
async def reject_oversized_requests(request: Request, call_next):
    """Reject declared oversized bodies before Pydantic parses nested telemetry."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_EVENT_BYTES:
                return JSONResponse(
                    status_code=413, content={"detail": "Request body too large"}
                )
        except ValueError:
            return JSONResponse(
                status_code=400, content={"detail": "Invalid Content-Length"}
            )
    return await call_next(request)


audit_vault: dict[str, dict[str, Any]] = {}
sealed_proofs: dict[str, dict[str, Any]] = {}


class _ReceiptAlreadyExistsError(Exception):
    """Raised when atomic publication finds an existing receipt path."""


def _bounded_dict() -> dict[str, Any]:
    return {}


class SDKTelemetryPayload(BaseModel):
    correlation_id: str = Field(min_length=36, max_length=36)
    task_name: str = Field(min_length=1, max_length=256)
    timestamp: float = Field(allow_inf_nan=False)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    completion_string: str | None = Field(default=None, max_length=100_000)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    agent_state: dict[str, Any] = Field(default_factory=_bounded_dict)


class GatewayTelemetryPayload(BaseModel):
    correlation_id: str = Field(min_length=36, max_length=36)
    timestamp: float = Field(allow_inf_nan=False)
    upstream_url: str = Field(max_length=2048)
    status_code: int = Field(ge=100, le=599)
    request_hmac: str | None = Field(default=None, max_length=256)
    response_hmac: str | None = Field(default=None, max_length=256)
    redacted_request: dict[str, Any] | None = None
    redacted_response: Any = None
    optimization_meta: dict[str, Any] | None = None


class ExtensionTelemetryPayload(BaseModel):
    correlation_id: str = Field(min_length=36, max_length=36)
    timestamp: float = Field(allow_inf_nan=False)
    url: str = Field(max_length=2048)
    event_type: str = Field(min_length=1, max_length=128)
    element_id: str | None = Field(default=None, max_length=512)
    dom_delta: dict[str, Any]


def _validate_uuidv7(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=400, detail="correlation_id must be a UUIDv7"
        ) from exc
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise HTTPException(status_code=400, detail="correlation_id must be a UUIDv7")
    return str(parsed)


def _authorize(request: Request, authorization: str | None) -> None:
    """Require a shared token when configured; otherwise permit loopback development only."""
    if INGEST_TOKEN:
        expected = f"Bearer {INGEST_TOKEN}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ingest token"
            )
        return
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Set LLMWITNESS_INGEST_TOKEN before accepting non-loopback traffic",
        )


def get_or_create_session(correlation_id: str) -> dict[str, Any]:
    correlation_id = _validate_uuidv7(correlation_id)
    if correlation_id not in audit_vault:
        if len(audit_vault) >= MAX_SESSIONS:
            raise HTTPException(status_code=429, detail="Local session limit reached")
        audit_vault[correlation_id] = {
            "correlation_id": correlation_id,
            "created_at": time.time(),
            "sdk_events": [],
            "gateway_events": [],
            "extension_events": [],
            "is_sealed": False,
        }
    return audit_vault[correlation_id]


def _append_event(session: dict[str, Any], stream: str, event: dict[str, Any]) -> int:
    if session["is_sealed"]:
        raise HTTPException(
            status_code=409, detail="Local receipt has already been created"
        )
    events = session[stream]
    if len(events) >= MAX_EVENTS_PER_STREAM:
        raise HTTPException(status_code=429, detail="Local event limit reached")
    if len(canonical_json(event).encode("utf-8")) > MAX_EVENT_BYTES:
        raise HTTPException(status_code=413, detail="Telemetry event too large")
    events.append(redact_payload(event))
    return len(events)


def _fsync_directory(directory: Path) -> None:
    """Best-effort sync of directory metadata on platforms that support it."""
    if os.name == "nt":
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _persist_receipt(receipt_path: Path, receipt: dict[str, Any]) -> None:
    """Durably write and atomically publish a receipt without replacing one."""
    temporary_path: Path | None = None
    temporary_fd: int | None = None
    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            dir=receipt_path.parent,
            prefix=f".{receipt_path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        handle = os.fdopen(temporary_fd, "w", encoding="utf-8")
        temporary_fd = None
        with handle:
            json.dump(receipt, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())

        # A hard-link publication is atomic and fails rather than replacing an
        # existing receipt. The temporary file is on the same filesystem.
        try:
            os.link(temporary_path, receipt_path)
        except FileExistsError as exc:
            raise _ReceiptAlreadyExistsError from exc
        _fsync_directory(receipt_path.parent)
    finally:
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # Preserve the persistence error/status if cleanup itself fails.
                pass
            _fsync_directory(receipt_path.parent)


@app.post("/ingest/sdk", status_code=201)
async def ingest_sdk_telemetry(
    payload: SDKTelemetryPayload,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _authorize(request, authorization)
    session = get_or_create_session(payload.correlation_id)
    count = _append_event(session, "sdk_events", payload.model_dump())
    return {
        "status": "accepted",
        "correlation_id": payload.correlation_id,
        "sdk_event_count": count,
    }


@app.post("/ingest/gateway", status_code=201)
async def ingest_gateway_telemetry(
    payload: GatewayTelemetryPayload,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _authorize(request, authorization)
    session = get_or_create_session(payload.correlation_id)
    count = _append_event(session, "gateway_events", payload.model_dump())
    return {
        "status": "accepted",
        "correlation_id": payload.correlation_id,
        "gateway_event_count": count,
    }


@app.post("/ingest/extension", status_code=201)
async def ingest_extension_telemetry(
    payload: ExtensionTelemetryPayload,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _authorize(request, authorization)
    session = get_or_create_session(payload.correlation_id)
    count = _append_event(session, "extension_events", payload.model_dump())
    return {
        "status": "accepted",
        "correlation_id": payload.correlation_id,
        "extension_event_count": count,
    }


@app.post("/ingest/seal")
async def seal_session_audit(
    request_data: dict[str, str],
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Create one local tamper-evident receipt for an in-memory session."""
    _authorize(request, authorization)
    correlation_id = _validate_uuidv7(request_data.get("correlation_id", ""))
    session = audit_vault.get(correlation_id)
    if not session:
        raise HTTPException(status_code=404, detail="Correlation session not found")
    if session["is_sealed"]:
        raise HTTPException(
            status_code=409, detail="Local receipt has already been created"
        )

    sealed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    signed_payload = {
        "correlation_id": correlation_id,
        "sealed_at": sealed_at,
        "events": {
            "sdk": session["sdk_events"],
            "gateway": session["gateway_events"],
            "extension": session["extension_events"],
        },
    }
    serialized = canonical_json(signed_payload)
    receipt = {
        **signed_payload,
        "receipt_version": 1,
        "signature_algorithm": "Ed25519",
        "ed25519_signature": key_manager.sign(serialized),
        "public_key_pem": key_manager.export_public_key_pem(),
        "public_key_fingerprint": key_manager.get_public_key_fingerprint(),
        "hmac_signature": compute_hmac_signature(serialized, get_secret_key()),
        "limitations": "Local file receipt; tamper-evident, not immutable or WORM storage.",
    }

    receipt_path = RECEIPT_DIR / f"{correlation_id}.json"
    try:
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=507, detail="Receipt directory could not be created"
        ) from exc
    try:
        _persist_receipt(receipt_path, receipt)
    except _ReceiptAlreadyExistsError as exc:
        raise HTTPException(
            status_code=409, detail="Receipt file already exists"
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=507, detail="Receipt could not be persisted"
        ) from exc

    session["is_sealed"] = True
    session["sealed_at"] = sealed_at
    sealed_proofs[correlation_id] = receipt
    return {
        "status": "receipt_created",
        "correlation_id": correlation_id,
        "hmac_signature": receipt["hmac_signature"],
        "ed25519_signature": receipt["ed25519_signature"],
        "public_key_fingerprint": receipt["public_key_fingerprint"],
        "receipt_file": str(receipt_path),
    }


@app.get("/ingest/session/{correlation_id}")
async def get_session_audit(
    correlation_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _authorize(request, authorization)
    correlation_id = _validate_uuidv7(correlation_id)
    if correlation_id not in audit_vault:
        raise HTTPException(status_code=404, detail="Correlation session not found")
    return audit_vault[correlation_id]


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "LLMWitness local ingestion",
        "timestamp": time.time(),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
