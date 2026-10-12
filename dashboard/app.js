"use strict";

const state = { meta: null, zoneNames: {}, cache: {}, map: null, patterns: null };
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) node.append(c instanceof Node ? c : document.createTextNode(c));
  return node;
}

async function getJson(path) {
  if (!state.cache[path]) {
    const response = await fetch(path);
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    state.cache[path] = await response.json();
  }
  return state.cache[path];
}

const zoneName = (key) => state.zoneNames[key] || key;
const fmt = (v, d = 1) => (v == null ? "n/a" : Number(v).toLocaleString("en", { maximumFractionDigits: d }));
const DATASETS = { day_ahead_prices: "Prices", actual_load: "Load" };

function segmented(options, current, onChange) {
  const box = el("div", { class: "seg" });
  for (const [value, label] of Object.entries(options)) {
    box.append(el("button", { class: value === current ? "on" : "", onclick: () => onChange(value) }, label));
  }
  return box;
}

function showTip(event, text) {
  const tip = $("#tip");
  tip.textContent = text;
  tip.hidden = false;
  tip.style.left = `${Math.min(event.clientX + 14, window.innerWidth - 300)}px`;
  tip.style.top = `${event.clientY + 14}px`;
}
const hideTip = () => { $("#tip").hidden = true; };

// ---------- Coverage ----------
async function renderCoverage() {
  const root = $("#coverage");
  const rows = await getJson("data/coverage.json");
  let dataset = root.dataset.dataset || "day_ahead_prices";
  let sortKey = root.dataset.sort || "zone_key";
  let desc = root.dataset.desc === "1";
  const draw = () => {
    root.replaceChildren(
      el("div", { class: "controls" },
        segmented(DATASETS, dataset, (v) => { root.dataset.dataset = v; renderCoverage(); })),
    );
    const data = rows.filter((r) => r.dataset === dataset)
      .sort((a, b) => (a[sortKey] > b[sortKey] ? 1 : a[sortKey] < b[sortKey] ? -1 : 0) * (desc ? -1 : 1));
    const heads = [
      ["zone_key", "Zone"], ["first_date", "First day"], ["last_date", "Last day"],
      ["completeness_share", "Complete days"], ["warning_checks_failed", "Warnings"],
      ["blocking_checks_failed", "Blocking"], ["freshness_ok", "Freshness"],
    ];
    const head = el("tr", {}, heads.map(([key, label]) => el("th", {
      class: key.includes("checks") ? "num" : "",
      onclick: () => { root.dataset.desc = sortKey === key && !desc ? "1" : "0"; root.dataset.sort = key; renderCoverage(); },
    }, label + (sortKey === key ? (desc ? " v" : " ^") : ""))));
    const body = data.map((r) => el("tr", {},
      el("td", {}, `${zoneName(r.zone_key)} (${r.zone_key})`),
      el("td", {}, r.first_date), el("td", {}, r.last_date),
      el("td", {},
        el("span", { class: "track" }, el("span", { class: "bar", style: `width:${(r.completeness_share * 90).toFixed(0)}px` })),
        `${(r.completeness_share * 100).toFixed(1)}%`),
      el("td", { class: "num" }, r.warning_checks_failed),
      el("td", { class: `num ${r.blocking_checks_failed ? "bad" : ""}` }, r.blocking_checks_failed),
      el("td", { class: r.freshness_ok ? "ok" : "bad", title: r.freshness_detail || "" },
        r.freshness_ok == null ? "n/a" : r.freshness_ok ? "fresh" : "stale")));
    root.append(el("table", {}, el("thead", {}, head), el("tbody", {}, body)),
      el("p", { class: "note" }, "Complete days: share of local days whose valid slots cover the whole DST-aware day. Prices count the SDAC series only."));
  };
  draw();
}

// ---------- Completeness heat map ----------
async function renderCompleteness() {
  const root = $("#completeness");
  const { months, series } = await getJson("data/completeness_monthly.json");
  const dataset = root.dataset.dataset || "day_ahead_prices";
  const zones = Object.keys(series[dataset] || {}).sort();
  const cell = { w: 5, h: 12, left: 150, top: 22 };
  const canvas = el("canvas");
  const dpr = window.devicePixelRatio || 1;
  const width = cell.left + months.length * cell.w + 10;
  const height = cell.top + zones.length * cell.h + 8;
  canvas.width = width * dpr; canvas.height = height * dpr;
  canvas.style.width = `${width}px`; canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  ctx.font = "11px system-ui"; ctx.fillStyle = css.getPropertyValue("--muted");
  zones.forEach((z, i) => ctx.fillText(z, 4, cell.top + i * cell.h + 9));
  months.forEach((m, j) => { if (m.endsWith("-01") && Number(m.slice(0, 4)) % 2 === 1) ctx.fillText(m.slice(0, 4), cell.left + j * cell.w, 12); });
  const color = (v) => (v == null ? css.getPropertyValue("--line") : `hsl(${Math.round(120 * Math.max(0, Math.min(1, (v - 0.5) * 2)))}, 55%, 45%)`);
  zones.forEach((z, i) => series[dataset][z].forEach((v, j) => {
    ctx.fillStyle = color(v);
    ctx.fillRect(cell.left + j * cell.w, cell.top + i * cell.h, cell.w - 1, cell.h - 1);
  }));
  canvas.addEventListener("mousemove", (e) => {
    const box = canvas.getBoundingClientRect();
    const j = Math.floor((e.clientX - box.left - cell.left) / cell.w);
    const i = Math.floor((e.clientY - box.top - cell.top) / cell.h);
    if (i < 0 || j < 0 || i >= zones.length || j >= months.length) return hideTip();
    const v = series[dataset][zones[i]][j];
    showTip(e, `${zoneName(zones[i])} ${months[j]}: ${v == null ? "no data" : (v * 100).toFixed(1) + "% covered"}`);
  });
  canvas.addEventListener("mouseleave", hideTip);
  root.replaceChildren(
    el("div", { class: "controls" }, segmented(DATASETS, dataset, (v) => { root.dataset.dataset = v; renderCompleteness(); })),
    el("div", { class: "card", style: "overflow-x:auto" }, canvas),
    el("p", { class: "note" }, "Each cell is one zone and month. Green is full coverage of the local days, red is mostly missing, grey is no data. Zones whose series start later or stop earlier show grey at the edges."));
}

// ---------- Line chart ----------
function lineChart(container, { dates, series, band, height = 220, unit = "" }) {
  const W = 1000, H = height, m = { l: 52, r: 12, t: 10, b: 24 };
  const t = dates.map((d) => Date.parse(d));
  const all = series.flatMap((s) => s.values).concat(band ? band.min.concat(band.max) : []).filter((v) => v != null);
  if (!t.length || !all.length) { container.replaceChildren(el("p", { class: "note" }, "No data in this range.")); return; }
  const x0 = Math.min(...t), x1 = Math.max(...t);
  let y0 = Math.min(...all), y1 = Math.max(...all);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.05; y0 -= pad; y1 += pad;
  const X = (v) => m.l + ((v - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
  const Y = (v) => H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const add = (name, attrs, text) => { const n = document.createElementNS(ns, name); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); if (text) n.textContent = text; svg.append(n); return n; };
  const css = getComputedStyle(document.documentElement);
  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4;
    add("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: css.getPropertyValue("--line") });
    add("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: css.getPropertyValue("--muted") }, fmt(v, 0));
  }
  const years = new Set(dates.map((d) => d.slice(0, 4)));
  const step = Math.max(1, Math.ceil(years.size / 8));
  [...years].forEach((y, i) => { if (i % step === 0) { const px = X(Date.parse(`${y}-01-01`)); if (px >= m.l) add("text", { x: px, y: H - 6, "font-size": 11, fill: css.getPropertyValue("--muted") }, y); } });
  const segments = (values) => {
    const parts = []; let cur = [];
    values.forEach((v, i) => { if (v == null) { if (cur.length) parts.push(cur); cur = []; } else cur.push([X(t[i]), Y(v)]); });
    if (cur.length) parts.push(cur);
    return parts;
  };
  if (band) {
    const upper = segments(band.max), lower = segments(band.min);
    upper.forEach((u, k) => { const l = lower[k]; if (!l) return; add("path", { d: "M" + u.map((p) => p.join(",")).join("L") + "L" + [...l].reverse().map((p) => p.join(",")).join("L") + "Z", fill: series[0].color, opacity: 0.15 }); });
  }
  for (const s of series) {
    for (const part of segments(s.values)) add("path", { d: "M" + part.map((p) => p.join(",")).join("L"), fill: "none", stroke: s.color, "stroke-width": 1.4 });
  }
  const cross = add("line", { y1: m.t, y2: H - m.b, stroke: css.getPropertyValue("--muted"), opacity: 0 });
  svg.addEventListener("mousemove", (e) => {
    const box = svg.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    const target = x0 + ((px - m.l) / (W - m.l - m.r)) * (x1 - x0);
    let best = 0;
    for (let i = 1; i < t.length; i++) if (Math.abs(t[i] - target) < Math.abs(t[best] - target)) best = i;
    cross.setAttribute("x1", X(t[best])); cross.setAttribute("x2", X(t[best])); cross.setAttribute("opacity", 1);
    const parts = series.map((s) => `${s.name}: ${fmt(s.values[best], 1)}${unit}`);
    if (band) parts.push(`range: ${fmt(band.min[best], 1)} to ${fmt(band.max[best], 1)}`);
    showTip(e, `${dates[best]}  ${parts.join("  |  ")}`);
  });
  svg.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  container.replaceChildren(svg);
}

function legend(items) {
  return el("div", { class: "legend" }, items.map(([c, label]) => el("span", {}, el("i", { style: `background:${c}` }), label)));
}

// ---------- Explorer ----------
async function renderExplorer() {
  const root = $("#explorer");
  const zone = root.dataset.zone || "ES";
  const range = root.dataset.range || "all";
  const data = await getJson(`data/zones/${zone}.json`);
  const last = Date.parse(data.dates[data.dates.length - 1]);
  const days = { all: Infinity, "5y": 1826, "2y": 730, "1y": 365, "90d": 90 }[range];
  const keep = data.dates.map((d, i) => (last - Date.parse(d)) / 86400000 <= days ? i : -1).filter((i) => i >= 0);
  const pick = (arr) => keep.map((i) => arr[i]);
  const dates = pick(data.dates);
  const select = el("select", { onchange: (e) => { root.dataset.zone = e.target.value; renderExplorer(); } },
    state.meta.zones.map((z) => el("option", { value: z.key, selected: z.key === zone }, `${z.name} (${z.key})`)));
  const priceBox = el("div", { class: "chart" }), loadBox = el("div", { class: "chart" });
  root.replaceChildren(
    el("div", { class: "controls" }, select,
      segmented({ all: "All", "5y": "5 years", "2y": "2 years", "1y": "1 year", "90d": "90 days" }, range, (v) => { root.dataset.range = v; renderExplorer(); })),
    el("div", { class: "card" }, el("h3", {}, "Day-ahead price, daily mean with min-max range (EUR/MWh)"), priceBox,
      legend([["#d9a441", "daily mean"], ["rgba(217,164,65,0.28)", "min to max of the hourly prices"]])),
    el("div", { class: "card" }, el("h3", {}, "Actual load, daily mean (MW)"), loadBox),
    el("p", { class: "note" }, `${dates.length} days shown. Days without data are left out and the line is cut at gaps. Hover for values.`));
  lineChart(priceBox, { dates, series: [{ name: "mean", values: pick(data.price_mean), color: "#d9a441" }], band: { min: pick(data.price_min), max: pick(data.price_max) }, unit: " EUR" });
  lineChart(loadBox, { dates, series: [{ name: "load", values: pick(data.load_mean), color: "#6fb7d1" }], unit: " MW" });
}

// ---------- Negative prices ----------
async function renderNegative() {
  const root = $("#negative");
  const { years, zones } = await getJson("data/negative_hours.json");
  const year = Number(root.dataset.year || years[years.length - 2] || years[years.length - 1]);
  const idx = years.indexOf(year);
  const ranking = Object.entries(zones).map(([z, h]) => [z, h[idx]]).filter(([, h]) => h > 0).sort((a, b) => b[1] - a[1]);
  const max = ranking.length ? ranking[0][1] : 1;
  const sel = root.dataset.zone || (ranking[0] ? ranking[0][0] : "ES");
  const trend = el("div", { class: "chart" });
  const select = el("select", { onchange: (e) => { root.dataset.year = e.target.value; renderNegative(); } },
    years.map((y) => el("option", { value: y, selected: y === year }, y)));
  root.replaceChildren(
    el("div", { class: "controls" }, el("label", {}, "Year "), select),
    el("div", { class: "card" }, el("h3", {}, `Hours with a negative price in ${year}`),
      ranking.length ? ranking.map(([z, h]) => el("div", { class: "rank", onclick: () => { root.dataset.zone = z; renderNegative(); } },
        el("span", {}, z), el("div", {}, el("div", { class: "fill", style: `width:${(h / max * 100).toFixed(1)}%` })), el("span", { class: "num" }, h))) : el("p", { class: "note" }, "No negative prices in this year.")),
    el("div", { class: "card" }, el("h3", {}, `${zoneName(sel)} (${sel}): negative-price hours per year`), trend),
    el("p", { class: "note" }, "Counts hours of the harmonised hourly grid whose EUR price is below zero. Click a zone to see its yearly trend. Incomplete days lower the count."));
  const values = zones[sel] || [];
  const w = 1000, h = 160, bw = (w - 60) / years.length;
  const top = Math.max(1, ...values);
  const bars = values.map((v, i) => `<rect x="${50 + i * bw + 3}" y="${h - 22 - (v / top) * (h - 40)}" width="${bw - 6}" height="${(v / top) * (h - 40)}" fill="#6fb7d1"><title>${years[i]}: ${v} h</title></rect><text x="${50 + i * bw + bw / 2}" y="${h - 6}" font-size="11" text-anchor="middle" fill="currentColor">${years[i]}</text>`).join("");
  trend.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${bars}<text x="46" y="14" font-size="11" text-anchor="end" fill="currentColor">${top}</text></svg>`;
}

// ---------- Spreads ----------
async function renderSpreads() {
  const root = $("#spreads");
  const data = await getJson("data/spreads.json");
  const pairs = data.pairs;
  const key = (p) => `${p.a}|${p.b}`;
  const current = pairs.find((p) => key(p) === root.dataset.pair)
    || pairs.find((p) => key(p) === "DE_LU|FR") || pairs[0];
  const recent = data.months.length - 12;
  const average = (arr) => { const v = arr.slice(recent).filter((x) => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
  const table = pairs.map((p) => ({ p, v: average(p.abs_mean) })).filter((r) => r.v != null).sort((a, b) => b.v - a.v).slice(0, 12);
  const select = el("select", { onchange: (e) => { root.dataset.pair = e.target.value; renderSpreads(); } },
    pairs.map((p) => el("option", { value: key(p), selected: key(p) === key(current) }, `${p.a} - ${p.b}`)));
  const box = el("div", { class: "chart" });
  root.replaceChildren(
    el("div", { class: "controls" }, select),
    el("div", { class: "card" }, el("h3", {}, `${current.a} minus ${current.b}: monthly mean spread and mean absolute spread (EUR/MWh)`), box,
      legend([["#d9a441", "mean spread"], ["#6fb7d1", "mean absolute spread"]])),
    el("div", { class: "card" }, el("h3", {}, "Largest mean absolute spreads, last 12 months"),
      el("table", {}, el("tbody", {}, table.map((r) => el("tr", { style: "cursor:pointer", onclick: () => { root.dataset.pair = key(r.p); renderSpreads(); } },
        el("td", {}, `${r.p.a} - ${r.p.b}`), el("td", { class: "num" }, fmt(r.v)))))) ),
    el("p", { class: "note" }, "Spread is the price of the first zone minus the second, on hours where both zones have all their slots. Neighbouring pairs are listed in configs/neighbours.json."));
  lineChart(box, { dates: data.months.map((m) => `${m}-01`), series: [{ name: "spread", values: current.mean, color: "#d9a441" }, { name: "abs", values: current.abs_mean, color: "#6fb7d1" }], height: 240, unit: " EUR" });
}

// ---------- Boot ----------
const renderers = { overview: renderOverview, patterns: renderPatterns, forecast: renderForecast, coverage: renderCoverage, completeness: renderCompleteness, explorer: renderExplorer, negative: renderNegative, spreads: renderSpreads };

function activate(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main section").forEach((s) => { s.hidden = s.id !== name; });
  hideTip();
  renderers[name]().catch((e) => { $(`#${name}`).replaceChildren(el("p", { class: "bad" }, `Could not load data: ${e.message}`)); });
  history.replaceState(null, "", `#${name}`);
}

async function boot() {
  state.meta = await getJson("data/meta.json");
  state.zoneNames = Object.fromEntries(state.meta.zones.map((z) => [z.key, z.name]));
  $("#meta").textContent = `Data built ${state.meta.generated_at.slice(0, 16).replace("T", " ")} UTC`;
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => activate(b.dataset.tab)));
  activate(renderers[location.hash.slice(1)] ? location.hash.slice(1) : "overview");
}

boot().catch((e) => { document.body.prepend(el("p", { class: "bad", style: "padding:16px" }, `Could not start: ${e.message}`)); });
