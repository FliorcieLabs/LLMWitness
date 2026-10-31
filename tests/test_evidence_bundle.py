import json
import sys
import zipfile
from pathlib import Path

import pytest

from llmwitness.cli import main
from llmwitness.envelope import (
    Actor,
    Authority,
    AuthorityDecision,
    ContractReference,
    EffectDescriptor,
    EffectStatus,
    EvidenceReference,
    ExecutionEnvelope,
    Intent,
    Observation,
    RiskTier,
    sha256_ref,
)
from llmwitness.evidence_bundle import (
    BUNDLE_MANIFEST_NAME,
    BUNDLE_RECORDS_NAME,
    export_evidence_bundle,
    verify_evidence_bundle,
)
from llmwitness.journal import SQLiteJournalStore
from llmwitness.utils import canonical_json, generate_uuidv7


def _envelope(
    run_id: str,
    *,
    status: EffectStatus = EffectStatus.PLANNED,
    evidence_uri: str | None = None,
) -> ExecutionEnvelope:
    evidence = (
        [
            EvidenceReference(
                kind="provider-observation",
                sha256=sha256_ref(b"provider-observation"),
                uri=evidence_uri,
            )
        ]
        if evidence_uri is not None
        else []
    )
    return ExecutionEnvelope.create(
        run_id=run_id,
        actor=Actor(agent_id="agent", principal_id="principal"),
        intent=Intent(name="export-evidence"),
        authority=Authority(
            policy_id="policy",
            decision=AuthorityDecision.ALLOW,
            risk_tier=RiskTier.R1,
        ),
        contract=ContractReference(contract_id="contract"),
        effect=EffectDescriptor(
            adapter="test",
            idempotency_key=f"test:{generate_uuidv7()}",
            parameters_hash=sha256_ref(b"parameters"),
        ),
        observation=Observation(
            status=status,
            verifier="test-verifier" if status == EffectStatus.VERIFIED else None,
        ),
        evidence=evidence,
    )


def _create_bundle(tmp_path: Path) -> tuple[Path, str]:
    run_id = generate_uuidv7()
    journal_path = tmp_path / "journal.db"
    bundle_path = tmp_path / "run.llmwitness-evidence.zip"
    with SQLiteJournalStore(journal_path) as journal:
        journal.append(_envelope(run_id), "effect.planned")
        journal.append(
            _envelope(
                run_id,
                status=EffectStatus.VERIFIED,
                evidence_uri="memory://provider-observation",
            ),
            "effect.verified",
        )
        export_evidence_bundle(journal, run_id, bundle_path)
    return bundle_path, run_id


def _rewrite_bundle(
    source: Path, destination: Path, replacements: dict[str, bytes]
) -> None:
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members.update(replacements)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_bundle_verifies_offline_after_source_journal_is_removed(tmp_path):
    bundle_path, run_id = _create_bundle(tmp_path)
    (tmp_path / "journal.db").unlink()

    result = verify_evidence_bundle(bundle_path)

    assert result.valid
    assert result.run_id == run_id
    assert result.entries_checked == 2
    assert result.head_hash.startswith("sha256:")
    assert result.evidence_references == (sha256_ref(b"provider-observation"),)


def test_bundle_manifest_is_versioned_and_does_not_expose_source_paths(tmp_path):
    bundle_path, run_id = _create_bundle(tmp_path)

    with zipfile.ZipFile(bundle_path) as archive:
        assert archive.namelist() == [BUNDLE_RECORDS_NAME, BUNDLE_MANIFEST_NAME]
        manifest = json.loads(archive.read(BUNDLE_MANIFEST_NAME))

    assert manifest["schema_version"] == "0.1"
    assert manifest["bundle_type"] == "llmwitness.local-evidence"
    assert manifest["run_id"] == run_id
    assert "journal.db" not in canonical_json(manifest)
    assert str(tmp_path) not in canonical_json(manifest)


def test_modified_records_fail_bundle_verification(tmp_path):
    bundle_path, _ = _create_bundle(tmp_path)
    with zipfile.ZipFile(bundle_path) as archive:
        records = json.loads(archive.read(BUNDLE_RECORDS_NAME))
    records["entries"][0]["event_type"] = "effect.tampered"
    tampered = tmp_path / "tampered.zip"
    _rewrite_bundle(
        bundle_path,
        tampered,
        {BUNDLE_RECORDS_NAME: canonical_json(records).encode("utf-8")},
    )

    result = verify_evidence_bundle(tampered)

    assert not result.valid
    assert "digest" in (result.error or "")


def test_archive_with_unexpected_or_traversal_member_is_rejected(tmp_path):
    bundle_path, _ = _create_bundle(tmp_path)
    unsafe = tmp_path / "unsafe.zip"
    _rewrite_bundle(bundle_path, unsafe, {"../outside": b"not extracted"})

    result = verify_evidence_bundle(unsafe)

    assert not result.valid
    assert "members" in (result.error or "")
    assert not (tmp_path.parent / "outside").exists()


def test_export_rejects_local_evidence_paths_and_never_overwrites(tmp_path):
    run_id = generate_uuidv7()
    journal_path = tmp_path / "journal.db"
    destination = tmp_path / "bundle.zip"
    with SQLiteJournalStore(journal_path) as journal:
        journal.append(
            _envelope(run_id, evidence_uri="C:/private/evidence.json"),
            "effect.planned",
        )
        with pytest.raises(ValueError, match="local filesystem path"):
            export_evidence_bundle(journal, run_id, destination)

    destination.write_bytes(b"existing")
    clean_run = generate_uuidv7()
    with SQLiteJournalStore(tmp_path / "clean.db") as journal:
        journal.append(_envelope(clean_run), "effect.planned")
        with pytest.raises(FileExistsError):
            export_evidence_bundle(journal, clean_run, destination)
    assert destination.read_bytes() == b"existing"


def test_verifier_enforces_member_size_budget_before_reading(tmp_path):
    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(oversized, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(BUNDLE_RECORDS_NAME, b"{}")
        archive.writestr(BUNDLE_MANIFEST_NAME, b"{}")

    result = verify_evidence_bundle(oversized, max_bundle_bytes=1)

    assert not result.valid
    assert "size limit" in (result.error or "")


def test_export_rejects_secret_like_journal_metadata(tmp_path):
    run_id = generate_uuidv7()
    with SQLiteJournalStore(tmp_path / "journal.db") as journal:
        journal.append(_envelope(run_id), "sk-abcdefghijklmnopqrstuvwxyz")

        with pytest.raises(ValueError, match="recognized raw secret"):
            export_evidence_bundle(journal, run_id, tmp_path / "bundle.zip")


def test_cli_exports_and_verifies_bundle(tmp_path, monkeypatch, capsys):
    run_id = generate_uuidv7()
    journal_path = tmp_path / "journal.db"
    bundle_path = tmp_path / "cli-bundle.zip"
    with SQLiteJournalStore(journal_path) as journal:
        journal.append(_envelope(run_id), "effect.planned")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llmwitness",
            "evidence-bundle",
            "export",
            "--journal",
            str(journal_path),
            "--run-id",
            run_id,
            "--output",
            str(bundle_path),
        ],
    )
    main()
    exported = json.loads(capsys.readouterr().out)
    assert exported["run_id"] == run_id

    monkeypatch.setattr(
        sys,
        "argv",
        ["llmwitness", "evidence-bundle", "verify", str(bundle_path)],
    )
    main()
    verified = json.loads(capsys.readouterr().out)
    assert verified["valid"] is True
    assert verified["run_id"] == run_id
