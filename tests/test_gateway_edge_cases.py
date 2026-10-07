"""Boundary behaviour of the local development gateway."""

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from llmwitness import gateway
from llmwitness.utils import generate_uuidv7

JSON_HEADERS = {"content-type": "application/json"}
CHAT_BODY = {"model": "test", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
def mock_upstream(monkeypatch):
    """Serve the built-in mock upstream so no network call is attempted."""
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", None)
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "true")
    return TestClient(gateway.app)


def _upstream_client(monkeypatch, handler, telemetry: list | None = None):
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upstream.test":
            return handler(request)
        if telemetry is not None:
            telemetry.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "accepted"})

    monkeypatch.setattr(
        gateway, "http_client", httpx.AsyncClient(transport=httpx.MockTransport(route))
    )
    monkeypatch.setattr(gateway, "UPSTREAM_OPENAI_URL", "https://upstream.test")
    monkeypatch.setattr(gateway, "INGESTION_SERVER_URL", "http://ingest.test")
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", None)
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "false")
    return TestClient(gateway.app)


def test_a_missing_correlation_header_is_generated_as_uuidv7(mock_upstream):
    response = mock_upstream.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 200
    generated = uuid.UUID(response.headers["X-LLMWitness-Correlation-ID"])
    assert generated.version == 7


def test_a_supplied_uuidv7_is_echoed_back_unchanged(mock_upstream):
    correlation_id = generate_uuidv7()
    response = mock_upstream.post(
        "/v1/chat/completions",
        json=CHAT_BODY,
        headers={"X-LLMWitness-Correlation-ID": correlation_id},
    )
    assert response.headers["X-LLMWitness-Correlation-ID"] == correlation_id


@pytest.mark.parametrize(
    "correlation_id",
    ["", "not-a-uuid", "2b1f9d3c-8b6a-4f2e-9c1d-6a5b4c3d2e1f", "0" * 36],
)
def test_correlation_headers_that_are_not_uuidv7_are_refused(
    mock_upstream, correlation_id
):
    response = mock_upstream.post(
        "/v1/chat/completions",
        json=CHAT_BODY,
        headers={"X-LLMWitness-Correlation-ID": correlation_id},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "body", [b"", b"{not json", b"null", b"[1, 2]", b'"a string"', b"12", b"\xff\xfe{}"]
)
def test_only_json_objects_are_proxied(mock_upstream, body):
    response = mock_upstream.post(
        "/v1/chat/completions", content=body, headers=JSON_HEADERS
    )
    assert response.status_code == 400


def test_malformed_content_length_is_refused(mock_upstream):
    response = mock_upstream.post(
        "/v1/chat/completions",
        content=json.dumps(CHAT_BODY),
        headers={**JSON_HEADERS, "content-length": "not-a-number"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Content-Length"


def test_bodies_beyond_the_size_ceiling_are_refused(mock_upstream, monkeypatch):
    monkeypatch.setattr(gateway, "MAX_REQUEST_BYTES", 16)
    response = mock_upstream.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 413


def test_chunked_bodies_are_measured_after_reading_them(mock_upstream, monkeypatch):
    """A chunked upload declares no length, so the raw body must be bounded too."""
    monkeypatch.setattr(gateway, "MAX_REQUEST_BYTES", 16)

    def chunks():
        yield json.dumps(CHAT_BODY).encode("utf-8")

    response = mock_upstream.post(
        "/v1/chat/completions", content=chunks(), headers=JSON_HEADERS
    )
    assert response.status_code == 413


def test_non_loopback_clients_are_refused_until_a_token_is_configured(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", None)
    remote = TestClient(gateway.app, client=("10.0.0.5", 5000))
    response = remote.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 403
    assert "LLMWITNESS_GATEWAY_TOKEN" in response.json()["detail"]


@pytest.mark.parametrize(
    ("token", "expected"), [(None, 401), ("wrong-token", 401), ("gateway-secret", 200)]
)
def test_configured_gateway_token_is_enforced(monkeypatch, token, expected):
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", "gateway-secret")
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "true")
    headers = {"X-LLMWitness-Gateway-Token": token} if token else None
    response = TestClient(gateway.app).post(
        "/v1/chat/completions", json=CHAT_BODY, headers=headers
    )
    assert response.status_code == expected


def test_upstream_failures_become_a_bad_gateway_with_the_correlation_id(monkeypatch):
    telemetry: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream is down")

    client = _upstream_client(monkeypatch, handler, telemetry)
    correlation_id = generate_uuidv7()
    response = client.post(
        "/v1/chat/completions",
        json=CHAT_BODY,
        headers={"X-LLMWitness-Correlation-ID": correlation_id},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "Upstream request failed"}
    assert response.headers["X-LLMWitness-Correlation-ID"] == correlation_id
    assert telemetry[0]["status_code"] == 502
    assert telemetry[0]["redacted_response"] == {"error_type": "ConnectError"}


def test_upstream_error_status_codes_are_passed_through_untouched(monkeypatch):
    payload = b'{"error":{"message":"rate limited"}}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=payload,
            headers={"content-type": "application/json", "retry-after": "30"},
        )

    response = _upstream_client(monkeypatch, handler).post(
        "/v1/chat/completions", json=CHAT_BODY
    )
    assert response.status_code == 429
    assert response.content == payload
    assert response.headers["retry-after"] == "30"


def test_hop_by_hop_and_unlisted_upstream_headers_are_not_forwarded(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": []},
            headers={
                "content-type": "application/json",
                "set-cookie": "session=secret",
                "server": "upstream-server",
            },
        )

    response = _upstream_client(monkeypatch, handler).post(
        "/v1/chat/completions", json=CHAT_BODY
    )
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert response.headers.get("server") != "upstream-server"


def test_client_credentials_are_never_copied_into_audit_telemetry(monkeypatch):
    telemetry: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": []}, headers={"content-type": "application/json"}
        )

    client = _upstream_client(monkeypatch, handler, telemetry)
    client.post(
        "/v1/chat/completions",
        json={
            "model": "test",
            "messages": [{"role": "user", "content": "123-45-6789"}],
        },
        headers={"Authorization": "Bearer sk-abcdefghijklmnopqrstuvwxyz"},
    )

    recorded = json.dumps(telemetry[0])
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in recorded
    assert "123-45-6789" not in recorded


def test_non_json_upstream_bodies_are_summarised_as_a_bounded_preview():
    response = httpx.Response(
        200,
        content=b"plain text with 123-45-6789",
        headers={"content-type": "text/html"},
    )
    audit = gateway._audit_copy(response)
    assert audit["body_preview"] == "plain text with [REDACTED_SSN]"
    assert audit["truncated"] is False


def test_undecodable_json_upstream_bodies_fall_back_to_a_preview():
    response = httpx.Response(
        200, content=b"{truncated", headers={"content-type": "application/json"}
    )
    assert gateway._audit_copy(response)["body_preview"] == "{truncated"


def test_long_upstream_bodies_are_truncated_and_flagged(monkeypatch):
    monkeypatch.setattr(gateway, "MAX_AUDIT_TEXT_BYTES", 8)
    response = httpx.Response(
        200, content=b"0123456789abcdef", headers={"content-type": "text/plain"}
    )
    audit = gateway._audit_copy(response)
    assert audit["body_preview"] == "01234567"
    assert audit["truncated"] is True


def test_a_proxy_call_still_succeeds_when_telemetry_delivery_fails(monkeypatch):
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upstream.test":
            return httpx.Response(
                200, json={"choices": []}, headers={"content-type": "application/json"}
            )
        raise httpx.ConnectError("ingestion is down")

    monkeypatch.setattr(
        gateway, "http_client", httpx.AsyncClient(transport=httpx.MockTransport(route))
    )
    monkeypatch.setattr(gateway, "UPSTREAM_OPENAI_URL", "https://upstream.test")
    monkeypatch.setattr(gateway, "INGESTION_SERVER_URL", "http://ingest.test")
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", None)
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "false")

    response = TestClient(gateway.app).post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 200
