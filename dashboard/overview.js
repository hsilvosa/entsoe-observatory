"use strict";

/* Overview: headline KPIs and the zone map. Loaded before app.js and uses its helpers
   (el, getJson, state, fmt, showTip, hideTip, segmented) at call time. */

const PRICE_STOPS = [[-20, "#5fb4cf"], [0, "#2c4452"], [60, "#5f7a68"], [120, "#c2a24a"], [180, "#d9772e"], [240, "#c2412d"]];
const LOAD_STOPS = [[0, "#1c2832"], [1500, "#2d4a5c"], [8000, "#4f7f98"], [25000, "#8fb8c9"], [60000, "#e4eef2"]];
const NEG_STOPS = [[0, "#1c2832"], [1, "#2c5266"], [6, "#4f93ad"], [12, "#7ec4dc"], [24, "#e4f3f8"]];
const COV_STOPS = [[0, "#b5483a"], [0.6, "#c98a3a"], [0.95, "#6f9a76"], [1, "#8fb996"]];
const METRICS = {
  price: { label: "Price", unit: "EUR/MWh", key: "price", stops: PRICE_STOPS, fmt: (v) => fmt(v, 1), legend: [-20, 30, 70, 110, 160, 230] },
  load: { label: "Load", unit: "MW (daily mean)", key: "load", stops: LOAD_STOPS, fmt: (v) => fmt(v, 0), legend: [0, 1500, 8000, 25000, 60000] },
  neg: { label: "Neg. hours", unit: "hours in the day", key: "neg", stops: NEG_STOPS, fmt: (v) => fmt(v, 0), legend: [0, 1, 6, 12, 24] },
  cov: { label: "Completeness", unit: "share of the day covered by prices", key: "pcov", stops: COV_STOPS, fmt: (v) => `${(v * 100).toFixed(0)}%`, legend: [0, 0.6, 0.95, 1] },
};

function hexToRgb(h) { return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)); }
function colorFor(stops, v) {
  if (v == null) return "#141c25";
  if (v <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i++) {
    if (v <= stops[i][0]) {
      const [a, ca] = stops[i - 1], [b, cb] = stops[i];
      const t = (v - a) / (b - a), x = hexToRgb(ca), y = hexToRgb(cb);
      return `rgb(${x.map((c, k) => Math.round(c + (y[k] - c) * t)).join(",")})`;
    }
  }
  return stops[stops.length - 1][1];
}

function sparkline(values, color, w = 160, h = 40) {
  const v = values.filter((x) => x != null);
  if (v.length < 2) return el("span");
  const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  const pts = values.map((x, i) => (x == null ? null : `${((i / (values.length - 1)) * w).toFixed(1)},${(h - 3 - ((x - lo) / span) * (h - 6)).toFixed(1)}`)).filter(Boolean);
  const wrap = document.createElement("div");
  wrap.innerHTML = `<svg viewBox="0 0 ${w} ${h}" class="spark" preserveAspectRatio="none"><polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/><circle cx="${pts[pts.length - 1].split(",")[0]}" cy="${pts[pts.length - 1].split(",")[1]}" r="2.6" fill="${color}"/></svg>`;
  return wrap.firstChild;
}

function kpi(label, value, unit, detail, extra, cls = "") {
  return el("div", { class: `kpi ${cls}` },
    el("div", { class: "kpi-label" }, label),
    el("div", { class: "kpi-value" }, value, unit ? el("span", { class: "kpi-unit" }, unit) : ""),
    detail ? el("div", { class: "kpi-detail" }, detail) : "",
    extra || "");
}

function delta(now, before) {
  if (before == null) return null;
  const d = ((now - before) / Math.abs(before)) * 100;
  return el("span", { class: d > 0 ? "up" : "down" }, `${d > 0 ? "+" : ""}${d.toFixed(0)}% vs last 7 days`);
}

async function renderOverview() {
  const root = $("#overview");
  const [o, mapData, shapes] = await Promise.all([
    getJson("data/overview.json"), getJson("data/map.json"), getJson("map-zones.json"),
  ]);
  if (!o.day) { root.replaceChildren(el("p", { class: "note" }, "No overview data yet.")); return; }
  const dates = mapData.dates;
  const headline = Math.max(0, dates.indexOf(o.day));
  const ui = state.map || (state.map = { metric: "price", index: headline, playing: false, speed: 1, timer: null, selected: null });
  if (ui.timer) { clearInterval(ui.timer); ui.timer = null; ui.playing = false; }
  ui.index = Math.min(ui.index, dates.length - 1);

  const nameOf = (z) => zoneName(z);
  const zn = (z) => z.replace("_", " ");
  const num = (v, d = 1) => el("span", { class: "n" }, fmt(v, d));
  const change = o.price_week_mean == null ? null : ((o.price_mean - o.price_week_mean) / Math.abs(o.price_week_mean)) * 100;
  const lede = el("div", { class: "lede" },
    el("p", { class: "lede-text" },
      `On ${new Date(o.day + "T12:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}, day-ahead electricity in ${o.zones_priced} European zones averages `, num(o.price_mean), " EUR/MWh",
      change == null ? "." : `, ${Math.abs(change).toFixed(0)}% ${change >= 0 ? "above" : "below"} the previous seven days.`,
      ` ${zn(o.cheapest.zone)} is the cheapest at `, num(o.cheapest.price), ` and ${zn(o.dearest.zone)} the dearest at `, num(o.dearest.price), "."),
    el("div", { class: "lede-spark" }, sparkline(o.price_series.map((p) => p[1]), "#d9a441", 260, 46),
      el("span", { class: "spark-cap" }, "average price, last 90 days")));
  const facts = el("dl", { class: "facts" },
    el("div", {}, el("dt", {}, "Negative-price zones"),
      el("dd", {}, o.negative_zones.length ? `${o.negative_zones.length} (${o.negative_zones.slice(0, 5).map(zn).join(", ")}${o.negative_zones.length > 5 ? "..." : ""})` : "none")),
    el("div", {}, el("dt", {}, "Widest hourly spread"),
      el("dd", {}, o.spread ? [num(o.spread.value, 0), ` EUR/MWh, ${zn(o.spread.a)} to ${zn(o.spread.b)}`] : "n/a")),
    el("div", {}, el("dt", {}, `Load on ${o.load_day || "n/a"}`),
      el("dd", {}, o.load_total_mw ? [num(o.load_total_mw / 1000, 0), ` GW across ${o.zones_loaded} zones`] : "n/a")),
    o.negative_record ? el("div", {}, el("dt", {}, "Most negative-price hours in a day"),
      el("dd", {}, [num(o.negative_record.hours, 0), ` h, ${zn(o.negative_record.zone)}, ${o.negative_record.day}`])) : "");
  const strip = el("div", { class: "intro" }, lede, facts);

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 1000 1000");
  svg.setAttribute("class", "map");
  const paths = {};
  svg.setAttribute("viewBox", shapes._viewBox.join(" "));
  for (const [zone, shape] of Object.entries(shapes)) {
    if (zone.startsWith("_")) continue;
    const p = document.createElementNS(svgNS, "path");
    p.setAttribute("d", shape.d); p.setAttribute("class", "zone");
    p.addEventListener("mousemove", (e) => showTip(e, tipText(zone)));
    p.addEventListener("mouseleave", hideTip);
    p.addEventListener("click", () => { ui.selected = ui.selected === zone ? null : zone; paint(); });
    svg.append(p); paths[zone] = p;
  }

  const valueOf = (zone, i = ui.index) => { const s = mapData.zones[zone]; return s ? s[METRICS[ui.metric].key][i] : null; };
  function tipText(zone) {
    const m = METRICS[ui.metric], v = valueOf(zone);
    return `${nameOf(zone)} (${zone}) | ${dates[ui.index]} | ${v == null ? "no data" : m.fmt(v) + " " + m.unit}`;
  }

  const legend = el("div", { class: "legend-bar" });
  const rank = el("div", { class: "rank-list" });
  const dateLabel = el("span", { class: "date-label" });
  const slider = el("input", { type: "range", min: 0, max: dates.length - 1, value: ui.index, "aria-label": "Date" });
  const play = el("button", { class: "icon-btn", "aria-label": "Play", onclick: () => toggle() }, "Play");
  const speedBtn = el("button", { class: "icon-btn", "aria-label": "Playback speed", onclick: () => { ui.speed = ui.speed === 1 ? 2 : 1; speedBtn.textContent = `x${ui.speed}`; if (ui.playing) { toggle(); toggle(); } } }, `x${ui.speed}`);
  const detail = el("div", { class: "zone-detail" });

  function toggle() {
    ui.playing = !ui.playing;
    play.textContent = ui.playing ? "Pause" : "Play";
    if (ui.timer) { clearInterval(ui.timer); ui.timer = null; }
    if (ui.playing) {
      if (ui.index >= dates.length - 1) ui.index = 0;
      ui.timer = setInterval(() => { ui.index = Math.min(dates.length - 1, ui.index + 1); slider.value = ui.index; paint(); if (ui.index >= dates.length - 1) toggle(); }, 90 / ui.speed);
    }
  }
  slider.addEventListener("input", () => { ui.index = Number(slider.value); paint(); });

  function paint() {
    const m = METRICS[ui.metric];
    for (const [zone, p] of Object.entries(paths)) {
      p.style.fill = colorFor(m.stops, valueOf(zone));
      p.classList.toggle("selected", zone === ui.selected);
    }
    dateLabel.textContent = dates[ui.index];
    const gradient = m.stops.map(([v, c]) => c).join(",");
    legend.replaceChildren(
      el("div", { class: "grad", style: `background:linear-gradient(90deg,${gradient})` }),
      el("div", { class: "ticks" }, m.legend.map((v) => el("span", {}, m.fmt(v)))),
      el("div", { class: "unit" }, m.unit));
    const items = Object.keys(paths).map((z) => [z, valueOf(z)]).filter(([, v]) => v != null);
    items.sort((a, b) => (ui.metric === "cov" ? a[1] - b[1] : b[1] - a[1]));
    const max = Math.max(...items.map(([, v]) => v), 1e-9), min = Math.min(...items.map(([, v]) => v), 0);
    rank.replaceChildren(...items.slice(0, 12).map(([z, v]) => el("div", { class: `rank-row ${z === ui.selected ? "on" : ""}`, onclick: () => { ui.selected = ui.selected === z ? null : z; paint(); } },
      el("span", { class: "rk-zone" }, z.replace("_", " ")),
      el("span", { class: "rk-bar" }, el("i", { style: `width:${Math.max(2, ((v - Math.min(0, min)) / (max - Math.min(0, min) || 1)) * 100)}%;background:${colorFor(m.stops, v)}` })),
      el("span", { class: "rk-val" }, m.fmt(v)))));
    paintDetail();
  }

  function paintDetail() {
    const z = ui.selected;
    if (!z) { detail.replaceChildren(el("p", { class: "hint" }, "Click a zone on the map or in the ranking to see its last year.")); return; }
    const s = mapData.zones[z];
    const series = s ? s[METRICS[ui.metric].key] : [];
    detail.replaceChildren(
      el("div", { class: "zd-head" }, el("strong", {}, nameOf(z)), el("span", { class: "zd-code" }, z),
        el("a", { href: "#explorer", class: "zd-link", onclick: () => { $("#explorer").dataset.zone = z; } }, "Open in explorer")),
      sparkline(series, "#d9a441", 360, 60),
      el("div", { class: "zd-foot" }, el("span", {}, dates[0]), el("span", {}, dates[dates.length - 1])));
  }

  const metricBox = segmented(Object.fromEntries(Object.entries(METRICS).map(([k, m]) => [k, m.label])), ui.metric, (v) => { ui.metric = v; metricBox.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.textContent === METRICS[v].label)); paint(); });

  root.replaceChildren(
    strip,
    el("div", { class: "stage" },
      el("div", { class: "map-card" }, svg,
        el("div", { class: "map-controls" }, play, speedBtn, slider, dateLabel)),
      el("aside", { class: "side" },
        el("div", { class: "side-head" }, metricBox),
        legend,
        el("h3", { class: "side-title" }, "Highest values on the selected day"),
        rank, detail)),
    el("p", { class: "note" }, "Zones inside Norway, Sweden, Denmark and Italy follow administrative borders, so their outlines are approximations of the real bidding zones. Map outlines: Natural Earth (public domain)."));
  paint();
}
