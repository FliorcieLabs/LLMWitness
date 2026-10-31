"""
LLMWitness Community - Security Regression Suite
Verifies pattern scrubbing, sensitive-field handling, and receipt-signature primitives.
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from llmwitness.utils import (
    compute_hmac_signature,
    redact_payload,
)


def test_pii_scrubbing_never_leaks_tokens():
    sensitive_inputs = [
        ("sk-abcdef1234567890abcdef1234567890", "[REDACTED_API_TOKEN]"),
        ("123-45-6789", "[REDACTED_SSN]"),
        ("4111-1111-1111-1111", "[REDACTED_CREDIT_CARD]"),
        (
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "[REDACTED_API_TOKEN]",
        ),
    ]

    for secret, expected_redaction in sensitive_inputs:
        payload = {"data": secret}
        redacted = redact_payload(payload)
        assert secret not in str(redacted["data"])
        assert expected_redaction in str(redacted["data"])


def test_allow_list_preserves_only_explicit_exceptions():
    payload = {"ssn": "123-45-6789", "allowed_code": "SAFE_PROMPT_123"}
    redacted = redact_payload(payload, allow_list=["SAFE_PROMPT_123"])
    assert redacted["ssn"] == "[REDACTED_SSN]"
    assert redacted["allowed_code"] == "SAFE_PROMPT_123"


def test_hmac_changes_when_payload_is_modified():
    secret_key = "security-regression-secret-vault-key"
    payload_str = json.dumps({"events": ["event1", "event2"]}, sort_keys=True)

    sig1 = compute_hmac_signature(payload_str, secret_key)
    sig2 = compute_hmac_signature(payload_str, secret_key)

    assert sig1 == sig2

    # Modified payload produces completely different signature
    tampered_str = json.dumps(
        {"events": ["event1", "event2", "injected"]}, sort_keys=True
    )
    tampered_sig = compute_hmac_signature(tampered_str, secret_key)

    assert tampered_sig != sig1
