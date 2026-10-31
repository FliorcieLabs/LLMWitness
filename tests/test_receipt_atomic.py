import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from llmwitness import ingest
from llmwitness.utils import generate_uuidv7


def _create_session(client: TestClient) -> str:
    correlation_id = generate_uuidv7()
    response = client.post(
        "/ingest/sdk",
        json={
            "correlation_id": correlation_id,
            "task_name": "atomic-receipt-test",
            "timestamp": 1.0,
        },
    )
    assert response.status_code == 201
    return correlation_id


def _receipt_temporary_files(receipt_dir: Path) -> list[Path]:
    return list(receipt_dir.glob(".*.tmp"))


def test_partial_write_is_cleaned_up_and_seal_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path)
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    client = TestClient(ingest.app)
    correlation_id = _create_session(client)
    real_dump = json.dump

    def fail_during_dump(_receipt: dict[str, Any], handle, **_kwargs: Any) -> None:
        handle.write('{"partial":')
        raise OSError("simulated write failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(ingest.json, "dump", fail_during_dump)
        failed = client.post("/ingest/seal", json={"correlation_id": correlation_id})

    assert failed.status_code == 507
    assert not (tmp_path / f"{correlation_id}.json").exists()
    assert _receipt_temporary_files(tmp_path) == []
    assert ingest.audit_vault[correlation_id]["is_sealed"] is False

    # Keep the local reference live so a monkeypatching regression cannot hide
    # an accidental replacement of the standard-library function.
    assert ingest.json.dump is real_dump
    retried = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert retried.status_code == 200
    assert (tmp_path / f"{correlation_id}.json").exists()
    assert _receipt_temporary_files(tmp_path) == []


def test_publication_failure_is_cleaned_up_and_seal_can_be_retried(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path)
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    client = TestClient(ingest.app)
    correlation_id = _create_session(client)

    def fail_publication(_source: Path, _destination: Path) -> None:
        raise OSError("simulated publication failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(ingest.os, "link", fail_publication)
        failed = client.post("/ingest/seal", json={"correlation_id": correlation_id})

    assert failed.status_code == 507
    assert not (tmp_path / f"{correlation_id}.json").exists()
    assert _receipt_temporary_files(tmp_path) == []
    assert ingest.audit_vault[correlation_id]["is_sealed"] is False

    retried = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert retried.status_code == 200
    assert (tmp_path / f"{correlation_id}.json").exists()
    assert _receipt_temporary_files(tmp_path) == []


def test_existing_receipt_is_never_overwritten_and_retry_can_recover(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path)
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    client = TestClient(ingest.app)
    correlation_id = _create_session(client)
    receipt_path = tmp_path / f"{correlation_id}.json"
    existing_contents = '{"existing":"receipt"}'
    receipt_path.write_text(existing_contents, encoding="utf-8")

    duplicate = client.post("/ingest/seal", json={"correlation_id": correlation_id})

    assert duplicate.status_code == 409
    assert receipt_path.read_text(encoding="utf-8") == existing_contents
    assert _receipt_temporary_files(tmp_path) == []
    assert ingest.audit_vault[correlation_id]["is_sealed"] is False

    receipt_path.unlink()
    recovered = client.post("/ingest/seal", json={"correlation_id": correlation_id})
    assert recovered.status_code == 200
    assert ingest.audit_vault[correlation_id]["is_sealed"] is True
    assert _receipt_temporary_files(tmp_path) == []
