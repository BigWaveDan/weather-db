// Shared helpers. The pages only load the DB and run simple SELECTs against views that
// fetch.py/init_db.py maintain; unit conversion and formatting are display-only.
const DB_URL = "../data/weather.db";

async function openDb() {
  const SQL = await initSqlJs({ locateFile: (f) => "vendor/" + f });
  // no-cache = always revalidate with the server (ETag/304), so a new deploy is picked up
  // immediately while an unchanged DB isn't re-downloaded.
  const res = await fetch(DB_URL, { cache: "no-cache" });
  if (!res.ok) throw new Error(`Could not load ${DB_URL}: HTTP ${res.status}`);
  return new SQL.Database(new Uint8Array(await res.arrayBuffer()));
}

function q(db, sql, params = []) {
  const st = db.prepare(sql);
  st.bind(params);
  const out = [];
  while (st.step()) out.push(st.getAsObject());
  st.free();
  return out;
}

// ---- Units (stored metric; converted only for display) ----
const Units = {
  get system() { return localStorage.getItem("wx-units") || "us"; },
  set system(v) { localStorage.setItem("wx-units", v); },
  temp(c) { return c == null ? null : this.system === "us" ? c * 9 / 5 + 32 : c; },
  speed(k) { return k == null ? null : this.system === "us" ? k / 1.609344 : k; },
  precip(mm) { return mm == null ? null : this.system === "us" ? mm / 25.4 : mm; },
  get tUnit() { return this.system === "us" ? "°F" : "°C"; },
  get sUnit() { return this.system === "us" ? "mph" : "km/h"; },
  get pUnit() { return this.system === "us" ? "in" : "mm"; },
  fmtT(c, d = 0) { const v = this.temp(c); return v == null ? "–" : v.toFixed(d) + this.tUnit; },
  fmtS(k) { const v = this.speed(k); return v == null ? "–" : Math.round(v) + " " + this.sUnit; },
  fmtP(mm) { const v = this.precip(mm); return v == null ? "–" : v.toFixed(this.system === "us" ? 2 : 1) + " " + this.pUnit; },
};

function setupUnitToggle(onChange) {
  const btns = document.querySelectorAll(".units button");
  const sync = () => btns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.u === Units.system)));
  btns.forEach((b) => b.addEventListener("click", () => { Units.system = b.dataset.u; sync(); onChange(); }));
  sync();
}

function fmtTime(iso, tz, opts = {}) {
  if (!iso) return "–";
  return new Date(iso).toLocaleString("en-US", { timeZone: tz, month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", timeZoneName: "short", ...opts });
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
const LOC_COLORS = () => [cssVar("--c1"), cssVar("--c2"), cssVar("--c3")];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
