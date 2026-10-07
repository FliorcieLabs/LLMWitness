import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from llmwitness import cli, gateway, ingest
from llmwitness.config import get_secret_key
from llmwitness.utils import generate_uuidv7, verify_proof_receipt


def test_benchmark_harness_smoke():
    from benchmarks.benchmark_local import run

    result = run(iterations=3, warmup=1)
    assert result["schema_version"] == 1
    assert result["methodology"]["iterations_per_case"] == 3
    assert set(result["cases"]) == {
        "redact_payload",
        "ed25519_sign",
        "ed25519_verify",
        "passport_issue",
        "passport_verify",
        "passport_runtime_map",
        "sdk_record_event",
        "mock_gateway_request",
    }


def test_real_upstream_body_is_not_redacted(monkeypatch):
    raw_upstream = (
        b'{"value":"123-45-6789","nested":{"token":"sk-12345678901234567890"}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upstream.test":
            return httpx.Response(
                200, content=raw_upstream, headers={"content-type": "application/json"}
            )
        return httpx.Response(201, json={"status": "accepted"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gateway, "http_client", client)
    monkeypatch.setattr(gateway, "UPSTREAM_OPENAI_URL", "https://upstream.test")
    monkeypatch.setattr(gateway, "INGESTION_SERVER_URL", "http://ingest.test")
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", None)
    monkeypatch.setenv("LLMWITNESS_MOCK_UPSTREAM", "false")

    response = TestClient(gateway.app).post(
        "/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert response.content == raw_upstream


def test_gateway_telemetry_accepts_json_array_response(monkeypatch):
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    ingest.audit_vault.clear()
    client = TestClient(ingest.app)
    payload = {
        "correlation_id": generate_uuidv7(),
        "timestamp": 1.0,
        "upstream_url": "https://upstream.test/v1/chat/completions",
        "status_code": 200,
        "redacted_request": {"model": "test"},
        "redacted_response": [{"result": "ok"}],
    }
    response = client.post("/ingest/gateway", json=payload)
    assert response.status_code == 201


@pytest.mark.parametrize("timestamp", ["NaN", "Infinity", "-Infinity"])
def test_ingestion_rejects_non_finite_timestamps(monkeypatch, timestamp):
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    body = {
        "correlation_id": generate_uuidv7(),
        "task_name": "timestamp-validation",
        "timestamp": timestamp,
    }
    assert TestClient(ingest.app).post("/ingest/sdk", json=body).status_code == 422


def test_receipt_lifecycle_and_tamper_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path)
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    client = TestClient(ingest.app)
    correlation_id = generate_uuidv7()

    accepted = client.post(
        "/ingest/sdk",
        json={
            "correlation_id": correlation_id,
            "task_name": "receipt-test",
            "timestamp": 1.0,
            "completion_string": "SSN 123-45-6789",
        },
    )
    assert accepted.status_code == 201

    sealed = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert sealed.status_code == 200
    receipt_path = sealed.json()["receipt_file"]
    assert verify_proof_receipt(receipt_path, get_secret_key())

    receipt = json.loads(
        tmp_path.joinpath(f"{correlation_id}.json").read_text(encoding="utf-8")
    )
    assert "123-45-6789" not in json.dumps(receipt)
    receipt["events"]["sdk"][0]["task_name"] = "tampered"
    tmp_path.joinpath("tampered.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert not verify_proof_receipt(str(tmp_path / "tampered.json"), get_secret_key())

    duplicate = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert duplicate.status_code == 409


def test_ingest_token_and_uuidv7_are_enforced(monkeypatch):
    monkeypatch.setattr(ingest, "INGEST_TOKEN", "local-test-token")
    ingest.audit_vault.clear()
    client = TestClient(ingest.app)
    body = {"correlation_id": generate_uuidv7(), "task_name": "auth", "timestamp": 1.0}

    assert client.post("/ingest/sdk", json=body).status_code == 401
    headers = {"Authorization": "Bearer local-test-token"}
    assert client.post("/ingest/sdk", json=body, headers=headers).status_code == 201

    body["correlation_id"] = "not-a-uuid"
    assert client.post("/ingest/sdk", json=body, headers=headers).status_code == 422


def test_cli_enforces_trusted_signer_fingerprint(tmp_path, monkeypatch, capsys):
    payload = {"correlation_id": generate_uuidv7(), "sealed_at": "now", "events": {}}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    manager = ingest.Ed25519KeyManager()
    fingerprint = manager.get_public_key_fingerprint()
    receipt = {
        **payload,
        "ed25519_signature": manager.sign(serialized),
        "public_key_pem": manager.export_public_key_pem(),
        "public_key_fingerprint": fingerprint,
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llmwitness",
            "verify",
            str(receipt_path),
            "--trusted-fingerprint",
            fingerprint,
        ],
    )
    cli.main()
    assert fingerprint in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        ["llmwitness", "verify", str(receipt_path), "--trusted-fingerprint", "0" * 64],
    )
    with pytest.raises(SystemExit):
        cli.main()
