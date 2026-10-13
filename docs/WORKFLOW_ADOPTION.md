# Community workflow adoption

Unreleased source-checkout functionality; component `FL-WIT-CAPTURE-001`.
Python 3.10+ is required. Community is single-user localhost development,
not a hosted platform or replacement for LangChain, LangGraph or agent runtimes.

## Screenshot baseline audit

The supplied screenshots describe an earlier in-memory-only baseline; their
test count, line count and timing are historical, not current measurements.

| Earlier gap | Current implementation | Remaining boundary |
| --- | --- | --- |
| Unsealed runs lost on restart | `session_store` SQLite persistence before acceptance; restart and disk-failure regressions | Explicit `off` mode, idle expiry and local file deletion still permit loss |
| Silent telemetry loss | SDK/gateway retries, bounded spool, counters and logs | Best-effort, at-least-once; full queues or full disks can still lose events |
| Restart-generated signer | Persistent private key plus local/configured trusted fingerprint checks | Key trust is separate from agent identity; readable private keys permit signing |
| Regex misses/over-redaction | Luhn card checks, email/formatted-phone patterns, custom fields/patterns/scrubbers | Names/addresses need application detectors; no complete privacy guarantee |
| No Anthropic/streaming path | Messages route, OpenAI/Anthropic wrappers and experimental gateway SSE pass-through | Narrow API surfaces; not complete streaming capture or universal compatibility |
| Session cap and no search | Sealed-session eviction, idle TTL, `list`, timeline `show` and `diff` | Limits remain intentional; no full-text search or unbounded retention |
| Multi-tenancy/dashboard and early project | Offline Faultboard and deterministic local evidence | No team tenancy/managed dashboard; maturity requires outside-user evidence and authorized releases |

## Three-command offline quickstart

From the LLMWitness source checkout:

```bash
python -m venv .venv
# Activate .venv using your platform's usual command, then:
python -m pip install -e .
python -m llmwitness.cli capture --source generic --correlation-id 019c865d-1420-7ca8-9253-4b781cc52fa2 --input examples/workflow_metadata.jsonl --output captured.jsonl
```

Expected result: four metadata JSONL records in a new `captured.jsonl` file.
No service, account, API key or model call is needed. The example UUID is a
fixture: generate a new UUIDv7 for each real workflow and share it across that
workflow's integrations. Existing output files are never replaced.

## Compatibility: precise scope

| Integration | Implemented path | Evidence and limits |
| --- | --- | --- |
| Codex CLI | `capture --source codex-jsonl` | Documented JSONL metadata profile and adversarial fixtures; does not execute Codex, capture transcripts or certify every CLI version |
| Claude Code | `capture --source claude-code-hook` | One hook-input JSON object, including multiline input; no automatic hook installation or end-to-end native hook certification |
| Anthropic SDK | `tracker.wrap_anthropic_client(client)` | Sync/async `messages.create`, including `stream=True`, tested with provider fixtures; `messages.stream()` helper is not intercepted |
| OpenAI SDK | `tracker.wrap_openai_client(client)` | Sync/async chat completions, including `stream=True`, tested with fixtures; Responses API is not intercepted |
| LangChain | `make_langchain_handler(tracker)` | Real optional callback library, model/tool callbacks; existing handler records content subject to best-effort pattern scrubbing |
| LangGraph | Same LangChain callback via `config` | Real local graph, fake model and tool; sync/async success/failure tests; not every graph event, checkpoint, interrupt or deployment mode |
| Custom workflows | `WorkflowCapture(sink).capture("generic", event, run_id)` | Framework-neutral metadata protocol; application explicitly supplies observations and delivery |
| CrewAI, AutoGen, LlamaIndex, OpenAI Agents, MCP, OpenTelemetry | Existing Runtime mapping translators | Normalized mapping conformance only, not native framework SDK certification |

The base installation adds no framework dependency. Tested optional versions
on Windows/Python 3.14: LangGraph 1.2.14 and langchain-core 1.6.9. CI installs
the optional extra in one existing Linux/Python 3.11 job; other jobs retain
base-dependency coverage. Local evidence is not evidence that CI has run.

## Codex and Claude Code metadata

Normalize an existing Codex JSONL export, without running its commands:

```bash
llmwitness capture --source codex-jsonl --correlation-id YOUR_UUIDV7 --input codex.jsonl --output codex-metadata.jsonl
```

Codex profile events: `thread.started`, `turn.started`, `turn.completed`,
`turn.failed`, `item.started`, `item.updated`, `item.completed`, `error`.
Command, file-change and MCP-tool terminal items must declare `completed` or
`failed`; native failure is never normalized as success. Supported item types
are explicit in `workflow_capture.py`; unsupported types reject the batch.

Normalize one saved Claude Code hook input:

```bash
llmwitness capture --source claude-code-hook --correlation-id YOUR_UUIDV7 --input hook.json --output hook-metadata.jsonl
```

Hook profile events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `PostToolUseFailure`, `Stop`, `SessionEnd`, `SubagentStart`,
`SubagentStop`, `StopFailure`. Other hook types require a reviewed profile update.
For explicit hook integration, pass a workflow-scoped UUIDv7 and a unique
output path. Do not send capture JSONL to Claude's hook-control stdout. This
tool neither approves nor blocks tool execution and installs no hooks.

`--input -` reads binary UTF-8 input from stdin; `--output -` writes JSONL to
stdout. Limits: 100 KiB per record, 1,000 records, 4 MiB total input and 64 JSON
nesting levels. Duplicate keys, non-finite numbers, invalid UTF-8, unsupported
shapes and invalid UUIDs fail before any output. Blank lines count toward the
total byte limit. File output uses a flushed temporary metadata file and an
exclusive hard link; filesystems without hard-link support fail closed (stdout
remains available). An interrupted stdout write may be partial; output delivery
is not a transaction or durable telemetry guarantee.

Only allowlisted event/type/outcome values, bounded token counts, UUIDv7 and
source-separated digest references are retained. Prompts, commands, tool
arguments/results, paths, environment data and source error messages are omitted.
Identifier hashes are pseudonyms, not anonymization, proof of identity or trust.
Raw input may contain secrets: this command does not erase the source file.

## Minimal Python integration

```python
from llmwitness import LLMWitnessTracker, WorkflowCapture, generate_uuidv7

run_id = generate_uuidv7()
tracker = LLMWitnessTracker(capture_inputs=False)
try:
    capture = WorkflowCapture(tracker)
    capture.capture("generic", {"type": "workflow.started"}, run_id)
    # Your existing orchestrator owns execution, retries and error handling.
    capture.capture("generic", {"type": "workflow.completed"}, run_id)
finally:
    tracker.shutdown()
```

Start `llmwitness serve --no-gateway` locally before explicit SDK delivery.
`generic` supports `workflow.started/completed/failed`,
`tool.started/completed/failed`, and `usage`; its event field is `type`.
Optional `session_id` and `event_id` become digest references; `usage` accepts
integer `input_tokens` and `output_tokens`. Caller-managed streaming/error
paths must emit their own observations. Repeated observations remain repeated
telemetry; no durable deduplication is added.

`normalize_workflow_event(..., observed_at=...).to_sdk_payload()` provides
observation-time metadata offline. `WorkflowCapture.capture()` uses the existing
SDK sink, whose timestamp is recording time. Queue acceptance is not confirmed
delivery: inspect `tracker.stats()` for drops, failures and spool activity.
An SDK sink can have application-configured pricing; the offline export does not
infer costs. `capture_inputs=False` alone is not a metadata-only mode for other
SDK wrappers or the existing content-recording LangChain handler.

## Run a real local LangGraph

```bash
python -m pip install -e ".[langgraph]"
llmwitness serve --no-gateway
# In a second terminal using the same environment:
python examples/langgraph_example.py
```

The graph runs a fake model and a local tool, prints one shared UUIDv7 and
delivery counters. No paid model/provider is called. Pass
`config={"callbacks": [make_langchain_handler(tracker)]}` inside
`tracker.trace_session(...)` in your existing graph. Preserve that context
when moving work into another process; cross-process propagation is not added.
These callbacks record synthetic prompt/output/tool content in the example;
they are not the strict metadata-only Codex/Claude/generic profile.

## Connect the rest of Community

See [Community use cases](COMMUNITY_USE_CASES.md) for policy/Safe Effects,
standalone WitnessID, Reliability/Faultboard and offline bundle composition.
Native `completed` means only that the source reported completion. It is never
Runtime `VERIFIED`, provider truth, Passport acceptance or authorization.
Local signatures establish integrity of recorded bytes, not external truth.
Receipts remain ordinary local tamper-evident files, not immutable/WORM storage.

## Source contracts and reproduction

Profiles are based on the official [Codex SDK event source](https://github.com/openai/codex/blob/main/sdk/typescript/src/events.ts),
[Codex item source](https://github.com/openai/codex/blob/main/sdk/typescript/src/items.ts),
[Claude Code hooks](https://code.claude.com/docs/en/hooks) and
[LangGraph documentation](https://docs.langchain.com/oss/python/langgraph/overview),
checked 2026-10-11. Profiles are versioned local allowlists, not a promise of
compatibility with future upstream changes.

```bash
python -m pytest tests/test_workflow_capture.py tests/test_capture_cli.py tests/test_langgraph_callbacks.py -q
```

Install `.[langgraph]` to execute graph tests rather than skip them.

For installed-wheel reproduction, build with `python -m build`, install the
new wheel with its `[langgraph]` extra into a fresh virtual environment outside
the checkout, then use that environment's Python:

```bash
python -I scripts/verify_workflow_adoption.py
```

Run the command from the checkout so the script path resolves. Isolation mode
excludes checkout imports; the script rejects a source import, blocks socket
connections during capture, and uses an in-memory HTTP transport. Expected
summary: three profiles, real graph and offline CLI passed, UUIDv7 preserved.

No package publication, universal performance, complete telemetry,
comprehensive streaming support or production readiness is claimed.

For the additive opt-in metadata-only graph callback, plain OpenAI Responses
wrapper and offline report Proof Cards, see [adoption extensions](ADOPTION_EXTENSIONS.md).
