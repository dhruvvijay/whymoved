// SVG charts drawn from plain result data. Text goes in through textContent only.
import { layoutWaterfall } from "./chart.js";
import { makeFormatter } from "./fmt.js";
import { svg } from "./dom.js";

export function waterfallSvg(result) {
  const f = makeFormatter(result);
  const L = layoutWaterfall(result.waterfall);
  const root = svg("svg", { class: "wf", viewBox: `0 0 ${L.width} ${L.height}`, role: "img",
    "aria-label": `Waterfall chart: ${result.waterfall.map((b) => `${b.label} ${b.kind === "total" ? f.level(b.value) : f.delta(b.value)}`).join("; ")}` });
  root.append(svg("line", { class: "axis", x1: L.pad.l, x2: L.width - L.pad.r, y1: L.baseline, y2: L.baseline }));
  L.connectors.forEach((c) => root.append(svg("line", { class: "conn", x1: c.x1, x2: c.x2, y1: c.y, y2: c.y })));
  L.bars.forEach((b) => {
    const cls = `${b.direction}${b.kind === "rest" ? " rest" : ""}`;
    const rect = svg("rect", { class: cls, x: b.x, y: b.y, width: b.w, height: b.h, rx: 3 });
    rect.append(svg("title", {}, `${b.label}: ${b.kind === "total" ? f.level(b.value) : f.delta(b.value)}`));
    root.append(rect);
    const text = b.kind === "total" ? f.level(b.value) : f.delta(b.value);
    root.append(svg("text", { class: "val", x: b.cx, y: Math.max(14, b.y - 6), "text-anchor": "middle" }, text));
    wrap(b.label, 15).slice(0, 3).forEach((line, i) =>
      root.append(svg("text", { x: b.cx, y: L.baseline + 18 + i * 14, "text-anchor": "middle" }, line)));
  });
  if (L.truncated) root.append(svg("text", { x: L.pad.l, y: L.height - 6 }, "Note: the vertical axis does not start at zero, so the moves are readable."));
  return root;
}

function wrap(text, width) {
  const words = String(text).split(/\s+/); const lines = []; let line = "";
  for (const w of words) {
    if ((line + " " + w).trim().length > width && line) { lines.push(line); line = w; }
    else line = (line + " " + w).trim();
  }
  if (line) lines.push(line);
  return lines;
}

// Monthly line chart: the value, and the 12-month moving average when there is one.
export function lineChartSvg(rows, fmtLevel) {
  const W = 720, H = 280, pad = { l: 92, r: 16, t: 16, b: 34 };
  const pts = rows.map((r, i) => ({ i, v: r.value, m: r.ma12, month: r.month }))
    .filter((p) => p.v !== null && p.v !== undefined);
  const all = pts.flatMap((p) => (p.m === null || p.m === undefined ? [p.v] : [p.v, p.m]));
  const lo = Math.min(...all), hi = Math.max(...all);
  const span = hi - lo || 1;
  const x = (i) => pad.l + (i / Math.max(1, rows.length - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / span) * (H - pad.t - pad.b);
  const root = svg("svg", { class: "wf line", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Monthly values from ${rows[0].month} to ${rows[rows.length - 1].month}, lowest ${fmtLevel(lo)}, highest ${fmtLevel(hi)}` });
  root.append(svg("line", { class: "axis", x1: pad.l, x2: W - pad.r, y1: H - pad.b, y2: H - pad.b }));
  [lo, hi].forEach((v) => root.append(svg("text", { x: pad.l - 6, y: y(v) + 4, "text-anchor": "end" }, fmtLevel(v))));
  const path = (key) => {
    let d = "";
    pts.forEach((p) => {
      const v = key === "v" ? p.v : p.m;
      if (v === null || v === undefined) return;
      d += `${d ? "L" : "M"}${x(p.i).toFixed(1)},${y(v).toFixed(1)}`;
    });
    return d;
  };
  root.append(svg("path", { class: "ln value", d: path("v"), fill: "none" }));
  if (pts.some((p) => p.m !== null && p.m !== undefined)) root.append(svg("path", { class: "ln avg", d: path("m"), fill: "none" }));
  rows.forEach((r, i) => {
    if (r.month.endsWith("-01") || i === 0) root.append(svg("text", { x: x(i), y: H - 12, "text-anchor": "middle" }, r.month.slice(0, 4)));
  });
  return root;
}

