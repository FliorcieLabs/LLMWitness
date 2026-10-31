"""Regression tests for artifact and receipt module ownership."""

from __future__ import annotations

import ast
from pathlib import Path

import llmwitness
import llmwitness.artifacts as artifacts
import llmwitness.journal as journal
import llmwitness.receipts as receipts
from llmwitness.utils import Ed25519KeyManager


def test_journal_compatibility_exports_are_owned_by_narrow_modules():
    assert journal.FileArtifactStore is artifacts.FileArtifactStore
    assert journal.ReceiptService is receipts.ReceiptService
    assert journal.SignedJournalHead is receipts.SignedJournalHead
    assert llmwitness.FileArtifactStore is artifacts.FileArtifactStore

    assert artifacts.FileArtifactStore.__module__ == "llmwitness.artifacts"
    assert receipts.ReceiptService.__module__ == "llmwitness.receipts"
    assert receipts.SignedJournalHead.__module__ == "llmwitness.receipts"


def test_journal_module_does_not_reclaim_artifact_or_receipt_classes():
    source = Path(journal.__file__).read_text(encoding="utf-8")
    class_names = {
        node.name for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
    }
    assert "FileArtifactStore" not in class_names
    assert "ReceiptService" not in class_names
    assert "SignedJournalHead" not in class_names


def test_receipt_service_depends_on_signer_port():
    expected = receipts.SignedJournalHead(
        run_id="run-1",
        run_sequence=2,
        head_hash="sha256:head",
        key_id="local-key",
        public_key_fingerprint="sha256:fingerprint",
        signed_at="2026-09-08T00:00:00+00:00",
        signature="signature",
    )

    class FakeSigner:
        def sign_head(self, run_id, manager, key_id):
            assert run_id == "run-1"
            assert isinstance(manager, Ed25519KeyManager)
            assert key_id == "local-key"
            return expected

    service = receipts.ReceiptService(Ed25519KeyManager(), "local-key")
    assert service.issue(FakeSigner(), "run-1") is expected
