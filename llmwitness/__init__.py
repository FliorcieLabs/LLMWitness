"""
LLMWitness Community: local-first telemetry for AI-agent runs.
"""

__version__ = "0.1.0"
__author__ = "LLMWitness maintainers"
__license__ = "Apache-2.0"

from llmwitness.artifacts import FileArtifactStore
from llmwitness.envelope import ExecutionEnvelope
from llmwitness.journal import SQLiteJournalStore
from llmwitness.sdk import LLMWitnessTracker, get_current_correlation_id, trace_session
from llmwitness.utils import (
    Ed25519KeyManager,
    compute_hmac_signature,
    generate_uuidv7,
    redact_payload,
    redact_pii,
    verify_proof_receipt,
)

__all__ = [
    "__version__",
    "__license__",
    "LLMWitnessTracker",
    "get_current_correlation_id",
    "trace_session",
    "redact_pii",
    "redact_payload",
    "generate_uuidv7",
    "verify_proof_receipt",
    "Ed25519KeyManager",
    "compute_hmac_signature",
    "ExecutionEnvelope",
    "SQLiteJournalStore",
    "FileArtifactStore",
]
