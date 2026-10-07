import os

import pytest

# Services started as subprocesses inherit these, so a test run never writes
# session databases or key files into the repository's working directory.
os.environ.setdefault("LLMWITNESS_SESSION_DB", "off")
os.environ.setdefault("LLMWITNESS_EPHEMERAL_KEYS", "1")
os.environ.setdefault("LLMWITNESS_SPOOL_DIR", "off")


@pytest.fixture(autouse=True)
def _isolated_local_state(monkeypatch, tmp_path_factory):
    """Keep key files and the telemetry spool out of the working directory."""
    state = tmp_path_factory.mktemp("llmwitness-state")
    monkeypatch.setenv("LLMWITNESS_KEY_DIR", str(state / "keys"))
    monkeypatch.setenv("LLMWITNESS_SPOOL_DIR", str(state / "spool"))
    monkeypatch.delenv("LLMWITNESS_TRUSTED_FINGERPRINT", raising=False)
    monkeypatch.delenv("LLMWITNESS_SCRUB_RULES_FILE", raising=False)
