# ADR 0027: metadata-only Responses wrapper

Date: 2026-10-11. Status: bounded implementation decision.
Task/component: `TASK-327`, `FL-WIT-RESP-001`.

Add a separate `wrap_openai_responses_client` SDK entry point. An edge module
owns plain sync/async `responses.create` metadata: UUIDv7 header, latency,
bounded usage, allowlisted native status and response digest reference. Inputs,
outputs, tools, model names and raw exception messages are never captured.
The original result/exception is preserved; telemetry failure cannot interfere.
Repeated wrapping by the same sink is idempotent; another sink is rejected.

Automatic extension of chat-completions would change an existing contract.
Content reconstruction or stream interception would expand privacy/lifecycle
scope; explicit `stream=True` remains unobserved pass-through. Parse, retrieve,
stream helpers, background polling and provider retries remain outside scope.
Local fixture/real SDK mock tests—not paid calls—precede verification. A source
completion is not Runtime `VERIFIED`, identity acceptance or provider truth.
