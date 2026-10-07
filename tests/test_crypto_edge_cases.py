"""Boundary behaviour of key handling and receipt verification."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from llmwitness.utils import (
    Ed25519KeyManager,
    canonical_json,
    compute_hmac_signature,
    generate_uuidv7,
    verify_proof_receipt,
)


def _rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _write_receipt(
    path, manager: Ed25519KeyManager, secret: str | None = None, **overrides
):
    payload = {
        "correlation_id": generate_uuidv7(),
        "sealed_at": "2026-01-01T00:00:00+00:00",
        "events": {"sdk": [], "gateway": [], "extension": []},
    }
    serialized = canonical_json(payload)
    receipt = {
        **payload,
        "receipt_version": 1,
        "signature_algorithm": "Ed25519",
        "ed25519_signature": manager.sign(serialized),
        "public_key_pem": manager.export_public_key_pem(),
        "public_key_fingerprint": manager.get_public_key_fingerprint(),
    }
    if secret is not None:
        receipt["hmac_signature"] = compute_hmac_signature(serialized, secret)
    receipt.update(overrides)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def test_generated_manager_round_trips_its_own_material():
    manager = Ed25519KeyManager()
    restored = Ed25519KeyManager(private_key_pem=manager.export_private_key_pem())
    assert restored.get_public_key_fingerprint() == manager.get_public_key_fingerprint()


def test_manager_reads_pem_from_environment(monkeypatch):
    manager = Ed25519KeyManager()
    monkeypatch.setenv("LLMWITNESS_PRIVATE_KEY_PEM", manager.export_private_key_pem())
    assert Ed25519KeyManager().get_public_key_fingerprint() == (
        manager.get_public_key_fingerprint()
    )


def test_manager_accepts_pem_supplied_as_bytes():
    manager = Ed25519KeyManager()
    pem_bytes = manager.export_private_key_pem().encode("utf-8")
    assert Ed25519KeyManager(
        private_key_pem=pem_bytes
    ).get_public_key_fingerprint() == (manager.get_public_key_fingerprint())


@pytest.mark.parametrize("field", ["private_key_pem", "public_key_pem"])
def test_non_ed25519_keys_are_rejected(field):
    rsa_pem = _rsa_private_key_pem()
    if field == "public_key_pem":
        rsa_pem = (
            serialization.load_pem_private_key(rsa_pem.encode("utf-8"), password=None)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )
    with pytest.raises(ValueError, match="not an Ed25519 key"):
        Ed25519KeyManager(**{field: rsa_pem})


def test_verifier_only_manager_cannot_sign_or_export_a_private_key():
    signer = Ed25519KeyManager()
    verifier = Ed25519KeyManager(public_key_pem=signer.export_public_key_pem())

    assert verifier.private_key is None
    with pytest.raises(ValueError, match="Cannot sign"):
        verifier.sign("payload")
    with pytest.raises(ValueError, match="Private key not loaded"):
        verifier.export_private_key_pem()
    assert verifier.verify("payload", signer.sign("payload")) is True


def test_a_manager_without_any_public_key_refuses_every_public_operation():
    manager = Ed25519KeyManager()
    manager.public_key = None

    with pytest.raises(ValueError, match="Public key not initialized"):
        manager.export_public_key_pem()
    with pytest.raises(ValueError, match="Public key not initialized"):
        manager.get_public_key_fingerprint()
    assert manager.verify("payload", "c2ln") is False


def test_signing_accepts_both_text_and_bytes_payloads():
    manager = Ed25519KeyManager()
    assert manager.verify(b"raw-bytes", manager.sign(b"raw-bytes")) is True
    assert manager.verify("raw-bytes", manager.sign(b"raw-bytes")) is True


@pytest.mark.parametrize(
    "signature",
    ["", "not-base64!!", base64.b64encode(b"0" * 64).decode(), "YQ=="],
)
def test_malformed_or_wrong_signatures_verify_as_false(signature):
    assert Ed25519KeyManager().verify("payload", signature) is False


def test_signature_does_not_transfer_between_keys():
    first, second = Ed25519KeyManager(), Ed25519KeyManager()
    assert second.verify("payload", first.sign("payload")) is False


def test_canonical_json_is_order_independent_and_compact():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert canonical_json({"a": "é"}) == '{"a":"é"}'


def test_hmac_is_deterministic_and_key_sensitive():
    assert compute_hmac_signature("payload", "key") == compute_hmac_signature(
        "payload", "key"
    )
    assert compute_hmac_signature("payload", "key") != compute_hmac_signature(
        "payload", "other-key"
    )


def test_verification_fails_for_missing_and_unreadable_files(tmp_path):
    assert verify_proof_receipt(str(tmp_path / "absent.json")) is False

    unreadable = tmp_path / "broken.json"
    unreadable.write_text("{not json", encoding="utf-8")
    assert verify_proof_receipt(str(unreadable)) is False


@pytest.mark.parametrize(
    "missing", ["ed25519_signature", "public_key_pem", "public_key_fingerprint"]
)
def test_receipts_missing_required_fields_are_rejected(tmp_path, missing):
    path = tmp_path / "receipt.json"
    _write_receipt(path, Ed25519KeyManager())
    receipt = json.loads(path.read_text(encoding="utf-8"))
    del receipt[missing]
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert verify_proof_receipt(str(path)) is False


def test_fingerprint_mismatch_is_rejected_even_with_a_valid_signature(tmp_path):
    path = tmp_path / "receipt.json"
    _write_receipt(path, Ed25519KeyManager(), public_key_fingerprint="0" * 64)
    assert verify_proof_receipt(str(path)) is False


@pytest.mark.parametrize("field", ["correlation_id", "sealed_at", "events"])
def test_tampering_with_any_signed_field_breaks_verification(tmp_path, field):
    path = tmp_path / "receipt.json"
    _write_receipt(path, Ed25519KeyManager())
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt[field] = (
        {"sdk": [{"injected": True}], "gateway": [], "extension": []}
        if field == "events"
        else "tampered"
    )
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert verify_proof_receipt(str(path)) is False


def test_hmac_is_only_checked_when_a_secret_is_supplied(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    path = tmp_path / "receipt.json"
    _write_receipt(path, Ed25519KeyManager(), secret="local-receipt-secret")

    assert verify_proof_receipt(str(path)) is True
    assert verify_proof_receipt(str(path), "local-receipt-secret") is True
    assert verify_proof_receipt(str(path), "wrong-secret") is False


def test_receipt_without_hmac_fails_when_a_secret_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    path = tmp_path / "receipt.json"
    _write_receipt(path, Ed25519KeyManager())

    assert verify_proof_receipt(str(path)) is True
    assert verify_proof_receipt(str(path), "local-receipt-secret") is False


def test_secret_key_is_read_from_the_environment_when_not_passed(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    _write_receipt(path, Ed25519KeyManager(), secret="environment-secret")
    monkeypatch.setenv("LLMWITNESS_SECRET_KEY", "environment-secret")
    assert verify_proof_receipt(str(path)) is True

    monkeypatch.setenv("LLMWITNESS_SECRET_KEY", "a-different-secret")
    assert verify_proof_receipt(str(path)) is False
