import { api, session, ApiError } from "./api.js";
import { makeFormatter, formatPct } from "./fmt.js";
import { waterfallSvg } from "./charts.js";
import { h, notice, toast } from "./dom.js";
import { downloadCsv, downloadText, reportMarkdown } from "./export.js";
import { extraForms, renderCompare, renderOther, statementsForm, subtitleFor, titleFor } from "./finance.js";

const view = document.getElementById("view");
let me = null;

const show = (...nodes) => { view.replaceChildren(...nodes.flat().filter(Boolean)); window.scrollTo(0, 0); };
const loading = (t = "Loading…") => show(h("p", { class: "spinner" }, t));

const VERDICTS = {
  clear_driver: "Clear driver found",
  partial_driver: "Partly explained",
  offsetting_moves: "Offsetting moves",
  no_clear_driver: "No clear driver",
  negligible_change: "Change is negligible",
};

function errorView(err, back = "#/") {
  if (err instanceof ApiError && err.status === 401) { go("#/login"); return; }
  show(notice("error", err.message || "Something went wrong."), h("a", { href: back }, "Back"));
}

// ---------------------------------------------------------------- router
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }

async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  try {
    if (parts[0] === "shared" && parts[1]) return await sharedPage(parts[1]);
    if (!session.token) return authPage(parts[0] === "register" ? "register" : "login");
    if (!me) {
      try { me = await api.me(); } catch (e) { if (e.status === 401) return authPage("login"); throw e; }
    }
    renderWho();
    if (parts[0] === "dataset" && parts[1]) return await datasetPage(parts[1]);
    if (parts[0] === "analysis" && parts[1]) return await analysisPage(parts[1]);
    if (parts[0] === "compare" && parts[1] && parts[2]) return await comparePage(parts[1], parts[2]);
    return await homePage();
  } catch (e) { errorView(e); }
}

function renderWho() {
  document.getElementById("who").textContent = me ? me.user.email : "";
  document.getElementById("logout").hidden = !me;
}

// ---------------------------------------------------------------- colour theme (auto / light / dark)
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  document.getElementById("theme").textContent = t === "auto" ? "Auto" : t === "light" ? "Light" : "Dark";
}
{
  let saved = "auto";
  try { saved = localStorage.getItem("wm_theme") || "auto"; } catch { /* ignore */ }
  applyTheme(THEMES.includes(saved) ? saved : "auto");
  document.getElementById("theme").addEventListener("click", () => {
    const now = document.documentElement.getAttribute("data-theme") || "auto";
    const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
    applyTheme(next);
    try { localStorage.setItem("wm_theme", next); } catch { /* ignore */ }
  });
}

document.getElementById("logout").addEventListener("click", async () => {
  try { await api.logout(); } catch { /* token may already be gone */ }
  session.setToken(null); me = null; renderWho(); go("#/login");
});

// ---------------------------------------------------------------- sign in / register
function authPage(mode) {
  me = null; renderWho();
  const reg = mode === "register";
  const msg = h("div");
  const email = h("input", { type: "email", id: "email", autocomplete: "email", required: true });
  const pass = h("input", { type: "password", id: "password", required: true,
    autocomplete: reg ? "new-password" : "current-password" });
  const ws = h("input", { type: "text", id: "wsname", placeholder: "My workspace" });
  const btn = h("button", { class: "primary", type: "submit" }, reg ? "Create account" : "Sign in");
  const form = h("form", { class: "card", onsubmit: async (ev) => {
    ev.preventDefault();
    btn.disabled = true; msg.replaceChildren();
    try {
      const out = reg ? await api.register(email.value, pass.value, ws.value || null)
                      : await api.login(email.value, pass.value);
      session.setToken(out.token); me = null; go("#/");
    } catch (e) { msg.replaceChildren(notice("error", e.message)); btn.disabled = false; }
  } },
    h("label", { for: "email" }, "Email"), email,
    h("div", { style: "height:10px" }),
    h("label", { for: "password" }, reg ? "Password (at least 10 characters)" : "Password"), pass,
    reg ? [h("div", { style: "height:10px" }), h("label", { for: "wsname" }, "Workspace name (optional)"), ws] : null,
    h("div", { style: "height:14px" }), btn, msg);
  show(h("h1", {}, reg ? "Create your account" : "Sign in"),
    h("p", { class: "sub" }, "Upload a table of numbers, pick two periods, and see which segments moved the metric."),
    form,
    h("p", { class: "muted" }, reg ? "Already have an account? " : "New here? ",
      h("a", { href: reg ? "#/login" : "#/register" }, reg ? "Sign in" : "Create an account")));
}

// ---------------------------------------------------------------- home: datasets
let currentWs = null;
function workspace() {
  const list = me.workspaces;
  let saved = null;
  try { saved = localStorage.getItem("wm_ws"); } catch { /* ignore */ }
  currentWs = list.find((w) => w.id === (currentWs && currentWs.id || saved)) || list[0];
  return currentWs;
}

const badgeFor = (m) => (m.kind === "statements"
  ? h("span", { class: "badge fin" }, "Financial statement") : h("span", { class: "badge" }, "Dated table"));

async function homePage() {
  loading();
  const ws = workspace();
  if (!ws) return show(notice("error", "This account has no workspace."));
  const datasets = await api.datasets(ws.id);
  const msg = h("div");
  let busy = false;
  const send = async (file) => {
    if (busy) return;
    if (!file) { msg.replaceChildren(notice("error", "Choose a CSV or Excel file first.")); return; }
    busy = true; msg.replaceChildren(notice("info", `Reading and checking ${file.name}…`));
    try { const d = await api.upload(ws.id, file, null); go(`#/dataset/${d.id}`); }
    catch (e) { busy = false; if (e.status === 401) return errorView(e); msg.replaceChildren(notice("error", e.message)); }
  };
  const file = h("input", { type: "file", id: "file", accept: ".csv,.xlsx,.xls,.tsv",
    onchange: () => send(file.files[0]) });
  const drop = h("label", { class: "drop", for: "file" },
    h("strong", {}, "Drop a file here, or click to choose one"),
    h("span", { class: "muted" }, "CSV or Excel, up to 20 MB. It is checked as soon as you add it."), file);
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) send(f); });
  const demoBtn = h("button", { class: "primary" }, "Try sample data");
  demoBtn.addEventListener("click", async () => {
    demoBtn.disabled = true; msg.replaceChildren(notice("info", "Creating a synthetic sample table…"));
    try { const d = await api.demo(ws.id); go(`#/dataset/${d.id}`); }
    catch (e) { if (e.status === 401) return errorView(e); msg.replaceChildren(notice("error", e.message)); demoBtn.disabled = false; }
  });
  const wsPick = me.workspaces.length > 1 ? h("select", { "aria-label": "Workspace", onchange: (e) => {
    currentWs = me.workspaces.find((w) => w.id === e.target.value);
    try { localStorage.setItem("wm_ws", currentWs.id); } catch { /* ignore */ }
    route();
  } }, me.workspaces.map((w) => h("option", { value: w.id, selected: w.id === ws.id }, `${w.name} (${w.role})`))) : null;

  const describe = (d) => (d.meta.kind === "statements"
    ? `${d.meta.periods.length} periods · ${d.meta.company || d.meta.sheets.join(", ")}`
    : `${d.meta.rows.toLocaleString()} rows · ${d.meta.date_min} to ${d.meta.date_max}`) + (d.meta.synthetic ? " · synthetic sample" : "");
  const cards = datasets.length ? h("div", { class: "dscards" }, datasets.map((d) => h("div", { class: "dscard" },
    h("div", {}, badgeFor(d.meta)), h("a", { class: "title", href: `#/dataset/${d.id}` }, d.name),
    h("div", { class: "muted" }, describe(d)), h("div", {}, h("a", { href: `#/dataset/${d.id}` }, "Open →")))))
    : h("div", { class: "card empty" }, "Nothing here yet. Add a file above, or try the sample data to see what a result looks like.");

  const stmts = datasets.filter((d) => d.meta.kind === "statements");
  let compareCard = null;
  if (stmts.length >= 2) {
    const mk = (id, idx) => h("select", { id }, stmts.map((d, i) => h("option", { value: d.id, selected: i === idx }, d.name)));
    const sa = mk("cmp_a", 0), sb = mk("cmp_b", 1);
    compareCard = h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Compare two companies"),
      h("p", { class: "muted" }, "Lines up the ratios both statements have, side by side."),
      h("div", { class: "picker" }, h("div", {}, h("label", { for: "cmp_a" }, "First"), sa), h("div", {}, h("label", { for: "cmp_b" }, "Second"), sb),
        h("div", {}, h("button", { class: "primary", onclick: () => {
          if (sa.value === sb.value) { msg.replaceChildren(notice("error", "Choose two different files.")); return; }
          go(`#/compare/${sa.value}/${sb.value}`);
        } }, "Compare"))));
  }
  show(h("div", { class: "hero" }, h("h1", {}, "Find where your numbers moved"),
      h("p", { class: "sub" }, `${ws.name}. Upload a file and WhyMoved shows which parts of the business account for a change. It never guesses the reason.`),
      h("ol", { class: "steps" }, h("li", {}, "Add a file"), h("li", {}, "Check what was found in it"), h("li", {}, "Read the result and share it")), wsPick),
    drop, msg,
    h("div", { class: "kinds" },
      h("div", { class: "card" }, h("h3", {}, h("span", { class: "badge" }, "Dated table"), "Sales, visits, orders…"),
        h("p", { class: "muted", style: "margin:0" }, "A date column, category columns (region, channel…) and numbers. Shows which segments moved a metric, price/volume/mix, market share and trends.")),
      h("div", { class: "card" }, h("h3", {}, h("span", { class: "badge fin" }, "Financial statement"), "Annual report exports…"),
        h("p", { class: "muted", style: "margin:0" }, "Line items down the side, years across (Screener.in and similar). Shows ratios, the ROE split, a watchlist, and company-to-company comparison."))),
    compareCard,
    h("div", { style: "display:flex;align-items:center;gap:12px;flex-wrap:wrap" }, h("h2", { style: "margin:24px 0 8px" }, "Your files"),
      h("span", { class: "spacer", style: "flex:1" }), datasets.length ? null : demoBtn),
    cards, datasets.length ? h("p", {}, demoBtn) : null);
}

// ---------------------------------------------------------------- dataset: roles + analysis form
const NAMED = [
  ["conversion", "Conversion (orders ÷ visits)", ["orders", "visits"]],
  ["aov", "Average order value (revenue ÷ orders)", ["revenue", "orders"]],
  ["margin", "Gross margin ((revenue − cost) ÷ revenue)", ["revenue", "cost"]],
  ["cost_ratio", "Cost ratio (cost ÷ revenue)", ["cost", "revenue"]],
  ["gross_profit", "Gross profit (revenue − cost)", ["revenue", "cost"]],
];

async function datasetPage(did) {
  loading();
  const [ds, past] = await Promise.all([api.dataset(did), api.analyses(did)]);
  const m = ds.meta;
  if (m.kind === "statements") return statementsPage(ds, did, past);
  const cols = new Set(m.metric_cols.map((c) => c.toLowerCase()));
  const metricSel = h("select", { id: "metric" },
    h("optgroup", { label: "Your columns (totals)" }, m.metric_cols.map((c) => h("option", { value: c }, c))),
    NAMED.filter(([, , need]) => need.every((n) => cols.has(n))).length ?
      h("optgroup", { label: "Ready-made ratios" }, NAMED.filter(([, , need]) => need.every((n) => cols.has(n)))
        .map(([v, label]) => h("option", { value: v }, label))) : null,
    h("option", { value: "__custom" }, "Custom formula…"));
  const custom = h("input", { type: "text", id: "custom", placeholder: "e.g. orders / visits or revenue - cost" });
  const customRow = h("div", { hidden: true }, h("label", { for: "custom" }, "Formula (use your column names; one division at most)"), custom);
  metricSel.addEventListener("change", () => { customRow.hidden = metricSel.value !== "__custom"; });

  const hasBudget = Object.keys(m.budget_columns || {}).length > 0;
  const modeSel = h("select", { id: "mode" }, h("option", { value: "previous" }, "Compare two periods"),
    hasBudget ? h("option", { value: "budget" }, "Actual vs budget") : null);
  const pa = m.default_periods ? m.default_periods.a : ["", ""];
  const pb = m.default_periods ? m.default_periods.b : ["", ""];
  const dateIn = (id, v) => h("input", { type: "date", id, value: v, min: m.date_min, max: m.date_max });
  const a1 = dateIn("a1", pa[0]), a2 = dateIn("a2", pa[1]), b1 = dateIn("b1", pb[0]), b2 = dateIn("b2", pb[1]);
  const periodBox = h("div", { class: "row" },
    h("div", {}, h("label", { for: "a1" }, "Earlier period starts"), a1),
    h("div", {}, h("label", { for: "a2" }, "Earlier period ends"), a2),
    h("div", {}, h("label", { for: "b1" }, "Later period starts"), b1),
    h("div", {}, h("label", { for: "b2" }, "Later period ends"), b2));
  const budgetNote = h("p", { class: "muted", hidden: true },
    `Budget columns found: ${Object.entries(m.budget_columns || {}).map(([k, v]) => `${k} → ${v}`).join(", ")}. The later period is compared with the budget for the same dates.`);
  modeSel.addEventListener("change", () => { budgetNote.hidden = modeSel.value !== "budget"; });

  const currency = h("input", { type: "text", id: "currency", maxlength: "8", placeholder: "e.g. Rs or $", style: "max-width:140px" });
  const indian = h("input", { type: "checkbox", id: "indian" });
  const dimBoxes = m.dimension_cols.map((c) => h("input", { type: "checkbox", value: c, checked: true, id: `dim_${c}` }));
  const msg = h("div");
  const run = h("button", { class: "primary", type: "submit" }, "Find where it moved");

  const form = h("form", { class: "card", onsubmit: async (ev) => {
    ev.preventDefault();
    msg.replaceChildren(); run.disabled = true;
    const dims = dimBoxes.filter((b) => b.checked).map((b) => b.value);
    if (!dims.length) { msg.replaceChildren(notice("error", "Keep at least one category column ticked.")); run.disabled = false; return; }
    const metric = metricSel.value === "__custom" ? custom.value.trim() : metricSel.value;
    if (!metric) { msg.replaceChildren(notice("error", "Type a formula.")); run.disabled = false; return; }
    const params = { metric, mode: modeSel.value, currency: currency.value.trim(), indian: indian.checked, dimensions: dims };
    if (a1.value && a2.value && b1.value && b2.value) { params.period_a = [a1.value, a2.value]; params.period_b = [b1.value, b2.value]; }
    try {
      msg.replaceChildren(notice("info", "Analysing. Larger files take a few seconds…"));
      const out = await api.analyse(did, params); go(`#/analysis/${out.id}`);
    } catch (e) { if (e.status === 401) return errorView(e); msg.replaceChildren(notice("error", e.message)); run.disabled = false; }
  } },
    h("h2", { style: "margin-top:0" }, "Run an analysis"),
    h("div", { class: "row" }, h("div", {}, h("label", { for: "metric" }, "Metric"), metricSel),
      h("div", {}, h("label", { for: "mode" }, "Question"), modeSel)),
    customRow, h("div", { style: "height:10px" }), periodBox, budgetNote,
    h("div", { class: "row" }, h("div", {}, h("label", { for: "currency" }, "Currency symbol (optional)"), currency),
      h("div", {}, h("label", { for: "indian" }, "Digit grouping"),
        h("label", { style: "color:var(--ink)" }, indian, " Indian (12,34,567)"))),
    h("p", { class: "muted", style: "margin-bottom:4px" }, "Which category columns may be searched?"),
    h("div", { class: "checks" }, m.dimension_cols.map((c, i) => h("label", {}, dimBoxes[i], c))),
    h("div", { style: "height:14px" }), run, msg);

  const issues = (m.issues || []).filter((i) => i.severity !== "info");
  const del = h("button", { class: "danger", onclick: async () => {
    if (!confirm("Delete this dataset and all its analyses?")) return;
    try { await api.deleteDataset(did); go("#/"); } catch (e) { msg.replaceChildren(notice("error", e.message)); }
  } }, "Delete dataset");
  const pastList = past.length ? h("ul", { class: "list" }, past.map((a) => h("li", {},
    h("div", { class: "grow" }, h("a", { href: `#/analysis/${a.id}` }, a.headline.metric || "analysis"),
      h("div", { class: "muted" }, `${a.created_at.slice(0, 16).replace("T", " ")} UTC · ${VERDICTS[a.headline.verdict] || a.headline.verdict || ""}`
        + (a.shared ? " · shared" : ""))))))
    : h("p", { class: "muted" }, "No analyses yet.");

  show(h("p", {}, h("a", { href: "#/" }, "← All datasets")), h("h1", {}, ds.name),
    h("p", { class: "sub" }, `${m.rows.toLocaleString()} rows · ${m.date_min} to ${m.date_max} · ${m.frequency || "unknown"} data`),
    m.synthetic ? notice("info", `Synthetic sample. Planted problem (the answer): ${m.planted}. Try the "conversion" ratio to find it.`) : null,
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "What I detected"),
      h("p", { class: "muted", style: "margin:0" }, "Date column"), h("div", { class: "chips" }, h("span", { class: "chip" }, m.date_col)),
      h("p", { class: "muted", style: "margin:0" }, "Number columns (can be measured)"),
      h("div", { class: "chips" }, m.metric_cols.map((c) => h("span", { class: "chip" }, c))),
      h("p", { class: "muted", style: "margin:0" }, "Category columns (can be searched for drivers)"),
      h("div", { class: "chips" }, m.dimension_cols.map((c) => h("span", { class: "chip" }, c))),
      issues.length ? issues.map(issueNode) : h("p", { class: "muted" }, "No problems found in the file.")),
    m.dimension_cols.length && m.metric_cols.length > 1 ? scanCard(did) : null,
    form, h("h2", {}, "Other questions about this file"), extraForms(ds, did, { go, errorView }),
    h("h2", {}, "Earlier analyses"), h("div", { class: "card" }, pastList), h("div", { class: "toolbar" }, del));
}

function pastItems(past) {
  return past.length ? h("ul", { class: "list" }, past.map((a) => h("li", {},
    h("div", { class: "grow" }, h("a", { href: `#/analysis/${a.id}` }, a.headline.metric || "analysis"),
      h("div", { class: "muted" }, `${a.created_at.slice(0, 16).replace("T", " ")} UTC` + (a.shared ? " · shared" : ""))))))
    : h("p", { class: "muted" }, "No analyses yet.");
}

function statementsPage(ds, did, past) {
  const m = ds.meta;
  const del = h("button", { class: "danger", onclick: async () => {
    if (!confirm("Delete this dataset and all its analyses?")) return;
    try { await api.deleteDataset(did); go("#/"); } catch (e) { show(notice("error", e.message)); }
  } }, "Delete dataset");
  show(h("p", {}, h("a", { href: "#/" }, "← All datasets")), h("h1", {}, ds.name),
    h("p", { class: "sub" }, `Financial statement${m.company ? ` · ${m.company}` : ""} · ${m.periods.length} periods`),
    statementsForm(ds, did, { go, errorView }),
    h("h2", {}, "Earlier analyses"), h("div", { class: "card" }, pastItems(past)), h("div", { class: "toolbar" }, del));
}

function issueNode(i) {
  const kind = i.severity === "error" ? "error" : i.severity === "warning" ? "warning" : "info";
  const ex = i.examples && i.examples.length ? ` (e.g. ${i.examples.slice(0, 3).join(", ")})` : "";
  return notice(kind, i.message + ex);
}

// ---------------------------------------------------------------- result rendering
export function renderResult(result) {
  const other = renderOther(result);
  if (other) return other;
  const f = makeFormatter(result);
  const ratio = result.is_ratio;
  const budget = result.mode === "budget";
  const sign = (v) => (v < 0 ? "neg" : v > 0 ? "pos" : "");
  const findings = result.findings || [];
  const table = findings.length ? h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Segment"), h("th", { class: "num" }, "Moved by"),
      h("th", { class: "num" }, "Share of change"), h("th", { class: "num" }, "95% interval"), ratio ? h("th", { class: "num" }, "Rate / mix") : null)),
    h("tbody", {}, findings.map((x) => h("tr", {},
      h("td", {}, x.rank), h("td", {}, x.label),
      h("td", { class: `num ${sign(x.explained)}` }, f.delta(x.explained)),
      h("td", { class: "num" }, formatPct(x.share_of_change)),
      h("td", { class: "num" }, `${f.delta(x.ci_low)} to ${f.delta(x.ci_high)}`),
      ratio ? h("td", { class: "num" }, x.rate_effect === null || x.rate_effect === undefined ? "n/a"
        : `${f.delta(x.rate_effect)} / ${f.delta(x.mix_effect)}`) : null))))
    : h("p", { class: "muted" }, "No segment stood out from the rest.");
  const tr = result.trend;
  const trendNode = tr && tr.available ? h("p", { class: "muted" },
    `Trend check: the metric moved ${(tr.actual_change_pct * 100).toFixed(1)}% against ${(tr.expected_change_pct * 100).toFixed(1)}% expected from its recent weekly trend.`)
    : tr ? h("p", { class: "muted" }, `Trend check not run. ${tr.reason || ""}`) : null;
  const diff = result.total_b - result.total_a;
  const rel = result.total_a ? ` (${diff / Math.abs(result.total_a) > 0 ? "+" : ""}${(diff / Math.abs(result.total_a) * 100).toFixed(1)}%)` : "";
  const labels = budget ? ["Budget", "Actual"] : ["Earlier period", "Later period"];
  return h("div", {},
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("div", { class: "muted" }, labels[0]), h("div", { class: "n" }, f.level(result.total_a))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, labels[1]), h("div", { class: "n" }, f.level(result.total_b))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "Change"), h("div", { class: `n ${sign(diff)}` }, f.delta(diff) + (f.percent ? "" : rel))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "Verdict"), h("span", { class: "verdict" }, VERDICTS[result.verdict] || result.verdict))),
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Where the change sits"),
      h("div", { class: "chartwrap" }, waterfallSvg(result)),
      h("p", { class: "muted" }, "Bars show how the metric got from the first value to the second. “Everything else” is what the highlighted segments do not account for.")),
    h("div", { class: "card summary" }, h("h2", { style: "margin-top:0" }, "In words"),
      String(result.summary || "").split(/\n\n+/).map((p) => h("p", {}, p))),
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Segments"), h("div", { class: "chartwrap" }, table),
      (result.notes || []).map((n) => h("p", { class: "muted" }, n)), trendNode),
    (result.validation || []).filter((i) => i.severity !== "info").length ?
      h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Data checks"),
        result.validation.filter((i) => i.severity !== "info").map(issueNode)) : null,
    notice("warning", result.caveat));
}

// ---------------------------------------------------------------- copy / CSV / print
function resultTools(r, notes = []) {
  const copy = h("button", { type: "button", onclick: async () => {
    const text = `${titleFor(r)}\n${subtitleFor(r)}\n\n${r.summary || ""}`;
    try { await navigator.clipboard.writeText(text); toast("Summary copied"); } catch { toast("Copy is not allowed here; select the text instead"); }
  } }, "Copy summary");
  const csv = h("button", { type: "button", onclick: () => downloadCsv(r, titleFor(r)) }, "Download CSV");
  const report = h("button", { type: "button", onclick: () => downloadText(
    reportMarkdown(r, titleFor(r), subtitleFor(r), typeof notes === "function" ? notes() : notes), `whymoved-${titleFor(r).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50)}.md`) }, "Download report (.md)");
  const print = h("button", { type: "button", onclick: () => window.print() }, "Print or save as PDF");
  return [h("div", { class: "toolbar2" }, copy, report, csv, print),
    h("p", { class: "print-only muted" }, `WhyMoved · ${new Date().toISOString().slice(0, 10)}`)];
}

// ---------------------------------------------------------------- notes: where a person writes the "why"
function notesCard(aid, notes, readonly = false) {
  const list = h("div", { class: "notes" });
  const draw = () => {
    list.replaceChildren(...(notes.length ? notes.map((n) => h("div", { class: "note" },
      h("div", { class: "muted" }, `${n.author ? n.author + " · " : ""}${n.created_at.slice(0, 16).replace("T", " ")} UTC`
        + (readonly ? "" : n.public ? " · on the share link" : " · private")),
      h("p", { class: "notebody" }, n.body),
      !readonly && n.mine ? h("button", { class: "link", type: "button", onclick: async () => {
        try { await api.deleteNote(aid, n.id); notes.splice(notes.indexOf(n), 1); draw(); toast("Note deleted"); }
        catch (e) { toast(e.message); }
      } }, "Delete") : null))
      : [h("p", { class: "muted" }, readonly ? "" : "No notes yet. WhyMoved can show where a number moved; you or your team know why.")]));
  };
  draw();
  if (readonly) return notes.length ? h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Notes from the author"),
    h("p", { class: "muted" }, "Written by a person, not worked out by WhyMoved."), list) : null;
  const text = h("textarea", { id: "notetext", rows: "3", maxlength: "1500", placeholder: "e.g. North ran a promotion in that fortnight (from the sales team)" });
  const pub = h("input", { type: "checkbox", id: "notepub" });
  const add = h("button", { class: "primary", type: "submit" }, "Add note");
  const form = h("form", { onsubmit: async (ev) => {
    ev.preventDefault(); if (!text.value.trim()) return;
    add.disabled = true;
    try { notes.push(await api.addNote(aid, text.value, pub.checked)); text.value = ""; draw(); toast("Note added"); }
    catch (e) { toast(e.message); }
    add.disabled = false;
  } }, h("label", { for: "notetext" }, "Add your own explanation"), text,
    h("div", { class: "row", style: "align-items:center" }, h("label", { style: "color:var(--ink)" }, pub, " Show on the share link"), add));
  return h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Notes"),
    h("p", { class: "muted" }, "WhyMoved shows where a number moved. The reason comes from people who know the business. Notes are marked as written by a person."),
    list, form);
}

// ---------------------------------------------------------------- scan: where to look first
function scanCard(did) {
  const out = h("div"); const go1 = h("button", { type: "button" }, "Scan every number column");
  go1.addEventListener("click", async () => {
    go1.disabled = true; out.replaceChildren(notice("info", "Scanning each column over the latest periods…"));
    try {
      const s = await api.scan(did);
      const pct = (v) => (v === null || v === undefined ? "n/a" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`);
      const rows = s.rows.map((r) => h("tr", {}, h("td", {}, r.metric),
        h("td", { class: `num ${r.change > 0 ? "pos" : r.change < 0 ? "neg" : ""}` }, pct(r.change_pct)),
        h("td", {}, VERDICTS[r.verdict] || r.verdict),
        h("td", {}, r.top ? `${r.top.label} (${formatPct(r.top.share_of_change)} of the change)` : "none stood out"),
        h("td", {}, h("button", { type: "button", onclick: async (ev) => {
          ev.target.disabled = true;
          try { const a = await api.analyse(did, { metric: r.formula, mode: "previous" }); go(`#/analysis/${a.id}`); }
          catch (e) { ev.target.disabled = false; toast(e.message); }
        } }, "Open"))));
      out.replaceChildren(h("div", { class: "chartwrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Column"),
        h("th", { class: "num" }, "Change"), h("th", {}, "Verdict"), h("th", {}, "Largest segment"), h("th", {}, ""))), h("tbody", {}, rows))),
      s.skipped.map((x) => h("p", { class: "muted" }, `${x.metric}: skipped (${x.reason})`)),
      s.scanned < s.total_columns ? h("p", { class: "muted" }, `Only the first ${s.scanned} of ${s.total_columns} columns were scanned.`) : null,
      h("p", { class: "muted" }, s.caveat));
    } catch (e) { out.replaceChildren(notice("error", e.message)); }
    go1.disabled = false;
  });
  return h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Where should I look first?"),
    h("p", { class: "muted" }, "Runs the \"what moved\" check on every number column and ranks them by how much they changed between the latest two periods."), go1, out);
}

// ---------------------------------------------------------------- two companies
async function comparePage(a, b) {
  loading("Lining the two companies up…");
  const c = await api.compare(a, b);
  show(h("p", {}, h("a", { href: "#/" }, "← All files")), h("h1", {}, `${c.names[0]} vs ${c.names[1]}`),
    h("div", { class: "toolbar2" }, h("button", { type: "button", onclick: () => window.print() }, "Print or save as PDF")),
    renderCompare(c));
}

// ---------------------------------------------------------------- analysis page (signed in)
async function analysisPage(aid) {
  loading();
  const a = await api.analysis(aid);
  const r = a.result;
  const notes = a.notes || [];
  const msg = h("div"); const box = h("div");
  const days = h("select", { id: "days", "aria-label": "Link lifetime" },
    h("option", { value: "7" }, "expires in 7 days"), h("option", { value: "30" }, "expires in 30 days"),
    h("option", { value: "" }, "never expires"));
  const mk = h("button", { class: "primary" }, a.shared ? "Create another link" : "Create share link");
  mk.addEventListener("click", async () => {
    mk.disabled = true; msg.replaceChildren();
    try {
      const s = await api.share(aid, days.value ? Number(days.value) : null);
      const url = `${location.origin}${location.pathname}#/shared/${s.token}`;
      const input = h("input", { type: "text", readonly: true, value: url, "aria-label": "Share link" });
      const copy = h("button", {}, "Copy");
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(url); copy.textContent = "Copied"; } catch { input.select(); }
      });
      box.replaceChildren(h("div", { class: "sharebox" }, input, copy),
        h("p", { class: "muted" }, "Anyone with this link can see this result (not your data, not your other analyses). The link is shown once; revoke it below if it leaks."));
    } catch (e) { msg.replaceChildren(notice("error", e.message)); }
    mk.disabled = false;
  });
  const rv = h("button", { onclick: async () => {
    try { const o = await api.unshare(aid); box.replaceChildren(); msg.replaceChildren(notice("info", `Revoked ${o.revoked} link(s).`)); }
    catch (e) { msg.replaceChildren(notice("error", e.message)); }
  } }, "Revoke all links");
  const del = h("button", { class: "danger", onclick: async () => {
    if (!confirm("Delete this analysis?")) return;
    try { await api.deleteAnalysis(aid); go(`#/dataset/${a.dataset_id}`); } catch (e) { msg.replaceChildren(notice("error", e.message)); }
  } }, "Delete analysis");
  show(h("p", {}, h("a", { href: `#/dataset/${a.dataset_id}` }, "← Back to dataset")),
    h("h1", {}, titleFor(r)), h("p", { class: "sub" }, subtitleFor(r)), resultTools(r, () => notes.filter((n) => n.public)),
    renderResult(r), notesCard(aid, notes),
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "Share"),
      a.shared ? h("p", { class: "muted" }, "This analysis has an active share link.") : null,
      h("div", { class: "row" }, h("div", {}, days), h("div", {}, mk), h("div", {}, rv)), box, msg),
    h("div", { class: "toolbar" }, del));
}

// ---------------------------------------------------------------- public share page
async function sharedPage(token) {
  me = null; renderWho(); loading();
  try {
    const s = await api.shared(token); const r = s.result;
    show(h("h1", {}, titleFor(r)),
      h("p", { class: "sub" }, `Shared result · created ${s.created_at.slice(0, 10)}`), resultTools(r, s.notes || []), renderResult(r),
      notesCard(null, s.notes || [], true),
      h("p", { class: "muted" }, "Read-only. ", h("a", { href: "#/login" }, "Make your own")));
  } catch (e) {
    show(notice("error", e.status === 404 ? "This link does not exist, has expired, or was revoked." : e.message));
  }
}

window.addEventListener("hashchange", route);
route();
