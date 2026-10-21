// Waterfall geometry. Pure: takes result.waterfall and a size, returns what to draw.
// Totals are bars from the axis floor; findings float between the running total before and after.
// When the totals are far larger than the moves, the axis does not start at zero, and the chart
// says so (truncated = true) rather than hiding it.

export function layoutWaterfall(steps, { width = 720, height = 340, pad = { l: 16, r: 16, t: 28, b: 84 } } = {}) {
  const innerW = width - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;
  let running = 0;
  const bars = steps.map((s) => {
    if (s.kind === "total") {
      running = s.value;
      return { ...s, from: 0, to: s.value, end: s.value };
    }
    const from = running;
    running += s.value;
    return { ...s, from, to: running, end: running };
  });
  // running levels the chart must show (a total's own bar starts at the floor, not at a level)
  const levels = bars.flatMap((b) => (b.kind === "total" ? [b.end] : [b.from, b.to]));
  const hi = Math.max(...levels);
  const lo = Math.min(...levels);
  // lowest level a total bar must reach for the moves to be readable
  const moveSizes = bars.filter((b) => b.kind !== "total").map((b) => Math.abs(b.to - b.from));
  const span = hi - lo;
  const biggestMove = Math.max(0, ...moveSizes);
  let truncated = false;
  let floor = Math.min(0, lo);
  if (lo > 0 && biggestMove > 0 && biggestMove < 0.25 * hi) {
    // zoom in: floor sits a little below the lowest running level
    const padding = Math.max(span, biggestMove) * 0.6;
    floor = Math.max(0, lo - padding);
    truncated = floor > 0;
  }
  const topLevel = truncated ? hi : Math.max(hi, 0);
  const ceil = topLevel + (topLevel - floor) * 0.08 || 1;
  const range = ceil - floor || 1;
  const y = (v) => pad.t + innerH - ((v - floor) / range) * innerH;
  const slot = innerW / bars.length;
  const barW = Math.min(88, slot * 0.62);
  const out = bars.map((b, i) => {
    const x = pad.l + slot * i + (slot - barW) / 2;
    const top = y(Math.max(b.from, b.to, floor));
    const bottom = y(Math.max(Math.min(b.from, b.to), floor));
    return { ...b, x, w: barW, y: top, h: Math.max(2, bottom - top), cx: x + barW / 2,
             direction: b.kind === "total" ? "total" : b.value < 0 ? "down" : b.value > 0 ? "up" : "flat" };
  });
  const connectors = out.slice(0, -1).map((b, i) => ({
    x1: b.x + b.w, x2: out[i + 1].x, y: y(b.end),
  }));
  return { width, height, pad, bars: out, connectors, floor, ceil, truncated, y, baseline: y(floor) };
}
