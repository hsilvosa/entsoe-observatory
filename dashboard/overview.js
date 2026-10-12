"use strict";

/* Overview: headline KPIs and the zone map. Loaded before app.js and uses its helpers
   (el, getJson, state, fmt, showTip, hideTip, segmented) at call time. */

const PRICE_STOPS = [[-20, "#35d0ff"], [30, "#2a9fb0"], [70, "#4fae86"], [110, "#c9b038"], [160, "#ff9a1f"], [230, "#ff4d4d"]];
const LOAD_STOPS = [[0, "#0f2433"], [1500, "#14506b"], [8000, "#1f8fb5"], [25000, "#6fd3f2"], [60000, "#e6f9ff"]];
const NEG_STOPS = [[0, "#16212d"], [1, "#1b4c63"], [6, "#2a9fb0"], [12, "#35d0ff"], [24, "#e6f9ff"]];
const COV_STOPS = [[0, "#ff4d4d"], [0.6, "#ff9a1f"], [0.95, "#4fae86"], [1, "#3ddc97"]];
const METRICS = {
  price: { label: "Price", unit: "EUR/MWh", key: "price", stops: PRICE_STOPS, fmt: (v) => fmt(v, 1), legend: [-20, 30, 70, 110, 160, 230] },
  load: { label: "Load", unit: "MW (daily mean)", key: "load", stops: LOAD_STOPS, fmt: (v) => fmt(v, 0), legend: [0, 1500, 8000, 25000, 60000] },
  neg: { label: "Negative hours", unit: "hours in the day", key: "neg", stops: NEG_STOPS, fmt: (v) => fmt(v, 0), legend: [0, 1, 6, 12, 24] },
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
  const ui = state.map || (state.map = { metric: "price", index: headline, playing: false, timer: null, selected: null });
  if (ui.timer) { clearInterval(ui.timer); ui.timer = null; ui.playing = false; }
  ui.index = Math.min(ui.index, dates.length - 1);

  const nameOf = (z) => zoneName(z);
  const strip = el("div", { class: "kpis" },
    kpi(`Average day-ahead price for ${o.day}`, fmt(o.price_mean, 1), "EUR/MWh",
      delta(o.price_mean, o.price_week_mean), sparkline(o.price_series.map((p) => p[1]), "#ffb020", 240, 52), "lead"),
    kpi("Cheapest zone", o.cheapest.zone.replace("_", " "), "", `${fmt(o.cheapest.price, 1)} EUR/MWh`, null, "cool"),
    kpi("Most expensive zone", o.dearest.zone.replace("_", " "), "", `${fmt(o.dearest.price, 1)} EUR/MWh`, null, "hot"),
    kpi("Negative-price zones", String(o.negative_zones.length), "", o.negative_zones.length ? o.negative_zones.slice(0, 6).join(", ") + (o.negative_zones.length > 6 ? "..." : "") : "none today"),
    kpi("Widest spread", o.spread ? fmt(o.spread.value, 0) : "n/a", "EUR/MWh", o.spread ? `${o.spread.a.replace("_", " ")} to ${o.spread.b.replace("_", " ")}` : ""),
    kpi(`Total load on ${o.load_day || "n/a"}`, o.load_total_mw ? fmt(o.load_total_mw / 1000, 0) : "n/a", "GW", `${o.zones_loaded} zones, daily mean`),
  );

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
  const detail = el("div", { class: "zone-detail" });

  function toggle() {
    ui.playing = !ui.playing;
    play.textContent = ui.playing ? "Pause" : "Play";
    if (ui.timer) { clearInterval(ui.timer); ui.timer = null; }
    if (ui.playing) {
      if (ui.index >= dates.length - 1) ui.index = 0;
      ui.timer = setInterval(() => { ui.index = Math.min(dates.length - 1, ui.index + 1); slider.value = ui.index; paint(); if (ui.index >= dates.length - 1) toggle(); }, 90);
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
      sparkline(series, "#35d0ff", 360, 70),
      el("div", { class: "zd-foot" }, el("span", {}, dates[0]), el("span", {}, dates[dates.length - 1])));
  }

  const metricBox = segmented(Object.fromEntries(Object.entries(METRICS).map(([k, m]) => [k, m.label])), ui.metric, (v) => { ui.metric = v; metricBox.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.textContent === METRICS[v].label)); paint(); });

  root.replaceChildren(
    strip,
    el("div", { class: "stage" },
      el("div", { class: "map-card" }, svg,
        el("div", { class: "map-controls" }, play, slider, dateLabel)),
      el("aside", { class: "side" },
        el("div", { class: "side-head" }, metricBox),
        legend,
        el("h3", { class: "side-title" }, "Highest values on the selected day"),
        rank, detail)),
    el("p", { class: "note" }, "Zones inside Norway, Sweden, Denmark and Italy follow administrative borders, so their outlines are approximations of the real bidding zones. Map outlines: Natural Earth (public domain)."));
  paint();
}
