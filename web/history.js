// History page: SELECTs from site_data_status, site_hourly_wide and site_daily_wide only.
let db, locs, status, plots = [];
const $ = (id) => document.getElementById(id);
const DAY = 86400;

function key(slug) { return slug.replace(/-/g, "_"); }

function selection() {
  const through = Math.max(...status.map((s) => Date.parse(s.history_through) / 1000)) + 3600;
  const from = Math.min(...status.map((s) => Date.parse(s.history_from) / 1000));
  let start, end;
  if ($("range").value === "custom" && $("from").value && $("to").value) {
    start = Date.parse($("from").value + "T00:00:00Z") / 1000;
    end = Date.parse($("to").value + "T00:00:00Z") / 1000 + DAY;
  } else {
    const n = $("range").value === "custom" ? 30 : +$("range").value;
    end = through; start = end - n * DAY;
  }
  start = Math.max(start, from); end = Math.min(end, through);
  const daily = end - start > 31 * DAY;
  const chosen = $("loc").value === "all" ? locs : locs.filter((l) => l.slug === $("loc").value);
  return { start, end, daily, chosen };
}

function load() {
  const s = selection();
  let rows;
  if (s.daily) {
    const d0 = new Date(s.start * 1000).toISOString().slice(0, 10);
    const d1 = new Date((s.end - 1) * 1000).toISOString().slice(0, 10);
    rows = q(db, "SELECT * FROM site_daily_wide WHERE date >= ? AND date <= ? ORDER BY date", [d0, d1]);
  } else {
    rows = q(db, "SELECT * FROM site_hourly_wide WHERE epoch >= ? AND epoch < ? ORDER BY epoch", [s.start, s.end]);
  }
  return { ...s, rows };
}

function axisStyle() {
  const fg = cssVar("--muted"), grid = cssVar("--grid");
  return { stroke: fg, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1 } };
}

function makePlot(el, title, data, series, yLabel, opts = {}) {
  const width = el.clientWidth || 340;
  const tz = opts.tz;
  const ax = axisStyle();
  const o = {
    width, height: width < 500 ? 220 : 280,
    tzDate: tz ? (ts) => uPlot.tzDate(new Date(ts * 1e3), tz) : undefined,
    cursor: { drag: { x: true, y: false } },
    scales: { x: { time: true }, y: opts.yMin0 ? { range: (u, mn, mx) => [0, Math.max(mx * 1.1, opts.yFloor || 0.1)] } : {} },
    axes: [{ ...ax }, { ...ax, label: yLabel, size: 52, labelSize: 18 }],
    series: [{ label: opts.daily ? "Date" : "Time",
               value: (u, v) => v == null ? "–" : opts.daily
                 ? new Date(v * 1000).toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric", year: "numeric" })
                 : new Date(v * 1000).toLocaleString("en-US", { timeZone: tz, month: "short", day: "numeric", hour: "numeric", timeZoneName: "short" }) },
             ...series],
    legend: { live: true },
  };
  return new uPlot(o, data, el);
}

function render() {
  plots.forEach((p) => p.destroy()); plots = [];
  const d = load();
  const cols = LOC_COLORS();
  const x = d.rows.map((r) => r.epoch);
  const tz = d.chosen.length === 1 ? d.chosen[0].timezone : "America/Chicago";
  const col = (c) => d.rows.map((r) => r[c]);
  const fmt = (f, dec) => (u, v) => v == null ? "–" : (+v).toFixed(dec);
  const T = (v) => Units.temp(v), S = (v) => Units.speed(v), P = (v) => Units.precip(v);
  const pDec = Units.system === "us" ? 2 : 1;
  const bars = uPlot.paths.bars({ size: [0.8, 40], align: 0 });

  const tempS = [], tempD = [x], precS = [], precD = [x], windS = [], windD = [x];
  d.chosen.forEach((l) => {
    const i = locs.indexOf(l), k = key(l.slug), c = cols[i % 3], name = l.name;
    if (d.daily) {
      const one = d.chosen.length === 1;
      tempD.push(col(`${k}_temp_max_c`).map(T)); tempS.push({ label: `${name} high`, stroke: one ? "#ef4444" : c, width: 1.5, value: fmt(0, 1) });
      tempD.push(col(`${k}_temp_min_c`).map(T)); tempS.push({ label: `${name} low`, stroke: one ? "#3b82f6" : c, width: 1.5, dash: one ? undefined : [5, 4], value: fmt(0, 1) });
      precD.push(col(`${k}_precip_mm`).map(P));
      windD.push(col(`${k}_wind_max_kmh`).map(S)); windS.push({ label: `${name} max wind`, stroke: c, width: 1.5, value: fmt(0, 0) });
      windD.push(col(`${k}_gust_max_kmh`).map(S)); windS.push({ label: `${name} max gust`, stroke: c, width: 1, dash: [5, 4], value: fmt(0, 0) });
    } else {
      tempD.push(col(`${k}_temp_c`).map(T)); tempS.push({ label: name, stroke: c, width: 1.5, value: fmt(0, 1) });
      precD.push(col(`${k}_precip_mm`).map(P));
      windD.push(col(`${k}_wind_kmh`).map(S)); windS.push({ label: `${name} wind`, stroke: c, width: 1.5, value: fmt(0, 0) });
      windD.push(col(`${k}_gust_kmh`).map(S)); windS.push({ label: `${name} gust`, stroke: c, width: 1, dash: [5, 4], value: fmt(0, 0) });
    }
    precS.push({ label: name, stroke: c, fill: c + "99", width: 1, paths: bars, points: { show: false }, value: fmt(0, pDec) });
  });

  const per = d.daily ? "daily" : "hourly";
  const tzNote = d.daily ? "local days" : (d.chosen.length === 1 ? "" : "times CT");
  $("h-temp").textContent = `${Units.tUnit}, ${d.daily ? (d.chosen.length === 1 ? "daily high (red) / low (blue)" : "daily high / low (dashed)") : "hourly"}`;
  $("h-precip").textContent = `${Units.pUnit}, ${d.daily ? "daily total" : "per hour"}`;
  $("h-wind").textContent = `${Units.sUnit}, ${d.daily ? "daily max wind / max gust (dashed)" : "hourly wind / gust (dashed)"}`;
  ["c-temp", "c-precip", "c-wind"].forEach((id) => ($(id).hidden = false));
  $("status").hidden = true;
  if (!x.length) { $("status").hidden = false; $("status").textContent = "No data in this range."; }
  plots.push(makePlot($("c-temp").querySelector(".plot"), "Temperature", tempD, tempS, Units.tUnit, { tz, daily: d.daily }));
  plots.push(makePlot($("c-precip").querySelector(".plot"), "Precipitation", precD, precS, Units.pUnit, { tz, daily: d.daily, yMin0: true, yFloor: Units.system === "us" ? 0.05 : 1 }));
  plots.push(makePlot($("c-wind").querySelector(".plot"), "Wind", windD, windS, Units.sUnit, { tz, daily: d.daily, yMin0: true, yFloor: 10 }));

  const span = d.rows.length ? (d.daily ? `${d.rows[0].date} – ${d.rows[d.rows.length - 1].date}`
    : `${fmtTime(d.rows[0].time, tz)} – ${fmtTime(d.rows[d.rows.length - 1].time, tz)}`) : "no rows";
  $("note").textContent = `Showing ${per} data, ${span}${tzNote ? " (" + tzNote + ")" : ""}. ` + dataThrough();
}

function dataThrough() {
  const t = status.map((s) => s.history_through).sort()[0];
  const f = status.map((s) => s.history_from).sort()[0];
  return `History ${f.slice(0, 10)} → data through ${fmtTime(t, "America/Chicago")}` +
    ` (the archive runs ~1–5 days behind; newest days may be revised).`;
}

(async () => {
  try {
    db = await openDb();
    locs = q(db, "SELECT id, slug, name, state, timezone FROM locations ORDER BY id");
    status = q(db, "SELECT * FROM site_data_status WHERE history_through IS NOT NULL ORDER BY location_id");
    if (!status.length) { $("status").textContent = "No historical data yet."; return; }
    locs.forEach((l) => $("loc").add(new Option(`${l.name}, ${l.state}`, l.slug)));
    const f = status.map((s) => s.history_from).sort()[0].slice(0, 10);
    const t = status.map((s) => s.history_through).sort().reverse()[0].slice(0, 10);
    Object.assign($("from"), { min: f, max: t, value: f }); Object.assign($("to"), { min: f, max: t, value: t });
    const params = new URLSearchParams(location.search);
    if (params.get("loc")) $("loc").value = params.get("loc");
    if (params.get("range")) $("range").value = params.get("range");
    $("custom").classList.toggle("on", $("range").value === "custom");
    $("loc").addEventListener("change", render);
    $("range").addEventListener("change", () => { $("custom").classList.toggle("on", $("range").value === "custom"); if ($("range").value !== "custom") render(); });
    $("apply").addEventListener("click", render);
    setupUnitToggle(render);
    let rt, lastW = innerWidth; addEventListener("resize", () => { if (innerWidth === lastW) return; lastW = innerWidth; clearTimeout(rt); rt = setTimeout(render, 150); });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
    render();
  } catch (e) {
    console.error(e);
    $("status").innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
})();
