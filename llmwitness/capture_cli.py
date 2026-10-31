"""Offline, metadata-only normalization for supported workflow event formats.

This module is deliberately an edge: it reads local input, validates every
record, and writes SDK-compatible telemetry JSONL.  It does not construct an
SDK tracker, contact a host, or assign Runtime meaning to a workflow event.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import BinaryIO, TextIO

from llmwitness.workflow_capture import (
    SUPPORTED_SOURCES,
    CaptureError,
    normalize_workflow_event,
)

MAX_RECORD_BYTES = 100 * 1024
MAX_RECORDS = 1_000
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 64
logger = logging.getLogger(__name__)


class CaptureCliError(ValueError):
    """A content-free validation or local I/O failure at the capture boundary."""


class _InvalidJson(ValueError):
    """Internal marker used to keep JSON parser details out of diagnostics."""


def add_capture_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the offline capture command without importing SDK delivery code."""
    parser = subparsers.add_parser(
        "capture",
        help="Normalize supported local workflow metadata into SDK-compatible JSONL",
    )
    parser.add_argument("--source", choices=SUPPORTED_SOURCES, required=True)
    parser.add_argument(
        "--correlation-id", required=True, help="Caller-supplied RFC 9562 UUIDv7"
    )
    parser.add_argument(
        "--input", default="-", help="JSONL or hook JSON input path; '-' reads stdin"
    )
    parser.add_argument(
        "--output", default="-", help="JSONL output path; '-' writes stdout"
    )


def run_capture(
    args: argparse.Namespace,
    *,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Validate a complete local batch, then emit its normalized JSONL once."""
    try:
        input_stream, close_input = _open_input(args.input, stdin)
        try:
            observations = _normalize_batch(
                args.source, args.correlation_id, input_stream
            )
        finally:
            if close_input:
                input_stream.close()

        serialized = _serialize_observations(observations)
        if args.output == "-":
            (stdout or sys.stdout).write(serialized)
            return
        _write_new_output(Path(args.output), serialized)
    except (OSError, UnicodeError):
        raise CaptureCliError("workflow capture local I/O failed") from None


def _open_input(input_name: str, stdin: BinaryIO | None) -> tuple[BinaryIO, bool]:
    if input_name == "-":
        return (stdin or sys.stdin.buffer), False
    try:
        return open(input_name, "rb"), True
    except (OSError, TypeError):
        raise CaptureCliError("workflow capture input could not be opened") from None


def _normalize_batch(
    source: str, correlation_id: str, stream: BinaryIO
) -> list[dict[str, object]]:
    events = _read_events(source, stream)
    try:
        return [
            normalize_workflow_event(source, event, correlation_id).to_sdk_payload()
            for event in events
        ]
    except CaptureError:
        raise CaptureCliError("workflow capture input is invalid") from None


def _read_events(source: str, stream: BinaryIO) -> list[Mapping[str, object]]:
    if source == "claude-code-hook":
        event = _decode_json(_read_single_record(stream))
        return [_mapping_record(event)]
    return list(_read_jsonl_records(stream))


def _read_single_record(stream: BinaryIO) -> bytes:
    data = stream.read(MAX_TOTAL_BYTES + 1)
    if len(data) > MAX_TOTAL_BYTES or len(data) > MAX_RECORD_BYTES:
        raise CaptureCliError("workflow capture input exceeds its size limit")
    if not data.strip():
        raise CaptureCliError("workflow capture input contains no records")
    return data


def _read_jsonl_records(stream: BinaryIO) -> Iterable[Mapping[str, object]]:
    total_bytes = 0
    record_count = 0
    while True:
        line = stream.readline(MAX_RECORD_BYTES + 1)
        if not line:
            break
        total_bytes += len(line)
        if len(line) > MAX_RECORD_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise CaptureCliError("workflow capture input exceeds its size limit")
        if not line.strip():
            continue
        record_count += 1
        if record_count > MAX_RECORDS:
            raise CaptureCliError("workflow capture input exceeds its record limit")
        yield _mapping_record(_decode_json(line))
    if record_count == 0:
        raise CaptureCliError("workflow capture input contains no records")


def _decode_json(data: bytes) -> object:
    try:
        _check_json_nesting(data)
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
            parse_float=_finite_json_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CaptureCliError("workflow capture input is not valid JSON") from None


def _check_json_nesting(data: bytes) -> None:
    """Bound decoder nesting before parsing, ignoring delimiters in strings."""
    depth = 0
    in_string = False
    escaped = False
    for character in data:
        if in_string:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                in_string = False
        elif character == 34:
            in_string = True
        elif character in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _InvalidJson()
        elif character in (93, 125):
            depth -= 1


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJson()
        result[key] = value
    return result


def _reject_nonfinite_number(_: str) -> None:
    raise _InvalidJson()


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise _InvalidJson()
    return number


def _mapping_record(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CaptureCliError("workflow capture record must be a JSON object")
    return value


def _serialize_observations(observations: list[dict[str, object]]) -> str:
    try:
        return "".join(
            json.dumps(
                observation, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            + "\n"
            for observation in observations
        )
    except (TypeError, ValueError):
        raise CaptureCliError("workflow capture output could not be prepared") from None


def _write_new_output(output_path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        if output_path.exists() or not output_path.parent.is_dir():
            raise CaptureCliError("workflow capture output is unavailable")
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=output_path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_path, output_path)
    except (OSError, TypeError):
        raise CaptureCliError("workflow capture output could not be created") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("workflow capture temporary metadata cleanup failed")
