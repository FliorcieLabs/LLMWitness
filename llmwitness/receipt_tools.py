"""Read-only inspection of local receipt files: list, show, diff and chain checks."""

from __future__ import annotations

import datetime
import difflib
import html
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llmwitness.utils import (
    CHAINED_RECEIPT_VERSION,
    as_mapping,
    receipt_hash,
    verify_proof_receipt,
)

DEFAULT_RECEIPT_DIR = ".llmwitness/receipts"


def receipt_directory(directory: str | os.PathLike[str] | None = None) -> Path:
    return Path(directory or os.getenv("LLMWITNESS_RECEIPT_DIR") or DEFAULT_RECEIPT_DIR)


def load_receipt(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        receipt = json.load(handle)
    if not isinstance(receipt, dict):
        raise ValueError(f"{path} does not contain a receipt object")
    return receipt


def resolve_receipt(
    reference: str, directory: str | os.PathLike[str] | None = None
) -> Path:
    """Accept either a receipt path or a correlation ID in the receipt directory."""
    candidate = Path(reference)
    if candidate.is_file():
        return candidate
    try:
        correlation_id = str(uuid.UUID(reference))
    except ValueError:
        raise FileNotFoundError(f"Receipt file not found: {reference}") from None
    by_id = receipt_directory(directory) / f"{correlation_id}.json"
    if not by_id.is_file():
        raise FileNotFoundError(f"No receipt for {correlation_id} in {by_id.parent}")
    return by_id


@dataclass(frozen=True)
class ReceiptSummary:
    path: Path
    correlation_id: str
    sealed_at: str
    sdk_events: int
    gateway_events: int
    extension_events: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float | None
    fingerprint: str
    chain_index: int | None
    signature_valid: bool

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "path": str(self.path)}


def _events(receipt: dict[str, Any], stream: str) -> list[dict[str, Any]]:
    events = (receipt.get("events") or {}).get(stream) or []
    return [event for event in events if isinstance(event, dict)]


def _number(value: Any) -> float:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


def summarize(path: Path, receipt: dict[str, Any]) -> ReceiptSummary:
    sdk = _events(receipt, "sdk")
    costs = [event.get("estimated_cost_usd") for event in sdk]
    known_costs = [_number(cost) for cost in costs if isinstance(cost, (int, float))]
    chain = as_mapping(receipt.get("chain"))
    index = chain.get("index")
    return ReceiptSummary(
        path=path,
        correlation_id=str(receipt.get("correlation_id", "")),
        sealed_at=str(receipt.get("sealed_at", "")),
        sdk_events=len(sdk),
        gateway_events=len(_events(receipt, "gateway")),
        extension_events=len(_events(receipt, "extension")),
        prompt_tokens=int(sum(_number(event.get("prompt_tokens")) for event in sdk)),
        completion_tokens=int(
            sum(_number(event.get("completion_tokens")) for event in sdk)
        ),
        estimated_cost_usd=round(sum(known_costs), 8) if known_costs else None,
        fingerprint=str(receipt.get("public_key_fingerprint", "")),
        chain_index=index if isinstance(index, int) else None,
        signature_valid=verify_proof_receipt(str(path)),
    )


def list_receipts(
    directory: str | os.PathLike[str] | None = None,
) -> list[ReceiptSummary]:
    """Summaries of every readable receipt, newest first."""
    summaries: list[ReceiptSummary] = []
    root = receipt_directory(directory)
    if not root.is_dir():
        return summaries
    for path in sorted(root.glob("*.json")):
        try:
            summaries.append(summarize(path, load_receipt(path)))
        except (OSError, ValueError):
            continue
    summaries.sort(key=lambda item: item.sealed_at, reverse=True)
    return summaries


@dataclass(frozen=True)
class TimelineEntry:
    timestamp: float
    source: str
    title: str
    lines: tuple[str, ...] = field(default_factory=tuple)


def _short(text: Any, limit: int = 400) -> str:
    value = (
        text
        if isinstance(text, str)
        else json.dumps(text, ensure_ascii=False, default=str)
    )
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _tool_names(event: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for call in event.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = as_mapping(call.get("function"))
        name = function.get("name") or call.get("name")
        if name:
            names.append(str(name))
    return names


def timeline(receipt: dict[str, Any]) -> list[TimelineEntry]:
    """Merge the three event streams of one run into time order."""
    entries: list[TimelineEntry] = []
    for event in _events(receipt, "sdk"):
        lines: list[str] = []
        if event.get("input_messages"):
            last = event["input_messages"][-1]
            content = last.get("content") if isinstance(last, dict) else last
            lines.append(f"prompt: {_short(content)}")
        if event.get("completion_string"):
            lines.append(f"output: {_short(event['completion_string'])}")
        tools = _tool_names(event)
        if tools:
            lines.append("tools: " + ", ".join(tools))
        if event.get("error"):
            lines.append(f"error: {_short(event['error'])}")
        facts = [
            f"{event.get('prompt_tokens', 0)}+{event.get('completion_tokens', 0)} tokens"
        ]
        if event.get("model"):
            facts.insert(0, str(event["model"]))
        if isinstance(event.get("latency_ms"), (int, float)):
            facts.append(f"{event['latency_ms']:.0f} ms")
        if isinstance(event.get("estimated_cost_usd"), (int, float)):
            facts.append(f"${event['estimated_cost_usd']:.6f}")
        entries.append(
            TimelineEntry(
                _number(event.get("timestamp")),
                "sdk",
                f"{event.get('task_name', 'task')} ({', '.join(facts)})",
                tuple(lines),
            )
        )
    for event in _events(receipt, "gateway"):
        response = event.get("redacted_response")
        lines = []
        if isinstance(response, dict) and response.get("stream"):
            lines.append(f"stream text: {_short(response.get('text', ''))}")
        entries.append(
            TimelineEntry(
                _number(event.get("timestamp")),
                "gateway",
                f"HTTP {event.get('status_code')} from {event.get('upstream_url')}",
                tuple(lines),
            )
        )
    for event in _events(receipt, "extension"):
        entries.append(
            TimelineEntry(
                _number(event.get("timestamp")),
                "browser",
                f"{event.get('event_type')} on {event.get('url')}",
                (f"element: {event['element_id']}",) if event.get("element_id") else (),
            )
        )
    entries.sort(key=lambda entry: entry.timestamp)
    return entries


def _clock(timestamp: float) -> str:
    # Browser events carry millisecond timestamps; SDK and gateway use seconds.
    seconds = timestamp / 1000 if timestamp > 1e11 else timestamp
    try:
        moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "--:--:--"
    return moment.strftime("%H:%M:%S.%f")[:-3]


def render_text(path: Path, receipt: dict[str, Any]) -> str:
    summary = summarize(path, receipt)
    cost = (
        f"${summary.estimated_cost_usd:.6f}"
        if summary.estimated_cost_usd is not None
        else "not recorded"
    )
    lines = [
        f"Run        {summary.correlation_id}",
        f"Sealed at  {summary.sealed_at}",
        f"Signature  {'valid' if summary.signature_valid else 'INVALID'}"
        f" (signer {summary.fingerprint[:16]}…)",
        f"Chain      {'#' + str(summary.chain_index) if summary.chain_index is not None else 'not chained'}",
        f"Events     {summary.sdk_events} sdk, {summary.gateway_events} gateway,"
        f" {summary.extension_events} browser",
        f"Tokens     {summary.prompt_tokens} in, {summary.completion_tokens} out",
        f"Est. cost  {cost}",
        "",
    ]
    for entry in timeline(receipt):
        lines.append(f"{_clock(entry.timestamp)}  [{entry.source:<7}] {entry.title}")
        lines.extend(f"{'':14}{detail}" for detail in entry.lines)
    return "\n".join(lines) + "\n"


def render_html(path: Path, receipt: dict[str, Any]) -> str:
    """A self-contained static page; every value from the receipt is escaped."""
    summary = summarize(path, receipt)
    rows = []
    for entry in timeline(receipt):
        details = "".join(
            f"<div class=d>{html.escape(line)}</div>" for line in entry.lines
        )
        rows.append(
            f"<tr><td class=t>{html.escape(_clock(entry.timestamp))}</td>"
            f"<td><span class='s {html.escape(entry.source)}'>{html.escape(entry.source)}</span></td>"
            f"<td><div>{html.escape(entry.title)}</div>{details}</td></tr>"
        )
    facts = {
        "Run": summary.correlation_id,
        "Sealed at": summary.sealed_at,
        "Signature": "valid" if summary.signature_valid else "INVALID",
        "Signer fingerprint": summary.fingerprint,
        "Chain position": (
            "not chained" if summary.chain_index is None else f"#{summary.chain_index}"
        ),
        "Tokens": f"{summary.prompt_tokens} in, {summary.completion_tokens} out",
        "Estimated cost": (
            "not recorded"
            if summary.estimated_cost_usd is None
            else f"${summary.estimated_cost_usd:.6f}"
        ),
    }
    fact_rows = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>"
        for key, value in facts.items()
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>LLMWitness run {html.escape(summary.correlation_id)}</title>"
        "<style>body{font:14px/1.5 system-ui,sans-serif;margin:24px;color:#1b1f24;background:#fff}"
        "table{border-collapse:collapse;width:100%;margin-bottom:24px}"
        "th,td{text-align:left;vertical-align:top;padding:6px 10px;border-bottom:1px solid #e3e6ea}"
        "th{width:180px;font-weight:600}.t{font-family:ui-monospace,monospace;white-space:nowrap}"
        ".s{padding:1px 8px;border-radius:10px;font-size:12px;background:#e8eef7}"
        ".gateway{background:#e9f5ea}.browser{background:#fbf0e0}"
        ".d{color:#505a66;overflow-wrap:anywhere}"
        "@media(prefers-color-scheme:dark){body{background:#14171b;color:#e6e9ed}"
        "th,td{border-color:#2b3138}.d{color:#a3adb8}.s{background:#243248}"
        ".gateway{background:#1f3a24}.browser{background:#40311a}}</style></head><body>"
        f"<h1>LLMWitness run</h1><table>{fact_rows}</table>"
        f"<h2>Timeline</h2><table>{''.join(rows)}</table>"
        "<p>Local receipt; tamper-evident, not immutable storage. Content was "
        "scrubbed on a best-effort basis before it was recorded.</p></body></html>"
    )


def _comparable(event: dict[str, Any]) -> dict[str, Any]:
    """The parts of an SDK event that describe behaviour rather than one run."""
    return {
        "task": event.get("task_name"),
        "model": event.get("model"),
        "tools": _tool_names(event),
        "output": event.get("completion_string") or "",
        "error": event.get("error"),
    }


def diff_receipts(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Describe where two runs diverged, ignoring IDs and timestamps."""
    lines: list[str] = []
    for stream in ("sdk", "gateway", "extension"):
        a, b = len(_events(left, stream)), len(_events(right, stream))
        if a != b:
            lines.append(f"{stream} events: {a} -> {b}")
    left_sdk, right_sdk = _events(left, "sdk"), _events(right, "sdk")
    for label, key in (
        ("prompt tokens", "prompt_tokens"),
        ("completion tokens", "completion_tokens"),
    ):
        a = int(sum(_number(event.get(key)) for event in left_sdk))
        b = int(sum(_number(event.get(key)) for event in right_sdk))
        if a != b:
            lines.append(f"{label}: {a} -> {b}")
    for index in range(max(len(left_sdk), len(right_sdk))):
        if index >= len(left_sdk):
            lines.append(
                f"step {index + 1}: only in second run ({_comparable(right_sdk[index])['task']})"
            )
            continue
        if index >= len(right_sdk):
            lines.append(
                f"step {index + 1}: only in first run ({_comparable(left_sdk[index])['task']})"
            )
            continue
        a_view, b_view = _comparable(left_sdk[index]), _comparable(right_sdk[index])
        for name in ("task", "model", "tools", "error"):
            if a_view[name] != b_view[name]:
                lines.append(
                    f"step {index + 1} {name}: {a_view[name]!r} -> {b_view[name]!r}"
                )
        if a_view["output"] != b_view["output"]:
            lines.append(f"step {index + 1} output differs:")
            lines.extend(
                "    " + line
                for line in difflib.unified_diff(
                    a_view["output"].splitlines(),
                    b_view["output"].splitlines(),
                    "first",
                    "second",
                    lineterm="",
                    n=1,
                )
            )
    left_status = [event.get("status_code") for event in _events(left, "gateway")]
    right_status = [event.get("status_code") for event in _events(right, "gateway")]
    if left_status != right_status:
        lines.append(f"gateway status codes: {left_status} -> {right_status}")
    return lines


@dataclass(frozen=True)
class ChainReport:
    """Result of checking the hash chain across one receipt directory."""

    chained: int
    unchained: int
    problems: tuple[str, ...]
    signers: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.problems


def verify_chain(directory: str | os.PathLike[str] | None = None) -> ChainReport:
    """Check that chained receipts form one unbroken, correctly signed sequence.

    A gap means a receipt was removed; a hash mismatch means one was replaced
    or re-signed. Someone who can rewrite every later receipt with the signing
    key can still forge a consistent chain, so this is evidence, not proof.
    """
    root = receipt_directory(directory)
    problems: list[str] = []
    by_index: dict[int, tuple[Path, dict[str, Any]]] = {}
    unchained = 0
    signers: set[str] = set()
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            receipt = load_receipt(path)
        except (OSError, ValueError):
            problems.append(f"{path.name}: not a readable receipt")
            continue
        if receipt.get("receipt_version") != CHAINED_RECEIPT_VERSION:
            unchained += 1
            continue
        chain = receipt.get("chain")
        index = chain.get("index") if isinstance(chain, dict) else None
        if not isinstance(index, int) or index < 0:
            problems.append(f"{path.name}: missing chain position")
            continue
        if not verify_proof_receipt(str(path)):
            problems.append(f"{path.name}: signature does not verify")
        if index in by_index:
            problems.append(
                f"{path.name}: chain position {index} is also used by {by_index[index][0].name}"
            )
            continue
        by_index[index] = (path, receipt)
        signers.add(str(receipt.get("public_key_fingerprint", "")))

    previous_hash: str | None = None
    expected = 0
    for index in sorted(by_index):
        path, receipt = by_index[index]
        if index != expected:
            problems.append(
                f"receipts {expected} to {index - 1} are missing before {path.name}"
            )
            previous_hash = None
        elif receipt["chain"].get("previous_receipt_hash") != previous_hash:
            problems.append(f"{path.name}: does not link to the receipt before it")
        previous_hash = receipt_hash(receipt)
        expected = index + 1
    return ChainReport(
        len(by_index), unchained, tuple(problems), tuple(sorted(signers))
    )
