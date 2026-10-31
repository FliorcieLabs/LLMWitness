# Opt-in Community adoption extensions

These additive local features observe existing workflows. They do not replace
LangGraph, authorize tools, verify provider effects or introduce hosted services.

## Metadata-only LangChain/LangGraph

Install `llmwitness[langgraph]` in your development environment. Unlike the
existing content-oriented `make_langchain_handler`, the separate handler below
does not inspect prompts, outputs, model names, tool arguments or exception text.

```python
from llmwitness import LLMWitnessTracker, make_metadata_langchain_handler

tracker = LLMWitnessTracker()
with tracker.trace_session("local-graph") as run_id:
    callbacks = make_metadata_langchain_handler(tracker, run_id)
    result = graph.invoke(inputs, config={"callbacks": [callbacks]})
    # graph.ainvoke supports the same callback config.
print(callbacks.stats())
tracker.shutdown()
```

`graph` and `inputs` belong to your application. Use one handler per correlated
run. Native run references are hashed; lifecycle outcome and latency are retained.
Defaults: 1,024 pending model/tool runs, 300-second maximum age. Eviction, expiry,
duplicate starts, unmatched ends and sink failures are counted. An expired or
evicted terminal callback emits nothing. State is thread-safe, process-local
and not durable deduplication. Sink calls occur outside its state lock.
Instrumented model/tool callbacks are not complete graph telemetry.

## OpenAI Responses metadata

The OpenAI SDK is application-owned and not a base LLMWitness dependency.

```python
from openai import OpenAI
from llmwitness import LLMWitnessTracker

tracker = LLMWitnessTracker()
client = tracker.wrap_openai_responses_client(OpenAI())
with tracker.trace_session("responses"):
    response = client.responses.create(model=YOUR_MODEL, input=YOUR_INPUT)
tracker.shutdown()
```

`AsyncOpenAI` plain `await client.responses.create(...)` is also supported.
The wrapper preserves returned objects and original exceptions/cancellation,
adds the UUIDv7 correlation header, and records allowlisted status, latency,
bounded input/output token counts and a hashed response reference. No content,
tool arguments, model name or exception message is captured. Unsupported usage
counts become zero, not measured usage. Metadata failure produces a fixed
content-free warning and cannot replace an application result.

Wrapping twice with the same tracker is idempotent; another telemetry owner is
rejected. `stream=True` passes through unchanged **without capture**. Retrieve,
polling, parse and streaming helpers are not instrumented. No wrapper retry is
introduced; provider SDK retry configuration is unchanged. Real application
calls can leave your device and incur provider charges.

Native mock-transport tests ran against OpenAI SDK 3.29.0 on Python 3.14.6;
this is bounded local compatibility evidence, not future-version certification.
Contract: [official OpenAI Python SDK](https://github.com/openai/openai-python).

## Offline Proof Cards

Generate the source reports through their existing owning commands, then:

```bash
llmwitness proof-card --reliability reliability-summary.json --adapters adapter-report.json --bundle bundle-verification.json --output proof-card.md
llmwitness proof-card --reliability reliability-summary.json --format json --output proof-card.json
```

Inputs are JSON Reliability summaries, mapping-adapter conformance reports, and
the JSON result of existing `evidence-bundle verify` (not the ZIP itself).
Optional `--authority authority.json` accepts a minimized declaration:

```json
{"schema_version":"0.1","status":"unknown"}
```

Allowed authority labels: `allowed`, `denied`, `unknown`, `not_checked`. Do not
submit credentials. The renderer imports no WitnessID or provider SDK, runs no
checks, and makes no network calls. JSON and Markdown outputs retain only fixed
labels, declared outcomes/counts and source-byte SHA-256 references; raw names,
details, errors, paths and identities are omitted. Missing reports remain
`not_checked`; failures and unknown authority remain visible. Each input is
bounded to 1 MiB and 64 nesting levels. Duplicate keys, nonfinite numbers,
unsupported versions and inconsistent counts fail before output. File output
is exclusive and never replaces an existing file.

**Supplied reports are not authenticated.** A forged source can forge a declared
result. A card is a shareable local summary, not a verifier, trust decision,
provider-truth proof, credential, compliance claim or universal score. Receipts
remain ordinary local tamper-evident files, not immutable/WORM storage.

## Reproduce local checks

```bash
python -m pytest tests/test_adoption_extensions.py tests/test_langgraph_callbacks.py -q
python -I scripts/verify_workflow_adoption.py
```

Install optional LangGraph and OpenAI dependencies to execute native local tests
instead of skipping them. The second command requires a separately installed
new wheel with LangGraph; its fixture Responses client needs no OpenAI SDK.
It rejects checkout imports and blocks socket connections during observation.
No release, external adoption, production readiness or universal support is implied.
