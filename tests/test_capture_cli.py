"""Tests for the offline metadata-only workflow capture CLI edge."""

from __future__ import annotations

import argparse
import io
import json
import socket
from pathlib import Path

import pytest

from llmwitness.capture_cli import (
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    MAX_TOTAL_BYTES,
    CaptureCliError,
    run_capture,
)
from llmwitness.utils import generate_uuidv7


def _arguments(
    source: str = "generic", input_name: str = "-", output_name: str = "-"
) -> argparse.Namespace:
    return argparse.Namespace(
        source=source,
        correlation_id=generate_uuidv7(),
        input=input_name,
        output=output_name,
    )


def _run(data: bytes, args: argparse.Namespace | None = None) -> str:
    output = io.StringIO()
    run_capture(args or _arguments(), stdin=io.BytesIO(data), stdout=output)
    return output.getvalue()


def test_normalizes_jsonl_to_metadata_only_sdk_payload():
    output = _run(
        b'{"type":"workflow.started","session_id":"private-session","secret":"omit"}\n'
    )
    payload = json.loads(output)

    assert payload["correlation_id"]
    assert payload["task_name"] == "workflow:generic"
    assert payload["completion_string"] is None
    assert payload["input_messages"] == []
    assert "private-session" not in output
    assert "secret" not in output
    assert payload["agent_state"]["native_references"]["session_id_ref"].startswith(
        "sha256:"
    )


def test_claude_hook_allows_one_multiline_json_object():
    args = _arguments(source="claude-code-hook")
    output = _run(
        b'{\n  "hook_event_name": "PreToolUse",\n  "session_id": "private"\n}', args
    )

    assert json.loads(output)["agent_state"]["source_event"] == "PreToolUse"


@pytest.mark.parametrize(
    "data",
    [
        b'{"type":"workflow.started","type":"workflow.completed"}\n',
        b'{"type":"workflow.started","usage":{"input_tokens":NaN}}\n',
        b'{"type":"workflow.started"}\n{"type":"unknown"}\n',
        b'{"type":"workflow.started"}\nnot-json\n',
    ],
)
def test_invalid_batch_emits_no_partial_stdout(data: bytes):
    output = io.StringIO()
    with pytest.raises(CaptureCliError):
        run_capture(_arguments(), stdin=io.BytesIO(data), stdout=output)
    assert output.getvalue() == ""


def test_rejects_non_object_claude_input_without_stdout():
    output = io.StringIO()
    with pytest.raises(CaptureCliError):
        run_capture(
            _arguments(source="claude-code-hook"),
            stdin=io.BytesIO(b"[]"),
            stdout=output,
        )
    assert output.getvalue() == ""


def test_rejects_record_larger_than_limit_without_stdout():
    oversized = b"{" + b'"x":"' + b"a" * MAX_RECORD_BYTES + b'"}\n'
    output = io.StringIO()
    with pytest.raises(CaptureCliError, match="size limit"):
        run_capture(_arguments(), stdin=io.BytesIO(oversized), stdout=output)
    assert output.getvalue() == ""


def test_rejects_more_than_record_limit_without_stdout():
    event = b'{"type":"workflow.started"}\n'
    output = io.StringIO()
    with pytest.raises(CaptureCliError, match="record limit"):
        run_capture(
            _arguments(), stdin=io.BytesIO(event * (MAX_RECORDS + 1)), stdout=output
        )
    assert output.getvalue() == ""


def test_does_not_overwrite_existing_output(tmp_path):
    output_path = tmp_path / "captured.jsonl"
    output_path.write_text("existing", encoding="utf-8")
    arguments = _arguments(input_name="-", output_name=str(output_path))

    with pytest.raises(CaptureCliError, match="output is unavailable"):
        run_capture(arguments, stdin=io.BytesIO(b'{"type":"workflow.started"}\n'))
    assert output_path.read_text(encoding="utf-8") == "existing"


def test_writes_new_file_only_after_complete_validation(tmp_path):
    output_path = tmp_path / "captured.jsonl"
    arguments = _arguments(output_name=str(output_path))
    with pytest.raises(CaptureCliError):
        run_capture(
            arguments,
            stdin=io.BytesIO(b'{"type":"workflow.started"}\n{"type":"untrusted"}\n'),
        )
    assert not output_path.exists()


def test_creates_new_output_file_with_sdk_compatible_jsonl(tmp_path):
    output_path = tmp_path / "captured.jsonl"
    arguments = _arguments(output_name=str(output_path))
    run_capture(arguments, stdin=io.BytesIO(b'{"type":"workflow.completed"}\n'))

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["correlation_id"] == arguments.correlation_id


def test_never_opens_a_network_connection(monkeypatch):
    def _unexpected_network(*_args, **_kwargs):
        raise AssertionError("capture CLI must remain offline")

    monkeypatch.setattr(socket, "create_connection", _unexpected_network)
    output = _run(b'{"type":"workflow.started"}\n')
    assert json.loads(output)["task_name"] == "workflow:generic"


def test_diagnostics_do_not_include_source_content_or_paths(tmp_path):
    secret = "super-secret-value"
    hidden_path = tmp_path / "private-input.jsonl"
    arguments = _arguments(input_name=str(hidden_path))
    with pytest.raises(CaptureCliError) as failure:
        run_capture(arguments)
    assert secret not in str(failure.value)
    assert str(hidden_path) not in str(failure.value)


@pytest.mark.parametrize(
    "data",
    [
        b'{"type":"workflow.started","ignored":1e999}\n',
        b'{"type":"workflow.started","session_id":"private-\\ud800"}\n',
        b'{"type":"workflow.started","ignored":'
        + b"[" * 2000
        + b"0"
        + b"]" * 2000
        + b"}\n",
        b'{"type":"workflow.started","ignored":' + b"1" * 5000 + b"}\n",
        b"\xff\n",
        b"\n\n",
    ],
    ids=[
        "nonfinite",
        "surrogate",
        "deep-json",
        "large-integer",
        "invalid-utf8",
        "empty",
    ],
)
def test_hostile_json_failures_are_sanitized_and_emit_nothing(data):
    output = io.StringIO()
    with pytest.raises(CaptureCliError) as failure:
        run_capture(_arguments(), stdin=io.BytesIO(data), stdout=output)
    assert output.getvalue() == ""
    assert "private" not in str(failure.value)


def test_total_input_limit_includes_empty_lines():
    with pytest.raises(CaptureCliError, match="size limit"):
        _run(b"\n" * (MAX_TOTAL_BYTES + 1))


def test_json_depth_check_ignores_braces_and_escaped_quotes_in_strings():
    data = json.dumps({"type": "workflow.started", "ignored": ('\\"[' * 1000)}).encode()
    assert json.loads(_run(data))["agent_state"]["outcome"] == "started"


def test_reads_binary_input_file_without_retaining_content(tmp_path):
    input_path = tmp_path / "events.jsonl"
    input_path.write_bytes(b'{"type":"workflow.started","prompt":"never retained"}\n')
    output = _run(b"", _arguments(input_name=str(input_path)))
    assert "never retained" not in output


def test_local_read_failure_has_no_path_or_partial_output():
    class BrokenInput(io.BytesIO):
        def readline(self, size=-1):
            raise OSError("private-input-path")

    output = io.StringIO()
    with pytest.raises(CaptureCliError) as failure:
        run_capture(_arguments(), stdin=BrokenInput(), stdout=output)
    assert "private-input-path" not in str(failure.value)
    assert output.getvalue() == ""


def test_cleanup_failure_preserves_successful_output(tmp_path, monkeypatch, caplog):
    output_path = tmp_path / "captured.jsonl"

    def fail_cleanup(self, *args, **kwargs):
        raise OSError("private-temporary-path")

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    run_capture(
        _arguments(output_name=str(output_path)),
        stdin=io.BytesIO(b'{"type":"workflow.completed"}\n'),
    )
    assert json.loads(output_path.read_text())["agent_state"]["outcome"] == "completed"
    assert "cleanup failed" in caplog.text
    assert "private-temporary-path" not in caplog.text


def test_output_creation_race_never_overwrites_another_writer(tmp_path, monkeypatch):
    import os

    destination = tmp_path / "captured.jsonl"
    original_link = os.link

    def competing_writer(source, target):
        destination.write_text("other writer", encoding="utf-8")
        original_link(source, target)

    monkeypatch.setattr(os, "link", competing_writer)
    with pytest.raises(CaptureCliError, match="could not be created"):
        run_capture(
            _arguments(output_name=str(destination)),
            stdin=io.BytesIO(b'{"type":"workflow.started"}\n'),
        )
    assert destination.read_text() == "other writer"


def test_cli_dispatch_has_content_free_failure_and_no_stdout(monkeypatch, capsys):
    from llmwitness.cli import main

    monkeypatch.setattr(
        "sys.stdin", io.TextIOWrapper(io.BytesIO(b"private malformed input"))
    )
    with pytest.raises(SystemExit) as failure:
        main(["capture", "--source", "generic", "--correlation-id", generate_uuidv7()])
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private malformed input" not in captured.err
    assert "[ERROR]" in captured.err


def test_cli_dispatch_succeeds_offline(monkeypatch, capsys):
    from llmwitness.cli import main

    run_id = generate_uuidv7()
    monkeypatch.setattr(
        "sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"type":"workflow.started"}\n'))
    )
    main(["capture", "--source", "generic", "--correlation-id", run_id])
    captured = capsys.readouterr()
    assert json.loads(captured.out)["correlation_id"] == run_id
    assert captured.err == ""


@pytest.mark.parametrize(
    "hook_input",
    [
        b'{"hook_event_name":"Notification","session_id":"s"}',
        b"not json",
    ],
    ids=["unsupported-hook-event", "malformed-hook-input"],
)
def test_rejected_hook_input_never_exits_with_the_blocking_code(
    monkeypatch, capsys, hook_input
):
    """Claude Code reads hook exit code 2 as "block"; capture must not block."""
    from llmwitness.cli import main

    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(hook_input)))
    with pytest.raises(SystemExit) as failure:
        main(
            [
                "capture",
                "--source",
                "claude-code-hook",
                "--correlation-id",
                generate_uuidv7(),
            ]
        )
    assert failure.value.code == 1
    assert capsys.readouterr().out == ""
