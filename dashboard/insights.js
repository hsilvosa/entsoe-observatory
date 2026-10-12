"use strict";

/* Patterns and forecast tabs. Uses helpers from app.js (el, getJson, state, fmt, showTip,
   hideTip, segmented, zoneName, legend) at call time. */

const SVG_NS = "http://www.w3.org/2000/svg";
const INK = { muted: "#8e9aa5", line: "#29323b", accent: "#d9a441", cool: "#6fb7d1", warm: "#c2412d" };

function svgRoot(W, H) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const add = (name, attrs, text) => {
    const n = document.createElementNS(SVG_NS, name);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    svg.append(n);
    return n;
  };
  return { svg, add };
}

/* Numeric x line chart. series: [{ name, values, color, dash, width, end }]; band: { lo, hi, color }. */
function xyChart(container, { xs, series, band, xTicks, height = 260, unit = "", tipLabel = (x) => String(x), zeroLine = true }) {
  const W = 1000, H = height, m = { l: 52, r: 56, t: 12, b: 26 };
  const vals = series.flatMap((s) => s.values).concat(band ? band.lo.concat(band.hi) : []).filter((v) => v != null);
  let y0 = Math.min(...vals, zeroLine ? 0 : Infinity), y1 = Math.max(...vals);
  const pad = (y1 - y0) * 0.06; y1 += pad; if (y0 < 0) y0 -= pad;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const X = (v) => m.l + ((v - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
  const Y = (v) => H - m.b - ((v - y0) / (y1 - y0 || 1)) * (H - m.t - m.b);
  const { svg, add } = svgRoot(W, H);
  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4;
    add("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: INK.line });
    add("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: INK.muted }, fmt(v, 0));
  }
  if (y0 < 0 && zeroLine) add("line", { x1: m.l, x2: W - m.r, y1: Y(0), y2: Y(0), stroke: INK.muted, "stroke-dasharray": "2 3" });
  for (const [x, label] of xTicks) add("text", { x: X(x), y: H - 7, "text-anchor": "middle", "font-size": 11, fill: INK.muted }, label);
  const path = (vs) => {
    const parts = []; let cur = [];
    vs.forEach((v, i) => { if (v == null) { if (cur.length) parts.push(cur); cur = []; } else cur.push(`${X(xs[i]).toFixed(1)},${Y(v).toFixed(1)}`); });
    if (cur.length) parts.push(cur);
    return parts.map((p) => "M" + p.join("L")).join("");
  };
  if (band) {
    const from = band.from || 0;
    const up = band.hi.map((v, i) => `${X(xs[from + i]).toFixed(1)},${Y(v).toFixed(1)}`);
    const dn = band.lo.map((v, i) => `${X(xs[from + i]).toFixed(1)},${Y(v).toFixed(1)}`).reverse();
    add("path", { d: `M${up.join("L")}L${dn.join("L")}Z`, fill: band.color, opacity: 0.22 });
  }
  for (const s of series) {
    add("path", { d: path(s.values), fill: "none", stroke: s.color, "stroke-width": s.width || 1.6, "stroke-dasharray": s.dash || "", "stroke-linejoin": "round" });
    if (s.end) {
      const i = s.values.length - 1 - [...s.values].reverse().findIndex((v) => v != null);
      add("text", { x: X(xs[i]) + 6, y: Y(s.values[i]) + 4, "font-size": 11, fill: s.color }, s.end);
    }
  }
  const cross = add("line", { y1: m.t, y2: H - m.b, stroke: INK.muted, opacity: 0 });
  svg.addEventListener("mousemove", (e) => {
    const box = svg.getBoundingClientRect();
    const target = x0 + ((((e.clientX - box.left) / box.width) * W - m.l) / (W - m.l - m.r)) * (x1 - x0);
    let best = 0;
    xs.forEach((x, i) => { if (Math.abs(x - target) < Math.abs(xs[best] - target)) best = i; });
    cross.setAttribute("x1", X(xs[best])); cross.setAttribute("x2", X(xs[best])); cross.setAttribute("opacity", 1);
    const parts = series.map((s) => `${s.name}: ${fmt(s.values[best], 1)}${unit}`);
    const bi = best - (band ? band.from || 0 : 0);
    if (band && bi >= 0 && band.lo[bi] != null) parts.push(`range ${fmt(band.lo[bi], 0)} to ${fmt(band.hi[bi], 0)}`);
    showTip(e, `${tipLabel(xs[best])}  |  ${parts.join("  |  ")}`);
  });
  svg.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  container.replaceChildren(svg);
}

/* Signed columns around zero, one per category. */
function columnChart(container, { labels, values, sub, unit, height = 220 }) {
  const W = 1000, H = height, m = { l: 52, r: 12, t: 16, b: 44 };
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  const Y = (v) => m.t + ((hi - v) / (hi - lo || 1)) * (H - m.t - m.b);
  const { svg, add } = svgRoot(W, H);
  add("line", { x1: m.l, x2: W - m.r, y1: Y(0), y2: Y(0), stroke: INK.muted });
  const slot = (W - m.l - m.r) / values.length;
  values.forEach((v, i) => {
    const x = m.l + i * slot + slot * 0.2, w = slot * 0.6;
    const rect = add("rect", { x, width: w, y: Math.min(Y(v), Y(0)), height: Math.abs(Y(v) - Y(0)), fill: v > 0 ? INK.warm : INK.cool });
    rect.addEventListener("mousemove", (e) => showTip(e, `${labels[i]}: ${v > 0 ? "+" : ""}${fmt(v, 1)} ${unit}`));
    rect.addEventListener("mouseleave", hideTip);
    add("text", { x: x + w / 2, y: v >= 0 ? Y(v) - 5 : Y(v) + 13, "text-anchor": "middle", "font-size": 12, fill: "#e9e6df" }, `${v > 0 ? "+" : ""}${fmt(v, 0)}`);
    add("text", { x: x + w / 2, y: H - 24, "text-anchor": "middle", "font-size": 11, fill: INK.muted }, labels[i]);
    if (sub) add("text", { x: x + w / 2, y: H - 10, "text-anchor": "middle", "font-size": 10, fill: INK.muted }, sub[i]);
  });
  container.replaceChildren(svg);
}

function rankRows(items, { selected, onPick, format, max, good }) {
  const top = max || Math.max(...items.map(([, v]) => Math.abs(v)), 1e-9);
  return el("div", { class: "rank-list" }, items.map(([zone, v]) => el("div", { class: `rank-row ${zone === selected ? "on" : ""}`, onclick: () => onPick(zone) },
    el("span", { class: "rk-zone" }, zone.replace("_", " ")),
    el("span", { class: "rk-bar" }, el("i", { style: `width:${Math.max(2, (Math.abs(v) / top) * 100)}%;background:${good ? (v > 0 ? INK.accent : INK.warm) : (v > 0 ? INK.warm : INK.cool)}` })),
    el("span", { class: "rk-val" }, format(v)))));
}

function yearColor(i, n) {
  const t = n <= 1 ? 1 : i / (n - 1);
  const a = [74, 90, 105], b = [217, 164, 65];
  return `rgb(${a.map((c, k) => Math.round(c + (b[k] - c) * t)).join(",")})`;
}

// ---------- Patterns ----------
async function renderPatterns() {
  const root = $("#patterns");
  const p = await getJson("data/patterns.json");
  const ui = state.patterns || (state.patterns = { duck: "ES", resp: "wind", zone: "DE_LU" });
  if (!p.duck[ui.duck]) ui.duck = Object.keys(p.duck)[0];

  // Duck curve
  const years = Object.keys(p.duck[ui.duck]).sort();
  const duckBox = el("div", { class: "chart" });
  const first = years[0], last = years[years.length - 1];
  const duckSeries = years.map((y, i) => ({ name: y, values: p.duck[ui.duck][y], color: yearColor(i, years.length), width: y === last ? 2.4 : 1.2, end: y === first || y === last || y === "2022" ? y : "" }));
  const midday = p.duck[ui.duck][last][13], evening = p.duck[ui.duck][last][21];
  const duck = el("section", { class: "pattern" },
    el("h2", {}, "The duck curve: midday solar empties the price, the evening refills it"),
    el("div", { class: "controls" }, segmented(Object.fromEntries(Object.keys(p.duck).map((z) => [z, z.replace("_", " ")])), ui.duck, (v) => { ui.duck = v; renderPatterns(); })),
    duckBox,
    el("p", { class: "note" }, `Average day-ahead price by local hour of the day, June to August, one line per year. In ${zoneName(ui.duck)} the ${last} summer averages ${fmt(midday, 0)} EUR/MWh at 13:00 and ${fmt(evening, 0)} at 21:00. Only years with a full 24-hour profile are drawn.`));
  xyChart(duckBox, { xs: [...Array(24).keys()], series: duckSeries, height: 300, unit: " EUR", tipLabel: (h) => `${String(h).padStart(2, "0")}:00`, xTicks: [0, 3, 6, 9, 12, 15, 18, 21].map((h) => [h, `${String(h).padStart(2, "0")}:00`]) });

  // Weekend effect
  const gaps = Object.entries(p.weekday).map(([z, v]) => [z, v.weekend_gap_pct]).sort((a, b) => a[1] - b[1]);
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const wz = p.weekday[ui.zone] ? ui.zone : "DE_LU";
  const weekBox = el("div", { class: "chart" });
  const weekend = el("section", { class: "pattern" },
    el("h2", {}, `Weekends are cheaper in ${gaps.filter(([, v]) => v < 0).length} of ${gaps.length} zones, by a median of ${fmt(-gaps[Math.floor(gaps.length / 2)][1], 0)}%`),
    el("div", { class: "two" },
      el("div", {}, el("h3", { class: "side-title" }, "Weekend price against weekdays, by zone"),
        rankRows(gaps.slice(0, 12), { selected: wz, onPick: (z) => { ui.zone = z; renderPatterns(); }, format: (v) => `${fmt(v, 0)}%`, max: Math.abs(gaps[0][1]) }),
        el("p", { class: "hint" }, `Smallest gaps: ${gaps.slice(-3).map(([z, v]) => `${z.replace("_", " ")} ${fmt(v, 0)}%`).join(", ")}.`)),
      el("div", {}, el("h3", { class: "side-title" }, `${zoneName(wz)}: average price by weekday, difference to the weekly mean (EUR/MWh)`), weekBox)),
    el("p", { class: "note" }, `Last three years of daily means (${p.since} to ${p.until}). Weekend is Saturday and Sunday against Monday to Friday; zones with a mean price under 5 EUR/MWh are left out.`));
  columnChart(weekBox, { labels: names, values: p.weekday[wz].index, unit: "EUR/MWh" });

  // Wind and sun
  const r = p.response[ui.resp] || {};
  if (!Object.keys(r).length) { root.replaceChildren(duck, weekend); return; }
  const rz = r[ui.zone] ? ui.zone : "DE_LU";
  const corr = Object.entries(r).map(([z, v]) => [z, v.r]).sort((a, b) => a[1] - b[1]);
  const respBox = el("div", { class: "chart" });
  const unit = ui.resp === "wind" ? "m/s" : "W/m2";
  const quint = ["lowest fifth", "", "middle", "", "highest fifth"];
  const response = el("section", { class: "pattern" },
    el("h2", {}, "Wind and sun pull prices down"),
    el("div", { class: "controls" }, segmented({ wind: "Wind speed", sun: "Sunshine" }, ui.resp, (v) => { ui.resp = v; renderPatterns(); })),
    el("div", { class: "two" },
      el("div", {}, el("h3", { class: "side-title" }, `Correlation with price, ${ui.resp === "wind" ? "10 m wind speed" : "solar radiation in daylight hours"}`),
        rankRows(corr.slice(0, 12), { selected: rz, onPick: (z) => { ui.zone = z; renderPatterns(); }, format: (v) => v.toFixed(2), max: 0.7 })),
      el("div", {}, el("h3", { class: "side-title" }, `${zoneName(rz)}: price against ${ui.resp === "wind" ? "wind" : "sun"}, grouped in fifths of the hours (EUR/MWh vs the month's mean)`), respBox)),
    el("p", { class: "note" }, `Hourly price and weather over the last three years, both measured as the difference to their own month mean in the same zone, so that the season does not drive the result. The bars show the average price gap in each fifth of hours, sorted from the lowest to the highest ${ui.resp === "wind" ? "wind" : "radiation"} (${fmt(r[rz].dx[0], 1)} to ${fmt(r[rz].dx[4], 1)} ${unit} against the month mean). The weather is the average over a few points per zone, so it is a rough proxy for what the plants actually see. Correlation is not causation: wind and sun also move with demand.`));
  columnChart(respBox, { labels: quint.map((q, i) => q || ["", "low", "", "high", ""][i]), values: r[rz].dp, sub: r[rz].dx.map((d) => `${d > 0 ? "+" : ""}${fmt(d, ui.resp === "wind" ? 1 : 0)} ${unit}`), unit: "EUR/MWh", height: 240 });

  root.replaceChildren(duck, weekend, response);
}

// ---------- Forecast ----------
async function renderForecast() {
  const root = $("#forecast");
  const f = await getJson("data/forecast.json");
  const keys = Object.keys(f.zones).sort();
  const zone = f.zones[root.dataset.zone] ? root.dataset.zone : (f.zones.ES ? "ES" : keys[0]);
  const z = f.zones[zone];
  const H = f.horizons;
  const day = (d) => Date.parse(d);
  const hist = z.history, fc = z.forecast;
  const xs = hist.dates.map(day).concat(fc.map((r) => day(r.date)));
  const n = hist.dates.length;
  const nulls = (k) => Array(k).fill(null);
  const bridge = hist.values[n - 1];
  const actual = hist.values.concat(nulls(H));
  const predicted = nulls(n - 1).concat([bridge], fc.map((r) => r.mid));
  const lo = nulls(n - 1).concat([bridge], fc.map((r) => r.lo));
  const hi = nulls(n - 1).concat([bridge], fc.map((r) => r.hi));
  const chartBox = el("div", { class: "chart" });
  const select = el("select", { onchange: (e) => { root.dataset.zone = e.target.value; renderForecast(); } },
    keys.map((k) => el("option", { value: k, selected: k === zone }, `${zoneName(k)} (${k})`)));
  const avg = (a) => a.reduce((s, v) => s + v, 0) / a.length;
  const gain = (k) => (1 - avg(z.mae.model.slice(0, k)) / avg(z.mae.naive.slice(0, k))) * 100;
  const rows = Array.from({ length: H }, (_, h) => {
    const m = z.mae.model[h], b = z.mae.naive[h], c = z.mae.persist[h];
    return el("tr", {}, el("td", {}, `+${h + 1} day${h ? "s" : ""}`), el("td", { class: "num" }, fmt(m, 1)), el("td", { class: "num" }, fmt(c, 1)), el("td", { class: "num" }, fmt(b, 1)),
      el("td", { class: `num ${m < b ? "ok" : "bad"}` }, `${m < b ? "-" : "+"}${Math.abs((1 - m / b) * 100).toFixed(0)}%`));
  });
  const skill = keys.map((k) => { const q = f.zones[k]; const a = (v) => v.reduce((s, x) => s + x, 0) / v.length; return [k, (1 - a(q.mae.model) / a(q.mae.naive)) * 100]; }).sort((a, b) => b[1] - a[1]);
  root.replaceChildren(
    el("section", { class: "pattern" },
      el("h2", {}, "A seven-day price outlook, and how often it was right"),
      el("div", { class: "controls" }, select),
      el("div", { class: "card" }, el("h3", {}, `${zoneName(zone)}: daily mean price, last ${n} days and next ${H} (EUR/MWh)`), chartBox,
        legend([[INK.accent, "actual"], [INK.cool, `forecast with ${f.interval[0]}-${f.interval[1]}% interval`]])),
      el("p", { class: "note" }, `Forecast made from data up to ${z.origin}. Over the last 90 days of backtest the interval held the real price ${(z.coverage * 100).toFixed(0)}% of the time (the target is ${f.interval[1] - f.interval[0]}%), and the model's average error over the week is ${gain(H).toFixed(0)}% ${gain(H) >= 0 ? "lower" : "higher"} than repeating last week's price.`),
      el("h3", { class: "side-title" }, "Backtest, last 90 days: mean absolute error in EUR/MWh"),
      el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ["Horizon", "Model", "Last 7-day mean", "Same day last week", "Model vs same day"].map((t, i) => el("th", { class: i ? "num" : "" }, t)))), el("tbody", {}, rows))),
      el("h3", { class: "side-title", style: "margin-top:22px" }, "Improvement over repeating last week's price, by zone (average over the 7 horizons)"),
      el("div", { class: "two" }, rankRows(skill.slice(0, 10), { selected: zone, onPick: (k) => { root.dataset.zone = k; renderForecast(); }, format: (v) => `${v.toFixed(0)}%`, max: 100, good: true }),
        rankRows(skill.slice(-10), { selected: zone, onPick: (k) => { root.dataset.zone = k; renderForecast(); }, format: (v) => `${v.toFixed(0)}%`, max: 100, good: true })),
      el("p", { class: "note" }, "How it works: the level is the average of the last 7 and last 28 days, plus a weekday effect measured over the last 26 full weeks. Nothing is trained: there are no fitted parameters, and every forecast uses only data available at its origin. The interval comes from the model's own past errors at each horizon. It does not know about weather, fuel prices or outages, so it is a demonstration of what the dataset allows, not a trading signal. Where the bar is red the simple baseline wins; that is the case for five of the six Italian zones, whose prices are almost the same across the country and barely follow a weekly pattern.")));
  const xTicks = xs.filter((_, i) => i % 7 === (n - 1) % 7).map((x) => [x, new Date(x).toLocaleDateString("en-GB", { day: "numeric", month: "short" })]);
  xyChart(chartBox, {
    xs, height: 280, unit: " EUR", xTicks, zeroLine: false,
    tipLabel: (x) => new Date(x).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }),
    series: [{ name: "actual", values: actual, color: INK.accent, width: 1.8 }, { name: "forecast", values: predicted, color: INK.cool, dash: "5 4", width: 1.8 }],
    band: { lo: lo.slice(n - 1), hi: hi.slice(n - 1), color: INK.cool, from: n - 1 },
  });
}
