# Python SDK reference

```python
from llmwitness import LLMWitnessTracker, trace_session
```

`LLMWitnessTracker.record_event(...)` queues a scrubbed telemetry event without waiting for network delivery. A full queue increments `dropped_events`. A failed HTTP delivery increments `delivery_failures`. Neither condition is retried durably.

`tracker.flush()` waits for the current queue to be processed and can therefore wait on configured HTTP timeouts. `tracker.shutdown(timeout_sec=15)` requests worker shutdown and returns whether the worker stopped within the timeout.

`trace_session(task_name, correlation_id=None)` binds a UUIDv7 correlation ID through Python context variables. Caller-supplied IDs are validated as RFC 9562 UUIDv7 values and normalized to canonical lowercase text before they enter the telemetry queue. UUIDv7 is for correlation, not authorization or replay protection.

The browser bridge applies the same validation to `correlationId` constructor,
`init`, and setter values. When ingestion authentication is enabled, use
`window.LLMWitness.setIngestToken(token)` (or the `ingestToken` constructor or
`init` option in module-based integrations). The token remains in memory, is
sent only as a Bearer authorization header, and can be cleared by passing
`null`. Browser delivery remains best-effort; `deliveryFailures` counts failed
requests, including non-2xx responses.

## Unreleased SDK additions

`LLMWitnessTracker(...)` takes `max_retries` (default 2), `retry_backoff_sec`, `spool_dir`, `durable` (set `False` to keep nothing on disk), `capture_inputs` (set `False` to skip prompts) and `pricing`. A delivery that fails is retried; if it still fails the event is written to the spool and re-sent after the next successful delivery. Only the first failure of an outage is retried, so an outage does not slow the worker. Warnings go to the `llmwitness.sdk` logger, not to stdout. `tracker.stats()` returns the dropped, failed, spooled and replayed counts.

`record_event(...)` also accepts `provider`, `model`, `latency_ms`, `input_messages` and `error`. Prompts are scrubbed and capped at about 100 KB, keeping the most recent messages.

`wrap_openai_client(client)` and `wrap_anthropic_client(client)` record the model, prompt, latency, token counts, output and tool calls for sync and async clients, including `stream=True`. A stream is passed through unchanged and recorded once, when it ends. Anthropic's `messages.stream()` helper is not intercepted.

`tracker.seal(correlation_id=None)` flushes the queue and seals the run, returning the service's response including `receipt_file`. `tracker.trace_session(name, auto_seal=True)` seals when the block exits; a failed seal is logged, not raised.

```python
from llmwitness.integrations import make_langchain_handler, witness

@witness(tracker, "nightly-report", auto_seal=True)
def run_report(): ...

chain.invoke(inputs, config={"callbacks": [make_langchain_handler(tracker)]})
```

`@witness` records the task name, duration and any exception, not arguments or return values. `make_langchain_handler` needs `langchain-core` (`pip install "llmwitness[langchain]"`); its test runs against the installed library and is skipped when it is absent.

For names and addresses, register your own detector; it runs after the built-in patterns:

```python
from llmwitness.utils import register_text_scrubber
register_text_scrubber(lambda text: my_ner_redact(text))
```

`llmwitness verify RECEIPT --trusted-fingerprint SHA256_HEX` verifies the receipt signature and requires the embedded signing-key fingerprint to match a value obtained through a trusted channel. Without this option, signature verification proves only internal consistency with the key embedded in the receipt.

## Community end-to-end SDK

`CommunitySDK` is a thin Python composition facade for the complete local
reference workflow. It validates the project, delegates consequential work to
the existing `ProjectRunner`, reads recovery items through a read-only journal,
and emits the existing JSON, JUnit, and Markdown Reliability reports.

```python
from llmwitness.community import CommunitySDK

result = CommunitySDK().run_sync("./path/to/your-local-project.json")

print(result.project.outcome)
print(result.reliability["passed"], result.reliability["sample_count"])
print(result.recovery_items)
print(result.reports.markdown_path)
```

Async applications must avoid nesting an event loop:

```python
from llmwitness.community import CommunitySDK

result = await CommunitySDK().run(
    "./reference-project/fliorcie.project.json",
    report_dir="./reference-project/.llmwitness/sdk-reports",
)
```

The facade supports only the validated local reference effects. It does not
load arbitrary plugins, execute recovery actions, retry `UNKNOWN`, perform live
replay, call a model or paid service, or provide Cloud/Enterprise behavior. A
completed result demonstrates the checked local workflow and corpus only; it is
not a production-readiness, compliance, external-truth, or universal-safety
claim.

For a side-effect-free preview of the same reference jobs, use
`llmwitness project plan ./reference-project/fliorcie.project.json --all` or
`await ProjectRunner.from_file(path).plan_all()`. Plans expose authority and
contract status, request/value digests, and the next safe action. They do not
reserve idempotency, create a journal, load an adapter, or authorize a future
execution. The Community SDK's `run` method remains the separate execution
entry point.
