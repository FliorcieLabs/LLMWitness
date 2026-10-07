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
- In `0.1.0`, automatically generated signing and HMAC keys are process-local and change after restart. The development branch persists them in `.llmwitness/keys`; that directory is an ordinary local directory, and anyone who can read the private key can sign receipts.
- The development branch links each receipt to the previous one. Someone holding the signing key can still rewrite a receipt and every receipt after it, so the chain is evidence of tampering, not proof against it.
- An RFC 3161 timestamp token requested with `llmwitness timestamp` is stored but not validated by LLMWitness; verify it with the authority's certificate.
- Pattern scrubbing has false positives and false negatives and is not comprehensive DLP.
- Telemetry can be dropped on queue overflow or delivery failure; counters expose loss. The development branch retries and spools undelivered events to local disk, which narrows but does not remove loss, and can deliver an event twice.
- In `0.1.0`, in-memory sessions do not survive restart. The development branch mirrors unsealed sessions to a local SQLite file; a power loss can still drop the most recent events, and the service is not safe for multiple workers or replicas.
- The spool and the session database hold scrubbed telemetry in plain files under `.llmwitness/`.
- The optional effect journal improves local crash recovery but remains an ordinary, deletable local database. It is not an independent truth source, immutable storage, or a multi-process distributed coordinator.
- Reference framework adapters translate supplied events; they do not make the underlying frameworks, tools, or external APIs trustworthy.
- UUIDv7 is a correlation identifier, not authentication or replay prevention.
- The browser extension can observe sensitive page content. Review and restrict its permissions before enabling it.
- In `0.1.0` the gateway supports bounded JSON chat-completions requests only. The development branch adds an Anthropic Messages route and passes server-sent-event streams through; it does not claim full compatibility with either API.

Do not use Community `0.1.0` as the sole control for sensitive production workloads, retention obligations, access control, compliance, or incident evidence.
