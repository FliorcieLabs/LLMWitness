"""Dependency-free local STDIO MCP server for LLMWitness reliability tools."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from llmwitness.adapters import default_adapter_registry
from llmwitness.envelope import read_envelope
from llmwitness.journal import SQLiteJournalStore
from llmwitness.reliability import reliability_summary, run_reliability_corpus

SERVER_INSTRUCTIONS = (
    "Local Community reliability tools only. Validation and inspection are read-only. "
    "Never interpret a signature as proof that an external event is true. Preserve UNKNOWN outcomes, "
    "do not retry ambiguous writes, and require application policy before any live side effect."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "validate_execution_envelope",
        "description": "Validate an Execution Envelope v0.1 and its evidence invariants.",
        "inputSchema": {
            "type": "object",
            "properties": {"envelope": {"type": "object"}},
            "required": ["envelope"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify_journal",
        "description": "Verify one run's local hash-linked journal chain.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "run_id": {"type": "string"}},
            "required": ["path", "run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "inspect_run",
        "description": "Read the ordered entries for one local journal run.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "run_id": {"type": "string"}},
            "required": ["path", "run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_reliability_corpus",
        "description": "Run the deterministic, side-effect-free Community reliability scenarios.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "plan_recovery",
        "description": "Return the safe next step for an effect state without executing it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "compensation_available": {"type": "boolean"},
                "idempotency_guaranteed": {"type": "boolean"},
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_recovery_items",
        "description": "List unresolved effects from verified local journal chains without executing them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "include_terminal": {"type": "boolean"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_envelope_adapters",
        "description": "List dependency-isolated framework envelope translators.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

for _tool in TOOLS:
    _tool["annotations"] = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def _safe_path(raw: str) -> Path:
    root = Path(os.getenv("LLMWITNESS_MCP_ROOT", os.getcwd())).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("journal path is outside LLMWITNESS_MCP_ROOT") from exc
    return resolved


def _recovery_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    status = str(arguments["status"]).lower()
    if status == "unknown":
        return {
            "action": "reconcile",
            "automatic_retry": False,
            "reason": "UNKNOWN must be checked by an authoritative verifier or provider idempotency contract",
        }
    if status == "verified" and arguments.get("compensation_available"):
        return {"action": "compensate_then_verify", "automatic_retry": False}
    if status == "failed" and arguments.get("idempotency_guaranteed"):
        return {"action": "policy_review_before_retry", "automatic_retry": False}
    if status in {"manual_review", "compensating"}:
        return {"action": "manual_review", "automatic_retry": False}
    return {"action": "no_effect", "automatic_retry": False}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "validate_execution_envelope":
        try:
            envelope = read_envelope(arguments["envelope"])
            return {
                "valid": True,
                "run_id": envelope.run_id,
                "event_id": envelope.event_id,
            }
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            return {"valid": False, "error": str(exc)}
    if name in {"verify_journal", "inspect_run"}:
        path = _safe_path(str(arguments["path"]))
        if not path.is_file():
            raise FileNotFoundError(path)
        with SQLiteJournalStore(path, read_only=True) as journal:
            if name == "verify_journal":
                return journal.verify(str(arguments["run_id"])).__dict__
            return {
                "entries": [
                    entry.__dict__ for entry in journal.scan(str(arguments["run_id"]))
                ]
            }
    if name == "run_reliability_corpus":
        return reliability_summary(run_reliability_corpus())
    if name == "plan_recovery":
        return _recovery_plan(arguments)
    if name == "list_recovery_items":
        from llmwitness.recovery import RecoveryPlanner

        path = _safe_path(str(arguments["path"]))
        if not path.is_file():
            raise FileNotFoundError(path)
        with SQLiteJournalStore(path, read_only=True) as journal:
            items = RecoveryPlanner(journal).list_items(
                include_terminal=bool(arguments.get("include_terminal", False))
            )
        return {"items": [item.model_dump(mode="json") for item in items]}
    if name == "list_envelope_adapters":
        return {"adapters": list(default_adapter_registry().names())}
    raise KeyError(f"unknown tool {name!r}")


def _tool_result(value: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
        "structuredContent": value,
        "isError": is_error,
    }


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            requested = message.get("params", {}).get("protocolVersion")
            result: dict[str, Any] = {
                "protocolVersion": requested or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "llmwitness-community", "version": "0.1.0"},
                "instructions": SERVER_INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            try:
                value = call_tool(
                    str(params.get("name", "")), dict(params.get("arguments") or {})
                )
                result = _tool_result(value)
            except Exception as exc:
                result = _tool_result(
                    {"error": str(exc), "type": type(exc).__name__}, is_error=True
                )
        elif method in {"resources/list", "prompts/list"}:
            result = {"resources" if method.startswith("resources") else "prompts": []}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": str(exc)},
        }


def run_stdio() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle_message(message)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()
