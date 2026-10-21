// Pure-function tests for the browser app. Run: node web_tests/unit.mjs
import assert from "node:assert/strict";
import { formatNumber, makeFormatter, formatPct, groupDigits } from "../web/fmt.js";
import { layoutWaterfall } from "../web/chart.js";

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log("ok -", name); };

t("western grouping", () => assert.equal(formatNumber(1234567.8), "1,234,568"));
t("indian grouping", () => assert.equal(formatNumber(1234567.8, { indian: true }), "12,34,568"));
t("indian small numbers", () => { assert.equal(groupDigits("999", true), "999"); assert.equal(groupDigits("1000", true), "1,000"); });
t("negative and sign", () => {
  assert.equal(formatNumber(-4549603.14, { currency: "Rs " }), "-Rs 4,549,603");
  assert.equal(formatNumber(12, { sign: true }), "+12");
});
t("tiny negative rounds to plain zero", () => assert.equal(formatNumber(-0.0004), "0"));
t("missing values", () => { assert.equal(formatNumber(null), "n/a"); assert.equal(formatNumber(NaN), "n/a"); assert.equal(formatPct(Infinity), "n/a"); });

const conv = { is_ratio: true, total_a: 0.0406, total_b: 0.0389, currency: "Rs ", indian: false };
t("rate under 1 is a percent, change in pp", () => {
  const f = makeFormatter(conv);
  assert.equal(f.percent, true);
  assert.equal(f.level(0.0406), "4.06%");
  assert.equal(f.delta(-0.0017416), "-0.17 pp");
  assert.equal(f.delta(0.00001), "0.00 pp");
});
t("price-like ratio is not a percent and takes no currency prefix", () => {
  const f = makeFormatter({ is_ratio: true, total_a: 512.4, total_b: 530.1, currency: "Rs " });
  assert.equal(f.percent, false);
  assert.equal(f.level(512.4), "512.4");
  assert.equal(f.delta(17.7), "+17.7");
});
t("additive metric uses the currency", () => {
  const f = makeFormatter({ is_ratio: false, total_a: 1e6, total_b: 9e5, currency: "Rs ", indian: true });
  assert.equal(f.level(1000000), "Rs 10,00,000");
  assert.equal(f.delta(-100000), "-Rs 1,00,000");
});

const wf = (a, moves, b) => [{ label: "A", value: a, kind: "total" },
  ...moves.map((v, i) => ({ label: `m${i}`, value: v, kind: i === moves.length - 1 ? "rest" : "finding" })),
  { label: "B", value: b, kind: "total" }];

t("waterfall bars chain: each starts where the last ended", () => {
  const L = layoutWaterfall(wf(100, [-30, 5], 75));
  assert.equal(L.bars.length, 4);
  assert.equal(L.bars[1].from, 100); assert.equal(L.bars[1].to, 70);
  assert.equal(L.bars[2].from, 70); assert.equal(L.bars[2].to, 75);
  assert.equal(L.bars[3].end, 75);
  assert.equal(L.truncated, false); assert.equal(L.floor, 0);
});
t("down bars hang from the previous level, up bars rise from it", () => {
  const L = layoutWaterfall(wf(100, [-30, 5], 75));
  assert.ok(Math.abs(L.bars[1].y - L.y(100)) < 1e-6);
  assert.ok(Math.abs(L.bars[2].y - L.y(75)) < 1e-6);
  assert.equal(L.bars[1].direction, "down"); assert.equal(L.bars[2].direction, "up");
});
t("small moves on big totals zoom the axis and say so", () => {
  const L = layoutWaterfall(wf(0.0406, [-0.0017, 0.00001], 0.03889));
  assert.equal(L.truncated, true); assert.ok(L.floor > 0);
  for (const b of L.bars) { assert.ok(b.h >= 2); assert.ok(b.y >= 0 && b.y + b.h <= L.height); }
  assert.ok(L.bars[1].h > L.bars[0].h * 0.05, "the move must be visible next to the totals");
});
t("a big move needs no zoom", () => assert.equal(layoutWaterfall(wf(100, [-60, 0], 40)).truncated, false));
t("all-zero change does not crash", () => {
  const L = layoutWaterfall(wf(50, [0, 0], 50)); assert.equal(L.bars[1].direction, "flat");
});
t("negative totals keep a zero floor below them", () => {
  const L = layoutWaterfall(wf(-50, [-10, 0], -60)); assert.ok(L.floor <= -60);
});

t("negative totals stay inside the drawing area", () => {
  const L = layoutWaterfall(wf(-50, [-10, 0], -60));
  for (const b of L.bars) assert.ok(b.y >= 0 && b.y + b.h <= L.height, JSON.stringify(b));
});
import { reportMarkdown, toCsv } from "../web/export.js";
t("report has headline, words, table, flags, notes and the caveat", () => {
  const r = { kind: "changes", findings: [{ rank: 1, label: "region=North", explained: -5, share_of_change: 0.9, ci_low: -6, ci_high: -4 }],
    summary: "Revenue fell.", caveat: "Where, not why.", takeaways: ["Revenue -5%."] };
  const md = reportMarkdown(r, "Revenue: what changed", "A to B", [{ body: "Promo ended\nin March" }]);
  for (const part of ["# Revenue: what changed", "- Revenue -5%.", "## In words", "| Rank | Segment", "region=North", "> Promo ended\n> in March", "Where, not why."])
    assert.ok(md.includes(part), part);
});
t("report table cells cannot break the table", () => {
  const md = reportMarkdown({ kind: "changes", findings: [{ rank: 1, label: "a|b", explained: 1, share_of_change: 1, ci_low: 0, ci_high: 2 }], summary: "x" }, "t", "s");
  assert.ok(md.includes("a/b") && !md.includes("a|b"));
});
t("csv cells that look like formulas are neutralised", () => assert.equal(toCsv([["=1+1", "ok"]]), "'=1+1,ok\r\n"));
console.log(`${n} passed`);
