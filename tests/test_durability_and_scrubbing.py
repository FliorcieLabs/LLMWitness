"""Regression tests for durable sessions, key persistence, chaining and scrubbing."""

import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from llmwitness import cli, config, ingest, keys, utils
from llmwitness.receipt_tools import verify_chain
from llmwitness.session_store import SessionStore
from llmwitness.spool import EventSpool
from llmwitness.utils import (
    Ed25519KeyManager,
    generate_uuidv7,
    redact_payload,
    verify_proof_receipt,
)


@pytest.fixture
def service(tmp_path, monkeypatch):
    """An ingestion app with clean in-memory state and receipts in ``tmp_path``."""
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(ingest, "RECEIPT_DIR", receipts)
    monkeypatch.setattr(ingest, "INGEST_TOKEN", None)
    monkeypatch.setattr(ingest, "key_manager", Ed25519KeyManager())
    monkeypatch.setattr(config, "_LOCAL_SESSION_SECRET", "x" * 32)
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()
    ingest._chain_heads.clear()
    ingest._evicted_sealed_ids.clear()
    yield TestClient(ingest.app)
    ingest.disable_durable_state()
    ingest.audit_vault.clear()
    ingest.sealed_proofs.clear()


def _event(correlation_id: str, **extra) -> dict:
    return {
        "correlation_id": correlation_id,
        "task_name": "t",
        "timestamp": 1.0,
        **extra,
    }


def _seal(client: TestClient, correlation_id: str):
    return client.post("/ingest/seal", json={"correlation_id": correlation_id})


# --- scrubbing ---------------------------------------------------------------


def test_only_luhn_valid_numbers_are_treated_as_cards():
    assert redact_payload("card 4111 1111 1111 1111") == "card [REDACTED_CREDIT_CARD]"
    # A millisecond timestamp and an order number are not payment cards.
    assert redact_payload("at 1717171717171") == "at 1717171717171"
    assert redact_payload("order 4111-2222-3333-4444") == "order 4111-2222-3333-4444"


def test_emails_and_formatted_phone_numbers_are_redacted():
    text = redact_payload(
        "jane.doe+x@example.co.uk or (415) 555-2671 or +44 20 7946 0958"
    )
    assert text == "[REDACTED_EMAIL] or [REDACTED_PHONE] or [REDACTED_PHONE]"
    untouched = "date 2026-10-08 version 1.2.3 host 192.168.1.10 id 123456789012"
    assert redact_payload(untouched) == untouched


def test_allow_list_still_protects_an_email(monkeypatch):
    monkeypatch.setenv("LLMWITNESS_PII_ALLOW_LIST", "support@example.com")
    assert redact_payload("mail support@example.com") == "mail support@example.com"


def test_custom_patterns_fields_and_scrubbers_extend_the_defaults():
    try:
        utils.register_scrub_pattern("employee id", r"\bEMP-\d{6}\b")
        utils.register_sensitive_field("Session_Cookie")
        utils.register_text_scrubber(
            lambda text: text.replace("Alice", "[REDACTED_NAME]")
        )
        redacted = redact_payload(
            {"note": "Alice is EMP-123456, SSN 123-45-6789", "session_cookie": "abc"}
        )
    finally:
        utils.clear_custom_scrub_rules()
    assert redacted == {
        "note": "[REDACTED_NAME] is [REDACTED_EMPLOYEE_ID], SSN [REDACTED_SSN]",
        "session_cookie": "[REDACTED_SENSITIVE_FIELD]",
    }
    assert redact_payload("EMP-123456") == "EMP-123456"


def test_scrub_rules_file_is_loaded_from_the_environment(tmp_path, monkeypatch):
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "patterns": [{"name": "ticket", "regex": r"TCK-\d+"}],
                "sensitive_fields": ["pin"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLMWITNESS_SCRUB_RULES_FILE", str(rules))
    try:
        assert redact_payload({"a": "TCK-42", "pin": "1234"}) == {
            "a": "[REDACTED_TICKET]",
            "pin": "[REDACTED_SENSITIVE_FIELD]",
        }
    finally:
        utils.clear_custom_scrub_rules()


def test_a_broken_scrub_rules_file_is_reported_and_never_raises(
    tmp_path, monkeypatch, caplog
):
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps({"patterns": [{"name": "bad", "regex": "("}]}), encoding="utf-8"
    )
    monkeypatch.setenv("LLMWITNESS_SCRUB_RULES_FILE", str(rules))
    try:
        assert redact_payload("SSN 123-45-6789") == "SSN [REDACTED_SSN]"
        assert "Ignoring LLMWITNESS_SCRUB_RULES_FILE" in caplog.text
        assert any(
            "not usable" in error for error in config.LLMWitnessConfig().validate()
        )
    finally:
        utils.clear_custom_scrub_rules()


# --- signing identity --------------------------------------------------------


def test_explicit_public_key_is_not_replaced_by_the_environment_private_key(
    monkeypatch,
):
    other = Ed25519KeyManager()
    monkeypatch.setenv(
        "LLMWITNESS_PRIVATE_KEY_PEM", Ed25519KeyManager().export_private_key_pem()
    )
    verifier = Ed25519KeyManager(public_key_pem=other.export_public_key_pem())
    assert verifier.get_public_key_fingerprint() == other.get_public_key_fingerprint()
    assert verifier.private_key is None


def test_identity_is_created_once_with_private_permissions_and_reloaded(tmp_path):
    created = keys.create_identity(tmp_path / "keys")
    assert created.created
    private = tmp_path / "keys" / keys.PRIVATE_KEY_FILE
    if os.name != "nt":
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        keys.create_identity(tmp_path / "keys")
    loaded = keys.load_or_create_identity(tmp_path / "keys")
    assert not loaded.created
    assert loaded.fingerprint == created.fingerprint
    assert loaded.hmac_secret == created.hmac_secret
    assert keys.local_trusted_fingerprint(tmp_path / "keys") == created.fingerprint


def test_service_start_persists_the_signer_across_restarts(
    service, tmp_path, monkeypatch
):
    monkeypatch.delenv("LLMWITNESS_EPHEMERAL_KEYS", raising=False)
    monkeypatch.delenv("LLMWITNESS_PRIVATE_KEY_PEM", raising=False)
    ingest.enable_durable_state(session_db="off", key_dir=tmp_path / "keys")
    first = ingest.key_manager.get_public_key_fingerprint()
    monkeypatch.setattr(ingest, "key_manager", Ed25519KeyManager())
    ingest.enable_durable_state(session_db="off", key_dir=tmp_path / "keys")
    assert ingest.key_manager.get_public_key_fingerprint() == first
    assert config.LLMWitnessConfig  # config sees the persisted secret below
    assert config.get_secret_key() == keys.load_identity(tmp_path / "keys").hmac_secret


def test_verify_checks_the_local_signer_by_default(
    service, tmp_path, monkeypatch, capsys
):
    key_dir = tmp_path / "keys"
    monkeypatch.setenv("LLMWITNESS_KEY_DIR", str(key_dir))
    cli.main(["keygen"])
    assert "Signer fingerprint" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["keygen"])  # never overwrites
    capsys.readouterr()

    # A receipt signed by some other key verifies cryptographically but is not trusted.
    correlation_id = generate_uuidv7()
    service.post("/ingest/sdk", json=_event(correlation_id))
    receipt_file = _seal(service, correlation_id).json()["receipt_file"]
    with pytest.raises(SystemExit):
        cli.main(["verify", receipt_file])
    assert "does not match the trusted value" in capsys.readouterr().out
    cli.main(["verify", receipt_file, "--any-signer"])

    # A receipt signed by the persisted key passes with no flags.
    monkeypatch.setattr(ingest, "key_manager", keys.load_identity(key_dir).key_manager)
    trusted_id = generate_uuidv7()
    service.post("/ingest/sdk", json=_event(trusted_id))
    cli.main(["verify", _seal(service, trusted_id).json()["receipt_file"]])
    assert "signer matches the local signing key" in capsys.readouterr().out


# --- durable sessions and eviction ------------------------------------------


def test_unsealed_sessions_survive_a_restart(service, tmp_path):
    database = tmp_path / "sessions.db"
    ingest.enable_durable_state(session_db=database, key_dir=tmp_path / "keys")
    kept, sealed = generate_uuidv7(), generate_uuidv7()
    service.post("/ingest/sdk", json=_event(kept, completion_string="SSN 123-45-6789"))
    service.post("/ingest/sdk", json=_event(kept, task_name="second"))
    service.post("/ingest/sdk", json=_event(sealed))
    assert _seal(service, sealed).status_code == 200

    # Simulate a process restart: memory is gone, the database remains.
    ingest.disable_durable_state()
    ingest.audit_vault.clear()
    ingest.enable_durable_state(session_db=database, key_dir=tmp_path / "keys")

    assert set(ingest.audit_vault) == {kept}
    restored = ingest.audit_vault[kept]["sdk_events"]
    assert [event["task_name"] for event in restored] == ["t", "second"]
    assert "123-45-6789" not in database.read_bytes().decode("utf-8", errors="ignore")
    assert _seal(service, kept).status_code == 200
    assert verify_proof_receipt(str(ingest.RECEIPT_DIR / f"{kept}.json"))


def test_abandoned_sessions_are_not_restored(service, tmp_path, monkeypatch):
    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    stale = generate_uuidv7()
    store.create_session(stale, time.time() - 10_000)
    store.close()
    monkeypatch.setattr(ingest, "SESSION_IDLE_TTL_SECONDS", 60.0)
    ingest.enable_durable_state(session_db=database, key_dir=tmp_path / "keys")
    assert stale not in ingest.audit_vault
    assert ingest.session_store.load_sessions() == {}


def test_sealed_sessions_are_evicted_before_the_limit_rejects(service, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_SESSIONS", 2)
    first, second, third, fourth = (generate_uuidv7() for _ in range(4))
    assert service.post("/ingest/sdk", json=_event(first)).status_code == 201
    assert service.post("/ingest/sdk", json=_event(second)).status_code == 201
    # Both sessions are unsealed and recent: nothing may be discarded.
    assert service.post("/ingest/sdk", json=_event(third)).status_code == 429

    assert _seal(service, first).status_code == 200
    assert service.post("/ingest/sdk", json=_event(third)).status_code == 201
    assert first not in ingest.audit_vault
    assert (ingest.RECEIPT_DIR / f"{first}.json").is_file()
    # The evicted run already has a receipt, so late events are refused.
    assert service.post("/ingest/sdk", json=_event(first)).status_code == 409
    assert service.post("/ingest/sdk", json=_event(fourth)).status_code == 429


def test_idle_unsealed_session_is_dropped_only_past_the_idle_limit(
    service, monkeypatch, caplog
):
    monkeypatch.setattr(ingest, "MAX_SESSIONS", 1)
    idle, fresh = generate_uuidv7(), generate_uuidv7()
    service.post("/ingest/sdk", json=_event(idle))
    assert service.post("/ingest/sdk", json=_event(fresh)).status_code == 429
    ingest.audit_vault[idle]["updated_at"] -= ingest.SESSION_IDLE_TTL_SECONDS + 1
    assert service.post("/ingest/sdk", json=_event(fresh)).status_code == 201
    assert idle not in ingest.audit_vault
    assert "Dropping unsealed session" in caplog.text


def test_session_listing_and_new_sdk_fields(service):
    correlation_id = generate_uuidv7()
    accepted = service.post(
        "/ingest/sdk",
        json=_event(
            correlation_id,
            model="model-x",
            provider="openai",
            latency_ms=12.5,
            estimated_cost_usd=0.001,
            input_messages=[{"role": "user", "content": "mail jane@example.com"}],
        ),
    )
    assert accepted.status_code == 201
    stored = ingest.audit_vault[correlation_id]["sdk_events"][0]
    assert stored["model"] == "model-x" and stored["latency_ms"] == 12.5
    assert stored["input_messages"][0]["content"] == "mail [REDACTED_EMAIL]"
    listing = service.get("/ingest/sessions").json()
    assert listing["sessions"][0]["sdk_events"] == 1
    assert (
        service.post(
            "/ingest/sdk", json=_event(correlation_id, latency_ms=-1)
        ).status_code
        == 422
    )


def test_body_limit_counts_received_bytes_without_a_content_length(
    service, monkeypatch
):
    monkeypatch.setattr(ingest, "MAX_EVENT_BYTES", 2048)

    def chunks():
        yield b'{"correlation_id":"' + generate_uuidv7().encode() + b'","task_name":"'
        for _ in range(8):
            yield b"a" * 1024
        yield b'","timestamp":1.0}'

    oversized = service.post(
        "/ingest/sdk", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert oversized.status_code == 413
    assert ingest.audit_vault == {}

    def small():
        yield json.dumps(_event(generate_uuidv7())).encode()

    assert (
        service.post(
            "/ingest/sdk", content=small(), headers={"Content-Type": "application/json"}
        ).status_code
        == 201
    )


# --- receipt chain -----------------------------------------------------------


def _sealed_run(client: TestClient) -> str:
    correlation_id = generate_uuidv7()
    client.post("/ingest/sdk", json=_event(correlation_id))
    assert _seal(client, correlation_id).status_code == 200
    return correlation_id


def test_receipts_form_a_verifiable_chain(service):
    ids = [_sealed_run(service) for _ in range(3)]
    receipts = [
        json.loads((ingest.RECEIPT_DIR / f"{cid}.json").read_text(encoding="utf-8"))
        for cid in ids
    ]
    assert [receipt["chain"]["index"] for receipt in receipts] == [0, 1, 2]
    assert receipts[1]["chain"]["previous_receipt_hash"] == utils.receipt_hash(
        receipts[0]
    )
    report = verify_chain(ingest.RECEIPT_DIR)
    assert report.valid and report.chained == 3

    # The chain position is covered by the signature.
    receipts[2]["chain"]["index"] = 7
    forged = ingest.RECEIPT_DIR / "forged.json"
    forged.write_text(json.dumps(receipts[2]), encoding="utf-8")
    assert not verify_proof_receipt(str(forged))
    forged.unlink()


def test_chain_continues_after_a_restart_and_detects_a_removed_receipt(service):
    ids = [_sealed_run(service) for _ in range(2)]
    ingest._chain_heads.clear()  # a new process rediscovers the head from disk
    ids.append(_sealed_run(service))
    assert verify_chain(ingest.RECEIPT_DIR).valid

    (ingest.RECEIPT_DIR / f"{ids[1]}.json").unlink()
    report = verify_chain(ingest.RECEIPT_DIR)
    assert not report.valid
    assert any("missing" in problem for problem in report.problems)


def test_chain_detects_a_receipt_resigned_with_another_key(service, monkeypatch):
    ids = [_sealed_run(service) for _ in range(3)]
    target = ingest.RECEIPT_DIR / f"{ids[1]}.json"
    receipt = json.loads(target.read_text(encoding="utf-8"))
    receipt["events"]["sdk"][0]["task_name"] = "rewritten"
    attacker = Ed25519KeyManager()
    receipt["ed25519_signature"] = attacker.sign(
        utils.canonical_json(utils.signed_payload_from_receipt(receipt))
    )
    receipt["public_key_pem"] = attacker.export_public_key_pem()
    receipt["public_key_fingerprint"] = attacker.get_public_key_fingerprint()
    target.write_text(json.dumps(receipt), encoding="utf-8")

    assert verify_proof_receipt(str(target))  # self-consistent on its own
    report = verify_chain(ingest.RECEIPT_DIR)
    assert not report.valid
    assert any("does not link" in problem for problem in report.problems)
    assert len(report.signers) == 2


def test_version_one_receipts_still_verify():
    manager = Ed25519KeyManager()
    payload = {"correlation_id": generate_uuidv7(), "sealed_at": "now", "events": {}}
    receipt = {
        **payload,
        "receipt_version": 1,
        "ed25519_signature": manager.sign(utils.canonical_json(payload)),
        "public_key_pem": manager.export_public_key_pem(),
        "public_key_fingerprint": manager.get_public_key_fingerprint(),
    }
    assert utils.signed_payload_from_receipt(receipt) == payload


# --- spool -------------------------------------------------------------------


def test_spool_claim_release_and_size_cap(tmp_path):
    spool = EventSpool(tmp_path / "spool", max_bytes=200)
    assert spool.append("sdk", {"n": 1}) and spool.append("sdk", {"n": 2})
    assert not spool.append("sdk", {"big": "x" * 500})
    with (tmp_path / "spool" / "sdk.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{torn line\n")

    claim = spool.claim("sdk")
    assert [payload["n"] for payload in claim.payloads] == [1, 2]
    assert not spool.has_pending("sdk")
    spool.release("sdk", claim, claim.payloads[1:])
    assert [payload["n"] for payload in spool.claim("sdk").payloads] == [2]
    assert spool.claim("gateway") is None


def test_spool_adopts_a_claim_abandoned_by_a_dead_process(tmp_path):
    spool = EventSpool(tmp_path)
    spool.append("sdk", {"n": 1})
    abandoned = spool.claim("sdk")
    old = time.time() - 600
    os.utime(abandoned.path, (old, old))
    spool.append("sdk", {"n": 2})
    first = spool.claim("sdk")
    second = spool.claim("sdk")
    assert first is not None and second is not None
    assert sorted(payload["n"] for payload in [*first.payloads, *second.payloads]) == [
        1,
        2,
    ]
    assert not abandoned.path.exists()
