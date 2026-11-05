"""Deterministic, minimized projections of supplied local reports, not verification."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .utils import normalize_uuidv7

MAX_REPORT_BYTES = 1024 * 1024
REPORT_KINDS = ("reliability", "adapters", "bundle", "authority")
LIMITATIONS = (
    "Supplied reports are not authenticated. This card is a local summary, "
    "not a verifier, trust decision, external-truth proof, compliance claim, "
    "or universal score. Missing checks remain not_checked."
)


class ProofCardError(ValueError):
    """Content-free error for malformed or unsupported report input."""


def _reject(_: Any) -> Any:
    raise ProofCardError("invalid report JSON")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject(None)
        result[key] = value
    return result


def _float(value: str) -> float:
    number = float(value)
    return number if math.isfinite(number) else _reject(None)


def _decode(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_REPORT_BYTES:
        raise ProofCardError("report exceeds input bounds")
    depth = 0
    quoted = escaped = False
    for character in data:
        if quoted:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                quoted = False
        elif character == 34:
            quoted = True
        elif character in (91, 123):
            depth += 1
            if depth > 64:
                raise ProofCardError("report exceeds nesting bounds")
        elif character in (93, 125):
            depth -= 1
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject,
            parse_float=_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ProofCardError("invalid report JSON") from None
    if not isinstance(value, dict):
        raise ProofCardError("report must be an object")
    return value


def _count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 100_000:
        raise ProofCardError("invalid report count")
    return value


def _summary(kind: str, report: dict[str, Any]) -> dict[str, Any]:
    if kind in ("reliability", "adapters"):
        if report.get("schema_version") != "0.1":
            raise ProofCardError("unsupported report version")
        total, passed, failed = (
            _count(report.get(key)) for key in ("sample_count", "passed", "failed")
        )
        rows = report.get("dimensions" if kind == "reliability" else "results")
        if not isinstance(rows, list) or len(rows) != total or passed + failed != total:
            raise ProofCardError("inconsistent report counts")
        if any(
            not isinstance(row, dict) or type(row.get("passed")) is not bool
            for row in rows
        ):
            raise ProofCardError("invalid report outcomes")
        if sum(row["passed"] for row in rows) != passed:
            raise ProofCardError("inconsistent report outcomes")
        return {
            "status": "failed" if failed else ("passed" if total else "not_checked"),
            "total": total,
            "passed": passed,
            "failed": failed,
        }
    if kind == "bundle":
        valid = report.get("valid")
        if type(valid) is not bool:
            raise ProofCardError("invalid bundle outcome")
        checked = _count(report.get("entries_checked"))
        if valid and checked == 0:
            raise ProofCardError("invalid empty verified bundle")
        if valid:
            try:
                normalize_uuidv7(report.get("run_id"))
            except (ValueError, TypeError, AttributeError):
                raise ProofCardError("invalid bundle run identifier") from None
            head = report.get("head_hash")
            if not isinstance(head, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", head
            ):
                raise ProofCardError("invalid bundle head reference")
        return {"status": "passed" if valid else "failed", "entries_checked": checked}
    if report.get("schema_version") != "0.1" or report.get("status") not in (
        "allowed",
        "denied",
        "unknown",
        "not_checked",
    ):
        raise ProofCardError("invalid minimized authority summary")
    return {"status": report["status"]}


def build_proof_card(reports: Mapping[str, bytes]) -> dict[str, Any]:
    """Summarize declarations; never run checks or promote them to authority."""
    if not reports or set(reports) - set(REPORT_KINDS):
        raise ProofCardError("at least one supported report is required")
    checks = {}
    for kind in REPORT_KINDS:
        if kind not in reports:
            checks[kind] = {"status": "not_checked"}
            continue
        data = reports[kind]
        checks[kind] = {
            **_summary(kind, _decode(data)),
            "source_ref": "sha256:" + hashlib.sha256(data).hexdigest(),
        }
    return {"schema_version": "0.1", "checks": checks, "limitations": LIMITATIONS}


def render_proof_card(reports: Mapping[str, bytes], *, format: str = "markdown") -> str:
    """Render only validated fixed labels, counts and digest references."""
    card = build_proof_card(reports)
    if format == "json":
        return json.dumps(card, sort_keys=True, indent=2) + "\n"
    if format != "markdown":
        raise ProofCardError("unsupported card format")
    lines = [
        "# Local Proof Card",
        "",
        LIMITATIONS,
        "",
        "| Supplied report | Declared status | Counts | Source reference |",
        "| --- | --- | --- | --- |",
    ]
    for kind, check in card["checks"].items():
        counts = (
            ", ".join(
                f"{key}={check[key]}"
                for key in ("total", "passed", "failed", "entries_checked")
                if key in check
            )
            or "—"
        )
        lines.append(
            f"| {kind} | {check['status']} | {counts} | {check.get('source_ref', '—')} |"
        )
    return "\n".join(lines) + "\n"
