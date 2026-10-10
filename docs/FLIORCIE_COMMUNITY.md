# LLMWitness Community reliability workflow

This is the public implementation reference for the unreleased local reliability layer inside LLMWitness. It is not a separate hosted Fliorcie platform. The [architecture](ARCHITECTURE.md) describes the telemetry lane and its local persistence limits; the [decision index](adr/README.md) records the safety constraints for this reliability lane.

```text
framework event -> mapping adapter -> Execution Envelope -> local journal
intent -> authority -> contract -> atomic PREPARED reservation -> provider
                                                           |
                                    FAILED / UNKNOWN / SUCCEEDED_UNVERIFIED
                                                           |
                                            evidence-based verification
                                                           |
                                                        VERIFIED
```

`UNKNOWN` is not permission for blind retry. A Saga transaction compensates completed effects in reverse order when an error escapes its context. A caught effect failure must not upgrade the transaction to verified success, and no further effects may be dispatched through a halted transaction. An unexpected runner or compensation exception leaves its transaction in manual review and propagates the error. Catching a halt does not implicitly compensate earlier effects; inspect the failed transaction and use explicit recovery.

## Phase ownership

| Owner | Responsibility |
| --- | --- |
| `envelope`, `journal` | Strict versioned records, hash links, scoped idempotency and journal ports |
| `authority`, `contracts` | Authorization and deterministic gate evaluation |
| `effects`, `transactions` | Execution/verification transitions and explicit compensation |
| `state`, `replay` | Internal snapshots/restoration and default side-effect-free replay |
| `artifacts`, `receipts`, `evidence_bundle` | Content-addressed blobs, local signatures, bounded offline export/verification |
| `projects`, `planning`, `community` | Local composition and policy explanation, without a second state machine |
| `adapters`, `adapter_conformance`, `effect_conformance` | Edge translation and local fixture checks, not provider/framework certification |
| `reliability`, `faultboard`, `recovery`, `mcp_server` | Deterministic reports, offline presentation and read-only recovery inspection |

The `runtime` module is a compatibility facade, not another implementation of these phases. Optional framework dependencies remain at the integration edge. The local Passport reference module restricts Runtime authority using configured trust and process-local replay checks; it is not the independently maintained standards-based WitnessID product.

## Reproducible local entry point

```bash
llmwitness project init ./reference-project
llmwitness project validate ./reference-project/fliorcie.project.json
llmwitness project plan ./reference-project/fliorcie.project.json --all
llmwitness project run ./reference-project/fliorcie.project.json --all
llmwitness reliability --output-dir .llmwitness/reliability
llmwitness adapter-conformance --output-dir .llmwitness/adapter-conformance
```

The reference refund and CRM jobs are deterministic local fixtures, not live financial/customer operations. Passing local tests does not authorize providers, certify identity standards, admit Bench data, establish outside adoption, or approve publication.

Community remains single-user localhost development. Scrubbing is best-effort pattern scrubbing. Receipts and journals are local, deletable, tamper-evident files, not immutable/WORM storage. A signature establishes integrity of recorded bytes, not external truth. Cloud/Enterprise implementation, distributed transactions, complete telemetry, comprehensive streaming support, compliance claims and production-readiness claims remain excluded.
