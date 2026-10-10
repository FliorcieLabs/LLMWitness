"""Local content-addressed artifact storage."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from llmwitness.envelope import sha256_ref


class FileArtifactStore:
    """Local content-addressed blobs with atomic publication."""

    def __init__(self, root: str | os.PathLike[str], max_bytes: int = 10 * 1024 * 1024):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> tuple[str, Path]:
        if len(content) > self.max_bytes:
            raise ValueError("artifact exceeds configured size limit")
        reference = sha256_ref(content)
        digest = reference.removeprefix("sha256:")
        destination = self.root / digest[:2] / digest[2:]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != content:
                raise RuntimeError("content-address collision")
            return reference, destination
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent, prefix=".artifact-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, destination)
            except FileExistsError:
                if destination.read_bytes() != content:
                    raise RuntimeError("content-address collision") from None
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return reference, destination

    def get(self, reference: str) -> bytes:
        digest = reference.removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid artifact reference")
        content = (self.root / digest[:2] / digest[2:]).read_bytes()
        if sha256_ref(content) != reference:
            raise RuntimeError("artifact integrity check failed")
        return content


__all__ = ["FileArtifactStore"]
