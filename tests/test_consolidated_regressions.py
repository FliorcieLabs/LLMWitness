"""Failure-boundary regressions found in the consolidated branch audit."""

import json
import os
import sqlite3
import time
import zipfile
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from llmwitness import ingest
from llmwitness.evidence_bundle import verify_evidence_bundle
from llmwitness.receipt_tools import timeline
from llmwitness.sdk import LLMWitnessTracker, _RecordingStream
from llmwitness.spool import EventSpool
from llmwitness.utils import generate_uuidv7


@pytest.fixture
def local_service(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "RECEIPT_DIR", tmp_path / "receipts")
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    monkeypatch.setattr(ingest, "session_store", None)
    monkeypatch.setattr(ingest, "audit_vault", {})
    monkeypatch.setattr(ingest, "sealed_proofs", {})
    monkeypatch.setattr(ingest, "_chain_heads", {})
    monkeypatch.setattr(ingest, "_evicted_sealed_ids", deque(maxlen=4096))
    return TestClient(ingest.app)


@pytest.mark.parametrize("stream", ["sdk", "gateway", "extension"])
def test_persisted_receipt_prevents_reopening_after_memory_reset(stream, local_service):
    client = local_service
    cid = generate_uuidv7()
    event = {"correlation_id": cid, "timestamp": 1.0, "task_name": "run"}
    assert client.post("/ingest/sdk", json=event).status_code == 201
    assert client.post("/ingest/seal", json={"correlation_id": cid}).status_code == 200
    receipt = (ingest.RECEIPT_DIR / f"{cid}.json").read_bytes()
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    ingest._evicted_sealed_ids.clear()
    if stream == "gateway":
        event.update(upstream_url="http://localhost", status_code=200)
    elif stream == "extension":
        event.update(url="http://localhost", event_type="change", dom_delta={})
    assert client.post(f"/ingest/{stream}", json=event).status_code == 409
    assert cid not in ingest.audit_vault
    assert (ingest.RECEIPT_DIR / f"{cid}.json").read_bytes() == receipt


def test_spool_release_keeps_undelivered_batch_when_capacity_is_full(tmp_path):
    spool = EventSpool(tmp_path, max_bytes=18)
    assert spool.append("sdk", {"n": 1})
    assert spool.append("sdk", {"n": 2})
    claim = spool.claim("sdk")
    assert claim is not None
    spool.release("sdk", claim, claim.payloads[1:])
    assert spool.has_pending("sdk")
    recovered = spool.claim("sdk")
    assert recovered is not None and recovered.payloads == [{"n": 2}]
    spool.release("sdk", recovered, [])
    assert not spool.has_pending("sdk")


def test_spool_capacity_counts_actual_utf8_bytes_on_every_platform(tmp_path):
    spool = EventSpool(tmp_path, max_bytes=16)
    assert spool.append("sdk", {"n": 1})
    assert spool.append("sdk", {"n": 2})
    assert (tmp_path / "sdk.jsonl").stat().st_size == 16
    assert not spool.append("sdk", {"n": 3})


def test_claim_refreshes_lease_of_old_pending_file(tmp_path):
    spool = EventSpool(tmp_path)
    assert spool.append("sdk", {"n": 1})
    old = time.time() - 600
    os.utime(tmp_path / "sdk.jsonl", (old, old))
    claim = spool.claim("sdk")
    assert claim is not None
    assert not EventSpool(tmp_path).has_pending("sdk")
    assert EventSpool(tmp_path).claim("sdk") is None


@pytest.mark.parametrize("stream", ["../escape", "../a/b", "..\\escape", ""])
def test_spool_stream_cannot_escape_directory(tmp_path, stream):
    spool = EventSpool(tmp_path / "spool")
    with pytest.raises(ValueError, match="stream"):
        spool.append(stream, {"n": 1})


def test_release_write_failure_retains_original_claim(tmp_path, monkeypatch):
    spool = EventSpool(tmp_path)
    assert spool.append("sdk", {"n": 1})
    assert spool.append("sdk", {"n": 2})
    claim = spool.claim("sdk")
    assert claim is not None
    original = claim.path.read_bytes()

    def fail_replace(*args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as context:
        context.setattr(os, "replace", fail_replace)
        spool.release("sdk", claim, claim.payloads[1:])
    assert claim.path.read_bytes() == original
    assert [json.loads(line) for line in original.splitlines()] == claim.payloads


def test_timeline_sorts_browser_milliseconds_with_sdk_seconds():
    entries = timeline(
        {
            "events": {
                "sdk": [{"timestamp": 1700000001.0, "task_name": "later"}],
                "extension": [{"timestamp": 1700000000000, "event_type": "earlier"}],
            }
        }
    )
    assert [entry.source for entry in entries] == ["browser", "sdk"]


def test_receipt_inspection_handles_invalid_event_container():
    assert timeline({"events": ["invalid"]}) == []
    assert timeline({"events": {"sdk": 7}}) == []


@pytest.mark.parametrize("failure", ["create", "append"])
def test_storage_failure_returns_507_without_publishing_uncommitted_state(
    local_service, monkeypatch, failure
):
    def unavailable(*args):
        raise sqlite3.OperationalError("private storage diagnostic")

    store = SimpleNamespace(
        create_session=lambda *args: None, append_event=lambda *args: None
    )
    setattr(
        store,
        f"{failure}_session" if failure == "create" else "append_event",
        unavailable,
    )
    monkeypatch.setattr(ingest, "session_store", store)
    cid = generate_uuidv7()
    if failure == "append":
        session = ingest.get_or_create_session(cid)
        previous = session["updated_at"]
    response = local_service.post(
        "/ingest/sdk",
        json={
            "correlation_id": cid,
            "task_name": "run",
            "timestamp": 1.0,
        },
    )
    assert response.status_code == 507
    assert "private storage diagnostic" not in response.text
    if failure == "create":
        assert cid not in ingest.audit_vault
    else:
        assert session["sdk_events"] == []
        assert session["updated_at"] == previous


def test_receipt_commit_succeeds_even_if_database_cleanup_fails(
    local_service, monkeypatch
):
    def unavailable(*args):
        raise sqlite3.OperationalError("cleanup unavailable")

    monkeypatch.setattr(
        ingest,
        "session_store",
        SimpleNamespace(
            create_session=lambda *args: None,
            append_event=lambda *args: None,
            delete_session=unavailable,
        ),
    )
    cid = generate_uuidv7()
    assert (
        local_service.post(
            "/ingest/sdk",
            json={
                "correlation_id": cid,
                "task_name": "run",
                "timestamp": 1.0,
            },
        ).status_code
        == 201
    )
    assert (
        local_service.post("/ingest/seal", json={"correlation_id": cid}).status_code
        == 200
    )
    assert ingest.audit_vault[cid]["is_sealed"]


def test_stream_finalizer_failure_preserves_chunks_and_hides_diagnostic(caplog):
    def fail_recording():
        raise RuntimeError("private provider diagnostic")

    stream = _RecordingStream(
        iter(["first", "second"]), lambda chunk: None, fail_recording
    )
    assert list(stream) == ["first", "second"]
    assert "RuntimeError" in caplog.text
    assert "private provider diagnostic" not in caplog.text


def test_stream_finalizer_does_not_replace_provider_exception():
    def provider():
        yield "first"
        raise ValueError("provider failure")

    def fail_recording():
        raise RuntimeError("recording failure")

    stream = _RecordingStream(provider(), lambda chunk: None, fail_recording)
    with pytest.raises(ValueError, match="provider failure"):
        list(stream)


@pytest.mark.parametrize("unsupported_flag", [1, 32, 64])
def test_offline_verifier_rejects_unsupported_archive_flags(tmp_path, unsupported_flag):
    bundle = tmp_path / "encrypted.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("journal.json", "{}")
        archive.writestr("manifest.json", "{}")
    data = bytearray(bundle.read_bytes())
    # Model an untrusted encrypted member in both ZIP headers. Python's ZIP
    # writer cannot create encrypted files, but the reader honors this flag.
    for signature, flag_offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
        index = data.index(signature)
        data[index + flag_offset] |= unsupported_flag
    bundle.write_bytes(data)
    result = verify_evidence_bundle(bundle)
    assert not result.valid
    assert result.error is not None


def test_sdk_discovers_abandoned_spool_after_cached_pending_flag_clears(tmp_path):
    spool = EventSpool(tmp_path)
    assert spool.append("sdk", {"old": True})
    abandoned = spool.claim("sdk")
    assert abandoned is not None
    tracker = object.__new__(LLMWitnessTracker)
    tracker.spool = spool
    tracker._spool_pending = False
    tracker._consecutive_failures = 0
    tracker._max_retries = 0
    tracker.replayed_events = 0
    delivered = []

    def deliver(payload):
        delivered.append(payload)
        return "delivered"

    tracker._post_event = deliver
    os.utime(abandoned.path, (0, 0))
    tracker._deliver({"new": True})
    assert delivered == [{"new": True}, {"old": True}]
    assert tracker.replayed_events == 1
