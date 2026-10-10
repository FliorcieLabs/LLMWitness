# LLMWitness Community

LLMWitness Community is a **local-first telemetry toolkit for AI-agent runs**. It correlates SDK, gateway, and browser events with UUIDv7 identifiers, applies best-effort pattern scrubbing to audit copies, and creates locally verifiable tamper-evident receipts.

> **Community limitations:** `0.1.0` is intended for single-user localhost development. Pattern scrubbing cannot guarantee removal of all personal or secret data. Local receipt files are ordinary files—not immutable storage, WORM storage, legal evidence, or a compliance certification. Telemetry delivery is best-effort. Do not expose the services to untrusted networks or use them as a high-security production control plane.

![LLMWitness receipt verification demonstration](docs/assets/launch/demo.gif)

The demonstration uses synthetic data. It verifies an untouched local receipt, changes one signed field, and shows the modified copy being rejected.

## Available now

- Python SDK with a bounded background telemetry queue and observable drop/failure counters.
- RFC 9562 UUIDv7 execution correlation.
- Localhost OpenAI chat-completions JSON proxy.
- Best-effort SSN, payment-card, token, sensitive-field, and image-payload pattern scrubbing for audit copies.
- Local Ed25519-signed receipts with optional HMAC verification.
- Browser SDK and opt-in Manifest V3 extension.
- `llmwitness verify` and `llmwitness validate-config` commands.

The gateway returns the upstream response body to the application without applying audit redaction to that response. Broad OpenAI or Anthropic API compatibility, multi-tenancy, guaranteed delivery, and distributed operation are not claimed.

## Unreleased local durability and tooling

These changes are on the development branch and are not part of `0.1.0`.

- **Sessions survive a restart:** the ingestion service mirrors unsealed sessions to a local SQLite file (`LLMWITNESS_SESSION_DB`, default `.llmwitness/sessions.db`; `off` disables it). At the session limit the oldest sealed session is dropped from memory first; an unsealed session is dropped only after 24 hours without events (`LLMWITNESS_SESSION_IDLE_TTL_SECONDS`).
- **Persistent signer:** the service creates a signing key and HMAC secret in `.llmwitness/keys` on first start, or run `llmwitness keygen`. `llmwitness verify` then requires a receipt's signer to match that key (or `LLMWITNESS_TRUSTED_FINGERPRINT`) unless `--any-signer` is given. The key file is an ordinary file: anyone who can read it can sign.
- **Receipt chain:** each receipt signs a link to the previous one; `llmwitness verify-chain` reports broken links or invalid signatures in the supplied chain. Detecting deletion of the terminal suffix requires an independently retained checkpoint. This is local tamper evidence, not immutability.
- **Delivery retry and spool:** the SDK and gateway retry a failed submission, then write it to `.llmwitness/spool` (`LLMWITNESS_SPOOL_DIR`, `off` disables it) and re-send it later. Delivery remains best-effort and at-least-once; events are still dropped when the SDK queue is full.
- **Scrubbing:** card numbers must pass the Luhn check, email addresses and formatted phone numbers are redacted, and `LLMWITNESS_SCRUB_RULES_FILE` adds your own patterns and field names. Names and addresses are not detected unless you register a scrubber for them.
- **Gateway:** an Anthropic `POST /v1/messages` route, and experimental local server-sent-event pass-through for `"stream": true` on both routes. The audit copy holds scrubbed assembled text, not raw chunks; complete streaming capture and broad provider compatibility are not claimed.
- **SDK capture:** model, prompt messages, latency and an optional cost estimate from prices you supply; sync, async and streaming OpenAI and Anthropic clients; `tracker.seal()` and `trace_session(..., auto_seal=True)`.
- **Commands:** `llmwitness serve`, `seal`, `list`, `show` (terminal or `--html`), `diff`, `verify-chain`, `export-otel` and `timestamp`.
- **Integrations:** a `@witness` decorator and a LangChain callback handler (`llmwitness.integrations`).
- **Browser extension:** nothing is captured until you allow a site from the extension popup, and a visible notice stays on the page while it is recorded.

## Unreleased Community reliability workflow

The development branch also contains an unreleased local reliability workflow: Execution Envelope v0.1, a SQLite/WAL hash-linked journal, persistent idempotency records, contracts and authority checks, Safe Effects, Saga-style compensation, internal state snapshots, guarded replay, dependency-isolated framework translators, side-effect-free project planning, local provider-adapter conformance fixtures, a deterministic reliability corpus with versioned baseline comparison, and a local STDIO MCP server. These are unreleased Community features, not capabilities of the published `0.1.0` package.

The checked-in [Execution Envelope v0.1 schema](schemas/execution-envelope-v0.1.schema.json) is generated from the same strict runtime model exposed by `llmwitness schema`.

For a local visual tour of the deterministic fault corpus, run:

```bash
python -m llmwitness.faultboard --output-dir .llmwitness/faultboard/my-run
```

Open the generated `faultboard.html` locally. It uses the same Reliability
results as JSON, JUnit, and Markdown; it is an unreleased offline presentation
tool, not an additional safety or production claim.

Python applications can compose the full local Community reference path with
`CommunitySDK`: project validation, authority/contracts/Safe Effects, journal
verification, read-only recovery inspection, and deterministic Reliability
reports. See the [SDK reference](docs/SDK_REFERENCE.md#community-end-to-end-sdk).

A verified local journal run can be exported and checked without SQLite or a
network connection:

```bash
llmwitness evidence-bundle export --journal .llmwitness/journal.db --run-id RUN_ID --output run.llmwitness-evidence.zip
llmwitness evidence-bundle verify run.llmwitness-evidence.zip
```

The bundle omits referenced artifact bytes and source filesystem paths. A
passing check establishes consistency of the recorded local bytes; it does not
prove external truth or provide immutable or WORM storage.

Before proposing a real provider adapter, maintainers can validate a
metadata-only dossier without reading credentials or contacting the provider:

```bash
llmwitness provider-dossier provider-dossier.json --output-dir .llmwitness/provider-readiness
```

A passing dossier is only structurally ready for human review. It does not
approve the adapter, validate supplied references, or replace provider-specific
sandbox and conformance testing.

The same fail-closed preparation pattern is available for future Agent Evidence
Bench governance:

```bash
llmwitness bench-intake bench-intake.json --output-dir .llmwitness/bench-intake
```

This validates metadata and declared safeguards only. It does not read a
dataset or holdout, admit data, run baselines/models, or replace data-owner and
independent-reviewer acceptance.

Maintainers can prepare—but never authorize—a release review with:

```bash
llmwitness release-readiness release.json --output-dir .llmwitness/release-readiness
```

The offline report checks declared repository, CI, account-control, publishing,
artifact, SBOM/provenance, incident, and decision metadata. It does not verify
external settings, create a tag/release, or publish a package.

## Community architecture

![LLMWitness Community architecture](docs/assets/launch/architecture.png)

## Install

Install the current Community source in an isolated virtual environment. This
keeps the CLI and the source checkout on the same version; the older registry
artifact does not include the unreleased Runtime and Reliability modules.

```bash
git clone https://github.com/FliorcieLabs/LLMWitness.git
cd LLMWitness
python -m venv .venv
source .venv/bin/activate                 # Linux/macOS
# .venv\Scripts\Activate.ps1             # Windows PowerShell
python -m pip install --upgrade pip
python -m pip install .
llmwitness --help
```

For contributors and local verification, install the development extras:

```bash
python -m pip install -e ".[dev]"
```

For a laptop or VM without a source checkout, build a wheel on a trusted
machine and copy the resulting file from `dist/`:

```bash
python -m build --wheel
python -m pip install llmwitness-0.1.0-py3-none-any.whl
```

The wheel contains the complete Python Community package and its CLI; it does
not require the repository working directory at runtime. Keep the generated
`.llmwitness/` directory local to each installation.

Start the local services in separate terminals:

```bash
python -m uvicorn llmwitness.ingest:app --host 127.0.0.1 --port 8000
python -m uvicorn llmwitness.gateway:app --host 127.0.0.1 --port 8011
```

## Python SDK

```python
from llmwitness import LLMWitnessTracker

tracker = LLMWitnessTracker(ingestion_url="http://127.0.0.1:8000")
with tracker.trace_session("example") as correlation_id:
    tracker.record_event(
        completion_string="Example output",
        agent_state={"step": 1},
    )

tracker.flush()
tracker.shutdown()
print(correlation_id, tracker.dropped_events, tracker.delivery_failures)
```

On the development branch, `tracker.wrap_openai_client(client)` and
`tracker.wrap_anthropic_client(client)` record each call's model, prompt,
latency and output, and a run can seal itself:

```python
with tracker.trace_session("example", auto_seal=True):
    client.chat.completions.create(model="...", messages=[...])
print(tracker.last_receipt["receipt_file"], tracker.stats())
```

If `LLMWITNESS_INGEST_TOKEN` is configured on the ingestion service, the SDK and gateway read the same variable and authenticate their telemetry submissions. Browser components must receive the matching token explicitly; see the [browser extension setup](examples/browser_extension_setup.md). Without a token, ingestion is restricted to loopback development clients.

## Examples

See [`examples/README.md`](examples/README.md) for small integration snippets covering OpenAI wrapping, LangChain, LangGraph, AutoGen/CrewAI-style runs, and browser extension setup.

## Create and verify a receipt

After telemetry exists for a correlation ID:

```bash
curl -X POST http://127.0.0.1:8000/ingest/seal \
  -H "Content-Type: application/json" \
  -d '{"correlation_id":"YOUR_UUIDV7"}'

llmwitness verify .llmwitness/receipts/YOUR_UUIDV7.json
```

On the development branch the same flow is:

```bash
llmwitness serve                       # ingestion on 8000, gateway on 8011
llmwitness seal YOUR_UUIDV7
llmwitness list
llmwitness show YOUR_UUIDV7            # add --html run.html for a static page
llmwitness verify .llmwitness/receipts/YOUR_UUIDV7.json
llmwitness verify-chain
```

Run deterministic mapping-level conformance for every supported framework
translator without installing the optional frameworks:

```bash
llmwitness adapter-conformance --output-dir .llmwitness/adapter-conformance
```

The result covers the normalized mapping boundary only. It does not certify a
real framework SDK, native callback, framework version, or external deployment.

Verification proves that the signed fields match the public key embedded in the receipt. It does **not** prove who controlled that key. Compare the displayed fingerprint with a trusted value when signer identity matters. Configure persistent key material before expecting verification across restarts; automatically generated keys are development-only.

## Product boundary

**LLMWitness Community — available in this Apache-2.0 repository:** the SDK, localhost gateway, heuristic scrubber, local ingestion service, local tamper-evident receipts, browser components, and CLI.

**LLMWitness Cloud — planned, not available:** a managed service, team dashboard, hosted analytics, and operational management. Its implementation is not in this repository and is intended to remain proprietary.

**LLMWitness Enterprise — planned, not available:** multi-tenancy, RBAC/SSO, distributed storage and cache, Object Lock, HSM/KMS, policy enforcement, high availability, reporting workflows, and enterprise UI. These components are not implemented here and are intended to remain proprietary.

LLMWitness does not establish compliance with HIPAA, GDPR, SOC 2, the EU AI Act, or any other law or framework. Applicability depends on the deployment, processing purposes, jurisdiction, contracts, technical controls, and organizational practices. Obtain qualified legal and security advice.

## Branch workflow

Public contributors can clone the repository and open pull requests from feature branches or forks into `integ`. After review and CI, `integ` can be merged into `main` through a pull request. Direct pushes to `integ` and `main` are blocked.

## Security model

Read [SECURITY.md](SECURITY.md) before using the project. The Community edition is not an authentication, authorization, replay-prevention, DLP, retention, or compliance system. UUIDv7 provides correlation and approximate creation ordering; it is not a security token.

## Benchmarks

The repository includes a machine-specific development benchmark reference with p50, p90, p95, p99, mean, standard deviation, minimum, and maximum latency. It records the exact commit, runtime, dependency versions, workload, sample count, and warmup count. These microbenchmarks are reproducibility and regression references--not production latency, throughput, scalability, comparative-performance, or service-level claims.

Run the machine-specific development benchmark harness locally:

```bash
python -m benchmarks.benchmark_local --iterations 1000 --warmup 100
```

See [benchmarks/README.md](benchmarks/README.md) for methodology, limitations, the published reference report, and raw JSON.

## License

LLMWitness Community is licensed under [Apache License 2.0](LICENSE). That license permits copying, modification, redistribution, and commercial use subject to its terms. Public code cannot be made physically impossible to copy; proprietary Cloud and Enterprise implementations must remain in separate private repositories.
