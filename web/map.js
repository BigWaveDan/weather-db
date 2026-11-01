// Map page: SELECTs from site_current_conditions, site_forecast_upcoming, site_active_alerts only.
const ICONS = { sunny: "☀️", clear: "🌙", partly: "⛅", cloudy: "☁️", rain: "🌧️", snow: "🌨️",
                storm: "⛈️", fog: "🌫️", wind: "💨", unknown: "❔" };
let db, cur, map, markers = {}, selected;
const $ = (id) => document.getElementById(id);

function compass(deg) {
  if (deg == null) return "";
  return ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"][Math.round(deg / 22.5) % 16];
}

function spark(values, color, { min, max, labels, fmt }) {
  const w = 300, h = 46, pad = 4, n = values.length;
  if (!n) return "<p class='muted'>No forecast hours.</p>";
  const vs = values.map((v) => (v == null ? null : v));
  const dlo = Math.min(...vs.filter((v) => v != null)), dhi = Math.max(...vs.filter((v) => v != null));
  const lo = min ?? dlo, hi = max ?? dhi;
  const span = hi - lo || 1;
  const X = (i) => pad + (i * (w - 2 * pad)) / Math.max(n - 1, 1);
  const Y = (v) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const pts = vs.map((v, i) => (v == null ? null : `${X(i).toFixed(1)},${Y(v).toFixed(1)}`)).filter(Boolean);
  const area = `M${X(0)},${h - pad} L${pts.join(" L")} L${X(n - 1)},${h - pad} Z`;
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img">
    <path d="${area}" fill="${color}" fill-opacity=".18"></path>
    <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"></polyline>
  </svg><div class="lbl"><span>${labels[0]}</span><span>${fmt(dlo)} – ${fmt(dhi)}</span><span>${labels[1]}</span></div>`;
}

function pinHtml(c) {
  const ring = c.spc_day1_color || "#9AA0A6";
  return `<div class="pinwrap side-${esc(c.map_label_side || "center")}"><span class="dot" style="background:${ring}"></span>
    <div class="pin${selected === c.slug ? " sel" : ""}" style="border-color:${ring}" title="${esc(c.name)}">
    <span class="ic">${ICONS[c.condition_key] || ICONS.unknown}</span>${Units.fmtT(c.temperature_c)}
    ${c.alert_count ? `<span class="badge">${c.alert_count}</span>` : ""}</div></div>`;
}

function chip(label, name, bg, fg) {
  return label ? `<span class="chip" style="background:${bg};color:${fg}" title="${esc(name)}">${esc(label)}</span>` : "<span class='muted'>n/a</span>";
}

function detail(c) {
  const hours = q(db, `SELECT valid_time, temperature_c, precip_prob_pct FROM site_forecast_upcoming
                       WHERE location_id = ? AND source = 'open-meteo' AND hour_rank < 12 ORDER BY valid_time`, [c.location_id]);
  const alerts = q(db, `SELECT event, severity, headline, expires, ends FROM site_active_alerts
                        WHERE location_id = ? ORDER BY severity_rank DESC, expires`, [c.location_id]);
  const hr = (iso) => new Date(iso).toLocaleTimeString("en-US", { timeZone: c.timezone, hour: "numeric" });
  const lbl = hours.length ? [hr(hours[0].valid_time), hr(hours[hours.length - 1].valid_time)] : ["", ""];
  const tempVals = hours.map((h) => Units.temp(h.temperature_c));
  const accent = cssVar("--accent");
  return `<div class="card">
    <h2><span>${esc(c.name)}, ${esc(c.state)}</span>${c.alert_count ? `<span class="chip" style="background:var(--alert);color:#fff">${c.alert_count} alert${c.alert_count > 1 ? "s" : ""}</span>` : ""}</h2>
    <div class="muted">Forecast hour ${fmtTime(c.valid_time, c.timezone)}</div>
    <div class="now"><span class="ic">${ICONS[c.condition_key] || ICONS.unknown}</span>
      <span class="big">${Units.fmtT(c.temperature_c)}</span><span>${esc(c.short_forecast || "")}</span></div>
    <dl class="kv">
      <dt>Wind</dt><dd>${Units.fmtS(c.wind_speed_kmh)} ${compass(c.wind_dir_deg)}, gusts ${Units.fmtS(c.wind_gust_kmh)}</dd>
      <dt>Precip</dt><dd>${c.precip_prob_pct == null ? "–" : Math.round(c.precip_prob_pct) + "%"} chance · ${Units.fmtP(c.precip_mm)} this hour</dd>
      <dt>NWS temp</dt><dd>${Units.fmtT(c.nws_temperature_c)}</dd>
    </dl>
    <div class="spark"><h3>Next 12 h · temperature (${Units.tUnit})</h3>
      ${spark(tempVals, accent, { labels: lbl, fmt: (v) => Math.round(v) + "°" })}</div>
    <div class="spark"><h3>Next 12 h · precip chance</h3>
      ${spark(hours.map((h) => h.precip_prob_pct), "#0ea5e9", { min: 0, max: 100, labels: lbl, fmt: (v) => Math.round(v) + "%" })}</div>
    <h3>Active NWS alerts</h3>
    ${alerts.length ? alerts.map((a) => `<div class="alert"><b>${esc(a.event)} · ${esc(a.severity)}</b>${esc(a.headline || "")}
        <div class="muted">Expires ${fmtTime(a.ends || a.expires, c.timezone)}</div></div>`).join("")
      : `<div class="muted">None (checked ${fmtTime(c.alerts_checked_at, c.timezone)})</div>`}
    <h3>SPC convective outlook</h3>
    <div>Day 1 ${chip(c.spc_day1_risk, c.spc_day1_name, c.spc_day1_color, c.spc_day1_text_color)}
         Day 2 ${chip(c.spc_day2_risk, c.spc_day2_name, c.spc_day2_color, c.spc_day2_text_color)}</div>
    <div class="muted">${c.spc_day1_name ? esc(c.spc_day1_name) + " (Day 1)" : ""}${c.spc_day1_issued ? " · issued " + fmtTime(c.spc_day1_issued, c.timezone) : ""}</div>
    <h3>Fetched</h3>
    <div class="muted">Open-Meteo ${fmtTime(c.om_fetched_at, c.timezone)} · NWS ${fmtTime(c.nws_fetched_at, c.timezone)}</div>
  </div>`;
}

function select(slug) {
  selected = slug;
  const c = cur.find((x) => x.slug === slug);
  $("detail").innerHTML = detail(c);
  document.querySelectorAll("#list button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.slug === slug)));
  drawPins();
}

function drawPins() {
  cur.forEach((c) => {
    const icon = L.divIcon({ html: pinHtml(c), className: "", iconSize: null, iconAnchor: [0, 0] });
    if (markers[c.slug]) markers[c.slug].setIcon(icon);
    else markers[c.slug] = L.marker([c.latitude, c.longitude], { icon, title: c.name, keyboard: true })
      .addTo(map).on("click", () => select(c.slug));
  });
}

(async () => {
  try {
    db = await openDb();
    cur = q(db, "SELECT * FROM site_current_conditions ORDER BY location_id");
    $("status").hidden = true;
    map = L.map("map", { zoomSnap: 0.25 });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    map.fitBounds(cur.map((c) => [c.latitude, c.longitude]), { padding: [70, 60] });
    $("list").innerHTML = cur.map((c) => `<button data-slug="${c.slug}">${esc(c.name)}</button>`).join("");
    $("list").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      select(b.dataset.slug); map.panTo(markers[b.dataset.slug].getLatLng());
    }));
    setupUnitToggle(() => select(selected));
    select(new URLSearchParams(location.search).get("loc") || cur[0].slug);
  } catch (e) {
    console.error(e);
    $("status").innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
})();
