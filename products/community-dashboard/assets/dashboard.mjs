import { parseReport, createDemoReports, exportSnapshot } from "./report-model.mjs";

const labels = { overview: "Overview", workflow: "Observations", reliability: "Reliability", adapters: "Adapter reports", "proof-card": "Proof Cards" };
const state = { reports: [], view: "overview", query: "", status: "all", selected: null, limit: 100, demo: false, generation: 0 };
const element = (id) => document.getElementById(id);

function announce(text, error = false) {
  element("message").textContent = text;
  element("message").classList.toggle("error", error);
}

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

function badge(status) {
  return node("span", `badge ${status}`, status.replaceAll("_", " "));
}

function observations() {
  return state.reports.flatMap((report) => report.rows.map((row, index) => ({ ...row, key: `${report.kind}:${index}`, report })));
}

function visibleRows() {
  return observations().filter((row) => (state.view === "overview" || row.report.kind === state.view)
    && (state.status === "all" || row.status === state.status)
    && `${row.label} ${row.category} ${row.correlationId ?? ""}`.toLowerCase().includes(state.query));
}

function selectView(view) {
  state.view = view;
  state.limit = 100;
  state.selected = null;
  element("view-name").textContent = labels[view];
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  render();
}

function renderMetrics() {
  const reportsWithCounts = state.reports.filter((report) => report.kind === "reliability" || report.kind === "adapters");
  element("report-count").textContent = String(state.reports.length);
  element("passed-count").textContent = reportsWithCounts.length ? String(reportsWithCounts.reduce((sum, report) => sum + report.counts.passed, 0)) : "—";
  element("failed-count").textContent = reportsWithCounts.length ? String(reportsWithCounts.reduce((sum, report) => sum + report.counts.failed, 0)) : "—";
  element("unknown-count").textContent = state.reports.length ? String(observations().filter((row) => row.status === "unknown").length) : "—";
  element("mode-banner").classList.toggle("demo", state.demo);
  element("mode-text").textContent = state.demo
    ? "SYNTHETIC DEMO · Illustrative local fixtures, not your runs, benchmarks or live provider results."
    : state.reports.length
      ? "IMPORTED DECLARATIONS · In browser memory only. Reports are not authenticated or re-verified."
      : "No reports loaded. Import local files or explore the synthetic demo.";
  element("clear").hidden = state.reports.length === 0;
  element("export").disabled = state.reports.length === 0;
}

function renderRows() {
  const filtered = visibleRows();
  const shown = filtered.slice(0, state.limit);
  const maxDuration = Math.max(1, ...filtered.map((row) => row.durationMs ?? 0));
  const fragment = document.createDocumentFragment();
  for (const row of shown) {
    const tr = node("tr", row.key === state.selected ? "selected" : "");
    const identity = node("td");
    const button = node("button", "row-button");
    button.type = "button";
    button.setAttribute("aria-label", `Inspect ${row.label}`);
    button.setAttribute("aria-pressed", String(row.key === state.selected));
    tr.dataset.rowKey = row.key;
    const glyph = node("span", "row-glyph", row.report.kind === "workflow" ? "⌁" : row.report.kind === "proof-card" ? "◇" : "◉");
    glyph.setAttribute("aria-hidden", "true");
    const name = node("span");
    name.append(node("span", "row-title", row.label), node("span", "row-category", row.category));
    button.append(glyph, name);
    button.addEventListener("click", () => {
      state.selected = row.key;
      element("rows").querySelectorAll("tr").forEach((item) => {
        const active = item.dataset.rowKey === row.key;
        item.classList.toggle("selected", active);
        item.querySelector("button").setAttribute("aria-pressed", String(active));
      });
      renderDetail();
    });
    identity.append(button);
    const status = node("td");
    status.append(badge(row.status));
    const duration = node("td", "duration-cell");
    duration.append(node("span", "duration-top", row.durationMs == null ? "not reported" : `${row.durationMs.toFixed(2)} ms`));
    if (row.durationMs != null) {
      const bar = node("div", "duration-bar");
      const fill = node("div", "duration-fill");
      fill.style.width = `${Math.max(1, row.durationMs / maxDuration * 100)}%`;
      bar.append(fill);
      duration.append(bar);
    }
    tr.append(identity, status, duration);
    fragment.append(tr);
  }
  element("rows").replaceChildren(fragment);
  element("row-count").textContent = `${filtered.length} entries`;
  element("shown-count").textContent = filtered.length ? `Showing ${shown.length} of ${filtered.length} minimized entries` : "No entries to display";
  element("more").hidden = shown.length >= filtered.length;
  element("empty").hidden = shown.length > 0;
  if (state.reports.length && shown.length === 0) {
    element("empty").querySelector("h3").textContent = "No matching declarations.";
    element("empty").querySelector("p").textContent = "Try another report view, status or search.";
  } else {
    element("empty").querySelector("h3").textContent = "Your evidence starts here.";
    element("empty").querySelector("p").textContent = "Import reports from Community tools. No synthetic data is loaded automatically.";
  }
}

function renderDetail() {
  const selected = visibleRows().find((row) => row.key === state.selected);
  if (!selected) {
    const placeholder = node("div", "detail-placeholder");
    placeholder.append(node("span", "", "⌁"), node("h3", "", "Select an observation"), node("p", "", "Inspect minimized metadata without exposing prompts, tool arguments or credentials."));
    element("detail").replaceChildren(placeholder);
    return;
  }
  const header = node("div", "detail-status");
  header.append(badge(selected.status));
  const list = node("dl");
  const pairs = [["Report kind", selected.report.title], ["Category", selected.category], ["Record source", selected.source], ["Source reference", selected.report.sourceRef ?? "not reported"], ["Local duration", selected.durationMs == null ? "not reported" : `${selected.durationMs.toFixed(2)} ms`]];
  if (selected.correlationId) pairs.push(["Caller UUIDv7", selected.correlationId]);
  if (selected.promptTokens != null) pairs.push(["Reported input tokens", String(selected.promptTokens)]);
  if (selected.completionTokens != null) pairs.push(["Reported output tokens", String(selected.completionTokens)]);
  for (const [label, value] of pairs) list.append(node("dt", "", label), node("dd", "", value));
  element("detail").replaceChildren(header, node("h3", "", selected.label), list);
}

function render() { renderMetrics(); renderRows(); renderDetail(); }

async function importFiles(files) {
  if (!files.length) return;
  const generation = ++state.generation;
  try {
    if (files.length > 4 || files.some((file) => file.size > 1024 * 1024)) throw new Error("Select up to four reports, each no larger than 1 MiB.");
    announce("Validating local report metadata…");
    const reports = await Promise.all(files.map(async (file) => parseReport(new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()))));
    if (generation !== state.generation) return;
    if (new Set(reports.map((report) => report.kind)).size !== reports.length) throw new Error("Select at most one report of each supported kind.");
    state.reports = reports;
    state.selected = null;
    state.demo = false;
    state.limit = 100;
    render();
    announce(`${reports.length} report(s) imported locally. No files were uploaded or authenticated.`);
  } catch {
    if (generation === state.generation) announce("Import rejected: use up to four supported reports (one per kind), valid counts and schema, each ≤1 MiB. Current workspace unchanged.", true);
  } finally {
    if (generation === state.generation) element("files").value = "";
  }
}

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => selectView(button.dataset.view)));
document.querySelectorAll("[data-focus]").forEach((button) => button.addEventListener("click", () => selectView(button.dataset.focus)));
element("search").addEventListener("input", (event) => { state.query = event.target.value.toLowerCase().trim(); state.limit = 100; renderRows(); renderDetail(); });
element("status").addEventListener("change", (event) => { state.status = event.target.value; state.limit = 100; renderRows(); renderDetail(); });
element("more").addEventListener("click", () => { state.limit += 100; renderRows(); });
element("import").addEventListener("click", () => element("files").click());
element("files").addEventListener("change", (event) => importFiles([...event.target.files]));
element("demo").addEventListener("click", async () => {
  const generation = ++state.generation;
  try {
    const reports = await createDemoReports();
    if (generation !== state.generation) return;
    state.reports = reports; state.demo = true; state.selected = null; state.query = ""; state.status = "all";
    element("search").value = ""; element("status").value = "all";
    selectView("overview"); announce("Synthetic demo loaded. These are illustrative fixtures, not real executions.");
  } catch { if (generation === state.generation) announce("Synthetic demo could not be prepared.", true); }
});
element("clear").addEventListener("click", () => {
  state.generation++; state.reports = []; state.demo = false; state.selected = null; state.query = ""; state.status = "all";
  element("search").value = ""; element("status").value = "all"; element("files").value = "";
  selectView("overview"); announce("Workspace cleared. No report state was saved.");
});
element("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme === "dark";
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  element("theme").setAttribute("aria-label", `Switch to ${dark ? "dark" : "light"} theme`);
});
element("export").addEventListener("click", () => {
  const content = JSON.parse(exportSnapshot(state.reports));
  content.synthetic_demo = state.demo;
  const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: "application/json" }));
  const link = node("a"); link.href = url; link.download = "fliorcie-local-snapshot.json"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  announce("Minimized snapshot exported locally. It is a summary, not a credential or verification result.");
});
render();
