import os
import secrets
from pathlib import Path

_LOCAL_SESSION_SECRET = secrets.token_hex(32)


def get_secret_key() -> str:
    """Return the configured key, the persisted local key, or a per-process one."""
    return os.getenv("LLMWITNESS_SECRET_KEY") or _LOCAL_SESSION_SECRET


def use_local_secret(secret: str) -> None:
    """Adopt a persisted HMAC secret as the fallback when the environment sets none."""
    global _LOCAL_SESSION_SECRET
    if len(secret) < 16:
        raise ValueError("local HMAC secret must be at least 16 characters")
    _LOCAL_SESSION_SECRET = secret


def _key_files_present() -> bool:
    root = Path(os.getenv("LLMWITNESS_KEY_DIR") or ".llmwitness/keys")
    return (root / "ed25519_private.pem").is_file() and (root / "hmac_secret").is_file()


class LLMWitnessConfig:
    """Type-safe configuration container with validation rules."""

    def __init__(self):
        persisted = _key_files_present()
        self.secret_key_configured = (
            bool(os.getenv("LLMWITNESS_SECRET_KEY")) or persisted
        )
        self.private_key_configured = (
            bool(os.getenv("LLMWITNESS_PRIVATE_KEY_PEM")) or persisted
        )
        self.secret_key: str = get_secret_key()
        self.ingestion_url: str = os.getenv(
            "INGESTION_SERVER_URL", "http://localhost:8000"
        )

    def validate(self) -> list[str]:
        """Validates configuration parameters and returns list of error messages (if any)."""
        errors = []
        if not self.secret_key_configured:
            errors.append(
                "LLMWITNESS_SECRET_KEY is unset; receipts will not have a durable HMAC identity. "
                "Run `llmwitness keygen` to create a persistent local key."
            )
        if not self.private_key_configured:
            errors.append(
                "LLMWITNESS_PRIVATE_KEY_PEM is unset; the Ed25519 signer changes after restart. "
                "Run `llmwitness keygen` to create a persistent local key."
            )
        if not self.secret_key:
            errors.append("LLMWITNESS_SECRET_KEY must not be empty.")
        if len(self.secret_key) < 16:
            errors.append(
                "LLMWITNESS_SECRET_KEY should be at least 16 characters for cryptographic security."
            )
        if not self.ingestion_url.startswith(("http://", "https://")):
            errors.append(
                f"INGESTION_SERVER_URL invalid scheme: '{self.ingestion_url}'. Must start with http:// or https://"
            )
        rules_file = os.getenv("LLMWITNESS_SCRUB_RULES_FILE")
        if rules_file:
            errors.extend(_scrub_rules_errors(rules_file))
        return errors


def _scrub_rules_errors(path: str) -> list[str]:
    """Check a custom scrub rules file without registering its rules."""
    import json
    import re

    try:
        with open(path, encoding="utf-8") as handle:
            rules = json.load(handle)
        if not isinstance(rules, dict):
            raise ValueError("file must contain a JSON object")
        for entry in rules.get("patterns", []):
            re.compile(entry["regex"])
    except (OSError, ValueError, KeyError, TypeError, re.error) as exc:
        return [f"LLMWITNESS_SCRUB_RULES_FILE is not usable: {exc}"]
    return []


_global_config: LLMWitnessConfig | None = None


def get_config() -> LLMWitnessConfig:
    global _global_config
    if _global_config is None:
        _global_config = LLMWitnessConfig()
    return _global_config
