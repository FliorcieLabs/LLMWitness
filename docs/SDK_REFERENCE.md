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
