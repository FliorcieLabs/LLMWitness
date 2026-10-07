import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def generate_uuidv7() -> str:
    """
    Generate an RFC 9562 compliant UUIDv7 identifier.
    Uses 48-bit millisecond timestamp + 4-bit version 7 + 12-bit rand_a + 2-bit variant + 62-bit rand_b.
    """
    ms_timestamp = int(time.time() * 1000)
    rand_a = int.from_bytes(os.urandom(2), byteorder="big") & 0x0FFF
    high = (ms_timestamp << 16) | (0x7 << 12) | rand_a
    rand_b = int.from_bytes(os.urandom(8), byteorder="big") & 0x3FFFFFFFFFFFFFFF
    low = (0x2 << 62) | rand_b
    val = (high << 64) | low
    return str(uuid.UUID(int=val))


# Regex patterns for sensitive PII data
SSN_REGEX = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CREDIT_CARD_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
API_TOKEN_REGEX = re.compile(
    r"(?i)\b(sk-[a-zA-Z0-9_-]{20,}|bearer\s+[a-zA-Z0-9._\-]{20,}|api[_-]?key[\s:=]+['\"]?[a-zA-Z0-9._\-]{16,}['\"]?)"
)

SENSITIVE_FIELD_REGEX = re.compile(
    r"(?i)^(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|client[_-]?secret)$"
)

# Patterns for Data URL base64 image strings. The inline form stops at the first
# whitespace so that prose following an embedded image is not swallowed; the
# wrapped form tolerates line breaks and is only applied to a whole string.
DATA_IMAGE_REGEX = re.compile(
    r"data:image\/[a-zA-Z0-9\+\-\.]+;base64,([A-Za-z0-9+/=]+)"
)
DATA_IMAGE_WRAPPED_REGEX = re.compile(
    r"data:image\/[a-zA-Z0-9\+\-\.]+;base64,([A-Za-z0-9+/=\r\n]+)"
)


def get_default_allow_list() -> list[str]:
    """Retrieve global PII allow-list items from environment setting."""
    env_allow = os.getenv("LLMWITNESS_PII_ALLOW_LIST", "")
    if env_allow:
        return [item.strip() for item in env_allow.split(",") if item.strip()]
    return []


def _is_raw_base64_image(data_str: str) -> int | None:
    """
    Checks if a string is a raw Base64 payload representing image data (>1000 chars).
    Returns decoded byte length if detected as image, else None.
    """
    cleaned = "".join(data_str.split())
    if len(cleaned) < 1000:
        return None
    if not re.match(r"^[A-Za-z0-9+/=]+$", cleaned):
        return None
    try:
        raw_bytes = base64.b64decode(cleaned, validate=True)
        is_image_header = (
            raw_bytes.startswith(b"\x89PNG")
            or raw_bytes.startswith(b"\xff\xd8\xff")
            or raw_bytes.startswith(b"GIF8")
            or raw_bytes.startswith(b"RIFF")
            or b"<svg" in raw_bytes[:100].lower()
        )
        if is_image_header:
            return len(raw_bytes)
    except (ValueError, TypeError, binascii.Error):
        return None
    return None


def redact_payload(
    data: Any, allow_list: list[str] | None = None, _depth: int = 0
) -> Any:
    """
    Deep-JSON & Multi-Modal PII Redaction Engine.
    Recursively traverses dicts, lists, primitives, and JSON strings.
    Redacts SSNs, Credit Cards, API Tokens (sk-...), and Base64 vision payload images.
    """
    if _depth > 32:
        return "[REDACTION_DEPTH_LIMIT]"
    effective_allow = (allow_list or []) + get_default_allow_list()

    if isinstance(data, dict):
        return {
            k: (
                "[REDACTED_SENSITIVE_FIELD]"
                if isinstance(k, str) and SENSITIVE_FIELD_REGEX.fullmatch(k)
                else redact_payload(v, allow_list=effective_allow, _depth=_depth + 1)
            )
            for k, v in data.items()
        }

    elif isinstance(data, (list, tuple, set, frozenset)):
        # Normalised to a list because JSON has a single array form; an
        # unhandled sequence type would otherwise reach the wire unredacted.
        return [
            redact_payload(item, allow_list=effective_allow, _depth=_depth + 1)
            for item in data
        ]

    elif isinstance(data, str):
        stripped = data.strip()

        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                parsed_json = json.loads(stripped)
                if isinstance(parsed_json, (dict, list)):
                    redacted_obj = redact_payload(
                        parsed_json, allow_list=effective_allow, _depth=_depth + 1
                    )
                    return json.dumps(redacted_obj)
            except (json.JSONDecodeError, RecursionError):
                pass

        def replace_data_image(match: re.Match) -> str:
            b64_str = match.group(1)
            try:
                b64_clean = "".join(b64_str.split())
                raw_bytes = base64.b64decode(b64_clean)
                byte_count = len(raw_bytes)
            except Exception:
                byte_count = len(b64_str)
            return f"[REDACTED_IMAGE_PAYLOAD_SIZE_{byte_count}_BYTES]"

        whole_image = DATA_IMAGE_WRAPPED_REGEX.fullmatch(data)
        if whole_image is not None:
            try:
                raw_bytes = base64.b64decode("".join(whole_image.group(1).split()))
                return f"[REDACTED_IMAGE_PAYLOAD_SIZE_{len(raw_bytes)}_BYTES]"
            except (ValueError, TypeError, binascii.Error):
                # Whitespace here separates prose rather than wrapped base64, so
                # fall through and redact only the contiguous payload below.
                pass
        if DATA_IMAGE_REGEX.search(data):
            data = DATA_IMAGE_REGEX.sub(replace_data_image, data)

        b64_byte_count = _is_raw_base64_image(data)
        if b64_byte_count is not None:
            return f"[REDACTED_IMAGE_PAYLOAD_SIZE_{b64_byte_count}_BYTES]"

        placeholders = {}
        processed_text = data
        # The nonce stops attacker-supplied text that already contains a
        # placeholder token from being rewritten into an allow-listed term.
        nonce = secrets.token_hex(8) if effective_allow else ""
        for idx, allowed_term in enumerate(effective_allow):
            if allowed_term in processed_text:
                ph_token = f"__LLMWITNESS_ALLOW_PH_{nonce}_{idx}__"
                placeholders[ph_token] = allowed_term
                processed_text = processed_text.replace(allowed_term, ph_token)

        text = SSN_REGEX.sub("[REDACTED_SSN]", processed_text)
        text = CREDIT_CARD_REGEX.sub("[REDACTED_CREDIT_CARD]", text)
        text = API_TOKEN_REGEX.sub("[REDACTED_API_TOKEN]", text)

        for ph_token, orig_val in placeholders.items():
            text = text.replace(ph_token, orig_val)

        return text

    return data


def redact_pii(
    content: str | dict[str, Any] | list[Any],
    allow_list: list[str] | None = None,
) -> str | dict[str, Any] | list[Any]:
    """
    Backward-compatible wrapper around deep redact_payload.
    """
    return redact_payload(content, allow_list=allow_list)


class Ed25519KeyManager:
    """
    Manages Ed25519 asymmetric key pairs, environment PEM loading,
    digital signing, and SHA-256 public key fingerprint generation.
    """

    def __init__(
        self,
        private_key_pem: str | bytes | None = None,
        public_key_pem: str | bytes | None = None,
    ):
        if not private_key_pem:
            private_key_pem = os.getenv("LLMWITNESS_PRIVATE_KEY_PEM")
        if not public_key_pem:
            public_key_pem = os.getenv("LLMWITNESS_PUBLIC_KEY_PEM")

        self.private_key: ed25519.Ed25519PrivateKey | None = None
        self.public_key: ed25519.Ed25519PublicKey | None = None

        if private_key_pem:
            pem_bytes = (
                private_key_pem.encode("utf-8")
                if isinstance(private_key_pem, str)
                else private_key_pem
            )
            loaded_private = serialization.load_pem_private_key(
                pem_bytes, password=None
            )
            if not isinstance(loaded_private, ed25519.Ed25519PrivateKey):
                raise ValueError("Configured private key is not an Ed25519 key")
            self.private_key = loaded_private
            self.public_key = self.private_key.public_key()
        elif public_key_pem:
            pem_bytes = (
                public_key_pem.encode("utf-8")
                if isinstance(public_key_pem, str)
                else public_key_pem
            )
            loaded_public = serialization.load_pem_public_key(pem_bytes)
            if not isinstance(loaded_public, ed25519.Ed25519PublicKey):
                raise ValueError("Configured public key is not an Ed25519 key")
            self.public_key = loaded_public
        else:
            self.private_key = ed25519.Ed25519PrivateKey.generate()
            self.public_key = self.private_key.public_key()

    def export_private_key_pem(self) -> str:
        if not self.private_key:
            raise ValueError(
                "Private key not loaded in this Ed25519KeyManager instance"
            )
        pem_bytes = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return pem_bytes.decode("utf-8")

    def export_public_key_pem(self) -> str:
        if not self.public_key:
            raise ValueError("Public key not initialized")
        pem_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return pem_bytes.decode("utf-8")

    def get_public_key_fingerprint(self) -> str:
        """
        Computes SHA-256 fingerprint hash of the Ed25519 public key bytes.
        """
        if not self.public_key:
            raise ValueError("Public key not initialized")
        raw_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return hashlib.sha256(raw_bytes).hexdigest()

    def sign(self, payload: str | bytes) -> str:
        """
        Signs string or byte payload using Ed25519 private key.
        Returns Base64-encoded signature.
        """
        if not self.private_key:
            raise ValueError("Cannot sign without a private key")
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        sig_bytes = self.private_key.sign(payload_bytes)
        return base64.b64encode(sig_bytes).decode("utf-8")

    def verify(self, payload: str | bytes, signature_b64: str) -> bool:
        """
        Verifies Base64 signature against payload using public key.
        """
        if not self.public_key:
            return False
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        try:
            sig_bytes = base64.b64decode(signature_b64)
            self.public_key.verify(sig_bytes, payload_bytes)
            return True
        except (InvalidSignature, Exception):
            return False


def compute_hmac_signature(data_string: str, secret_key: str) -> str:
    """
    Computes deterministic HMAC-SHA256 signature for data payload.
    """
    return hmac.new(
        secret_key.encode("utf-8"), data_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def canonical_json(data: Any) -> str:
    """Serialize signed data deterministically without insignificant whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def verify_proof_receipt(proof_file_path: str, secret_key: str | None = None) -> bool:
    """
    Verify a local tamper-evident receipt.

    Ed25519 verification is self-contained because the receipt carries the public
    key used to sign it. If ``secret_key`` is supplied, the optional HMAC is also
    checked. The embedded key proves consistency with that key, not the identity
    of the signer; callers must compare its fingerprint with a trusted value.
    """
    if not os.path.exists(proof_file_path):
        return False

    secret_key = secret_key or os.getenv("LLMWITNESS_SECRET_KEY")

    try:
        with open(proof_file_path, encoding="utf-8") as f:
            receipt = json.load(f)

        stored_signature = receipt.get("ed25519_signature")
        public_key_pem = receipt.get("public_key_pem")
        stored_fingerprint = receipt.get("public_key_fingerprint")
        if not all((stored_signature, public_key_pem, stored_fingerprint)):
            return False

        # Re-construct deterministic event payload string
        payload_dict = {
            "correlation_id": receipt.get("correlation_id"),
            "sealed_at": receipt.get("sealed_at"),
            "events": receipt.get("events"),
        }
        reconstructed_string = canonical_json(payload_dict)
        verifier = Ed25519KeyManager(public_key_pem=public_key_pem)
        if verifier.get_public_key_fingerprint() != stored_fingerprint:
            return False
        if not verifier.verify(reconstructed_string, stored_signature):
            return False

        stored_hmac = receipt.get("hmac_signature")
        if secret_key is not None:
            if not stored_hmac:
                return False
            computed_hmac = compute_hmac_signature(reconstructed_string, secret_key)
            if not hmac.compare_digest(computed_hmac, stored_hmac):
                return False
        return True
    except Exception:
        return False
