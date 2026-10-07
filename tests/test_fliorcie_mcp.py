import json
from pathlib import Path

from llmwitness.mcp_server import TOOLS, call_tool, handle_message


def test_mcp_initializes_and_lists_narrow_tools():
    initialized = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert initialized["result"]["serverInfo"]["name"] == "llmwitness-community"
    listed = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed["result"]["tools"] == TOOLS
    assert "execute" not in {tool["name"] for tool in TOOLS}
    assert all(tool["annotations"]["readOnlyHint"] for tool in TOOLS)


def test_mcp_recovery_keeps_unknown_and_reliability_is_machine_readable():
    plan = call_tool("plan_recovery", {"status": "unknown"})
    assert plan["action"] == "reconcile"
    assert not plan["automatic_retry"]
    report = call_tool("run_reliability_corpus", {})
    assert report["sample_count"] == 15
    assert report["failed"] == 0


def test_repo_extensions_exist_and_private_pack_is_ignored():
    root = Path(__file__).resolve().parents[1]
    assert (root / ".codex" / "config.toml").is_file()
    assert len(list((root / ".codex" / "agents").glob("*.toml"))) == 3
    skills = list((root / ".agents" / "skills").glob("*/SKILL.md"))
    assert len(skills) >= 3
    assert (root / ".agents" / "skills" / "professional-coder" / "SKILL.md").is_file()
    assert ".fliorcie-labs/" in (root / ".gitignore").read_text(encoding="utf-8")


def test_mcp_stdio_message_is_json_serializable():
    response = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_envelope_adapters", "arguments": {}},
        }
    )
    assert json.loads(json.dumps(response))["result"]["isError"] is False
