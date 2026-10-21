// Turn a result into rows for a CSV download, and trigger the download. Nothing here talks to
// the server: it uses the numbers already on the page.

const DANGEROUS = /^[=+\-@\t\r]/;

// Quote a cell; text that a spreadsheet could read as a formula gets a leading apostrophe.
export function csvCell(v) {
  if (v === null || v === undefined) return "";
  let s = typeof v === "number" ? String(v) : String(v);
  if (typeof v === "string" && DANGEROUS.test(s) && Number.isNaN(Number(s))) s = `'${s}`;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export const toCsv = (rows) => rows.map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";

const slug = (t) => String(t || "result").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50) || "result";

export function resultRows(r) {
  if (r.kind === "statements") {
    const rows = [["Line item", ...r.periods]];
    r.lines.forEach((l) => rows.push([l.label, ...l.values]));
    rows.push([], ["Ratio", ...r.periods]);
    r.ratios.forEach((x) => rows.push([x.label, ...x.values]));
    if ((r.growth || []).length) { rows.push([], ["Growth", ...r.periods]); r.growth.forEach((g) => rows.push([`${g.label} growth`, ...g.values])); }
    if (r.watchlist) { rows.push([], ["Watchlist", "Level", "Detail"]); r.watchlist.flags.forEach((f) => rows.push([f.title, f.level, f.detail])); }
    return rows;
  }
  if (r.kind === "pvm" || r.kind === "market") {
    const keys = r.segments.length ? Object.keys(r.segments[0]) : [];
    return [keys, ...r.segments.map((s) => keys.map((k) => s[k]))];
  }
  if (r.kind === "trends") {
    const keys = ["month", "value", "mom", "yoy", "ma3", "ma12"];
    return [keys, ...r.series.map((m) => keys.map((k) => m[k]))];
  }
  const rows = [["Rank", "Segment", "Moved by", "Share of change", "Interval low", "Interval high"]];
  (r.findings || []).forEach((x) => rows.push([x.rank, x.label, x.explained, x.share_of_change, x.ci_low, x.ci_high]));
  return rows;
}

export function downloadCsv(r, title) {
  const blob = new Blob(["﻿", toCsv(resultRows(r))], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `whymoved-${slug(title)}.csv`;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// A plain-text report (Markdown) of a result: headline, the written summary, the key numbers, and
// any notes a person wrote. Built from what is already on the page; nothing is sent anywhere.
export function reportMarkdown(r, title, subtitle, notes = []) {
  const out = [`# ${title}`, "", `_${subtitle}_`, ""];
  if ((r.takeaways || []).length) out.push("## Headlines", "", ...r.takeaways.map((t) => `- ${t}`), "");
  out.push("## In words", "", String(r.summary || ""), "");
  const rows = resultRows(r);
  if (rows.length > 1 && rows.length <= 60) {
    const width = Math.max(...rows.map((x) => x.length));
    const line = (x) => `| ${Array.from({ length: width }, (_, i) => String(x[i] ?? "").replace(/\|/g, "/")).join(" | ")} |`;
    out.push("## Numbers", "", line(rows[0]), `|${" --- |".repeat(width)}`, ...rows.slice(1).filter((x) => x.length).map(line), "");
  }
  if (r.watchlist && r.watchlist.flags.length) {
    out.push("## Worth a closer look", "", ...r.watchlist.flags.map((f) => `- **${f.title}.** ${f.detail}`), "");
  }
  if (notes.length) out.push("## Notes written by people", "", ...notes.map((n) => `> ${String(n.body).replace(/\n/g, "\n> ")}`), "");
  out.push("---", String(r.caveat || "This shows where the metric moved, not why it moved."), "");
  return out.join("\n");
}

export function downloadText(text, filename, type = "text/markdown;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
