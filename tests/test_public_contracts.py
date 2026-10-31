import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from llmwitness import cli, gateway, ingest
from llmwitness.config import LLMWitnessConfig
from llmwitness.sdk import LLMWitnessTracker
from llmwitness.utils import Ed25519KeyManager, generate_uuidv7


def test_config_reports_missing_keys_and_invalid_url(monkeypatch):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    monkeypatch.delenv("LLMWITNESS_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.setenv("INGESTION_SERVER_URL", "ftp://invalid")
    errors = LLMWitnessConfig().validate()
    assert any("durable HMAC identity" in error for error in errors)
    assert any("signer changes after restart" in error for error in errors)
    assert any("invalid scheme" in error for error in errors)


def test_config_accepts_explicit_local_security_material(monkeypatch):
    private_key = Ed25519KeyManager().export_private_key_pem()
    monkeypatch.setenv("LLMWITNESS_SECRET_KEY", "a-secure-local-test-secret")
    monkeypatch.setenv("LLMWITNESS_PRIVATE_KEY_PEM", private_key)
    monkeypatch.setenv("INGESTION_SERVER_URL", "http://127.0.0.1:8000")
    assert LLMWitnessConfig().validate() == []


def test_cli_validate_config_success_and_failure(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["llmwitness", "validate-config"])
    monkeypatch.setattr(cli, "get_config", lambda: SimpleNamespace(validate=lambda: []))
    cli.main()
    assert "[OK]" in capsys.readouterr().out

    monkeypatch.setattr(
        cli, "get_config", lambda: SimpleNamespace(validate=lambda: ["bad config"])
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "bad config" in capsys.readouterr().out


def test_cli_accepts_explicit_arguments_without_mutating_process_argv(capsys):
    cli.main(["schema", "--compact"])

    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "ExecutionEnvelope"


def test_cli_rejects_missing_and_invalid_receipts(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(sys, "argv", ["llmwitness", "verify", str(missing)])
    with pytest.raises(SystemExit):
        cli.main()
    assert "not found" in capsys.readouterr().out

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"not": "a receipt"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["llmwitness", "verify", str(invalid)])
    with pytest.raises(SystemExit):
        cli.main()
    assert "verification failed" in capsys.readouterr().out


def test_sdk_openai_wrapper_preserves_result_and_records_metadata(monkeypatch):
    tracker = LLMWitnessTracker()
    recorded = []
    monkeypatch.setattr(
        tracker, "record_event", lambda **kwargs: recorded.append(kwargs)
    )

    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="done",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup", arguments='{"id":1}'
                            ),
                        )
                    ],
                )
            )
        ],
    )
    calls = []

    def create(*args, **kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    wrapped = tracker.wrap_openai_client(client)
    assert wrapped.chat.completions.create(model="test") is response
    assert calls[0]["extra_headers"]["X-LLMWitness-Correlation-ID"]
    assert recorded[0]["prompt_tokens"] == 3
    assert recorded[0]["completion_tokens"] == 5
    assert recorded[0]["tool_calls"][0]["function"]["name"] == "lookup"
    tracker.shutdown()


def test_gateway_auth_size_json_and_header_contract(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", "gateway-secret")
    client = TestClient(gateway.app)
    assert client.post("/v1/chat/completions", json={}).status_code == 401

    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", None)
    invalid = client.post(
        "/v1/chat/completions",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert invalid.status_code == 400


def test_gateway_forwards_supported_openai_headers(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upstream.test":
            seen.update(request.headers)
            return httpx.Response(
                200,
                json={"choices": []},
                headers={
                    "content-type": "application/json",
                    "x-ratelimit-remaining-requests": "99",
                    "openai-processing-ms": "12",
                },
            )
        return httpx.Response(201, json={"status": "accepted"})

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gateway, "http_client", async_client)
    monkeypatch.setattr(gateway, "UPSTREAM_OPENAI_URL", "https://upstream.test")
    monkeypatch.setattr(gateway, "INGESTION_SERVER_URL", "http://ingest.test")
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "false")
    response = TestClient(gateway.app).post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
        headers={
            "OpenAI-Organization": "org-test",
            "OpenAI-Project": "project-test",
            "Idempotency-Key": "request-1",
        },
    )
    assert response.status_code == 200
    assert seen["openai-organization"] == "org-test"
    assert seen["openai-project"] == "project-test"
    assert seen["idempotency-key"] == "request-1"
    assert response.headers["x-ratelimit-remaining-requests"] == "99"
    assert response.headers["openai-processing-ms"] == "12"


def test_ingestion_session_and_seal_error_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path)
    ingest.audit_vault.clear()
    client = TestClient(ingest.app)
    correlation_id = generate_uuidv7()

    assert client.get(f"/ingest/session/{correlation_id}").status_code == 404
    assert (
        client.post("/ingest/seal", json={"correlation_id": correlation_id}).status_code
        == 404
    )
    accepted = client.post(
        "/ingest/sdk",
        json={
            "correlation_id": correlation_id,
            "task_name": "seal-contract",
            "timestamp": 1.0,
        },
    )
    assert accepted.status_code == 201
    assert client.get(f"/ingest/session/{correlation_id}").status_code == 200
