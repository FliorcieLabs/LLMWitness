"""Boundary behaviour of the CLI and configuration container."""

import json

import pytest

from llmwitness import cli, config
from llmwitness.config import LLMWitnessConfig, get_config, get_secret_key
from llmwitness.utils import Ed25519KeyManager, canonical_json, compute_hmac_signature


def _run(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["llmwitness", *argv])
    try:
        cli.main()
    except SystemExit as exit_signal:
        return int(exit_signal.code or 0)
    return 0


def _receipt_file(tmp_path, secret: str | None = None, **overrides):
    manager = Ed25519KeyManager()
    payload = {
        "correlation_id": "019fd93b-a06b-7799-947f-67f80b9edc04",
        "sealed_at": "2026-01-01T00:00:00+00:00",
        "events": {"sdk": [], "gateway": [], "extension": []},
    }
    serialized = canonical_json(payload)
    receipt = {
        **payload,
        "ed25519_signature": manager.sign(serialized),
        "public_key_pem": manager.export_public_key_pem(),
        "public_key_fingerprint": manager.get_public_key_fingerprint(),
    }
    if secret is not None:
        receipt["hmac_signature"] = compute_hmac_signature(serialized, secret)
    receipt.update(overrides)

    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path, receipt


def test_no_subcommand_prints_help_without_failing(monkeypatch, capsys):
    assert _run(monkeypatch, []) == 0
    assert "verify" in capsys.readouterr().out


def test_verify_reports_a_missing_receipt_path(monkeypatch, tmp_path, capsys):
    exit_code = _run(monkeypatch, ["verify", str(tmp_path / "absent.json")])
    assert exit_code == 1
    assert "Receipt file not found" in capsys.readouterr().out


def test_verify_rejects_a_directory_given_instead_of_a_file(
    monkeypatch, tmp_path, capsys
):
    assert _run(monkeypatch, ["verify", str(tmp_path)]) == 1
    assert "Receipt file not found" in capsys.readouterr().out


def test_verify_reports_an_unparsable_receipt(monkeypatch, tmp_path, capsys):
    path = tmp_path / "receipt.json"
    path.write_text("{not json", encoding="utf-8")
    assert _run(monkeypatch, ["verify", str(path)]) == 1
    assert "verification failed" in capsys.readouterr().out


def test_verify_reports_a_tampered_receipt(monkeypatch, tmp_path, capsys):
    path, _ = _receipt_file(tmp_path, sealed_at="1999-01-01T00:00:00+00:00")
    assert _run(monkeypatch, ["verify", str(path)]) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_verify_without_a_secret_warns_that_the_signer_is_unproven(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    path, receipt = _receipt_file(tmp_path)

    assert _run(monkeypatch, ["verify", str(path)]) == 0
    output = capsys.readouterr().out
    assert "Trust the signer only after checking its fingerprint" in output
    assert receipt["public_key_fingerprint"] in output


def test_verify_confirms_the_hmac_when_a_secret_is_supplied(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    path, _ = _receipt_file(tmp_path, secret="local-receipt-secret")

    exit_code = _run(
        monkeypatch, ["verify", str(path), "--secret-key", "local-receipt-secret"]
    )
    assert exit_code == 0
    assert "Ed25519 signature and HMAC verified" in capsys.readouterr().out


def test_verify_fails_when_the_supplied_secret_is_wrong(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    path, _ = _receipt_file(tmp_path, secret="local-receipt-secret")

    exit_code = _run(monkeypatch, ["verify", str(path), "--secret-key", "wrong-secret"])
    assert exit_code == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_trusted_fingerprint_is_compared_case_insensitively(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    path, receipt = _receipt_file(tmp_path)
    fingerprint = receipt["public_key_fingerprint"]

    assert (
        _run(
            monkeypatch,
            ["verify", str(path), "--trusted-fingerprint", fingerprint.upper()],
        )
        == 0
    )
    capsys.readouterr()

    assert (
        _run(monkeypatch, ["verify", str(path), "--trusted-fingerprint", "0" * 64]) == 1
    )
    assert "does not match the trusted value" in capsys.readouterr().out


def test_a_receipt_without_a_fingerprint_string_is_refused(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    path, _ = _receipt_file(tmp_path)
    monkeypatch.setattr(cli, "verify_proof_receipt", lambda *args, **kwargs: True)

    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["public_key_fingerprint"] = ""
    path.write_text(json.dumps(receipt), encoding="utf-8")

    assert _run(monkeypatch, ["verify", str(path)]) == 1
    assert "does not contain a public-key fingerprint" in capsys.readouterr().out


def test_a_receipt_that_becomes_unreadable_after_verification_is_refused(
    monkeypatch, tmp_path, capsys
):
    path, _ = _receipt_file(tmp_path)
    monkeypatch.setattr(cli, "verify_proof_receipt", lambda *args, **kwargs: True)
    path.write_text("{not json", encoding="utf-8")

    assert _run(monkeypatch, ["verify", str(path)]) == 1
    assert "fingerprint could not be read" in capsys.readouterr().out


def test_validate_config_fails_and_lists_every_problem(monkeypatch, capsys):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    monkeypatch.delenv("LLMWITNESS_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.setenv("INGESTION_SERVER_URL", "ftp://ingest.invalid")

    assert _run(monkeypatch, ["validate-config"]) == 1
    output = capsys.readouterr().out
    assert output.count("[ERROR]") >= 3


def test_secret_key_falls_back_to_a_process_scoped_value(monkeypatch):
    monkeypatch.delenv("LLMWITNESS_SECRET_KEY", raising=False)
    generated = get_secret_key()
    assert generated == get_secret_key()
    assert len(generated) >= 16

    monkeypatch.setenv("LLMWITNESS_SECRET_KEY", "an-explicit-local-secret")
    assert get_secret_key() == "an-explicit-local-secret"


def test_short_secret_keys_are_reported(monkeypatch):
    monkeypatch.setenv("LLMWITNESS_SECRET_KEY", "short")
    assert any(
        "at least 16 characters" in error for error in LLMWitnessConfig().validate()
    )


@pytest.mark.parametrize("url", ["http://localhost:8000", "https://ingest.test"])
def test_supported_ingestion_schemes_are_accepted(monkeypatch, url):
    monkeypatch.setenv("INGESTION_SERVER_URL", url)
    assert not any("invalid scheme" in error for error in LLMWitnessConfig().validate())


def test_get_config_is_cached_after_the_first_call(monkeypatch):
    monkeypatch.setattr(config, "_global_config", None)
    monkeypatch.setenv("INGESTION_SERVER_URL", "http://first.test")
    first = get_config()

    monkeypatch.setenv("INGESTION_SERVER_URL", "http://second.test")
    assert get_config() is first
    assert get_config().ingestion_url == "http://first.test"
