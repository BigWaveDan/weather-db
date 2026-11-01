# Weather DB

A free, zero-cost weather data pipeline and website. A Python script pulls
weather data from free, keyless public sources and merges it into one SQLite
file at `data/weather.db`, which is committed to this repo. A static two-page
website will read that same file. The frontend stays simple, and all the logic
lives in the fetcher.

## Locations

| slug | place | lat, lon |
|---|---|---|
| `worthington-mn` | Worthington, MN | 43.628733, -95.599702 |
| `brandon-sd` | Brandon, SD (Sioux Falls area) | 43.592908, -96.583769 |
| `tampa-fl` | Tampa, FL | 27.970086, -82.479673 |

The coordinates are the place internal points from the US Census Bureau 2024
Gazetteer.

## Sources (all free, no API keys)

- **Open-Meteo**: an hourly forecast covering 16 days (`temperature_2m`,
  `precipitation_probability`, `precipitation`, `wind_speed_10m`,
  `wind_direction_10m`, `wind_gusts_10m`), plus the Historical Weather API
  (hourly, back to 1940). The free tier is non-commercial and rate-limited.
- **National Weather Service** (`api.weather.gov`): `/points/{lat},{lon}`
  gives the forecast grid, which leads to the hourly forecast and the active
  alerts for that point. It requires a User-Agent with contact info, which
  this script sends.
- **Storm Prediction Center**: the Day 1 and Day 2 convective outlook text
  products (`day{1,2}otlk.txt`), plus the categorical GeoJSON layers
  (`day{1,2}otlk_cat.nolyr.geojson`). Each location's risk category is worked
  out with a standard-library ray-casting point-in-polygon check that
  respects holes.

The fetcher is polite with every source. It waits 1.5 s between requests,
then retries up to 5 times with exponential backoff (2, 4, 8, 16, 32 s plus
jitter) on HTTP 429, any 5xx, or a network error, and it honours
`Retry-After`.

## Usage

```bash
python init_db.py                                  # create/upgrade data/weather.db (idempotent)
python fetch.py all                                # EVERY current source, all 3 locations (~13-15 requests, ~20 s)
python fetch.py locations                          # list locations
python fetch.py forecast --location worthington-mn # Open-Meteo + NWS hourly forecast
python fetch.py forecast --location worthington-mn --source open-meteo
python fetch.py alerts   --location worthington-mn # NWS active alerts
python fetch.py spc [--day 1] [--day 2]            # SPC outlook text + per-location risk
python fetch.py backfill [--location X|all] [--chunks 3]  # walk history backward (30-day chunks)
python fetch.py prune                              # retention + VACUUM (also runs at the end of `all`)
```

`fetch.py all` runs these jobs in order:

1. For each location:
   - the Open-Meteo forecast
   - the NWS hourly forecast (the `/points` grid lookup is cached on
     `locations` and refreshed every 30 days)
   - NWS active alerts for the point
2. SPC Day 1 and Day 2.
3. `prune`, which applies the retention rules below.

Each job succeeds or fails on its own. A failure is recorded in
`fetch_runs.error` and marks the run `partial`, but the remaining jobs still
run. If an SPC categorical layer can't be reached, or its issue time doesn't
match the text, the outlook text is still stored. That outlook's
`layer_status` records the reason, no per-location risk rows are written for
it, and the run is marked `partial`. The script exits 0 only when the run
status is `ok`.

**SPC risk logic:**
- **Per location:** `spc_location_risk.risk` is the highest category whose
  polygon contains the point (TSTM < MRGL < SLGT < ENH < MDT < HIGH), or
  `NONE`.
- **National:** `spc_outlooks.max_risk` is the highest category in the layer.
  When the layer is unavailable, it falls back to the text headline ("...THERE
  IS A SLIGHT RISK..."), which can't tell TSTM from NONE, so it's left NULL in
  that case.
- **Re-fetching:** fetching the same issuance again updates `fetched_at`. It
  never downgrades an outlook whose layer already checked OK.

Every run adds a row to `fetch_runs`, and every HTTP response is saved
gzip-compressed, along with its SHA-256, in `raw_responses`.

## Historical backfill

`fetch.py backfill` reads the Open-Meteo **Historical Weather (archive) API**
(`archive-api.open-meteo.com/v1/archive`), hourly, in UTC. It pulls 7
variables: `temperature_2m`, `precipitation`, `wind_speed_10m`,
`wind_direction_10m`, `wind_gusts_10m`, `relative_humidity_2m`, and
`pressure_msl`. The last two are free variables on the same endpoint and
cost nothing extra.

**How it walks:**
- On the first run, it starts at **yesterday (UTC)** and moves **backward**
  in **30-day chunks** toward `backfill_progress.target_oldest_date`
  (1940-01-01).
- Each later run continues from the day before `oldest_date_fetched`.
- `--chunks` (default **3**) is the number of chunks per location per run.
  Locations take turns, one chunk each, so a stopped run leaves them all
  evenly advanced.
- `backfill_progress` is committed after **every** chunk, so an interrupted
  run picks up exactly where it stopped.
- Rows are upserted on `(location, time)`, so a re-fetch overwrites the old
  value and never creates a duplicate. Hours where every variable is null
  are skipped.

**Pacing and limits:**
- There's at least **2.5 s** between archive calls.
- On HTTP **429**, the backfill **stops immediately**. Progress is already
  saved, the run is marked `partial`, and the reason is stored in
  `backfill_progress.last_error`.
- A 5xx or network error on one chunk is recorded, that location stops for
  this run, and the other locations continue.
- Each run also has a hard cap of **300 weighted calls**.

**Open-Meteo free-tier limits** (non-commercial, from
https://open-meteo.com/en/terms, checked 2026-11-01):
- fewer than **600 calls per minute**
- **5,000 per hour**
- **10,000 per day**
- **300,000 per month**

A request with more than 10 variables, or covering more than 2 weeks for one
location, counts as several calls. Weight is fractional, roughly
`max(1, vars/10) × max(1, days/14)`. So a 30-day, 7-variable chunk counts as
about **2.1 calls**, and the default run (3 chunks × 3 locations) is about
**19 weighted calls**. The first 12-month slice was 36 chunks, about 77
weighted calls in 88 s.

**How far back it reaches:** 1940-01-01 is about 1,032 chunks per location,
or about 6,600 weighted calls in total. At the default pace, that takes
about 344 runs.

**Archive lag and the gap to today:**
- Open-Meteo documents ERA5 as "daily with **5 days delay**."
- The archive's default *best-match* model fills the most recent days from
  the ECMWF IFS analysis. On 2026-11-01, data through yesterday came back
  complete, but those last ~5 days may be **revised** when ERA5 catches up.
- The days between the archive's end and today are left out of
  `observations_hourly` on purpose. The forecast tables cover them.
- **Top-up:** if `newest_date_fetched` is earlier than yesterday when a
  backfill run starts, it first re-fetches from `newest_date_fetched − 6 days`
  through yesterday, one call per location. This keeps the recent edge
  current and picks up the ERA5 revisions.

**Storage warning:** each location-hour takes about 145 bytes, including the
index. The full history to 1940 is about 2.26M rows, roughly **330 MB**.
That is above GitHub's 100 MB per-file limit, so the committed single-file
DB can't hold the full history. A decision is needed before the backfill
passes about 25 years. The options are to split the DB by decade, keep old
years as daily aggregates, or keep full history outside git.

## Retention (`fetch.py prune`, also run at the end of `all`)

These rules keep the committed DB small:
- **`raw_responses` older than 7 days:** `body_gzip` is set to NULL and
  `body_pruned_at` is stamped. The metadata (url, status, time, size,
  **sha256**) is kept.
- **`forecast_hourly` snapshots older than 2 days:** only the newest
  snapshot per location + source in each **6-hour UTC window** of
  `fetched_at` is kept. The newest snapshot per location + source is never
  deleted, so `forecast_latest` stays correct even when fetching stops.
  Snapshots from the last 2 days are all kept.
- **Afterwards:** `VACUUM` reclaims the space.

## Website (static, zero-cost)

**Live site:** https://bigwavedan.github.io/weather-db/ (it redirects to `web/`)
- **History** (`web/index.html`): choose all locations or one, and a range
  (the last 7/30/90/365 days of data, or a custom range). It charts
  temperature, precipitation and wind (speed, with gusts dashed). Ranges up to
  31 days are hourly; longer ones use complete local-day aggregates. There's a
  °F/°C toggle, and it also switches wind to mph or km/h and precipitation to
  in or mm. A "data through" note shows how current the history is.
- **Map** (`web/map.html`): Leaflet with OSM tiles.
  - **Pins:** each pin shows the current temperature and a condition icon.
    Its ring color is the SPC Day 1 risk, and a red badge appears when NWS
    alerts are active.
  - **Detail card:** current conditions for the nearest forecast hour, 12-hour
    temperature and precip-chance sparklines, active alerts, SPC Day 1/2
    categories, and fetched-at times.

**How it stays simple:**
- No build step. Every library is **vendored** in `web/vendor/`: sql.js
  1.15.2 (WASM), uPlot 1.6.32 and Leaflet 1.9.4, with their licenses.
- Both pages download the **same** `data/weather.db` with sql.js and only
  run simple `SELECT`s on views. The DB is fetched with `cache: "no-cache"`,
  so the browser revalidates it with Pages' ETag and re-downloads only when a
  push has changed it.
- All the logic is in SQL or the fetcher:

| object | written by | used by |
|---|---|---|
| `observations_daily` | `fetch.py derive` (runs at the end of `backfill` and `all`). It aggregates complete **local** days (23 or more hours) using each location's IANA zone, so DST is handled | History, long ranges |
| `site_hourly_wide`, `site_daily_wide` | `init_db.py`, generated from `locations`. One row per timestamp and one column group per location | History charts |
| `site_data_status` | `schema.sql` | History "data through" note |
| `site_current_conditions` | `schema.sql`. Nearest hour from the latest Open-Meteo and NWS snapshots, the condition icon key (a CASE on the NWS shortForecast text), alert count, and SPC Day 1/2 risk with its color | Map pins and card |
| `site_forecast_upcoming` | `schema.sql`. The latest snapshot from the current hour on, ranked | Map sparklines |
| `site_active_alerts` | `schema.sql`. Alerts seen in the newest alerts fetch that haven't expired or ended | Map |
| `site_spc_risk`, `spc_categories` | `schema.sql`. The latest Day 1/2 risk per location; colors match SPC's own fills | Map |

Views that use `now` are evaluated by sql.js with the viewer's clock.
`locations.map_label_side` keeps the Worthington and Brandon pins, about
85 km apart, from overlapping.

**Deploy:** GitHub Pages serves `main` from the repo root. `.nojekyll`
serves files as they are. Run the fetcher, then commit and push, and the
site updates. Preview locally with `python -m http.server` from the repo
root, then open `/web/`.

## Schema (`schema.sql`)

| table | purpose |
|---|---|
| `locations` | the 3 points, plus the cached NWS grid, hourly-forecast URL, zone and county |
| `fetch_runs` | one row per run: start and end times, status (ok/partial/error), request, failure and row counts, error |
| `raw_responses` | every HTTP response: source, endpoint, url, fetched_at, http status, sha256, gzip body (pruned after 7 days) |
| `forecast_hourly` | hourly forecasts from `open-meteo` or `nws` |
| `forecast_latest` (view) | only the newest snapshot per location and source |
| `observations_hourly` | historical hourly data from the Open-Meteo archive: temperature, precipitation, wind speed/direction/gust, relative humidity, MSL pressure. Upserted, unique on (location, time) |
| `nws_alerts` | NWS alerts, upserted on the NWS alert id, with first_seen and last_seen |
| `spc_outlooks` | SPC Day 1/2 outlooks, unique on (day, issued): issued time (UTC), valid from and to, WMO header, full text, max risk, layer URL, layer issue time, layer status |
| `spc_location_risk` | per-location categorical risk for an outlook, with the SPC DN code and check time |
| `backfill_progress` | per location: the newest and oldest dates fetched, the target date (1940-01-01), chunk size, chunks fetched, done flag, last error |

**Forecast uniqueness policy: snapshots.** `forecast_hourly` is unique on
`(location, source, valid_time, fetched_at)`. Each fetch keeps its own rows,
which shows how a forecast changes over time. Query `forecast_latest` when
you only want the current forecast. Each `all` run adds about 1,620 forecast
rows, roughly 350 KB. The retention rules below thin old snapshots.

Units: °C, mm, km/h, degrees, and %. Times are stored in UTC ISO-8601 with a
trailing `Z`. In NWS rows, `precip_mm` and `wind_gust_kmh` are NULL because
the NWS hourly product doesn't include them. The NWS compass wind direction
is converted to degrees, and when a wind range is given, the upper value is
kept.

## Roadmap

1. ✅ **Scaffold**: the schema, and the fetcher for the Open-Meteo and NWS
   hourly forecast for Worthington.
2. ✅ **All three locations**: one `fetch.py all` call pulls everything for
   every location (forecasts, NWS alerts, SPC Day 1/2 text plus a risk
   check for each point).
3. ✅ **Historical backfill plus retention**: `fetch.py backfill` walks
   backward 30 days per chunk, paced and resumable. The first slice covers
   2025-11-06 to 2026-10-31 for all 3 locations. `fetch.py prune` keeps the
   DB small.
4. ✅ **Website**: a static two-page site, History and Map, that reads
   `data/weather.db` with sql.js. It's deployed on GitHub Pages.
5. Scheduled runs and daily fetch logs.

Weather data comes from Open-Meteo (open-meteo.com) (CC BY 4.0), NOAA/NWS, and NOAA/SPC.
