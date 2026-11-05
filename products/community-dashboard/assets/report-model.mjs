/**
 * Strict, local-only projections for Community dashboard reports.
 *
 * This module deliberately turns trusted-looking reports into a smaller view
 * model. It preserves caller UUIDv7 only, never native identifiers, report prose, prompts, model
 * data, credentials, or error text.
 */

const MAX_BYTES = 1_048_576;
const MAX_DEPTH = 64;
const MAX_ROWS = 1_000;
const MAX_COUNT = 1_000_000;
const MAX_DURATION_MS = 86_400_000;
const MAX_TOKENS = 10_000_000;
const SHA256_REFERENCE = /^sha256:[0-9a-f]{64}$/;
const UUID_V7 = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const PROOF_KINDS = ["reliability", "adapters", "bundle", "authority"];
const PROOF_STATUS = new Set(["passed", "failed", "unknown", "not_checked", "allowed", "denied"]);
const WORKFLOW_SOURCES = new Set([
  "codex-jsonl",
  "claude-code-hook",
  "generic",
]);
const WORKFLOW_EVENTS = new Set([
  "thread.started",
  "turn.started",
  "turn.completed",
  "turn.failed",
  "item.started",
  "item.updated",
  "item.completed",
  "error",
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "Stop",
  "SessionEnd",
  "SubagentStart",
  "SubagentStop",
  "StopFailure",
  "workflow.started",
  "workflow.completed",
  "workflow.failed",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "usage",
]);
const WORKFLOW_OUTCOMES = new Set(["started", "completed", "failed", "updated", "observed"]);
const RESPONSE_STATUS = new Set(["completed", "failed", "in_progress", "cancelled", "queued", "incomplete", "unknown"]);

export class ReportParseError extends Error {
  constructor(message = "Unsupported local report.") {
    super(message);
    this.name = "ReportParseError";
  }
}

/**
 * Project one supported local report into a UI-safe value object.
 *
 * @param {string} text complete UTF-8 report content
 * @returns {Promise<object>}
 */
export async function parseReport(text) {
  assertTextWithinBound(text);
  const sourceRef = await digestReference(text);
  const object = parseOneJsonDocument(text);

  if (object !== null) {
    return projectJsonReport(object, sourceRef);
  }
  return projectWorkflowJsonl(text, sourceRef);
}

/**
 * Create explicit, synthetic local examples.  They are not measurements.
 *
 * @returns {Promise<object[]>}
 */
export async function createDemoReports() {
  const correlationId = "018f2f0a-3b4c-7d5e-8f90-123456789abc";
  const reports = [
    {
      schema_version: "0.1",
      sample_count: 2,
      passed: 1,
      failed: 1,
      dimensions: [
        { name: "synthetic-one", invariant: "synthetic", passed: true, duration_ms: 12, detail: "synthetic" },
        { name: "synthetic-two", invariant: "synthetic", passed: false, duration_ms: 8, detail: "synthetic" },
      ],
      limitations: "synthetic demo",
    },
    {
      schema_version: "0.1",
      fixture_id: "mapping-equivalence-v0.1",
      sample_count: 1,
      passed: 1,
      failed: 0,
      results: [{ adapter_name: "synthetic", passed: true, dimensions: [{ name: "translation", passed: true, detail: "synthetic" }] }],
      limitations: "synthetic demo",
    },
    {
      schema_version: "0.1",
      checks: {
        reliability: { status: "not_checked" },
        adapters: { status: "not_checked" },
        bundle: { status: "not_checked", source_ref: `sha256:${"0".repeat(64)}` },
        authority: { status: "unknown" },
      },
    },
  ];
  const workflow = JSON.stringify({
    correlation_id: correlationId,
    task_name: "workflow:generic",
    timestamp: 0,
    prompt_tokens: 0,
    completion_tokens: 0,
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
    latency_ms: null,
    input_messages: [],
    estimated_cost_usd: null,
    error: null,
  });

  const projected = await Promise.all(reports.map((report) => parseReport(JSON.stringify(report))));
  projected.push(await parseReport(workflow));
  return projected.map((report) => ({ ...report, title: `Synthetic demo — ${report.title}` }));
}

/**
 * Export only the already minimized view model.  This is a convenience
 * snapshot, not a verifier, signature check, or statement about external
 * truth.
 *
 * @param {object[]} reports projected reports returned by parseReport
 * @returns {string} canonical minimized JSON
 */
export function exportSnapshot(reports) {
  if (!Array.isArray(reports) || reports.length > MAX_ROWS) {
    throw new ReportParseError("Snapshot contains an unsupported number of reports.");
  }
  return JSON.stringify(
    {
      schema_version: "0.1",
      reports: reports.map(sanitizeProjectedReport),
    },
    null,
    2,
  );
}

function projectJsonReport(value, sourceRef) {
  if (!isPlainObject(value)) {
    throw unsupported();
  }
  if (typeof value.correlation_id === "string") return projectWorkflowEvents([value], sourceRef);
  if (value.schema_version !== "0.1") {
    throw unsupported();
  }
  if (Array.isArray(value.dimensions)) {
    return projectReliability(value, sourceRef);
  }
  if (value.fixture_id === "mapping-equivalence-v0.1" && Array.isArray(value.results)) {
    return projectAdapters(value, sourceRef);
  }
  if (isPlainObject(value.checks)) {
    return projectProofCard(value, sourceRef);
  }
  throw unsupported();
}

function projectReliability(value, sourceRef) {
  const counts = readCounts(value, "sample_count", "passed", "failed");
  const dimensions = readArray(value.dimensions);
  if (dimensions.length !== counts.total) {
    throw unsupported();
  }
  const rows = dimensions.map((dimension, index) => {
    if (!isPlainObject(dimension) || typeof dimension.passed !== "boolean") {
      throw unsupported();
    }
    return {
      id: rowId("reliability", index),
      label: numberedLabel("Scenario", index),
      category: "Reliability",
      status: dimension.passed ? "passed" : "failed",
      durationMs: readDuration(dimension.duration_ms),
      source: "reliability",
    };
  });
  assertBooleanCounts(rows, counts);
  return { kind: "reliability", title: "Reliability report", sourceRef, counts, rows };
}

function projectAdapters(value, sourceRef) {
  const counts = readCounts(value, "sample_count", "passed", "failed");
  const results = readArray(value.results);
  if (results.length !== counts.total) {
    throw unsupported();
  }
  const rows = results.map((result, index) => {
    if (!isPlainObject(result) || typeof result.passed !== "boolean") {
      throw unsupported();
    }
    const dimensions = readArray(result.dimensions);
    if (dimensions.length === 0 || dimensions.length > MAX_ROWS) {
      throw unsupported();
    }
    const dimensionPasses = dimensions.map((dimension) => {
      if (!isPlainObject(dimension) || typeof dimension.passed !== "boolean") {
        throw unsupported();
      }
      return dimension.passed;
    });
    if (result.passed !== dimensionPasses.every(Boolean)) {
      throw unsupported();
    }
    return {
      id: rowId("adapter", index),
      label: numberedLabel("Adapter", index),
      category: "Adapter conformance",
      status: result.passed ? "passed" : "failed",
      durationMs: null,
      source: "adapters",
    };
  });
  assertBooleanCounts(rows, counts);
  return { kind: "adapters", title: "Adapter conformance", sourceRef, counts, rows };
}

function projectProofCard(value, sourceRef) {
  const checks = value.checks;
  if (!isPlainObject(checks) || !sameKeys(checks, PROOF_KINDS)) {
    throw unsupported();
  }
  const rows = PROOF_KINDS.map((kind, index) => {
    const check = checks[kind];
    validateProofCheck(kind, check);
    return {
      id: rowId("proof", index),
      label: { reliability: "Reliability checks", adapters: "Adapter checks", bundle: "Bundle byte consistency", authority: "Authority declaration" }[kind],
      category: "Proof Card",
      status: check.status,
      durationMs: null,
      source: "proof-card",
    };
  });
  const counts = {
    total: rows.length,
    passed: rows.filter((row) => row.status === "passed").length,
    failed: rows.filter((row) => row.status === "failed").length,
  };
  return { kind: "proof-card", title: "Offline Proof Card", sourceRef, counts, rows, checks: rows.map((row) => ({ ...row })) };
}

function projectWorkflowJsonl(text, sourceRef) {
  const lines = text.split(/\r?\n/).filter((line) => line.trim().length > 0);
  if (lines.length === 0 || lines.length > MAX_ROWS) {
    throw unsupported();
  }
  return projectWorkflowEvents(lines.map((line) => parseStrictJson(line)), sourceRef);
}

function projectWorkflowEvents(events, sourceRef) {
  const rows = events.map((event, index) => projectWorkflowEvent(event, index));
  const counts = {
    total: rows.length,
    passed: rows.filter((row) => row.status === "completed").length,
    failed: rows.filter((row) => row.status === "failed").length,
  };
  return { kind: "workflow", title: "Workflow metadata capture", sourceRef, counts, rows };
}

function projectWorkflowEvent(value, index) {
  if (!isPlainObject(value) || typeof value.correlation_id !== "string" || !UUID_V7.test(value.correlation_id)) {
    throw unsupported();
  }
  if (value.provider === "langchain-metadata") return projectLangChainMetadata(value, index);
  if (value.provider === "openai-responses") return projectResponsesMetadata(value, index);
  return projectWorkflowCapturePayload(value, index);
}

function projectWorkflowCapturePayload(value, index) {
  const state = value.agent_state;
  if (
    !isPlainObject(state) ||
    state.schema_version !== "0.1" ||
    !WORKFLOW_SOURCES.has(state.source) ||
    !WORKFLOW_EVENTS.has(state.source_event) ||
    !WORKFLOW_OUTCOMES.has(state.outcome) ||
    value.provider !== state.source ||
    value.task_name !== `workflow:${state.source}` ||
    !isFiniteNonnegative(value.timestamp) ||
    value.completion_string !== null ||
    !isEmptyArray(value.tool_calls) ||
    value.model !== null ||
    !(value.latency_ms === null || isFiniteNonnegative(value.latency_ms)) ||
    !isEmptyArray(value.input_messages) ||
    value.estimated_cost_usd !== null ||
    !(value.error === null || value.error === "workflow source reported a failure") ||
    !isReferenceMap(state.native_references)
  ) throw unsupported();
  return workflowRow(value, index, state.outcome, state.source, value.latency_ms ?? null);
}

function projectLangChainMetadata(value, index) {
  const state = value.agent_state;
  if (
    !isPlainObject(state) ||
    state.schema_version !== "0.1" ||
    !["model", "tool"].includes(state.kind) ||
    !["completed", "failed"].includes(state.outcome) ||
    !isDigest(state.run_ref) ||
    !hasNoRawCaptureFields(value)
  ) throw unsupported();
  return workflowRow(value, index, state.outcome, "langchain-metadata", value.latency_ms ?? null);
}

function projectResponsesMetadata(value, index) {
  const state = value.agent_state;
  if (
    !isPlainObject(state) ||
    state.schema_version !== "0.1" ||
    !RESPONSE_STATUS.has(state.status) ||
    !(state.response_ref === undefined || isDigest(state.response_ref)) ||
    !hasNoRawCaptureFields(value)
  ) throw unsupported();
  return workflowRow(value, index, state.status, "openai-responses", value.latency_ms ?? null);
}

function workflowRow(value, index, status, source, durationMs) {
  return {
    id: rowId("workflow", index),
    label: numberedLabel("Workflow", index),
    category: "Workflow metadata",
    status,
    durationMs: durationMs === null ? null : readDuration(durationMs),
    correlationId: value.correlation_id,
    promptTokens: readTokens(value.prompt_tokens ?? 0),
    completionTokens: readTokens(value.completion_tokens ?? 0),
    source,
  };
}

function sanitizeProjectedReport(report) {
  if (!isPlainObject(report) || !["reliability", "adapters", "proof-card", "workflow"].includes(report.kind)) {
    throw new ReportParseError("Snapshot contains an unsupported report.");
  }
  if (!isDigest(report.sourceRef) || !isPlainObject(report.counts)) {
    throw new ReportParseError("Snapshot contains an unsupported report.");
  }
  const counts = readCounts(report.counts, "total", "passed", "failed", { allowIncomplete: true });
  const rows = readArray(report.rows).map((row, index) => sanitizeProjectedRow(row, index));
  if (rows.length > MAX_ROWS || rows.length !== counts.total) {
    throw new ReportParseError("Snapshot contains an unsupported report.");
  }
  const sanitized = {
    kind: report.kind,
    title: fixedTitle(report.kind),
    source_ref: report.sourceRef,
    counts,
    rows,
  };
  if (report.kind === "proof-card") {
    sanitized.checks = rows.map((row) => ({ id: row.id, status: row.status, source: row.source }));
  }
  return sanitized;
}

function sanitizeProjectedRow(row, index) {
  if (!isPlainObject(row) || typeof row.status !== "string" || !(row.durationMs === null || Number.isFinite(row.durationMs))) {
    throw new ReportParseError("Snapshot contains an unsupported report.");
  }
  const source = typeof row.source === "string" ? row.source : "unknown";
  const allowedStatus = new Set(["passed", "failed", "unknown", "not_checked", "allowed", "denied", "started", "completed", "updated", "observed", "in_progress", "cancelled", "queued", "incomplete"]);
  const allowedSource = new Set([...WORKFLOW_SOURCES, "langchain-metadata", "openai-responses", "reliability", "adapters", "proof-card"]);
  if (!allowedStatus.has(row.status) || !allowedSource.has(source)) {
    throw new ReportParseError("Snapshot contains an unsupported report.");
  }
  const result = {
    id: typeof row.id === "string" && /^[a-z]+-\d{3,4}$/.test(row.id) ? row.id : rowId("row", index),
    label: numberedLabel("Item", index),
    category: "Local metadata",
    status: row.status,
    durationMs: row.durationMs === null ? null : readDuration(row.durationMs),
    source,
  };
  if (row.correlationId !== undefined) {
    if (typeof row.correlationId !== "string" || !UUID_V7.test(row.correlationId)) {
      throw new ReportParseError("Snapshot contains an unsupported report.");
    }
    result.correlationId = row.correlationId;
  }
  if (row.promptTokens !== undefined) result.promptTokens = readTokens(row.promptTokens);
  if (row.completionTokens !== undefined) result.completionTokens = readTokens(row.completionTokens);
  return result;
}

function parseOneJsonDocument(text) {
  try {
    return parseStrictJson(text);
  } catch (error) {
    if (error instanceof ReportParseError && /JSON document/.test(error.message)) {
      return null;
    }
    throw error;
  }
}

function parseStrictJson(text) {
  const parser = new StrictJsonParser(text);
  return parser.parse();
}

class StrictJsonParser {
  constructor(text) {
    this.text = text;
    this.index = 0;
  }

  parse() {
    this.skipWhitespace();
    const value = this.parseValue(0);
    this.skipWhitespace();
    if (this.index !== this.text.length) throw new ReportParseError("Invalid JSON document.");
    return value;
  }

  parseValue(depth) {
    const character = this.text[this.index];
    if (depth > MAX_DEPTH || (depth >= MAX_DEPTH && (character === "{" || character === "["))) throw new ReportParseError("Report nesting is too deep.");
    if (character === "{") return this.parseObject(depth + 1);
    if (character === "[") return this.parseArray(depth + 1);
    if (character === '"') return this.parseString();
    if (character === "t" && this.consumeLiteral("true")) return true;
    if (character === "f" && this.consumeLiteral("false")) return false;
    if (character === "n" && this.consumeLiteral("null")) return null;
    if (character === "-" || (character >= "0" && character <= "9")) return this.parseNumber();
    throw new ReportParseError("Invalid JSON document.");
  }

  parseObject(depth) {
    this.index += 1;
    this.skipWhitespace();
    const object = Object.create(null);
    const keys = new Set();
    if (this.text[this.index] === "}") {
      this.index += 1;
      return object;
    }
    while (true) {
      if (this.text[this.index] !== '"') throw new ReportParseError("Invalid JSON document.");
      const key = this.parseString();
      if (keys.has(key)) throw new ReportParseError("Report contains duplicate JSON keys.");
      keys.add(key);
      this.skipWhitespace();
      if (this.text[this.index] !== ":") throw new ReportParseError("Invalid JSON document.");
      this.index += 1;
      this.skipWhitespace();
      object[key] = this.parseValue(depth);
      this.skipWhitespace();
      if (this.text[this.index] === "}") {
        this.index += 1;
        return object;
      }
      if (this.text[this.index] !== ",") throw new ReportParseError("Invalid JSON document.");
      this.index += 1;
      this.skipWhitespace();
    }
  }

  parseArray(depth) {
    this.index += 1;
    this.skipWhitespace();
    const values = [];
    if (this.text[this.index] === "]") {
      this.index += 1;
      return values;
    }
    while (true) {
      if (values.length >= MAX_ROWS) throw new ReportParseError("Report has too many rows.");
      values.push(this.parseValue(depth));
      this.skipWhitespace();
      if (this.text[this.index] === "]") {
        this.index += 1;
        return values;
      }
      if (this.text[this.index] !== ",") throw new ReportParseError("Invalid JSON document.");
      this.index += 1;
      this.skipWhitespace();
    }
  }

  parseString() {
    const start = this.index;
    this.index += 1;
    let escaped = false;
    while (this.index < this.text.length) {
      const character = this.text[this.index];
      if (!escaped && character === '"') {
        this.index += 1;
        try {
          return JSON.parse(this.text.slice(start, this.index));
        } catch {
          throw new ReportParseError("Invalid JSON document.");
        }
      }
      if (character < " ") throw new ReportParseError("Invalid JSON document.");
      if (!escaped && character === "\\") {
        escaped = true;
      } else {
        escaped = false;
      }
      this.index += 1;
    }
    throw new ReportParseError("Invalid JSON document.");
  }

  parseNumber() {
    const match = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
    match.lastIndex = this.index;
    const value = match.exec(this.text);
    if (value === null) throw new ReportParseError("Invalid JSON document.");
    this.index = match.lastIndex;
    const number = Number(value[0]);
    if (!Number.isFinite(number)) throw new ReportParseError("Report contains a non-finite number.");
    return number;
  }

  consumeLiteral(literal) {
    if (this.text.slice(this.index, this.index + literal.length) !== literal) return false;
    this.index += literal.length;
    return true;
  }

  skipWhitespace() {
    while (/[\u0020\u0009\u000a\u000d]/.test(this.text[this.index] ?? "")) this.index += 1;
  }
}

function assertTextWithinBound(text) {
  if (typeof text !== "string" || new TextEncoder().encode(text).byteLength > MAX_BYTES) {
    throw new ReportParseError("Report exceeds the local input limit.");
  }
}

async function digestReference(text) {
  const bytes = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return `sha256:${[...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function readCounts(value, totalKey, passedKey, failedKey, options = {}) {
  if (!isPlainObject(value)) throw unsupported();
  const total = readCount(value[totalKey]);
  const passed = readCount(value[passedKey]);
  const failed = readCount(value[failedKey]);
  if ((!options.allowIncomplete && passed + failed !== total) || passed + failed > total) throw unsupported();
  return { total, passed, failed };
}

function readCount(value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > MAX_COUNT) throw unsupported();
  return value;
}

function readDuration(value) {
  if (!Number.isFinite(value) || value < 0 || value > MAX_DURATION_MS) throw unsupported();
  return value;
}

function readTokens(value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > MAX_TOKENS) throw unsupported();
  return value;
}

function readArray(value) {
  if (!Array.isArray(value) || value.length > MAX_ROWS) throw unsupported();
  return value;
}

function assertBooleanCounts(rows, counts) {
  const passed = rows.filter((row) => row.status === "passed").length;
  const failed = rows.filter((row) => row.status === "failed").length;
  if (passed !== counts.passed || failed !== counts.failed) throw unsupported();
}

function validateProofCheck(kind, check) {
  if (!isPlainObject(check) || !PROOF_STATUS.has(check.status)) throw unsupported();
  if (check.source_ref !== undefined && !isDigest(check.source_ref)) throw unsupported();
  if (kind === "reliability" || kind === "adapters") {
    if (!["passed", "failed", "not_checked"].includes(check.status)) throw unsupported();
    const hasCounts = ["total", "passed", "failed"].some((key) => key in check);
    if (hasCounts) {
      const counts = readCounts(check, "total", "passed", "failed");
      const expected = counts.failed ? "failed" : counts.total ? "passed" : "not_checked";
      if (check.status !== expected) throw unsupported();
    }
    if (["passed", "failed"].includes(check.status) && !hasCounts) throw unsupported();
    return;
  }
  if (kind === "bundle") {
    if (!["passed", "failed", "not_checked"].includes(check.status)) throw unsupported();
    if (check.status !== "not_checked" && check.entries_checked === undefined) throw unsupported();
    if (check.entries_checked !== undefined) {
      const count = readCount(check.entries_checked);
      if (check.status === "passed" && count === 0) throw unsupported();
    }
    return;
  }
  if (!["allowed", "denied", "unknown", "not_checked"].includes(check.status)) throw unsupported();
}

function sameKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => keys.includes(key));
}

function isEmptyArray(value) {
  return Array.isArray(value) && value.length === 0;
}

function hasNoRawCaptureFields(value) {
  const nullOrMissing = (field) => value[field] === undefined || value[field] === null;
  const emptyOrMissing = (field) => value[field] === undefined || isEmptyArray(value[field]);
  return (
    nullOrMissing("completion_string") &&
    emptyOrMissing("tool_calls") &&
    emptyOrMissing("input_messages") &&
    nullOrMissing("error")
  );
}

function isReferenceMap(value) {
  if (!isPlainObject(value) || Object.keys(value).length > MAX_ROWS) return false;
  return Object.values(value).every(isDigest);
}

function isFiniteNonnegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function rowId(prefix, index) {
  return `${prefix}-${String(index + 1).padStart(3, "0")}`;
}

function numberedLabel(noun, index) {
  return `${noun} ${String(index + 1).padStart(2, "0")}`;
}

function fixedTitle(kind) {
  return {
    reliability: "Reliability report",
    adapters: "Adapter conformance",
    "proof-card": "Offline Proof Card",
    workflow: "Workflow metadata capture",
  }[kind];
}

function isDigest(value) {
  return typeof value === "string" && SHA256_REFERENCE.test(value);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function unsupported() {
  return new ReportParseError("Unsupported local report.");
}
