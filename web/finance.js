// The analyses beyond "what moved": financial statements, price / volume / mix, market share and
// trends. Forms for the dataset page and renderers for the result page. Pure DOM, no innerHTML.
import { api } from "./api.js";
import { formatNumber, formatPct, makeFormatter } from "./fmt.js";
import { lineChartSvg, waterfallSvg } from "./charts.js";
import { h, notice } from "./dom.js";

const KIND_TITLES = {
  changes: "what changed", statements: "financial statement ratios", pvm: "price, volume and mix",
  market: "market share bridge", trends: "trend and seasonality",
};

export function titleFor(r) {
  if (r.kind === "statements") return `${r.company || "Financial statements"}: ${KIND_TITLES.statements}`;
  if (r.kind === "pvm" || r.kind === "market" || r.kind === "trends") return `${r.metric}: ${KIND_TITLES[r.kind]}`;
  return `${r.metric}: ${r.mode === "budget" ? "actual vs budget" : "what changed"}`;
}

export function subtitleFor(r) {
  if (r.kind === "statements") return `${r.period_a} → ${r.period_b} · ${r.periods.length} periods read`;
  if (r.kind === "trends") return `${r.months} months of data`;
  if (r.mode === "budget") return `${r.period_b[0]} to ${r.period_b[1]}`;
  return `${r.period_a[0]} to ${r.period_a[1]}  →  ${r.period_b[0]} to ${r.period_b[1]}`;
}

const sign = (v) => (v < 0 ? "neg" : v > 0 ? "pos" : "");
const paragraphs = (text) => String(text || "").split(/\n\n+/).map((p) => h("p", {}, p));
const card = (title, ...kids) => h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, title), ...kids);
const issueNotes = (list, kind) => (list || []).map((t) => notice(kind, t));

// ---------------------------------------------------------------- result renderers
export function renderOther(r) {
  if (r.kind === "statements") return renderStatements(r);
  if (r.kind === "pvm" || r.kind === "market") return renderBridge(r);
  if (r.kind === "trends") return renderTrends(r);
  return null;
}

const ratioText = (unit, v) => {
  if (v === null || v === undefined) return "n/a";
  if (unit === "percent") return `${(v * 100).toFixed(1)}%`;
  if (unit === "days") return `${v.toFixed(1)} days`;
  return `${v.toFixed(2)}x`;
};

function renderStatements(r) {
  const money = (v) => formatNumber(v, { decimals: 0, currency: r.currency || "", indian: r.indian });
  const head = (cols) => h("thead", {}, h("tr", {}, h("th", {}, ""),
    cols.map((c) => h("th", { class: `num${c === r.period_a || c === r.period_b ? " cmp" : ""}` }, c))));
  const byKey = Object.fromEntries(r.ratios.map((x) => [x.key, x]));
  const ratioRows = r.ratio_groups.flatMap((g) => [
    h("tr", { class: "grouprow" }, h("td", { colspan: r.periods.length + 1 }, g.group)),
    ...g.keys.map((k) => h("tr", {}, h("td", { title: byKey[k].formula }, byKey[k].label),
      byKey[k].values.map((v) => h("td", { class: "num" }, ratioText(byKey[k].unit, v)))))]);
  const ratioTable = r.ratios.length ? h("div", { class: "chartwrap" }, h("table", {}, head(r.periods), h("tbody", {}, ratioRows)),
    h("p", { class: "muted" }, "Hover a ratio name to see its formula. Return ratios use the average of opening and closing balances, so the first period is blank."))
    : h("p", { class: "muted" }, "No ratio could be worked out from the lines chosen.");
  const lineTable = h("div", { class: "chartwrap" }, h("table", {}, head(r.periods), h("tbody", {},
    r.lines.map((l) => h("tr", {}, h("td", {}, l.label), l.values.map((v) => h("td", { class: "num" }, v === null ? "n/a" : money(v))))))));
  const growth = r.growth.length ? h("div", { class: "chartwrap" }, h("table", {}, head(r.periods), h("tbody", {},
    r.growth.map((g) => h("tr", {}, h("td", {}, `${g.label} growth`), g.values.map((v) => h("td", { class: `num ${v === null ? "" : sign(v)}` }, v === null ? "n/a" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`))))))) : null;
  const d = r.dupont;
  const dupont = d ? card("What moved return on equity",
    h("div", { class: "chartwrap" }, waterfallSvg({ ...r, is_ratio: true, total_a: 0, total_b: 0 })),
    h("p", { class: "muted" }, "Net margin, asset turnover and leverage add up exactly to the change in ROE. The split depends on the order they are changed in.")) : null;
  const map = Object.entries(r.mapping).map(([, m]) => h("span", { class: "chip" }, `${m.label} ← ${m.key}`));
  return h("div", {},
    r.financial ? notice("info", "Treated as a bank or finance company: ratios built for goods businesses are not shown.") : null,
    d ? h("div", { class: "stats" },
      h("div", { class: "stat" }, h("div", { class: "muted" }, `ROE ${r.period_a}`), h("div", { class: "n" }, ratioText("percent", d.roe_a))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, `ROE ${r.period_b}`), h("div", { class: "n" }, ratioText("percent", d.roe_b))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "Change"), h("div", { class: `n ${sign(d.change)}` }, `${d.change > 0 ? "+" : ""}${(d.change * 100).toFixed(1)} pp`))) : null,
    takeawayCard(r.takeaways),
    dupont,
    watchlistCard(r.watchlist),
    card("In words", h("div", { class: "summary" }, paragraphs(r.summary))),
    card("Ratios", ratioTable),
    card("Figures", lineTable, growth),
    card("What was used", h("div", { class: "chips" }, map)),
    (r.unavailable || []).length ? card("Not worked out", h("ul", {}, r.unavailable.map((u) => h("li", {}, `${u.label}: ${u.reason}`)))) : null,
    issueNotes(r.checks, "warning"));
}

function renderBridge(r) {
  const f = makeFormatter(r);
  const isPvm = r.kind === "pvm";
  const cols = isPvm ? [["Units", (s) => `${formatNumber(s.units_a)} → ${formatNumber(s.units_b)}`],
    ["Price", (s) => `${f.level(s.price_a)} → ${f.level(s.price_b)}`]]
    : [["Share", (s) => `${formatPct(s.share_a, 1)} → ${formatPct(s.share_b, 1)}`],
      ["Growth (us / market)", (s) => `${formatPct(s.our_growth, 1)} / ${formatPct(s.market_growth, 1)}`]];
  const effects = isPvm ? r.effects : ["market", "exposure", "share"];
  const names = { volume: "Volume", mix: "Mix", price: "Price", cost: "Cost", market: "Market growth", exposure: "Segment mix", share: "Share change" };
  const nz = (v) => (v === null || v === undefined ? "n/a" : v);
  const table = h("div", { class: "chartwrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Segment"), cols.map(([c]) => h("th", { class: "num" }, c)),
      effects.map((e) => h("th", { class: "num" }, names[e])), h("th", { class: "num" }, "Total"))),
    h("tbody", {}, r.segments.map((s) => h("tr", {}, h("td", {}, s.label),
      cols.map(([, fn]) => h("td", { class: "num" }, fn(s))),
      effects.map((e) => h("td", { class: `num ${sign(s[e])}` }, f.delta(nz(s[e])))),
      h("td", { class: `num ${sign(s.total)}` }, f.delta(nz(s.total))))))));
  const diff = r.total_b - r.total_a;
  const stats = [["Earlier period", f.level(r.total_a)], ["Later period", f.level(r.total_b)], ["Change", f.delta(diff), sign(diff)]];
  if (!isPvm) stats.push(["Our share", `${formatPct(r.share_a, 1)} → ${formatPct(r.share_b, 1)}`]);
  const ci = r.ci || {};
  const ciRow = Object.keys(ci).length ? h("p", { class: "muted" }, "95% intervals: " + Object.entries(ci).map(([k, [lo, hi]]) => `${names[k] || k} ${f.delta(lo)} to ${f.delta(hi)}`).join("; ") + ".") : null;
  return h("div", {},
    h("div", { class: "stats" }, stats.map(([k, v, c]) => h("div", { class: "stat" }, h("div", { class: "muted" }, k), h("div", { class: `n ${c || ""}` }, v)))),
    card("Where the change sits", h("div", { class: "chartwrap" }, waterfallSvg(r)), ciRow),
    card("In words", h("div", { class: "summary" }, paragraphs(r.summary))),
    card(`Segments${r.segments_total > r.segments.length ? ` (largest ${r.segments.length} of ${r.segments_total})` : ""}`, table),
    (r.notes || []).map((n) => h("p", { class: "muted" }, n)),
    (r.validation || []).filter((i) => i.severity !== "info").map((i) => notice(i.severity === "error" ? "error" : "warning", i.message)),
    notice("warning", r.caveat));
}

function renderTrends(r) {
  const f = makeFormatter({ ...r, is_ratio: r.is_ratio, total_a: r.total_a, total_b: r.total_b });
  const L = r.latest;
  const pct = (v) => (v === null || v === undefined ? "n/a" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`);
  const stats = [["Latest month", `${L.month}: ${f.level(L.value)}`], ["Vs same month last year", pct(L.yoy), sign(L.yoy)], ["Vs previous month", pct(L.mom), sign(L.mom)]];
  if (L.ttm_growth !== undefined) stats.push(["Last 12 months vs the 12 before", pct(L.ttm_growth), sign(L.ttm_growth)]);
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const s = r.seasonal;
  const seasonal = s ? card("Seasonal pattern",
    h("div", { class: "chips" }, s.index.map((v, i) => h("span", { class: "chip" }, `${MONTHS[i]} ${v.toFixed(2)}`))),
    h("p", { class: "muted" }, `${s.model === "multiplicative" ? "Multipliers" : "Adjustments"} around the trend, from ${s.years.toFixed(0)} years of history; the pattern accounts for about ${(s.strength * 100).toFixed(0)}% of the variation around the trend.`)) : null;
  const recent = r.series.slice(-12).reverse();
  const table = h("div", { class: "chartwrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Month", "Value", "vs last month", "vs last year", "3-month avg", "12-month avg"].map((c, i) => h("th", { class: i ? "num" : "" }, c)))),
    h("tbody", {}, recent.map((m) => h("tr", {}, h("td", {}, m.month), h("td", { class: "num" }, f.level(m.value)),
      h("td", { class: `num ${sign(m.mom)}` }, pct(m.mom)), h("td", { class: `num ${sign(m.yoy)}` }, pct(m.yoy)),
      h("td", { class: "num" }, m.ma3 === null ? "n/a" : f.level(m.ma3)), h("td", { class: "num" }, m.ma12 === null ? "n/a" : f.level(m.ma12)))))));
  return h("div", {},
    h("div", { class: "stats" }, stats.map(([k, v, c]) => h("div", { class: "stat" }, h("div", { class: "muted" }, k), h("div", { class: `n ${c || ""}` }, v)))),
    card("Month by month", h("div", { class: "chartwrap" }, lineChartSvg(r.series, f.level)),
      h("p", { class: "muted" }, "Thin line: the monthly value. Thick line: the 12-month moving average, which smooths out the seasonal pattern.")),
    card("In words", h("div", { class: "summary" }, paragraphs(r.summary))),
    seasonal, card("Last 12 months", table),
    (r.notes || []).map((n) => h("p", { class: "muted" }, n)),
    notice("warning", r.caveat));
}

// ---------------------------------------------------------------- forms
const colSelect = (id, cols, chosen, none) => h("select", { id },
  none ? h("option", { value: "" }, none) : null,
  cols.map((c) => h("option", { value: c, selected: c === chosen }, c)));

function periodInputs(m, prefix) {
  const pa = m.default_periods ? m.default_periods.a : ["", ""];
  const pb = m.default_periods ? m.default_periods.b : ["", ""];
  const d = (id, v) => h("input", { type: "date", id: `${prefix}_${id}`, value: v, min: m.date_min, max: m.date_max });
  const a1 = d("a1", pa[0]), a2 = d("a2", pa[1]), b1 = d("b1", pb[0]), b2 = d("b2", pb[1]);
  const node = h("div", { class: "row" },
    h("div", {}, h("label", { for: `${prefix}_a1` }, "Earlier period starts"), a1),
    h("div", {}, h("label", { for: `${prefix}_a2` }, "Earlier period ends"), a2),
    h("div", {}, h("label", { for: `${prefix}_b1` }, "Later period starts"), b1),
    h("div", {}, h("label", { for: `${prefix}_b2` }, "Later period ends"), b2));
  return { node, into(params) { if (a1.value && a2.value && b1.value && b2.value) { params.period_a = [a1.value, a2.value]; params.period_b = [b1.value, b2.value]; } } };
}

function formShell(did, title, intro, fields, build, ctx, button) {
  const msg = h("div");
  const run = h("button", { class: "primary", type: "submit" }, button);
  return h("form", { class: "card", onsubmit: async (ev) => {
    ev.preventDefault(); msg.replaceChildren(); run.disabled = true;
    try {
      const params = build();
      msg.replaceChildren(notice("info", "Analysing…"));
      const out = await api.analyse(did, params); ctx.go(`#/analysis/${out.id}`);
    } catch (e) {
      if (e.status === 401) return ctx.errorView(e);
      msg.replaceChildren(notice("error", e.message)); run.disabled = false;
    }
  } }, h("h2", { style: "margin-top:0" }, title), h("p", { class: "muted" }, intro), fields,
  h("div", { style: "height:12px" }), run, msg);
}

const moneyFields = () => {
  const currency = h("input", { type: "text", id: "cur", maxlength: "8", placeholder: "e.g. Rs or $", style: "max-width:140px" });
  const indian = h("input", { type: "checkbox", id: "ind" });
  return { currency, indian, node: h("div", { class: "row" },
    h("div", {}, h("label", { for: "cur" }, "Currency symbol (optional)"), currency),
    h("div", {}, h("label", { for: "ind" }, "Digit grouping"), h("label", { style: "color:var(--ink)" }, indian, " Indian (12,34,567)"))) };
};

// Extra analyses for a table of dated rows (price/volume/mix, market share, trends).
export function extraForms(ds, did, ctx) {
  const m = ds.meta, sug = m.suggested || {};
  const out = [];
  const dims = m.dimension_cols || [];
  if (dims.length) {
    const rev = colSelect("pvm_rev", m.metric_cols, sug.revenue), units = colSelect("pvm_units", m.metric_cols, sug.units);
    const cost = colSelect("pvm_cost", m.metric_cols, sug.cost, "none (revenue bridge only)");
    const per = periodInputs(m, "pvm"); const money = moneyFields();
    out.push(formShell(did, "Price, volume and mix",
      "Splits the change in revenue (or profit, if you give a cost column) into volume, mix between segments and price. Needs a units column that counts things sold.",
      [h("div", { class: "row" }, h("div", {}, h("label", { for: "pvm_rev" }, "Revenue column"), rev),
        h("div", {}, h("label", { for: "pvm_units" }, "Units column"), units),
        h("div", {}, h("label", { for: "pvm_cost" }, "Cost column"), cost)), per.node, money.node],
      () => { const p = { kind: "pvm", revenue: rev.value, units: units.value, currency: money.currency.value.trim(), indian: money.indian.checked }; if (cost.value) p.cost = cost.value; per.into(p); return p; },
      ctx, "Split into price, volume and mix"));
    const our = colSelect("mk_our", m.metric_cols, sug.revenue), mkt = colSelect("mk_mkt", m.metric_cols, sug.market);
    const per2 = periodInputs(m, "mk"); const money2 = moneyFields();
    out.push(formShell(did, "Market share",
      "Splits the change in your value into market growth, a shift in which segments you sell in, and a change in share. Needs a column holding the whole market's value for each row.",
      [h("div", { class: "row" }, h("div", {}, h("label", { for: "mk_our" }, "Our value column"), our),
        h("div", {}, h("label", { for: "mk_mkt" }, "Market value column"), mkt)), per2.node, money2.node],
      () => { const p = { kind: "market", revenue: our.value, market: mkt.value, currency: money2.currency.value.trim(), indian: money2.indian.checked }; per2.into(p); return p; },
      ctx, "Split into market and share"));
  }
  const freq = m.frequency;
  const metric = colSelect("tr_metric", m.metric_cols, sug.revenue || m.metric_cols[0]);
  const custom = h("input", { type: "text", id: "tr_custom", placeholder: "or a formula, e.g. revenue - cost" });
  const model = h("select", { id: "tr_model" }, h("option", { value: "multiplicative" }, "Multiplicative (seasonal swings grow with the level)"), h("option", { value: "additive" }, "Additive (seasonal swings stay the same size)"));
  const money3 = moneyFields();
  out.push(formShell(did, "Trend and seasonality",
    freq === "daily" || freq === "monthly" ? "Monthly totals, growth against last month and last year, a moving average, and the seasonal pattern when there are at least 24 months. Needs 13 months or more."
      : `Needs daily or monthly data; this file looks ${freq || "irregular"}.`,
    [h("div", { class: "row" }, h("div", {}, h("label", { for: "tr_metric" }, "Column"), metric), h("div", {}, h("label", { for: "tr_custom" }, "Formula (optional)"), custom), h("div", {}, h("label", { for: "tr_model" }, "Seasonal model"), model)), money3.node],
    () => ({ kind: "trends", metric: custom.value.trim() || metric.value, model: model.value, currency: money3.currency.value.trim(), indian: money3.indian.checked }),
    ctx, "Show the trend"));
  return out;
}

// Dataset page for a financial-statement workbook: confirm what each row is, then analyse.
export function statementsForm(ds, did, ctx) {
  const m = ds.meta;
  const picks = {};
  const groups = {};
  m.roles.forEach((r) => { (groups[r.group] ||= []).push(r); });
  const labelFor = (l) => (l.label === l.key ? l.label : `${l.label} (${l.sheet})`);
  const grid = Object.entries(groups).map(([group, roles]) => h("div", {}, h("h3", { class: "grp" }, group),
    h("div", { class: "row" }, roles.map((r) => {
      const sel = h("select", { id: `role_${r.role}` }, h("option", { value: "" }, "— not used —"),
        m.lines.map((l) => h("option", { value: l.key, selected: m.suggested_roles[r.role] === l.key }, labelFor(l))));
      picks[r.role] = sel;
      return h("div", {}, h("label", { for: `role_${r.role}` }, r.label), sel);
    }))));
  const periods = m.periods;
  const from = h("select", { id: "from" }, periods.map((p, i) => h("option", { value: p, selected: i === Math.max(0, periods.length - 2) }, p)));
  const to = h("select", { id: "to" }, periods.map((p, i) => h("option", { value: p, selected: i === periods.length - 1 }, p)));
  const fin = h("select", { id: "fin" }, h("option", { value: "" }, "Detect automatically"), h("option", { value: "yes" }, "Yes, a bank or finance company"), h("option", { value: "no" }, "No, an ordinary business"));
  const money = moneyFields();
  const intro = `Read ${m.rows} line items over ${periods.length} periods (${periods[0]} to ${periods[periods.length - 1]}). Check that each statement item points at the right line; blank means it will not be used.`;
  return h("div", {}, ...(m.notes || []).map((n) => notice("info", n)),
    m.rows > m.lines.length ? notice("warning", `The file has ${m.rows} lines; only the first ${m.lines.length} can be chosen here.`) : null,
    formShell(did, "Financial statement analysis", intro,
      [grid, h("div", { class: "row" }, h("div", {}, h("label", { for: "from" }, "Compare from"), from), h("div", {}, h("label", { for: "to" }, "to"), to),
        h("div", {}, h("label", { for: "fin" }, "Bank or finance company?"), fin)), money.node],
      () => {
        const mapping = {};
        Object.entries(picks).forEach(([role, sel]) => { if (sel.value) mapping[role] = sel.value; });
        const p = { kind: "statements", mapping, from_period: from.value, to_period: to.value, currency: money.currency.value.trim(), indian: money.indian.checked };
        if (fin.value) p.financial = fin.value === "yes";
        return p;
      }, ctx, "Work out the ratios"));
}

// ---------------------------------------------------------------- takeaways and watchlist
function takeawayCard(list) {
  if (!list || !list.length) return null;
  return h("div", { class: "card takeaways" }, h("h2", { style: "margin-top:0" }, "The short version"),
    h("ul", {}, list.map((t) => h("li", {}, t))));
}

const evidenceText = (e) => {
  const v = e.value;
  const t = e.unit === "percent" ? `${(v * 100).toFixed(1)}%` : e.unit === "days" ? `${v.toFixed(1)} days`
    : e.unit === "x" ? `${v.toFixed(2)}x` : formatNumber(v, { decimals: 0 });
  return `${e.period}: ${t}`;
};

function watchlistCard(w) {
  if (!w) return null;
  const looks = w.flags.filter((f) => f.level === "look").length;
  const head = w.flags.length
    ? `${w.flags.length} of ${w.checked} checks turned something up${looks ? `, ${looks} worth a closer look` : ""}.`
    : `${w.checked} checks ran and none turned anything up.`;
  const flags = w.flags.map((f) => h("div", { class: `flag ${f.level}` },
    h("div", { class: "flag-head" }, h("span", { class: "badge" }, f.level === "look" ? "Worth a look" : "Note"), h("strong", {}, f.title)),
    h("p", {}, f.detail),
    f.evidence.length ? h("div", { class: "chips" }, f.evidence.map((e) => h("span", { class: "chip" }, evidenceText(e)))) : null));
  const more = (title, items) => items.length ? h("details", {}, h("summary", {}, `${title} (${items.length})`),
    h("ul", {}, items.map((i) => h("li", {}, i)))) : null;
  return card("Watchlist", h("p", { class: "muted" }, head),
    flags,
    more("Checks that found nothing", w.clear),
    more("Checks skipped because a line was missing or does not apply", w.skipped),
    h("p", { class: "muted" }, "Each check compares numbers from this file against a fixed threshold. A flag is a prompt to look closer, not a finding of a problem, and it does not say why the number moved."));
}

// ---------------------------------------------------------------- two companies side by side
export function renderCompare(c) {
  const fmt = (unit, v) => (v === null || v === undefined ? "n/a" : ratioText(unit, v));
  const [A, B] = c.names;
  const bars = (unit, a, b) => {
    const top = Math.max(Math.abs(a ?? 0), Math.abs(b ?? 0)) || 1;
    const bar = (v, cls) => h("div", { class: `bar ${cls}`, style: `width:${Math.max(2, Math.abs(v ?? 0) / top * 100)}%` });
    return h("div", { class: "pairbars" }, bar(a, "a"), bar(b, "b"));
  };
  const table = h("div", { class: "chartwrap" }, h("table", { class: "cmp" },
    h("thead", {}, h("tr", {}, h("th", {}, "Ratio"), h("th", { class: "num" }, A), h("th", { class: "num" }, B), h("th", {}, ""))),
    h("tbody", {}, c.rows.map((r) => h("tr", {}, h("td", { title: r.formula }, r.label),
      h("td", { class: "num" }, fmt(r.unit, r.values[0].to)), h("td", { class: "num" }, fmt(r.unit, r.values[1].to)),
      h("td", { class: "barcell" }, bars(r.unit, r.values[0].to, r.values[1].to)))))));
  const growth = h("div", { class: "chartwrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Growth over the period"), h("th", { class: "num" }, A), h("th", { class: "num" }, B))),
    h("tbody", {}, c.growth.map((g) => h("tr", {}, h("td", {}, g.label),
      g.values.map((v) => h("td", { class: `num ${v === null ? "" : sign(v)}` }, v === null ? "n/a" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`)))))));
  const per = c.companies.map((x) => `${x.name}: ${x.period_a} to ${x.period_b}`).join(" · ");
  return h("div", {},
    h("p", { class: "sub" }, per),
    c.notes.slice(0, 1).map((n) => notice("warning", n)),
    card("Side by side", h("div", { class: "legend" }, h("span", { class: "key a" }), ` ${A}  `, h("span", { class: "key b" }), ` ${B}`), table,
      h("p", { class: "muted" }, "Latest period of each file. Hover a ratio name for its formula. Bars are scaled within each row, so they compare the two companies on that ratio only.")),
    card("Growth", growth),
    card("In words", h("ul", {}, c.summary_lines.map((l) => h("li", {}, l)))),
    c.notes.slice(1).length ? h("details", { class: "card" }, h("summary", {}, `Ratios not lined up (${c.notes.length - 1})`),
      h("ul", {}, c.notes.slice(1).map((n) => h("li", {}, n)))) : null,
    notice("warning", c.caveat));
}
