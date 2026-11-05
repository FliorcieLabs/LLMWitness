# ADR 0026: opt-in metadata framework callbacks

Date: 2026-10-11. Status: bounded implementation decision.
Task/component: `TASK-326`, `FL-WIT-META-001`.

Existing content callbacks are preserved for compatibility. A separate lazy
factory owns bounded model/tool lifecycle metadata, not orchestration. Caller
UUIDv7 and injected clock/sink make correlation and expiry explicit. Pending
runs retain only a hashed native run ID, kind and start time under a lock.
Sink calls occur after releasing the lock; failures become counters, not
application failures. Duplicate starts are ignored, expired/evicted/mismatched
ends are dropped with counters. No durable deduplication is promised.

Changing existing defaults would break users. Stateless terminal callbacks
cannot distinguish duplicates or preserve latency. A new workflow engine would
duplicate framework ownership. Acceptance requires privacy/bounds/concurrency,
sink failure and real sync/async graph tests. No framework certification,
effect verification, new dependency or release is implied.
