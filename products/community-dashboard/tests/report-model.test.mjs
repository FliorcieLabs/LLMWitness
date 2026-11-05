import assert from "node:assert/strict";
import test from "node:test";

import { ReportParseError, createDemoReports, exportSnapshot, parseReport } from "../assets/report-model.mjs";

const RUN_ID = "018f2f0a-3b4c-7d5e-8f90-123456789abc";
const SECRET = "prompt do not expose sk-abcdefghijklmnopqrstuvwxyz";

function reliabilityReport() {
  return {
    schema_version: "0.1",
    sample_count: 2,
    passed: 1,
    failed: 1,
    dimensions: [
      { name: "native secret name", invariant: SECRET, passed: true, duration_ms: 4.5, detail: SECRET },
      { name: "native failure name", invariant: "native", passed: false, duration_ms: 7, detail: SECRET },
    ],
    limitations: SECRET,
  };
}

function adaptersReport() {
  return {
    schema_version: "0.1",
    fixture_id: "mapping-equivalence-v0.1",
    sample_count: 2,
    passed: 1,
    failed: 1,
    results: [
      { adapter_name: SECRET, passed: true, dimensions: [{ name: "translation", passed: true, detail: SECRET }] },
      { adapter_name: "private adapter", passed: false, dimensions: [{ name: "translation", passed: false, detail: SECRET }] },
    ],
    limitations: SECRET,
  };
}

function proofCard() {
  return {
    schema_version: "0.1",
    checks: {
      reliability: { status: "passed", total: 2, passed: 2, failed: 0, source_ref: `sha256:${"a".repeat(64)}` },
      adapters: { status: "failed", total: 1, passed: 0, failed: 1, source_ref: `sha256:${"b".repeat(64)}` },
      bundle: { status: "not_checked" },
      authority: { status: "unknown", source_ref: `sha256:${"d".repeat(64)}` },
    },
    credentials: SECRET,
  };
}

function workflowLine(overrides = {}) {
  return {
    correlation_id: RUN_ID,
    task_name: "workflow:generic",
    timestamp: 123,
    prompt_tokens: 3,
    completion_tokens: 5,
    completion_string: null,
    tool_calls: [],
    agent_state: {
      schema_version: "0.1",
      source: "generic",
      source_event: "workflow.completed",
      outcome: "completed",
      native_references: {},
    },
    provider: "generic",
    model: null,
    latency_ms: 9,
    input_messages: [],
    estimated_cost_usd: null,
    error: null,
    input: SECRET,
    output: SECRET,
    ...overrides,
  };
}

test("projects actual reliability shape while omitting all report prose", async () => {
  const report = await parseReport(JSON.stringify(reliabilityReport()));

  assert.deepEqual(report.counts, { total: 2, passed: 1, failed: 1 });
  assert.deepEqual(report.rows.map((row) => row.label), ["Scenario 01", "Scenario 02"]);
  assert.equal(report.rows[0].durationMs, 4.5);
  assert.equal(JSON.stringify(report).includes(SECRET), false);
  assert.equal(report.sourceRef.startsWith("sha256:"), true);
});

test("projects mapping conformance shape and rejects inconsistent dimensions", async () => {
  const report = await parseReport(JSON.stringify(adaptersReport()));
  assert.deepEqual(report.rows.map((row) => row.label), ["Adapter 01", "Adapter 02"]);
  assert.equal(JSON.stringify(report).includes(SECRET), false);

  const invalid = adaptersReport();
  invalid.results[0].passed = false;
  await assert.rejects(() => parseReport(JSON.stringify(invalid)), ReportParseError);
});

test("projects the actual fixed Proof Card check object without upgrading unknown or not_checked", async () => {
  const report = await parseReport(JSON.stringify(proofCard()));
  assert.equal(report.kind, "proof-card");
  assert.deepEqual(report.rows.map((row) => row.status), ["passed", "failed", "not_checked", "unknown"]);
  assert.equal(JSON.stringify(report).includes(SECRET), false);

  const invalid = proofCard();
  invalid.checks.bundle.status = "verified";
  await assert.rejects(() => parseReport(JSON.stringify(invalid)), ReportParseError);
});

test("preserves UUIDv7 correlation and only accepts actual workflow-capture profiles", async () => {
  const failed = workflowLine({
    agent_state: { schema_version: "0.1", source: "generic", source_event: "workflow.failed", outcome: "failed", native_references: {} },
    error: "workflow source reported a failure",
  });
  const input = `${JSON.stringify(workflowLine())}\n${JSON.stringify(failed)}`;
  const report = await parseReport(input);

  assert.equal(report.kind, "workflow");
  assert.equal(report.rows[0].correlationId, RUN_ID);
  assert.equal(report.rows[1].status, "failed");
  assert.equal(JSON.stringify(report).includes(SECRET), false);

  await assert.rejects(
    () => parseReport(JSON.stringify(workflowLine({ provider: "untrusted-provider" }))),
    ReportParseError,
  );
  await assert.rejects(
    () => parseReport(JSON.stringify(workflowLine({ correlation_id: "not-a-uuid" }))),
    ReportParseError,
  );
});

test("accepts strict LangChain and Responses metadata-only extensions", async () => {
  const langchain = workflowLine({
    task_name: null,
    provider: "langchain-metadata",
    latency_ms: 2,
    agent_state: { schema_version: "0.1", kind: "model", outcome: "completed", run_ref: `sha256:${"e".repeat(64)}` },
  });
  const responses = workflowLine({
    task_name: null,
    provider: "openai-responses",
    latency_ms: 3,
    agent_state: { schema_version: "0.1", status: "incomplete", response_ref: `sha256:${"f".repeat(64)}` },
  });
  const report = await parseReport(`${JSON.stringify(langchain)}\n${JSON.stringify(responses)}`);

  assert.deepEqual(report.rows.map((row) => row.source), ["langchain-metadata", "openai-responses"]);
  assert.deepEqual(report.rows.map((row) => row.status), ["completed", "incomplete"]);
});

test("rejects duplicate keys, count contradictions, booleans, excessive rows and excessive bytes", async () => {
  await assert.rejects(
    () => parseReport('{"schema_version":"0.1","schema_version":"0.1"}'),
    /duplicate JSON keys/,
  );
  const invalidCounts = reliabilityReport();
  invalidCounts.passed = 2;
  await assert.rejects(() => parseReport(JSON.stringify(invalidCounts)), ReportParseError);

  const booleanCount = reliabilityReport();
  booleanCount.sample_count = true;
  await assert.rejects(() => parseReport(JSON.stringify(booleanCount)), ReportParseError);

  const oversized = " ".repeat(1_048_577);
  await assert.rejects(() => parseReport(oversized), /input limit/);

  const tooMany = reliabilityReport();
  tooMany.sample_count = 1_001;
  tooMany.passed = 1_001;
  tooMany.failed = 0;
  tooMany.dimensions = Array.from({ length: 1_001 }, () => ({ passed: true, duration_ms: 0 }));
  await assert.rejects(() => parseReport(JSON.stringify(tooMany)), ReportParseError);

  const deeplyNested = `${'{"child":'.repeat(65)}null${"}".repeat(65)}`;
  await assert.rejects(() => parseReport(deeplyNested), /nesting is too deep/);

  const hostile = JSON.stringify({ schema_version: "0.1", details: SECRET });
  await assert.rejects(
    () => parseReport(hostile),
    (error) => error instanceof ReportParseError && !error.message.includes(SECRET),
  );
});

test("exports deterministic minimized snapshots without source report content", async () => {
  const reports = [
    await parseReport(JSON.stringify(reliabilityReport())),
    await parseReport(JSON.stringify(adaptersReport())),
    await parseReport(JSON.stringify(proofCard())),
    await parseReport(JSON.stringify(workflowLine())),
  ];
  const snapshot = exportSnapshot(reports);
  const again = exportSnapshot(reports);

  assert.equal(snapshot, again);
  assert.equal(snapshot.includes(SECRET), false);
  assert.equal(snapshot.includes("native failure name"), false);
  const parsed = JSON.parse(snapshot);
  assert.equal(parsed.schema_version, "0.1");
  assert.equal(parsed.reports.length, 4);
});

test("creates clearly synthetic examples that include unknown status", async () => {
  const reports = await createDemoReports();

  assert.equal(reports.length, 4);
  assert.equal(reports.every((report) => report.title.startsWith("Synthetic demo — ")), true);
  assert.equal(reports.some((report) => report.rows.some((row) => row.status === "unknown")), true);
});

test("unreported duration stays absent, never a zero-time measurement", async () => {
  const adapter = await parseReport(JSON.stringify(adaptersReport()));
  const card = await parseReport(JSON.stringify(proofCard()));
  const workflow = await parseReport(JSON.stringify(workflowLine({ latency_ms: null })));
  assert.equal(adapter.rows[0].durationMs, null);
  assert.equal(card.rows[0].durationMs, null);
  assert.equal(workflow.rows[0].durationMs, null);
  assert.equal(JSON.parse(exportSnapshot([workflow])).reports[0].rows[0].durationMs, null);
});

test("contradictory Proof Card status and counts fail closed", async () => {
  const card = proofCard();
  card.checks.reliability.failed = 1;
  card.checks.reliability.passed = 1;
  await assert.rejects(() => parseReport(JSON.stringify(card)), ReportParseError);
  card.checks.reliability = { status: "not_checked", total: 1, passed: 1, failed: 0 };
  await assert.rejects(() => parseReport(JSON.stringify(card)), ReportParseError);
});

test("bundle declarations cannot pass with empty or absent verification counts", async () => {
  for (const check of [{ status: "passed" }, { status: "passed", entries_checked: 0 }, { status: "passed", entries_checked: true }]) {
    const card = proofCard(); card.checks.bundle = check;
    await assert.rejects(() => parseReport(JSON.stringify(card)), ReportParseError);
  }
});

test("strict JSON rejects nonfinite numbers, non-JSON whitespace and 65 empty containers", async () => {
  for (const raw of ['{"x":NaN}', '{"x":1e999}', '\u00a0{}', '['.repeat(65) + ']'.repeat(65), '{"schema_version":"0.1","x":[1,]}']) {
    await assert.rejects(() => parseReport(raw), ReportParseError);
  }
});

test("privacy survives HTML injection, prototype keys and escaped duplicate keys", async () => {
  const source = reliabilityReport();
  source.dimensions[0].name = '<img src=x onerror=alert(1)>';
  source["__proto__"] = { inherited: SECRET };
  const parsed = await parseReport(JSON.stringify(source));
  assert.ok(!JSON.stringify(parsed).includes('<img'));
  await assert.rejects(() => parseReport('{"x":1,"\\u0078":2}'), /duplicate/);
});

test("negative and nonnumeric duration or token metadata is rejected", async () => {
  for (const duration of [-1, "2", true]) {
    const source = reliabilityReport(); source.dimensions[0].duration_ms = duration;
    await assert.rejects(() => parseReport(JSON.stringify(source)), ReportParseError);
  }
  for (const tokens of [-1, true, 1.5]) {
    await assert.rejects(() => parseReport(JSON.stringify(workflowLine({ prompt_tokens: tokens }))), ReportParseError);
  }
});
