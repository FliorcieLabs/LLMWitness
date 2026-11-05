"""Optional RFC 3161 trusted timestamps for receipts.

``sealed_at`` is asserted by the machine that signs a receipt. A timestamp
token from an external time-stamping authority (TSA) is independent evidence
that the receipt existed at that time. Requesting one sends only a SHA-256
digest to the TSA you choose; no receipt content leaves the machine.

This module requests and stores the token. It does not validate the TSA's
signature; use ``openssl ts -verify`` with the TSA's certificate for that.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from llmwitness.receipt_tools import load_receipt
from llmwitness.utils import receipt_digest

_SHA256_ALGORITHM = bytes.fromhex("300d06096086480165030402010500")
_GRANTED_STATUSES = {0, 1}  # granted, grantedWithMods


def build_timestamp_request(digest: bytes, nonce: bytes | None = None) -> bytes:
    """DER-encode a ``TimeStampReq`` for a SHA-256 digest, asking for the TSA cert."""
    if len(digest) != 32:
        raise ValueError("digest must be a 32-byte SHA-256 value")
    nonce = os.urandom(8) if nonce is None else nonce
    if len(nonce) != 8:
        raise ValueError("nonce must be 8 bytes")
    # Keep the INTEGER positive and minimally encoded.
    nonce = bytes([(nonce[0] & 0x7F) | 0x40]) + nonce[1:]
    imprint_body = _SHA256_ALGORITHM + b"\x04\x20" + digest
    body = (
        b"\x02\x01\x01"
        + b"\x30"
        + bytes([len(imprint_body)])
        + imprint_body
        + b"\x02\x08"
        + nonce
        + b"\x01\x01\xff"
    )
    return b"\x30" + bytes([len(body)]) + body


def _read_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Return (tag, value, next offset) for the DER element at ``offset``."""
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        size = length & 0x7F
        if size == 0 or size > 4:
            raise ValueError("unsupported DER length")
        length = int.from_bytes(data[offset : offset + size], "big")
        offset += size
    end = offset + length
    if end > len(data):
        raise ValueError("truncated DER element")
    return tag, data[offset:end], end


def response_status(response: bytes) -> int:
    """Extract ``PKIStatus`` from a DER ``TimeStampResp``."""
    try:
        tag, outer, _ = _read_tlv(response, 0)
        if tag != 0x30:
            raise ValueError("not a DER sequence")
        tag, status_info, _ = _read_tlv(outer, 0)
        if tag != 0x30:
            raise ValueError("missing PKIStatusInfo")
        tag, status, _ = _read_tlv(status_info, 0)
        if tag != 0x02 or not status:
            raise ValueError("missing PKIStatus")
    except IndexError as exc:
        raise ValueError("truncated timestamp response") from exc
    return int.from_bytes(status, "big", signed=True)


def request_timestamp(
    receipt_path: str | os.PathLike[str],
    tsa_url: str,
    client: httpx.Client | None = None,
) -> tuple[Path, str]:
    """Ask ``tsa_url`` to timestamp a receipt; returns the token path and digest hex.

    The response is stored next to the receipt as ``<receipt>.tsr`` and never
    replaces an existing token.
    """
    path = Path(receipt_path)
    digest = receipt_digest(load_receipt(path))
    token_path = path.with_name(path.name + ".tsr")
    if token_path.exists():
        raise FileExistsError(f"{token_path} already exists")
    owned = client is None
    active = client or httpx.Client(timeout=20.0)
    try:
        response = active.post(
            tsa_url,
            content=build_timestamp_request(digest),
            headers={"Content-Type": "application/timestamp-query"},
        )
        response.raise_for_status()
    finally:
        if owned:
            active.close()
    status = response_status(response.content)
    if status not in _GRANTED_STATUSES:
        raise ValueError(
            f"time-stamping authority refused the request (status {status})"
        )
    with token_path.open("xb") as handle:
        handle.write(response.content)
    return token_path, digest.hex()
