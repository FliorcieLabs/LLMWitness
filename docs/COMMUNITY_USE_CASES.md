# Fliorcie Community use cases and boundaries

Use existing orchestrators; add local observations and explicit reliability
boundaries around consequential operations. Start with one use case, not the
entire portfolio. All examples below are unreleased source functionality for
single-user localhost development with Python 3.10+.

| Need | Smallest adoption path | Does not establish |
| --- | --- | --- |
| Debug a coding-agent run without retaining its transcript | [Codex/Claude metadata capture](WORKFLOW_ADOPTION.md) | Complete telemetry or provider truth |
| Correlate model/tool calls across a graph | Optional real LangGraph callbacks inside one UUIDv7 session | Orchestration, graph correctness or metadata-only content policy |
| Review a response-lost action safely | Runtime Safe Effects and read-only recovery inspection | Permission to retry an `UNKNOWN` external write |
| Explain why an operation is denied before dispatch | Side-effect-free project planning | Provider availability or successful execution |
| Check an agent's narrowly scoped authority | Standalone WitnessID gate and optional minimized evidence connector | Effect execution, provider verification or accepted standards/trust decisions |
| Review deterministic failure handling in a pull request | Reliability JSON/JUnit/Markdown and offline Faultboard | A universal reliability score or industry-wide performance |
| Transfer a local run for offline inspection | Evidence Bundle Verifier | External truth, immutable retention or artifact-byte inclusion |

## Ownership stays separate

```text
Codex / Claude / LangChain / LangGraph / caller workflow
  -> LLMWitness observations -> localhost ingestion -> local receipt

optional standalone WitnessID authority evidence
  -> existing Runtime policy/contracts -> Safe Effects -> local journal
                                                     -> recovery inspection
                                                     -> offline evidence bundle
deterministic local fixtures -> Reliability reports -> offline Faultboard
```

Observation timestamps and native completion are not effect verification.
WitnessID evidence cannot grant more authority than Runtime policy permits.
The connector remains optional: neither distribution requires the other for
standalone use. [WitnessID](https://github.com/FliorcieLabs/WitnessID) is a
separate repository/package; `products/` directories are planning workspaces,
not installable distributions. The local Passport reference in LLMWitness
is not a replacement for the standalone WitnessID product.

## Local response-loss review and policy explanation

Install LLMWitness from the source checkout in an activated virtual environment:

```bash
python -m pip install -e .
llmwitness project init ./community-demo
llmwitness project plan ./community-demo/fliorcie.project.json --all
llmwitness project run ./community-demo/fliorcie.project.json --all
```

Reference refund and CRM jobs are deterministic local fixtures, not real money
or customer operations. Planning does not mutate a journal or call a provider.
The runtime owns authority, contracts, idempotency, verification and
compensation. A lost response can remain `UNKNOWN`; inspect recovery rather
than retry blindly. `examples/safe_effects.py` demonstrates this explicitly.

For Python applications, the existing `CommunitySDK().run_sync(config_path)`
composes project execution, journal checks, recovery and reliability reports;
async applications use `await CommunitySDK().run(config_path)` instead.
See [SDK reference](SDK_REFERENCE.md#community-end-to-end-sdk) for actual APIs
and report locations. Do not nest the synchronous entry point inside an event loop.

## Share reproducible local checks

```bash
llmwitness reliability --output-dir ./community-reliability
llmwitness adapter-conformance --output-dir ./community-adapters
python -m llmwitness.faultboard --output-dir ./community-faultboard
```

Use a new output directory where an exporter requires exclusive output.
Review reports before sharing them; a user's input/journal may contain private
metadata. The deterministic corpus and fault visualization are useful for
teaching failure states, comparing a reviewed local baseline, or attaching
reproducible checks to a pull request. They do not prove outside adoption.

Export one recorded Runtime journal run using its actual run ID and configured
journal path:

```bash
llmwitness evidence-bundle export --journal PATH_TO_JOURNAL --run-id RUN_ID --output run.llmwitness-evidence.zip
llmwitness evidence-bundle verify run.llmwitness-evidence.zip
```

The offline verifier checks consistency of included local bytes without
extracting the archive. Referenced artifact bytes and source filesystem paths
are omitted. A telemetry receipt and a Runtime journal bundle are different
artifacts; a capture JSONL file cannot be substituted for either.

## Portfolio adoption priorities

1. Opt-in metadata-only LangChain/LangGraph callbacks, with bounded run-state
   and long-lived/concurrent callback regressions.
2. A separately specified local Proof Card renderer for already-checked report
   files: explicit checks, `UNKNOWN` and unresolved gates; no scores or new verifier.
3. One native integration at a time, including OpenAI Responses API and an
   independently run external-repository pilot, with versioned fixtures.
4. Approved provider-adapter sandbox work only after provider choice,
   reconciliation contracts, credentials and human authorization.

Candidates 1 and 2, and plain sync/async Responses capture from candidate 3,
are now implemented locally; see [adoption extensions](ADOPTION_EXTENSIONS.md).
Independent external adoption and approved provider work remain open. Provider Adapter
Kit presently means protocol/fixture conformance inside `llmwitness`, not live
approved refund/CRM adapters. Agent Evidence Bench presently has governance
intake/schema validation, not an admitted dataset, model runner or leaderboard.
Naming/IP clearance, release controls, outside-user evidence, Passport review
acceptance and governed Bench data remain human-controlled gates. Cloud,
Enterprise, multi-tenancy and managed dashboards remain deferred.

Community uses best-effort pattern scrubbing and best-effort telemetry delivery.
No hosted service/account is required, but configured provider clients, gateway
upstreams, OTLP export or timestamp authorities can send requests off-device.
"Local-first" does not mean every integration is network-isolated. Receipts are
ordinary local tamper-evident files; no compliance, immutable/WORM retention,
universal performance or production-readiness claim is made.
