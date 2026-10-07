"""Boundary behaviour of the local ingestion service."""

import json

import pytest
from fastapi.testclient import TestClient

from llmwitness import ingest
from llmwitness.utils import generate_uuidv7

JSON_HEADERS = {"content-type": "application/json"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An authenticated-by-loopback service with isolated in-memory state."""
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path)
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    return TestClient(ingest.app)


def _sdk_event(correlation_id: str | None = None, **overrides) -> dict:
    event = {
        "correlation_id": correlation_id or generate_uuidv7(),
        "task_name": "edge-case",
        "timestamp": 1.0,
    }
    event.update(overrides)
    return event


def test_health_check_is_unauthenticated_and_reports_the_service(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["service"] == "LLMWitness local ingestion"


def test_non_loopback_clients_are_refused_until_a_token_is_configured(monkeypatch):
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    remote = TestClient(ingest.app, client=("10.0.0.5", 5000))
    response = remote.post("/ingest/sdk", json=_sdk_event())
    assert response.status_code == 403
    assert "LLMWITNESS_INGEST_TOKEN" in response.json()["detail"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, 401),
        ({"Authorization": "Bearer wrong-token"}, 401),
        ({"Authorization": "local-ingest-token"}, 401),
        ({"Authorization": "Bearer local-ingest-token"}, 201),
    ],
)
def test_configured_token_is_required_and_compared_in_full(
    tmp_path, monkeypatch, header, expected
):
    monkeypatch.setattr(ingest, "INGEST_TOKEN", "local-ingest-token")
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path)
    ingest.audit_vault.clear()
    response = TestClient(ingest.app).post(
        "/ingest/sdk", json=_sdk_event(), headers=header
    )
    assert response.status_code == expected


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_timestamp_literals_are_rejected_without_a_server_error(
    client, literal
):
    """The default error handler cannot serialize these, so they must be filtered."""
    body = json.dumps(_sdk_event()).replace(
        '"timestamp": 1.0', f'"timestamp": {literal}'
    )
    response = client.post("/ingest/sdk", content=body, headers=JSON_HEADERS)

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "finite_number"


def test_correlation_ids_must_be_uuidv7(client):
    assert client.post("/ingest/sdk", json=_sdk_event("a" * 36)).status_code == 400
    assert (
        client.post(
            "/ingest/sdk", json=_sdk_event("2b1f9d3c-8b6a-4f2e-9c1d-6a5b4c3d2e1f")
        ).status_code
        == 400
    )
    assert client.post("/ingest/sdk", json=_sdk_event("too-short")).status_code == 422
    assert client.get("/ingest/session/not-a-uuid").status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_name": ""},
        {"task_name": "t" * 257},
        {"prompt_tokens": -1},
        {"completion_tokens": -1},
        {"completion_string": "x" * 100_001},
        {"tool_calls": [{"name": "call"}] * 101},
        {"timestamp": "not-a-number"},
    ],
)
def test_sdk_payload_bounds_are_enforced(client, overrides):
    assert client.post("/ingest/sdk", json=_sdk_event(**overrides)).status_code == 422


@pytest.mark.parametrize("status_code", [99, 600])
def test_gateway_payload_rejects_impossible_status_codes(client, status_code):
    payload = {
        "correlation_id": generate_uuidv7(),
        "timestamp": 1.0,
        "upstream_url": "https://upstream.test",
        "status_code": status_code,
    }
    assert client.post("/ingest/gateway", json=payload).status_code == 422


def test_extension_payload_requires_a_dom_delta(client):
    payload = {
        "correlation_id": generate_uuidv7(),
        "timestamp": 1.0,
        "url": "https://app.test",
        "event_type": "click",
    }
    assert client.post("/ingest/extension", json=payload).status_code == 422


def test_malformed_content_length_is_rejected_before_parsing(client):
    response = client.post(
        "/ingest/sdk",
        content=json.dumps(_sdk_event()),
        headers={**JSON_HEADERS, "content-length": "not-a-number"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Content-Length"


def test_oversized_bodies_are_rejected_by_the_middleware(client):
    """A declared oversize body is refused before Pydantic walks the payload."""
    response = client.post(
        "/ingest/sdk", json=_sdk_event(agent_state={"blob": "x" * 300_000})
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Request body too large"


def test_single_events_above_the_byte_ceiling_are_rejected(client, monkeypatch):
    """The stored event carries model defaults, so it can outgrow its own body."""
    event = _sdk_event()
    body = json.dumps(event)
    monkeypatch.setattr(ingest, "MAX_EVENT_BYTES", len(body.encode("utf-8")) + 20)

    response = client.post("/ingest/sdk", content=body, headers=JSON_HEADERS)
    assert response.status_code == 413
    assert response.json()["detail"] == "Telemetry event too large"


def test_session_count_is_capped(client, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_SESSIONS", 2)
    for _ in range(2):
        assert client.post("/ingest/sdk", json=_sdk_event()).status_code == 201

    response = client.post("/ingest/sdk", json=_sdk_event())
    assert response.status_code == 429
    assert response.json()["detail"] == "Local session limit reached"


def test_events_per_stream_are_capped_independently(client, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_EVENTS_PER_STREAM", 2)
    correlation_id = generate_uuidv7()
    for _ in range(2):
        assert (
            client.post("/ingest/sdk", json=_sdk_event(correlation_id)).status_code
            == 201
        )
    assert (
        client.post("/ingest/sdk", json=_sdk_event(correlation_id)).status_code == 429
    )

    other_stream = {
        "correlation_id": correlation_id,
        "timestamp": 1.0,
        "upstream_url": "https://upstream.test",
        "status_code": 200,
    }
    assert client.post("/ingest/gateway", json=other_stream).status_code == 201


def test_every_stream_is_redacted_at_rest(client):
    correlation_id = generate_uuidv7()
    client.post(
        "/ingest/sdk",
        json=_sdk_event(correlation_id, completion_string="ssn 123-45-6789"),
    )
    client.post(
        "/ingest/gateway",
        json={
            "correlation_id": correlation_id,
            "timestamp": 1.0,
            "upstream_url": "https://upstream.test",
            "status_code": 200,
            "redacted_request": {"prompt": "123-45-6789"},
        },
    )
    client.post(
        "/ingest/extension",
        json={
            "correlation_id": correlation_id,
            "timestamp": 1.0,
            "url": "https://app.test",
            "event_type": "input",
            "dom_delta": {"value": "123-45-6789"},
        },
    )

    session = client.get(f"/ingest/session/{correlation_id}").json()
    assert "123-45-6789" not in json.dumps(session)
    assert len(session["sdk_events"]) == 1
    assert len(session["gateway_events"]) == 1
    assert len(session["extension_events"]) == 1


def test_unknown_sessions_are_not_found(client):
    correlation_id = generate_uuidv7()
    assert client.get(f"/ingest/session/{correlation_id}").status_code == 404
    assert (
        client.post("/ingest/seal", json={"correlation_id": correlation_id}).status_code
        == 404
    )


def test_seal_requires_a_correlation_id_in_the_body(client):
    assert client.post("/ingest/seal", json={}).status_code == 400
    assert client.post("/ingest/seal", json={"correlation_id": ""}).status_code == 400


def test_a_session_can_only_be_sealed_once_and_is_then_append_only(client):
    correlation_id = generate_uuidv7()
    client.post("/ingest/sdk", json=_sdk_event(correlation_id))

    sealed = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert sealed.status_code == 200
    repeated = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert repeated.status_code == 409

    appended = client.post("/ingest/sdk", json=_sdk_event(correlation_id))
    assert appended.status_code == 409
    assert appended.json()["detail"] == "Local receipt has already been created"


def test_an_existing_receipt_file_is_never_overwritten(client, tmp_path):
    correlation_id = generate_uuidv7()
    client.post("/ingest/sdk", json=_sdk_event(correlation_id))
    tmp_path.joinpath(f"{correlation_id}.json").write_text("{}", encoding="utf-8")

    response = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert response.status_code == 409
    assert response.json()["detail"] == "Receipt file already exists"
    assert (
        tmp_path.joinpath(f"{correlation_id}.json").read_text(encoding="utf-8") == "{}"
    )


def test_an_unusable_receipt_directory_reports_insufficient_storage(
    client, tmp_path, monkeypatch
):
    blocked = tmp_path / "receipts-as-a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(ingest, "RECEIPT_DIR", blocked)

    correlation_id = generate_uuidv7()
    client.post("/ingest/sdk", json=_sdk_event(correlation_id))
    response = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert response.status_code == 507


def test_a_failed_receipt_write_reports_insufficient_storage(
    client, tmp_path, monkeypatch
):
    def failing_mkstemp(*args, **kwargs):
        raise OSError("no space left on device")

    # Receipts are written to a temporary file and then published atomically.
    monkeypatch.setattr(ingest.tempfile, "mkstemp", failing_mkstemp)
    correlation_id = generate_uuidv7()
    client.post("/ingest/sdk", json=_sdk_event(correlation_id))

    response = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert response.status_code == 507
    assert response.json()["detail"] == "Receipt could not be persisted"


def test_an_empty_session_still_produces_a_verifiable_receipt(client, tmp_path):
    correlation_id = generate_uuidv7()
    client.post(
        "/ingest/gateway",
        json={
            "correlation_id": correlation_id,
            "timestamp": 1.0,
            "upstream_url": "https://upstream.test",
            "status_code": 200,
        },
    )

    sealed = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert sealed.status_code == 200
    receipt = json.loads(
        tmp_path.joinpath(f"{correlation_id}.json").read_text(encoding="utf-8")
    )
    assert receipt["events"]["sdk"] == []
    assert receipt["events"]["extension"] == []
    assert receipt["receipt_version"] == 1
    assert receipt["signature_algorithm"] == "Ed25519"
    assert sealed.json()["public_key_fingerprint"] == receipt["public_key_fingerprint"]
