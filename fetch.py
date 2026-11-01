#!/usr/bin/env python3
"""Weather DB fetcher (Python stdlib only).

Usage:
  python fetch.py all                                   # every current source, all locations
  python fetch.py forecast --location worthington-mn [--source open-meteo|nws|all]
  python fetch.py alerts   --location worthington-mn
  python fetch.py spc      [--day 1|2]                  # SPC outlook text + per-location risk
  python fetch.py backfill [--location X|all] [--chunks 3]  # walk history backward, 30 days per chunk
  python fetch.py prune                                 # retention (also runs at the end of `all`)
  python fetch.py locations

All logic lives here; the future website only reads data/weather.db.
"""
import argparse, datetime as dt, gzip, hashlib, json, random, re, sqlite3, sys, time
import urllib.error, urllib.parse, urllib.request

from init_db import DB_PATH, init_db

USER_AGENT = "WeatherDB/0.1 (https://github.com/BigWaveDan/weather-db; onehumancompany@proton.me)"
POLITE_SLEEP_S = 1.5          # pause between every request
MAX_RETRIES = 5               # on 429 / 5xx / network errors
BACKOFF_BASE_S = 2.0          # 2, 4, 8, 16, 32 s (+ jitter); honours Retry-After

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
# 7 variables (< 10, so a 30-day chunk is weighted ~2-3 calls by Open-Meteo, not more).
OM_ARCHIVE_HOURLY = ["temperature_2m", "precipitation", "wind_speed_10m", "wind_direction_10m",
                     "wind_gusts_10m", "relative_humidity_2m", "pressure_msl"]
ARCHIVE_SLEEP_S = 2.5         # extra-generous pause between archive calls
BACKFILL_CHUNK_DAYS = 30
BACKFILL_TOPUP_DAYS = 7       # re-fetch this many trailing days so recent (IFS) values get ERA5 revisions
# Open-Meteo free tier (https://open-meteo.com/en/terms): < 600 calls/min, 5,000/hour, 10,000/day, 300,000/month.
# Stay far below: hard cap per run on *weighted* archive calls.
MAX_WEIGHTED_ARCHIVE_CALLS_PER_RUN = 300
RAW_BODY_RETENTION_DAYS = 7
FORECAST_FULL_RETENTION_DAYS = 2
FORECAST_THIN_WINDOW_HOURS = 6
OM_HOURLY = ["temperature_2m", "precipitation_probability", "precipitation",
             "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]
NWS_BASE = "https://api.weather.gov"
SPC_TEXT = "https://www.spc.noaa.gov/products/outlook/day{day}otlk.txt"
SPC_CAT_GEOJSON = "https://www.spc.noaa.gov/products/outlook/day{day}otlk_cat.nolyr.geojson"
SPC_DN = {"TSTM": 2, "MRGL": 3, "SLGT": 4, "ENH": 5, "MDT": 6, "HIGH": 7}
# Headline phrases in the SPC text, highest first.
SPC_TEXT_RISK = [("HIGH RISK", "HIGH"), ("MODERATE RISK", "MDT"), ("ENHANCED RISK", "ENH"),
                 ("SLIGHT RISK", "SLGT"), ("MARGINAL RISK", "MRGL")]
TZ_OFFSETS = {"UTC": 0, "GMT": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
              "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}

COMPASS = {d: i * 22.5 for i, d in enumerate(
    "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split())}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_utc(ts):
    """Normalize an ISO timestamp (with offset, or naive=UTC) to 'YYYY-MM-DDTHH:MM:SSZ'."""
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class RateLimited(RuntimeError):
    """Raised (instead of retrying) when a caller asked to stop on HTTP 429."""


def om_weight(days, n_vars):
    """Open-Meteo's documented fractional call weight: >10 variables or >2 weeks count as multiple calls."""
    return max(1.0, n_vars / 10) * max(1.0, days / 14)


class Fetcher:
    def __init__(self, con, command, args):
        self.con = con
        self._last_request = 0.0
        cur = con.execute("INSERT INTO fetch_runs(command, args, started_at) VALUES (?,?,?)",
                          (command, json.dumps(args), utcnow()))
        self.run_id = cur.lastrowid
        con.commit()
        self.requests = self.failed = self.inserted = self.updated = 0
        self.warnings = []   # degraded-but-not-failed sources (e.g. SPC layer unreachable -> text only)

    # ---- HTTP -----------------------------------------------------------
    def get(self, url, source, endpoint, location_id=None, accept="application/json",
            stop_on_429=False, min_interval=POLITE_SLEEP_S):
        """GET with polite pacing and retry/backoff. Stores every final response in raw_responses.
        stop_on_429=True raises RateLimited on the first 429 instead of retrying."""
        for attempt in range(MAX_RETRIES + 1):
            wait = min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            status, body, ctype, retry_after = None, b"", None, None
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    status, body, ctype = r.status, r.read(), r.headers.get("Content-Type")
            except urllib.error.HTTPError as e:
                status, body, ctype = e.code, e.read() or b"", e.headers.get("Content-Type")
                retry_after = e.headers.get("Retry-After")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                body = str(e).encode()
            if status == 429 and stop_on_429:
                self.failed += 1
                self._store_raw(source, endpoint, url, location_id, status, ctype, body)
                raise RateLimited(f"HTTP 429 from {url.split('?')[0]}: {body[:120]!r}")
            retryable = status is None or status == 429 or status >= 500
            if retryable and attempt < MAX_RETRIES:
                self.failed += 1
                delay = BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 1)
                if retry_after and retry_after.isdigit():
                    delay = max(delay, int(retry_after))
                print(f"  {status or 'network error'} from {url} - retry {attempt+1} in {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            raw_id = self._store_raw(source, endpoint, url, location_id, status, ctype, body)
            if status != 200:
                self.failed += 1
                raise RuntimeError(f"HTTP {status} for {url.split('?')[0]}: {body[:120]!r}")
            return body, raw_id

    def _store_raw(self, source, endpoint, url, location_id, status, ctype, body):
        cur = self.con.execute(
            """INSERT INTO raw_responses(fetch_run_id, location_id, source, endpoint, url, fetched_at,
               http_status, content_type, body_sha256, body_bytes, body_gzip)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (self.run_id, location_id, source, endpoint, url, utcnow(), status, ctype,
             hashlib.sha256(body).hexdigest(), len(body), gzip.compress(body)))
        self.con.commit()   # keep provenance even if the job later fails
        return cur.lastrowid

    def finish(self, status, error=None):
        self.con.execute(
            """UPDATE fetch_runs SET finished_at=?, status=?, requests_made=?, requests_failed=?,
               rows_inserted=?, rows_updated=?, error=? WHERE id=?""",
            (utcnow(), status, self.requests, self.failed, self.inserted, self.updated, error, self.run_id))
        self.con.commit()

    # ---- Open-Meteo forecast -------------------------------------------
    def open_meteo_forecast(self, loc):
        q = {"latitude": loc["latitude"], "longitude": loc["longitude"],
             "hourly": ",".join(OM_HOURLY), "timezone": "GMT", "forecast_days": 16,
             "wind_speed_unit": "kmh", "temperature_unit": "celsius", "precipitation_unit": "mm"}
        url = OPEN_METEO_FORECAST + "?" + urllib.parse.urlencode(q)
        body, raw_id = self.get(url, "open-meteo", "forecast", loc["id"])
        h = json.loads(body)["hourly"]
        fetched = utcnow()
        rows = [(loc["id"], "open-meteo", to_utc(t), h["temperature_2m"][i], h["precipitation_probability"][i],
                 h["precipitation"][i], h["wind_speed_10m"][i], h["wind_direction_10m"][i],
                 h["wind_gusts_10m"][i], None, fetched, self.run_id, raw_id)
                for i, t in enumerate(h["time"])]
        return self._insert_forecast(rows)

    # ---- NWS -----------------------------------------------------------
    def nws_points(self, loc, max_age_days=30):
        """Resolve (and cache on the locations row) the NWS grid + hourly forecast URL."""
        if loc["nws_forecast_hourly_url"] and loc["nws_points_checked_at"]:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(
                loc["nws_points_checked_at"].replace("Z", "+00:00"))
            if age.days < max_age_days:
                return loc["nws_forecast_hourly_url"]
        url = f"{NWS_BASE}/points/{loc['latitude']:.4f},{loc['longitude']:.4f}"
        body, _ = self.get(url, "nws", "points", loc["id"], accept="application/geo+json")
        p = json.loads(body)["properties"]
        self.con.execute(
            """UPDATE locations SET nws_office=?, nws_grid_x=?, nws_grid_y=?, nws_forecast_hourly_url=?,
               nws_forecast_zone=?, nws_county=?, nws_points_checked_at=? WHERE id=?""",
            (p.get("gridId"), p.get("gridX"), p.get("gridY"), p.get("forecastHourly"),
             (p.get("forecastZone") or "").rsplit("/", 1)[-1], (p.get("county") or "").rsplit("/", 1)[-1],
             utcnow(), loc["id"]))
        return p["forecastHourly"]

    def nws_hourly(self, loc):
        url = self.nws_points(loc) + "?units=si"
        body, raw_id = self.get(url, "nws", "forecast_hourly", loc["id"], accept="application/geo+json")
        fetched = utcnow()
        rows = []
        for per in json.loads(body)["properties"]["periods"]:
            temp = per.get("temperature")
            if isinstance(temp, dict):
                temp = temp.get("value")
            if per.get("temperatureUnit") == "F" and temp is not None:
                temp = round((temp - 32) * 5 / 9, 1)
            pop = (per.get("probabilityOfPrecipitation") or {}).get("value")
            ws = per.get("windSpeed")
            if isinstance(ws, str):   # e.g. "15 km/h" or "10 to 15 km/h" (take the upper value)
                nums = [float(x) for x in ws.replace("km/h", "").replace("mph", "").replace("to", " ").split()
                        if x.replace(".", "").isdigit()]
                ws = (nums[-1] * (1.609344 if "mph" in per["windSpeed"] else 1)) if nums else None
            rows.append((loc["id"], "nws", to_utc(per["startTime"]), temp, pop, None, ws,
                         COMPASS.get(per.get("windDirection")), None, per.get("shortForecast"),
                         fetched, self.run_id, raw_id))
        return self._insert_forecast(rows)

    def nws_alerts(self, loc):
        url = f"{NWS_BASE}/alerts/active?point={loc['latitude']:.4f},{loc['longitude']:.4f}"
        body, _ = self.get(url, "nws", "alerts", loc["id"], accept="application/geo+json")
        now, n = utcnow(), 0
        for f in json.loads(body).get("features", []):
            p = f["properties"]
            aid = p.get("id") or f.get("id")
            existed = self.con.execute("SELECT 1 FROM nws_alerts WHERE id=?", (aid,)).fetchone()
            self.con.execute(
                """INSERT INTO nws_alerts(id, location_id, event, severity, certainty, urgency, headline,
                   description, instruction, area_desc, sent, onset, expires, ends, first_seen_at, last_seen_at, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at, expires=excluded.expires,
                   ends=excluded.ends, headline=excluded.headline, raw_json=excluded.raw_json""",
                (aid, loc["id"], p.get("event"), p.get("severity"), p.get("certainty"), p.get("urgency"),
                 p.get("headline"), p.get("description"), p.get("instruction"), p.get("areaDesc"),
                 p.get("sent"), p.get("onset"), p.get("expires"), p.get("ends"), now, now, json.dumps(f)))
            if existed:
                self.updated += 1
            else:
                self.inserted += 1
            n += 1
        return n

    # ---- SPC convective outlooks --------------------------------------
    def spc_outlook(self, day, locations):
        """Store the Day N outlook text, then the per-location categorical risk from the GeoJSON layer.
        Returns a short summary string. The text is required; the layer is best-effort."""
        url = SPC_TEXT.format(day=day)
        body, raw_id = self.get(url, "spc", f"outlook_day{day}_text", accept="text/plain")
        text = body.decode("utf-8", "replace").replace("\r\n", "\n")
        issued, wmo, vfrom, vto = parse_spc_text_times(text)
        if not issued:
            raise RuntimeError(f"could not parse issued time from Day {day} text")
        max_risk = parse_spc_text_risk(text)
        now = utcnow()
        existed = self.con.execute("SELECT id FROM spc_outlooks WHERE day=? AND issued=?", (day, issued)).fetchone()
        self.con.execute(
            """INSERT INTO spc_outlooks(day, issued, product_url, text, max_risk, valid_from, valid_to,
               wmo_header, fetched_at, raw_response_id) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day, issued) DO UPDATE SET fetched_at=excluded.fetched_at, text=excluded.text,
               raw_response_id=excluded.raw_response_id,
               max_risk=CASE WHEN spc_outlooks.layer_status='ok' THEN spc_outlooks.max_risk
                             ELSE excluded.max_risk END""",
            (day, issued, url, text, max_risk, vfrom, vto, wmo, now, raw_id))
        outlook_id = self.con.execute("SELECT id FROM spc_outlooks WHERE day=? AND issued=?",
                                      (day, issued)).fetchone()[0]
        if existed:
            self.updated += 1
        else:
            self.inserted += 1
        self.con.commit()

        # Categorical layer -> point-in-polygon per location
        lurl = SPC_CAT_GEOJSON.format(day=day)
        layer_issued, status, risks = None, "ok", {}
        try:
            gbody, _ = self.get(lurl, "spc", f"outlook_day{day}_cat_geojson", accept="application/geo+json")
            feats = json.loads(gbody).get("features", [])
            for f in feats:
                layer_issued = layer_issued or to_utc(f["properties"]["ISSUE_ISO"])
            if layer_issued and layer_issued != issued:
                status = f"mismatch: layer issued {layer_issued}, text issued {issued}"
            else:
                for loc in locations:
                    risks[loc["id"]] = point_risk(loc["longitude"], loc["latitude"], feats)
                # The layer is authoritative for the national max category (text headline is the fallback).
                labels = [f["properties"].get("LABEL") for f in feats if f["properties"].get("LABEL") in SPC_DN]
                layer_max = max(labels, key=SPC_DN.get) if labels else "NONE"
                self.con.execute("UPDATE spc_outlooks SET max_risk=? WHERE id=?", (layer_max, outlook_id))
                max_risk = layer_max
        except Exception as e:
            status = f"unreachable: {e}"[:300]
        if status != "ok":
            self.warnings.append(f"spc day {day} layer {status}"[:200])
        # Never downgrade an outlook whose layer was already checked OK (its risk rows stay valid).
        self.con.execute("""UPDATE spc_outlooks SET layer_url=?, layer_issued=?, layer_status=?
                            WHERE id=? AND (? = 'ok' OR layer_status IS NULL OR layer_status != 'ok')""",
                         (lurl, layer_issued, status, outlook_id, status))
        for loc_id, (label, dn) in risks.items():
            seen = self.con.execute("SELECT 1 FROM spc_location_risk WHERE outlook_id=? AND location_id=?",
                                    (outlook_id, loc_id)).fetchone()
            self.con.execute(
                """INSERT INTO spc_location_risk(outlook_id, location_id, risk, risk_dn, checked_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(outlook_id, location_id) DO UPDATE SET
                   risk=excluded.risk, risk_dn=excluded.risk_dn, checked_at=excluded.checked_at""",
                (outlook_id, loc_id, label, dn, now))
            if seen:
                self.updated += 1
            else:
                self.inserted += 1
        self.con.commit()
        by_slug = {l["id"]: l["slug"] for l in locations}
        detail = ", ".join(f"{by_slug[k]}={v[0]}" for k, v in risks.items()) or "no point check"
        return f"issued {issued}, max {max_risk}, layer {status}; {detail}"

    # ---- Historical backfill (Open-Meteo archive) ----------------------
    def archive_chunk(self, loc, start, end):
        """Fetch [start, end] (dates, UTC) hourly from the archive and upsert into observations_hourly."""
        q = {"latitude": loc["latitude"], "longitude": loc["longitude"],
             "start_date": start.isoformat(), "end_date": end.isoformat(),
             "hourly": ",".join(OM_ARCHIVE_HOURLY), "timezone": "GMT",
             "wind_speed_unit": "kmh", "temperature_unit": "celsius", "precipitation_unit": "mm"}
        url = OPEN_METEO_ARCHIVE + "?" + urllib.parse.urlencode(q)
        body, raw_id = self.get(url, "open-meteo-archive", "archive", loc["id"],
                                stop_on_429=True, min_interval=ARCHIVE_SLEEP_S)
        h = json.loads(body)["hourly"]
        fetched = utcnow()
        rows, skipped = [], 0
        for i, t in enumerate(h["time"]):
            vals = [h[k][i] for k in OM_ARCHIVE_HOURLY]
            if all(v is None for v in vals):
                skipped += 1          # hour not available (yet) - don't store an empty row
                continue
            rows.append((loc["id"], to_utc(t), *vals, fetched, raw_id))
        before_rows = self.con.execute("SELECT COUNT(*) FROM observations_hourly WHERE location_id=?",
                                       (loc["id"],)).fetchone()[0]
        self.con.executemany(
            """INSERT INTO observations_hourly(location_id, time, temperature_c, precip_mm, wind_speed_kmh,
               wind_dir_deg, wind_gust_kmh, relative_humidity_pct, pressure_msl_hpa, fetched_at, raw_response_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(location_id, time) DO UPDATE SET temperature_c=excluded.temperature_c,
               precip_mm=excluded.precip_mm, wind_speed_kmh=excluded.wind_speed_kmh,
               wind_dir_deg=excluded.wind_dir_deg, wind_gust_kmh=excluded.wind_gust_kmh,
               relative_humidity_pct=excluded.relative_humidity_pct, pressure_msl_hpa=excluded.pressure_msl_hpa,
               fetched_at=excluded.fetched_at, raw_response_id=excluded.raw_response_id""", rows)
        after_rows = self.con.execute("SELECT COUNT(*) FROM observations_hourly WHERE location_id=?",
                                      (loc["id"],)).fetchone()[0]
        new = after_rows - before_rows
        self.inserted += new
        self.updated += len(rows) - new
        return len(rows), new, skipped

    def backfill(self, locations, chunks):
        """Walk each location backward in 30-day chunks, round-robin, saving progress after every chunk.
        First (per run) tops up the trailing window up to yesterday. Stops cleanly on HTTP 429."""
        yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
        weighted, log = 0.0, []
        n_vars = len(OM_ARCHIVE_HOURLY)

        def progress(loc):
            return dict(self.con.execute("SELECT * FROM backfill_progress WHERE location_id=?",
                                         (loc["id"],)).fetchone())

        def save(loc, **kw):
            kw["updated_at"] = utcnow()
            sets = ", ".join(f"{k}=?" for k in kw)
            self.con.execute(f"UPDATE backfill_progress SET {sets} WHERE location_id=?", (*kw.values(), loc["id"]))
            self.con.commit()

        plan = []   # (loc, kind)
        for loc in locations:
            pr = progress(loc)
            if pr["newest_date_fetched"] and pr["newest_date_fetched"] < yesterday.isoformat():
                plan.append((loc, "topup"))
        for _ in range(chunks):
            plan.extend((loc, "back") for loc in locations)

        failed = set()
        for loc, kind in plan:
            if loc["id"] in failed:
                continue
            pr = progress(loc)
            target = dt.date.fromisoformat(pr["target_oldest_date"])
            if kind == "topup":
                end = yesterday
                start = max(dt.date.fromisoformat(pr["newest_date_fetched"]) - dt.timedelta(days=BACKFILL_TOPUP_DAYS - 1),
                            target)
            else:
                if pr["done"]:
                    continue
                end = (dt.date.fromisoformat(pr["oldest_date_fetched"]) - dt.timedelta(days=1)
                       if pr["oldest_date_fetched"] else yesterday)
                if end < target:
                    save(loc, done=1)
                    continue
                start = max(end - dt.timedelta(days=(pr["chunk_days"] or BACKFILL_CHUNK_DAYS) - 1), target)
            w = om_weight((end - start).days + 1, n_vars)
            if weighted + w > MAX_WEIGHTED_ARCHIVE_CALLS_PER_RUN:
                log.append(f"stopped: per-run weighted-call cap {MAX_WEIGHTED_ARCHIVE_CALLS_PER_RUN} reached")
                break
            try:
                stored, new, skipped = self.archive_chunk(loc, start, end)
            except RateLimited as e:
                save(loc, last_error=str(e)[:300])
                self.warnings.append(f"backfill stopped on 429 at {loc['slug']} {start}..{end}; progress saved")
                log.append(f"{loc['slug']} {start}..{end}: 429 - stopping, progress saved")
                break
            except Exception as e:
                save(loc, last_error=str(e)[:300])
                self.warnings.append(f"backfill {loc['slug']} {start}..{end}: {e}"[:200])
                log.append(f"{loc['slug']} {start}..{end}: FAILED {e}"[:200])
                failed.add(loc["id"])
                continue    # other locations can still progress; this one retries next run
            weighted += w
            if kind == "topup":
                save(loc, newest_date_fetched=end.isoformat(), last_error=None)
            else:
                save(loc, oldest_date_fetched=start.isoformat(),
                     newest_date_fetched=pr["newest_date_fetched"] or end.isoformat(),
                     done=int(start <= target), chunks_fetched=pr["chunks_fetched"] + 1, last_error=None)
            msg = f"{loc['slug']} {kind} {start}..{end}: {stored} rows ({new} new"
            msg += f", {skipped} empty hours skipped)" if skipped else ")"
            log.append(msg)
            print("  " + msg)
        return f"{len([l for l in log if ': ' in l and 'rows' in l])} chunks, ~{weighted:.1f} weighted Open-Meteo calls" + \
            ("; " + log[-1] if log and "stop" in log[-1] else "")

    # ---- Retention ------------------------------------------------------
    def prune(self, now=None):
        """Keep the committed DB small:
        - raw_responses older than 7 days: drop the body, keep metadata + sha256.
        - forecast snapshots older than 2 days: keep only the newest snapshot per location+source
          per 6-hour UTC window of fetched_at. The newest snapshot per location+source (forecast_latest)
          is never deleted."""
        now = now or dt.datetime.now(dt.timezone.utc)
        iso = lambda t: t.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        raw_cut = iso(now - dt.timedelta(days=RAW_BODY_RETENTION_DAYS))
        fc_cut = iso(now - dt.timedelta(days=FORECAST_FULL_RETENTION_DAYS))
        c1 = self.con.execute("""UPDATE raw_responses SET body_gzip=NULL, body_pruned_at=?
                                 WHERE fetched_at < ? AND body_gzip IS NOT NULL""", (iso(now), raw_cut)).rowcount
        c2 = self.con.execute(f"""
            DELETE FROM forecast_hourly
            WHERE fetched_at < :cut
              AND (location_id, source, fetched_at) NOT IN (
                    SELECT location_id, source, MAX(fetched_at) FROM forecast_hourly GROUP BY location_id, source)
              AND (location_id, source, fetched_at) NOT IN (
                    SELECT location_id, source, MAX(fetched_at) FROM forecast_hourly
                    WHERE fetched_at < :cut
                    GROUP BY location_id, source, substr(fetched_at, 1, 10),
                             CAST(substr(fetched_at, 12, 2) AS INTEGER) / {FORECAST_THIN_WINDOW_HOURS})""",
                              {"cut": fc_cut}).rowcount
        self.con.commit()
        self.updated += c1
        self.con.execute("VACUUM")
        return f"{c1} raw bodies dropped, {c2} old forecast rows thinned"

    def _insert_forecast(self, rows):
        before = self.con.total_changes
        self.con.executemany(
            """INSERT OR IGNORE INTO forecast_hourly(location_id, source, valid_time, temperature_c,
               precip_prob_pct, precip_mm, wind_speed_kmh, wind_dir_deg, wind_gust_kmh, short_forecast,
               fetched_at, fetch_run_id, raw_response_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        n = self.con.total_changes - before
        self.inserted += n
        self.con.commit()
        return n


def parse_spc_text_times(text):
    """Return (issued_utc, wmo_header, valid_from_utc, valid_to_utc) from an SPC outlook text product."""
    wmo = re.search(r"^(ACUS0\d KWNS (\d{2})(\d{2})(\d{2}))", text, re.M)
    local = re.search(r"^(\d{3,4}) (AM|PM) ([A-Z]{3}) \w{3} (\w{3}) (\d{1,2}) (\d{4})", text, re.M)
    issued = None
    if local and local.group(3) in TZ_OFFSETS:
        hhmm = local.group(1).zfill(4)
        h, m = int(hhmm[:2]) % 12 + (12 if local.group(2) == "PM" else 0), int(hhmm[2:])
        t = dt.datetime.strptime(f"{local.group(4)} {local.group(5)} {local.group(6)}", "%b %d %Y")
        t = t.replace(hour=h, minute=m, tzinfo=dt.timezone(dt.timedelta(hours=TZ_OFFSETS[local.group(3)])))
        issued = to_utc(t.isoformat())
    base = dt.datetime.fromisoformat(issued.replace("Z", "+00:00")) if issued else None
    vfrom = vto = None
    v = re.search(r"^Valid (\d{2})(\d{2})(\d{2})Z - (\d{2})(\d{2})(\d{2})Z", text, re.M)
    if v and base:
        def ddhhmm(d, h, m):  # resolve day-of-month relative to the issue time (handles month rollover)
            t = base.replace(day=1, hour=int(h), minute=int(m), second=0)
            for months_ahead in (0, 1, -1):
                y, mo = t.year, t.month + months_ahead
                y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
                try:
                    c = t.replace(year=y, month=mo, day=int(d))
                except ValueError:
                    continue
                if abs((c - base).days) <= 5:
                    return to_utc(c.isoformat())
            return None
        vfrom, vto = ddhhmm(*v.group(1, 2, 3)), ddhhmm(*v.group(4, 5, 6))
    return issued, (wmo.group(1) if wmo else None), vfrom, vto


def parse_spc_text_risk(text):
    """Highest categorical risk named in the outlook headline(s); TSTM/NONE if no severe area."""
    head = text.split("...SUMMARY...")[0].upper().replace("\n", " ")
    for phrase, label in SPC_TEXT_RISK:
        if phrase in head:
            return label
    # No severe category in the headline: the text alone can't tell TSTM from NONE.
    return None


def point_in_ring(x, y, ring):
    """Ray casting: is (x, y) inside the closed ring [[x, y], ...]?"""
    inside, n = False, len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def point_in_geometry(x, y, geom):
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else \
        geom["coordinates"] if geom["type"] == "MultiPolygon" else []
    for rings in polys:
        if rings and point_in_ring(x, y, rings[0]) and not any(point_in_ring(x, y, h) for h in rings[1:]):
            return True
    return False


def point_risk(lon, lat, features):
    """Highest SPC categorical label whose polygon contains the point, else ('NONE', 0)."""
    best = ("NONE", 0)
    for f in features:
        p, g = f.get("properties", {}), f.get("geometry")
        label = p.get("LABEL")
        dn = p.get("DN") or SPC_DN.get(label, 0)
        if g and label in SPC_DN and dn > best[1] and point_in_geometry(lon, lat, g):
            best = (label, dn)
    return best


def load_location(con, slug):
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM locations WHERE slug=?", (slug,)).fetchone()
    if not row:
        slugs = [r[0] for r in con.execute("SELECT slug FROM locations")]
        sys.exit(f"Unknown location {slug!r}. Known: {', '.join(slugs)}")
    return dict(row)


def main():
    ap = argparse.ArgumentParser(description="Weather DB fetcher")
    sub = ap.add_subparsers(dest="command", required=True)
    f = sub.add_parser("forecast", help="hourly forecast (Open-Meteo and/or NWS)")
    f.add_argument("--location", required=True)
    f.add_argument("--source", choices=["open-meteo", "nws", "all"], default="all")
    a = sub.add_parser("alerts", help="NWS active alerts for a location")
    a.add_argument("--location", required=True)
    sp = sub.add_parser("spc", help="SPC convective outlooks + per-location risk")
    sp.add_argument("--day", type=int, choices=[1, 2], action="append")
    sub.add_parser("all", help="every current source for every location, then prune")
    b = sub.add_parser("backfill", help="walk Open-Meteo archive history backward, 30-day chunks")
    b.add_argument("--location", default="all")
    b.add_argument("--chunks", type=int, default=3, help="chunks per location this run (default 3)")
    sub.add_parser("prune", help="apply retention rules and VACUUM")
    sub.add_parser("locations", help="list configured locations")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    import pathlib
    con = init_db(pathlib.Path(args.db))
    if args.command == "locations":
        for r in con.execute("SELECT slug, name, state, latitude, longitude FROM locations"):
            print(*r, sep="\t")
        return

    con.row_factory = sqlite3.Row
    all_locs = [dict(r) for r in con.execute("SELECT * FROM locations ORDER BY id")]
    fx = Fetcher(con, args.command, {k: v for k, v in vars(args).items() if k != "db"})
    jobs = []   # (label, callable) - each job succeeds or fails on its own
    if args.command in ("forecast", "alerts"):
        locs = [load_location(con, args.location)]
    elif args.command == "backfill":
        bl = all_locs if args.location == "all" else [load_location(con, args.location)]
        jobs.append(("backfill", lambda: fx.backfill(bl, args.chunks)))
        locs = []
    elif args.command == "all":
        locs = all_locs
    else:
        locs = []
    for loc in locs:
        src = getattr(args, "source", "all")
        if args.command in ("forecast", "all") and src in ("open-meteo", "all"):
            jobs.append((f"{loc['slug']}: open-meteo forecast", lambda l=loc: fx.open_meteo_forecast(l)))
        if args.command in ("forecast", "all") and src in ("nws", "all"):
            jobs.append((f"{loc['slug']}: nws hourly forecast", lambda l=loc: fx.nws_hourly(l)))
        if args.command in ("alerts", "all"):
            jobs.append((f"{loc['slug']}: nws alerts", lambda l=loc: fx.nws_alerts(l)))
    if args.command in ("spc", "all"):
        for day in (getattr(args, "day", None) or [1, 2]):
            jobs.append((f"spc day {day}", lambda d=day: fx.spc_outlook(d, all_locs)))
    if args.command in ("all", "prune"):
        jobs.append(("prune", fx.prune))
    errors, done = [], []
    for label, fn in jobs:
        try:
            n = fn()
            done.append(label)
            print(f"{label}: {n}" + (" rows" if isinstance(n, int) else ""))
        except Exception as e:  # keep going; record the failure on the run
            con.rollback()
            errors.append(f"{label}: {e}")
            print(f"{label} FAILED: {e}", file=sys.stderr)
    if errors:
        status = "partial" if done else "error"
    else:
        status = "partial" if fx.warnings else "ok"
    fx.finish(status, "; ".join(errors + fx.warnings) or None)
    print(f"fetch_run {fx.run_id}: {status} ({fx.requests} requests, {fx.inserted} inserted, {fx.updated} updated)")
    sys.exit(0 if status == "ok" else 1)


if __name__ == "__main__":
    main()
