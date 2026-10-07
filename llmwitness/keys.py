"""Persistent local signing identity for the ingestion service.

Without a persisted key the Ed25519 signer changes on every restart, so a
receipt only proves it matches whatever key it carries. Keeping one key on disk
lets ``llmwitness verify`` compare a receipt's signer with a known fingerprint.
A local key file is still an ordinary file: anyone who can read it can sign.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from llmwitness.utils import Ed25519KeyManager

DEFAULT_KEY_DIR = ".llmwitness/keys"
PRIVATE_KEY_FILE = "ed25519_private.pem"
PUBLIC_KEY_FILE = "ed25519_public.pem"
HMAC_KEY_FILE = "hmac_secret"


@dataclass(frozen=True)
class LocalIdentity:
    """Signing material loaded from, or created in, one key directory."""

    key_manager: Ed25519KeyManager
    hmac_secret: str
    directory: Path
    created: bool

    @property
    def fingerprint(self) -> str:
        return self.key_manager.get_public_key_fingerprint()


def key_directory(directory: str | os.PathLike[str] | None = None) -> Path:
    return Path(directory or os.getenv("LLMWITNESS_KEY_DIR") or DEFAULT_KEY_DIR)


def _write_private_file(path: Path, content: str) -> None:
    """Create an owner-only file and refuse to replace an existing one."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def load_identity(
    directory: str | os.PathLike[str] | None = None,
) -> LocalIdentity | None:
    """Return the persisted identity, or ``None`` when no key has been created."""
    root = key_directory(directory)
    private_path = root / PRIVATE_KEY_FILE
    secret_path = root / HMAC_KEY_FILE
    if not private_path.is_file() or not secret_path.is_file():
        return None
    manager = Ed25519KeyManager(
        private_key_pem=private_path.read_text(encoding="utf-8")
    )
    secret = secret_path.read_text(encoding="utf-8").strip()
    if len(secret) < 16:
        raise ValueError(f"{secret_path} does not contain a usable HMAC secret")
    return LocalIdentity(manager, secret, root, created=False)


def create_identity(
    directory: str | os.PathLike[str] | None = None,
) -> LocalIdentity:
    """Generate and persist a new identity; never overwrites existing key files."""
    root = key_directory(directory)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    for name in (PRIVATE_KEY_FILE, PUBLIC_KEY_FILE, HMAC_KEY_FILE):
        if (root / name).exists():
            raise FileExistsError(f"{root / name} already exists")
    # A fresh key pair, deliberately ignoring any key supplied through the environment.
    fresh = Ed25519KeyManager(
        private_key_pem=_generate_private_pem(),
    )
    secret = secrets.token_hex(32)
    _write_private_file(root / PRIVATE_KEY_FILE, fresh.export_private_key_pem())
    _write_private_file(root / HMAC_KEY_FILE, secret + "\n")
    (root / PUBLIC_KEY_FILE).write_text(fresh.export_public_key_pem(), encoding="utf-8")
    return LocalIdentity(fresh, secret, root, created=True)


def _generate_private_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode("utf-8")
    )


def load_or_create_identity(
    directory: str | os.PathLike[str] | None = None,
) -> LocalIdentity:
    existing = load_identity(directory)
    if existing is not None:
        return existing
    try:
        return create_identity(directory)
    except FileExistsError:
        # Another process created the key between the check and the write.
        raced = load_identity(directory)
        if raced is not None:
            return raced
        raise RuntimeError(
            f"{key_directory(directory)} holds an incomplete key set; restore the "
            "missing file or move the directory away and run `llmwitness keygen`"
        ) from None


def local_trusted_fingerprint(
    directory: str | os.PathLike[str] | None = None,
) -> str | None:
    """Fingerprint of the locally persisted public key, if one exists."""
    public_path = key_directory(directory) / PUBLIC_KEY_FILE
    if not public_path.is_file():
        return None
    try:
        manager = Ed25519KeyManager(
            public_key_pem=public_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return manager.get_public_key_fingerprint()
