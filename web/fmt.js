// Number formatting that mirrors src/whymoved/narrative.py so the chart and the written
// summary never disagree. Pure functions, no DOM: tested under node (web/tests/run.mjs).

export function groupDigits(whole, indian) {
  if (!indian || whole.length <= 3) return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const head = whole.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${head},${whole.slice(-3)}`;
}

export function formatNumber(value, { decimals = 0, currency = "", indian = false, sign = false } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  const text = Math.abs(value).toFixed(decimals);
  const [whole, frac] = text.split(".");
  const body = `${currency}${groupDigits(whole, indian)}${frac ? "." + frac : ""}`;
  const isZero = Number(text) === 0;
  if (value < 0 && !isZero) return `-${body}`;
  return sign && !isZero ? `+${body}` : body;
}

// Same rule as the server: a ratio whose totals are both within [-1, 1] is shown as a percent.
export function makeFormatter(result) {
  const percent = !!result.is_ratio && Math.abs(result.total_a) <= 1 && Math.abs(result.total_b) <= 1;
  const currency = result.is_ratio ? "" : (result.currency || "");
  const indian = !!result.indian;
  const decimalsFor = (v) => {
    const a = Math.abs(v);
    if (a >= 1000) return 0;
    if (a >= 10) return 1;
    return a >= 1 ? 2 : 3;
  };
  return {
    percent,
    // absolute level of the metric
    level(v) {
      if (percent) return `${(v * 100).toFixed(2)}%`;
      return formatNumber(v, { decimals: result.is_ratio ? decimalsFor(v) : 0, currency, indian });
    },
    // a signed change in the metric (percentage points for percent metrics)
    delta(v) {
      if (percent) {
        const t = (v * 100).toFixed(2);
        return `${Number(t) > 0 ? "+" : ""}${t} pp`;
      }
      return formatNumber(v, { decimals: result.is_ratio ? decimalsFor(v) : 0, currency, indian, sign: true });
    },
  };
}

export function formatPct(share, decimals = 0) {
  if (share === null || share === undefined || !Number.isFinite(share)) return "n/a";
  return `${(share * 100).toFixed(decimals)}%`;
}
