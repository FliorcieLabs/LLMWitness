# Security policy and limitations

LLMWitness Community `0.1.0` is a single-user localhost development tool. It is not a production security boundary.

## Reporting

If GitHub private vulnerability reporting is enabled for the repository, use it and do not include sensitive details in a public issue. Otherwise, contact the maintainers through a verified private channel listed by the repository owner. Community response times are best-effort; no response or remediation SLA is offered.

Include affected versions, reproduction steps, impact, and a minimal proof of concept without real secrets or personal data.

## Security properties

- Receipt signatures detect changes to signed fields when verification succeeds.
- Ed25519 verification uses the public key embedded in a receipt. This alone does not establish signer identity; verify its fingerprint through a trusted channel.
- An optional HMAC can be verified using `LLMWITNESS_SECRET_KEY`.
- Ingestion applies best-effort pattern scrubbing again at the storage boundary.
- Services bind to loopback in documented examples. Configure shared tokens before any non-loopback use.
- The unreleased effect lane can persist hash-linked transitions and idempotency records in SQLite. `VERIFIED` envelopes require verifier identity and evidence.

## Limitations

- Local receipt files can be changed, replaced, or deleted. They are not WORM or immutable storage.
- Automatically generated signing and HMAC keys are process-local development keys and change after restart.
- Pattern scrubbing has false positives and false negatives and is not comprehensive DLP.
- Telemetry can be dropped on queue overflow or delivery failure; counters expose loss, but durable retry is not implemented.
- In-memory sessions do not survive restart and are not safe for multiple workers or replicas.
- The optional effect journal improves local crash recovery but remains an ordinary, deletable local database. It is not an independent truth source, immutable storage, or a multi-process distributed coordinator.
- Reference framework adapters translate supplied events; they do not make the underlying frameworks, tools, or external APIs trustworthy.
- UUIDv7 is a correlation identifier, not authentication or replay prevention.
- The browser extension can observe sensitive page content. Review and restrict its permissions before enabling it.
- The gateway currently supports bounded JSON chat-completions requests and buffers upstream responses; streaming is not implemented.

Do not use Community `0.1.0` as the sole control for sensitive production workloads, retention obligations, access control, compliance, or incident evidence.
