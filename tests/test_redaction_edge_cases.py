"""Boundary behaviour of the deep redaction engine."""

import base64
import json

import pytest

from llmwitness.utils import redact_payload, redact_pii


def test_scalar_and_unknown_types_pass_through_unchanged():
    assert redact_payload(None) is None
    assert redact_payload(42) == 42
    assert redact_payload(3.5) == 3.5
    assert redact_payload(True) is True
    assert redact_payload("") == ""
    assert redact_payload("   ") == "   "


def test_sequence_types_are_normalised_and_redacted():
    """Tuples and sets serialize as JSON arrays, so they must be scrubbed too."""
    payload = {
        "tuple": ("my ssn is 123-45-6789",),
        "set": {"my ssn is 123-45-6789"},
        "frozen": frozenset({"my ssn is 123-45-6789"}),
        "list": ["my ssn is 123-45-6789"],
    }
    redacted = redact_payload(payload)
    assert "123-45-6789" not in json.dumps(redacted)
    for key in ("tuple", "set", "frozen", "list"):
        assert redacted[key] == ["my ssn is [REDACTED_SSN]"]


def test_non_string_dict_keys_are_preserved_while_values_are_redacted():
    redacted = redact_payload({1: "123-45-6789", None: "sk-" + "a" * 20})
    assert redacted[1] == "[REDACTED_SSN]"
    assert redacted[None] == "[REDACTED_API_TOKEN]"


def test_sensitive_field_names_are_matched_whole_and_case_insensitively():
    redacted = redact_payload(
        {
            "Authorization": "anything",
            "api_key": "anything",
            "CLIENT-SECRET": "anything",
            "secretive": "not-a-sensitive-field",
            "x-authorization": "not-a-sensitive-field",
        }
    )
    assert redacted["Authorization"] == "[REDACTED_SENSITIVE_FIELD]"
    assert redacted["api_key"] == "[REDACTED_SENSITIVE_FIELD]"
    assert redacted["CLIENT-SECRET"] == "[REDACTED_SENSITIVE_FIELD]"
    assert redacted["secretive"] == "not-a-sensitive-field"
    assert redacted["x-authorization"] == "not-a-sensitive-field"


def test_recursion_depth_limit_stops_runaway_nesting():
    root: dict = {}
    cursor = root
    for _ in range(40):
        cursor["nested"] = {}
        cursor = cursor["nested"]
    cursor["ssn"] = "123-45-6789"

    serialized = json.dumps(redact_payload(root))
    assert "[REDACTION_DEPTH_LIMIT]" in serialized
    assert "123-45-6789" not in serialized


def test_embedded_json_strings_are_redacted_and_stay_strings():
    redacted = redact_payload('{"ssn": "123-45-6789"}')
    assert isinstance(redacted, str)
    assert json.loads(redacted) == {"ssn": "[REDACTED_SSN]"}


def test_malformed_json_shaped_strings_fall_back_to_text_scrubbing():
    """A truncated object still looks like JSON but must not leak on parse failure."""
    redacted = redact_payload('{"ssn": "123-45-6789"')
    assert "123-45-6789" not in redacted
    assert "[REDACTED_SSN]" in redacted


@pytest.mark.parametrize(
    "header",
    [b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xe0", b"GIF89a", b"RIFF____WEBP", b"<svg "],
)
def test_raw_base64_image_payloads_are_summarised_by_size(header):
    raw = header + b"A" * 2000
    encoded = base64.b64encode(raw).decode()
    assert redact_payload(encoded) == (
        f"[REDACTED_IMAGE_PAYLOAD_SIZE_{len(raw)}_BYTES]"
    )


def test_base64_that_is_short_or_not_an_image_is_left_alone():
    short = base64.b64encode(b"hello").decode()
    non_image = base64.b64encode(b"Z" * 2000).decode()
    assert redact_payload(short) == short
    assert redact_payload(non_image) == non_image


def test_data_url_images_are_redacted_whole_and_inline():
    raw = b"\x89PNG\r\n\x1a\n" + b"B" * 1500
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    expected = f"[REDACTED_IMAGE_PAYLOAD_SIZE_{len(raw)}_BYTES]"

    assert redact_payload(data_url) == expected
    inline = redact_payload(f"before {data_url} after")
    assert inline == f"before {expected} after"


def test_line_wrapped_data_url_is_redacted_as_a_whole_string():
    raw = b"\xff\xd8\xff\xe0" + b"C" * 1200
    encoded = base64.b64encode(raw).decode()
    wrapped = "data:image/jpeg;base64," + "\n".join(
        encoded[index : index + 76] for index in range(0, len(encoded), 76)
    )
    assert redact_payload(wrapped) == f"[REDACTED_IMAGE_PAYLOAD_SIZE_{len(raw)}_BYTES]"


def test_data_url_with_undecodable_base64_is_not_treated_as_an_image():
    assert redact_payload("data:image/png;base64,!!!!") == "data:image/png;base64,!!!!"


def test_prose_after_a_leading_data_url_is_not_swallowed():
    """Whitespace continues a payload only when the joined result still decodes."""
    redacted = redact_payload("data:image/png;base64,AAAAA hello")
    assert redacted == "[REDACTED_IMAGE_PAYLOAD_SIZE_5_BYTES] hello"


def test_valid_base64_characters_in_trailing_prose_are_not_swallowed():
    """Spaces delimit prose even when the following word is valid Base64 text."""
    redacted = redact_payload("data:image/png;base64,AAAA test")
    assert redacted == "[REDACTED_IMAGE_PAYLOAD_SIZE_3_BYTES] test"


def test_long_strings_that_are_not_valid_base64_are_left_alone():
    with_symbol = "A" * 900 + "!" + "A" * 200
    bad_padding = "A" * 1001
    assert redact_payload(with_symbol) == with_symbol
    assert redact_payload(bad_padding) == bad_padding


def test_allow_list_terms_survive_redaction_of_the_same_string():
    redacted = redact_payload("Nick sent 123-45-6789", allow_list=["Nick"])
    assert redacted == "Nick sent [REDACTED_SSN]"


def test_allow_list_placeholder_tokens_in_input_are_not_rewritten():
    """Model output must never be able to forge an allow-list placeholder."""
    forged = "__LLMWITNESS_ALLOW_PH_0__"
    redacted = redact_payload(f"Nick {forged}", allow_list=["Nick"])
    assert redacted == f"Nick {forged}"


def test_environment_allow_list_is_merged_with_the_explicit_one(monkeypatch):
    monkeypatch.setenv("LLMWITNESS_PII_ALLOW_LIST", " Ada , Grace ,, ")
    redacted = redact_payload("Ada Grace Nick 123-45-6789", allow_list=["Nick"])
    assert redacted == "Ada Grace Nick [REDACTED_SSN]"


def test_redaction_is_idempotent_and_stable_across_repeated_passes():
    original = {"note": "ssn 123-45-6789 token sk-" + "a" * 24}
    once = redact_payload(original)
    assert redact_payload(once) == once


def test_redact_pii_wrapper_matches_the_deep_engine():
    payload = {"messages": [{"content": "123-45-6789"}]}
    assert redact_pii(payload) == redact_payload(payload)
