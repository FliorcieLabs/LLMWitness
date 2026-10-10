"""Local development telemetry ingestion and tamper-evident receipts."""

import datetime
import hmac
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from llmwitness.config import get_secret_key, use_local_secret
from llmwitness.keys import load_or_create_identity
from llmwitness.session_store import SessionStore
from llmwitness.spool import is_disabled
from llmwitness.utils import (
    CHAINED_RECEIPT_VERSION,
    Ed25519KeyManager,
    canonical_json,
    compute_hmac_signature,
    normalize_uuidv7,
    receipt_hash,
    redact_payload,
)

logger = logging.getLogger("llmwitness.ingest")

RECEIPT_DIR = Path(os.getenv("LLMWITNESS_RECEIPT_DIR", ".llmwitness/receipts"))
MAX_SESSIONS = int(os.getenv("LLMWITNESS_MAX_SESSIONS", "256"))
MAX_EVENTS_PER_STREAM = int(os.getenv("LLMWITNESS_MAX_EVENTS_PER_STREAM", "1000"))
MAX_EVENT_BYTES = int(os.getenv("LLMWITNESS_MAX_EVENT_BYTES", "262144"))
INGEST_TOKEN = os.getenv("LLMWITNESS_INGEST_TOKEN")
# An unsealed session with no new events for this long may be dropped, but only
# when the session limit is reached and no sealed session can be evicted.
SESSION_IDLE_TTL_SECONDS = float(
    os.getenv("LLMWITNESS_SESSION_IDLE_TTL_SECONDS", str(24 * 60 * 60))
)

key_manager = Ed25519KeyManager()
session_store: SessionStore | None = None

audit_vault: dict[str, dict[str, Any]] = {}
sealed_proofs: dict[str, dict[str, Any]] = {}
# Sealed sessions dropped from memory to make room; their receipts stay on disk.
_evicted_sealed_ids: deque[str] = deque(maxlen=4096)
# Last receipt in each receipt directory's hash chain: (index, receipt hash).
_chain_heads: dict[str, tuple[int, str] | None] = {}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def enable_durable_state(
    session_db: str | Path | None = None,
    key_dir: str | Path | None = None,
) -> None:
    """Persist the signing identity and unsealed sessions across restarts.

    Called when the service starts. An Ed25519 key or HMAC secret supplied
    through the environment still takes precedence over the persisted files.
    """
    global key_manager, session_store
    if not _env_flag("LLMWITNESS_EPHEMERAL_KEYS"):
        identity = load_or_create_identity(key_dir)
        if not os.getenv("LLMWITNESS_PRIVATE_KEY_PEM"):
            key_manager = identity.key_manager
        use_local_secret(identity.hmac_secret)
        if identity.created:
            logger.info(
                "Created local signing key in %s (fingerprint %s)",
                identity.directory,
                identity.fingerprint,
            )

    configured = (
        str(session_db)
        if session_db is not None
        else os.getenv("LLMWITNESS_SESSION_DB", ".llmwitness/sessions.db")
    )
    if is_disabled(configured):
        return
    session_store = SessionStore(configured)
    restored = 0
    stale_before = time.time() - SESSION_IDLE_TTL_SECONDS
    for correlation_id, session in session_store.load_sessions().items():
        if (RECEIPT_DIR / f"{correlation_id}.json").exists():
            # Sealed just before the previous shutdown; the receipt is the record.
            session_store.delete_session(correlation_id)
            continue
        if session["updated_at"] < stale_before:
            # Abandoned runs must not fill the session limit across restarts.
            session_store.delete_session(correlation_id)
            continue
        audit_vault.setdefault(correlation_id, session)
        restored += 1
    if restored:
        logger.info("Restored %d unsealed session(s) from %s", restored, configured)


def disable_durable_state() -> None:
    global session_store
    if session_store is not None:
        session_store.close()
        session_store = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    enable_durable_state()
    try:
        yield
    finally:
        disable_durable_state()


app = FastAPI(
    title="LLMWitness Local Ingestion Service",
    description="Localhost telemetry collector with tamper-evident receipts",
    version="0.1.0",
    lifespan=_lifespan,
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


class _BodyLimitMiddleware:
    """Reject oversized bodies before Pydantic parses nested telemetry.

    The declared ``Content-Length`` is checked first, then the bytes actually
    received are counted, because a chunked request declares no length at all.
    """

    def __init__(self, wrapped_app):
        self.app = wrapped_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared:
            try:
                oversized = int(declared) > MAX_EVENT_BYTES
            except ValueError:
                response = JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length"}
                )
                await response(scope, receive, send)
                return
            if oversized:
                await self._too_large(scope, receive, send)
                return
        if scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        received = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                return  # client went away before sending a complete body
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > MAX_EVENT_BYTES:
                await self._too_large(scope, receive, send)
                return
            chunks.append(chunk)
            more_body = message.get("more_body", False)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _too_large(scope, receive, send) -> None:
        response = JSONResponse(
            status_code=413, content={"detail": "Request body too large"}
        )
        await response(scope, receive, send)


app.add_middleware(_BodyLimitMiddleware)


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
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=256)
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    input_messages: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    estimated_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    error: str | None = Field(default=None, max_length=2000)


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
        return normalize_uuidv7(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="correlation_id must be a UUIDv7"
        ) from exc


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
        # The persisted receipt remains the seal boundary after process restart
        # and after the bounded eviction cache forgets a session.
        if (RECEIPT_DIR / f"{correlation_id}.json").exists():
            raise HTTPException(
                status_code=409, detail="Local receipt has already been created"
            )
        if len(audit_vault) >= MAX_SESSIONS:
            _evict_sealed_session() or _expire_idle_session()
        if len(audit_vault) >= MAX_SESSIONS:
            raise HTTPException(status_code=429, detail="Local session limit reached")
        created_at = time.time()
        session = {
            "correlation_id": correlation_id,
            "created_at": created_at,
            "updated_at": created_at,
            "sdk_events": [],
            "gateway_events": [],
            "extension_events": [],
            "is_sealed": False,
        }
        if session_store is not None:
            try:
                session_store.create_session(correlation_id, created_at)
            except (sqlite3.Error, OSError) as exc:
                raise HTTPException(
                    status_code=507, detail="Session could not be persisted"
                ) from exc
        audit_vault[correlation_id] = session
    return audit_vault[correlation_id]


def _evict_sealed_session() -> bool:
    """Drop the oldest sealed session from memory; its receipt file remains.

    A sealed session is always preferred, because dropping an unsealed one
    discards a run that has no receipt yet.
    """
    sealed = [session for session in audit_vault.values() if session["is_sealed"]]
    if not sealed:
        return False
    oldest = min(sealed, key=lambda session: session["created_at"])
    correlation_id = oldest["correlation_id"]
    audit_vault.pop(correlation_id, None)
    sealed_proofs.pop(correlation_id, None)
    _evicted_sealed_ids.append(correlation_id)
    return True


def _expire_idle_session() -> bool:
    """Drop the longest-idle unsealed session once it has passed the idle limit."""
    if not audit_vault:
        return False
    idlest = min(
        audit_vault.values(),
        key=lambda session: session.get("updated_at", session["created_at"]),
    )
    idle_since = idlest.get("updated_at", idlest["created_at"])
    if time.time() - idle_since < SESSION_IDLE_TTL_SECONDS:
        return False
    correlation_id = idlest["correlation_id"]
    logger.warning(
        "Dropping unsealed session %s after %.0f seconds without events",
        correlation_id,
        time.time() - idle_since,
    )
    if session_store is not None:
        try:
            session_store.delete_session(correlation_id)
        except (sqlite3.Error, OSError) as exc:
            raise HTTPException(
                status_code=507, detail="Idle session could not be removed"
            ) from exc
    audit_vault.pop(correlation_id, None)
    return True


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
    redacted = redact_payload(event)
    updated_at = time.time()
    if session_store is not None:
        try:
            session_store.append_event(
                session["correlation_id"], stream, redacted, updated_at
            )
        except (sqlite3.Error, OSError) as exc:
            raise HTTPException(
                status_code=507, detail="Event could not be persisted"
            ) from exc
    session["updated_at"] = updated_at
    events.append(redacted)
    return len(events)


def _chain_head(receipt_dir: Path) -> tuple[int, str] | None:
    """Find the newest chained receipt in a directory, scanning it once per process."""
    key = str(receipt_dir)
    if key not in _chain_heads:
        head: tuple[int, str] | None = None
        try:
            candidates = sorted(receipt_dir.glob("*.json"))
        except OSError:
            candidates = []
        for candidate in candidates:
            try:
                receipt = json.loads(candidate.read_text(encoding="utf-8"))
                index = receipt["chain"]["index"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if (
                receipt.get("receipt_version") == CHAINED_RECEIPT_VERSION
                and isinstance(index, int)
                and (head is None or index > head[0])
            ):
                head = (index, receipt_hash(receipt))
        _chain_heads[key] = head
    return _chain_heads[key]


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
    head = _chain_head(RECEIPT_DIR)
    chain_index = 0 if head is None else head[0] + 1
    signed_payload: dict[str, Any] = {
        "correlation_id": correlation_id,
        "sealed_at": sealed_at,
        "events": {
            "sdk": session["sdk_events"],
            "gateway": session["gateway_events"],
            "extension": session["extension_events"],
        },
        "receipt_version": CHAINED_RECEIPT_VERSION,
        # Linking each receipt to the one before it makes a removed or
        # re-signed receipt detectable by `llmwitness verify-chain`.
        "chain": {
            "index": chain_index,
            "previous_receipt_hash": None if head is None else head[1],
        },
    }
    serialized = canonical_json(signed_payload)
    receipt = {
        **signed_payload,
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

    _chain_heads[str(RECEIPT_DIR)] = (
        chain_index,
        receipt_hash(receipt),
    )
    session["is_sealed"] = True
    session["sealed_at"] = sealed_at
    sealed_proofs[correlation_id] = receipt
    if session_store is not None:
        try:
            session_store.delete_session(correlation_id)
        except (sqlite3.Error, OSError):
            # The receipt is already committed. Startup reconciles this orphan
            # against the receipt file, so cleanup must not turn success into 500.
            logger.warning(
                "Receipt committed; session cleanup deferred for %s", correlation_id
            )
    return {
        "status": "receipt_created",
        "correlation_id": correlation_id,
        "hmac_signature": receipt["hmac_signature"],
        "ed25519_signature": receipt["ed25519_signature"],
        "public_key_fingerprint": receipt["public_key_fingerprint"],
        "chain_index": chain_index,
        "receipt_file": str(receipt_path),
    }


@app.get("/ingest/sessions")
async def list_sessions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Summarise the sessions currently held in memory."""
    _authorize(request, authorization)
    return {
        "sessions": [
            {
                "correlation_id": session["correlation_id"],
                "created_at": session["created_at"],
                "is_sealed": session["is_sealed"],
                "sdk_events": len(session["sdk_events"]),
                "gateway_events": len(session["gateway_events"]),
                "extension_events": len(session["extension_events"]),
            }
            for session in audit_vault.values()
        ],
        "limit": MAX_SESSIONS,
        "durable": session_store is not None,
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
        "sessions": len(audit_vault),
        "durable_sessions": session_store is not None,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
