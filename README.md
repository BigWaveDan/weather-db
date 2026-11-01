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
  products.

The fetcher is polite with every source. It waits 1.5 s between requests,
then retries up to 5 times with exponential backoff (2, 4, 8, 16, 32 s plus
jitter) on HTTP 429, any 5xx, or a network error, and it honours
`Retry-After`.

## Usage

```bash
python init_db.py                                  # create/upgrade data/weather.db (idempotent)
python fetch.py locations                          # list locations
python fetch.py forecast --location worthington-mn # Open-Meteo + NWS hourly forecast
python fetch.py forecast --location worthington-mn --source open-meteo
python fetch.py alerts   --location worthington-mn # NWS active alerts
```

Every run adds a row to `fetch_runs`, and every HTTP response is saved
gzip-compressed, along with its SHA-256, in `raw_responses`.

## Schema (`schema.sql`)

| table | purpose |
|---|---|
| `locations` | the 3 points, plus the cached NWS grid, hourly-forecast URL, zone and county |
| `fetch_runs` | one row per run: start and end times, status (ok/partial/error), request, failure and row counts, error |
| `raw_responses` | every HTTP response: source, endpoint, url, fetched_at, http status, sha256, gzip body |
| `forecast_hourly` | hourly forecasts from `open-meteo` or `nws` |
| `forecast_latest` (view) | only the newest snapshot per location and source |
| `observations_hourly` | historical hourly data, upserted, unique on (location, time) |
| `nws_alerts` | NWS alerts, upserted on the NWS alert id, with first_seen and last_seen |
| `spc_outlooks` | SPC Day 1/2 outlook text: issued time, text, max risk (TSTM/MRGL/SLGT/ENH/MDT/HIGH) when it can be parsed |
| `spc_location_risk` | per-location SPC risk for an outlook |
| `backfill_progress` | per location: the newest and oldest dates fetched so far for the backward walk, the target date (1940-01-01), chunk size, and whether it's done |

**Forecast uniqueness policy: snapshots.** `forecast_hourly` is unique on
`(location, source, valid_time, fetched_at)`. Each fetch keeps its own rows,
which shows how a forecast changes over time. Query `forecast_latest` when
you only want the current forecast.

Units: °C, mm, km/h, degrees, and %. Times are stored in UTC ISO-8601 with a
trailing `Z`. In NWS rows, `precip_mm` and `wind_gust_kmh` are NULL because
the NWS hourly product doesn't include them. The NWS compass wind direction
is converted to degrees, and when a wind range is given, the upper value is
kept.

## Roadmap

1. ✅ **Scaffold**: the schema, and the fetcher for the Open-Meteo and NWS
   hourly forecast for Worthington.
2. **All three locations**: one `fetch.py all` call pulls everything for
   every location (forecasts, NWS alerts, SPC Day 1/2).
3. **Historical backfill**: start at today and walk backward in small chunks
   (for example 30 days per location per run), pacing the requests and
   recording progress in `backfill_progress`. There is no single giant
   backfill.
4. **Website** (static, reads `data/weather.db`):
   - *Historical page*: charts of temperature, precipitation and wind over
     time, filterable by location.
   - *Map page*: pins at the three locations showing current conditions and
     active alerts.
5. Scheduled runs and daily fetch logs.

Weather data comes from Open-Meteo (CC BY 4.0), NOAA/NWS, and NOAA/SPC.
