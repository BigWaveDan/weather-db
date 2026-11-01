-- Weather DB schema (SQLite). Covers the whole project, not just milestone 1.
-- All timestamps are ISO-8601 strings in UTC with a trailing 'Z' unless noted.
PRAGMA foreign_keys = ON;

-- The weather points we track. Coordinates: US Census Bureau 2024 Gazetteer
-- (place internal points, INTPTLAT/INTPTLONG).
CREATE TABLE IF NOT EXISTS locations (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,          -- e.g. worthington-mn
    name            TEXT NOT NULL,
    state           TEXT NOT NULL,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    timezone        TEXT NOT NULL,                 -- IANA zone, e.g. America/Chicago
    coord_source    TEXT NOT NULL,
    -- Filled by the NWS /points lookup (cached so we don't re-query every run)
    nws_office      TEXT,
    nws_grid_x      INTEGER,
    nws_grid_y      INTEGER,
    nws_forecast_hourly_url TEXT,
    nws_forecast_zone TEXT,
    nws_county      TEXT,
    nws_points_checked_at TEXT,
    map_label_side  TEXT NOT NULL DEFAULT 'center'   -- site pin label placement: left | right | center
);

-- One row per invocation of fetch.py (any sub-command).
CREATE TABLE IF NOT EXISTS fetch_runs (
    id              INTEGER PRIMARY KEY,
    command         TEXT NOT NULL,                 -- forecast | alerts | spc | backfill | all
    args            TEXT,                          -- JSON of CLI args
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running', -- running | ok | partial | error
    requests_made   INTEGER NOT NULL DEFAULT 0,
    requests_failed INTEGER NOT NULL DEFAULT 0,
    rows_inserted   INTEGER NOT NULL DEFAULT 0,
    rows_updated    INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

-- Every HTTP response we get back, for provenance/replay. Body is stored
-- gzip-compressed with its SHA-256 so duplicates are cheap to spot.
CREATE TABLE IF NOT EXISTS raw_responses (
    id              INTEGER PRIMARY KEY,
    fetch_run_id    INTEGER REFERENCES fetch_runs(id),
    location_id     INTEGER REFERENCES locations(id),
    source          TEXT NOT NULL,                 -- open-meteo | open-meteo-archive | nws | spc
    endpoint        TEXT NOT NULL,                 -- forecast | archive | points | forecast_hourly | alerts | outlook_day1 ...
    url             TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    http_status     INTEGER,
    content_type    TEXT,
    body_sha256     TEXT,
    body_bytes      INTEGER,
    body_gzip       BLOB,                          -- NULL once pruned (see retention in README)
    body_pruned_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_source ON raw_responses(source, endpoint, fetched_at);

-- Hourly forecasts. Policy: SNAPSHOT — every fetch keeps its own rows, unique on
-- (location, source, valid_time, fetched_at). This preserves how forecasts evolve
-- over time; use the forecast_latest view for "the current forecast".
-- Units: temp °C, precip mm, wind km/h, direction degrees, probability %.
CREATE TABLE IF NOT EXISTS forecast_hourly (
    id              INTEGER PRIMARY KEY,
    location_id     INTEGER NOT NULL REFERENCES locations(id),
    source          TEXT NOT NULL CHECK (source IN ('open-meteo','nws')),
    valid_time      TEXT NOT NULL,                 -- start of the hour, UTC
    temperature_c   REAL,
    precip_prob_pct REAL,
    precip_mm       REAL,                          -- NWS hourly has no amount -> NULL
    wind_speed_kmh  REAL,
    wind_dir_deg    REAL,
    wind_gust_kmh   REAL,                          -- NWS hourly usually has no gust -> NULL
    short_forecast  TEXT,                          -- NWS text, e.g. "Mostly Cloudy"
    fetched_at      TEXT NOT NULL,
    fetch_run_id    INTEGER REFERENCES fetch_runs(id),
    raw_response_id INTEGER REFERENCES raw_responses(id),
    UNIQUE (location_id, source, valid_time, fetched_at)
);
CREATE INDEX IF NOT EXISTS ix_fc_lookup ON forecast_hourly(location_id, source, valid_time);

CREATE VIEW IF NOT EXISTS forecast_latest AS
SELECT f.* FROM forecast_hourly f
JOIN (SELECT location_id, source, MAX(fetched_at) AS fetched_at
      FROM forecast_hourly GROUP BY location_id, source) m
  ON m.location_id = f.location_id AND m.source = f.source AND m.fetched_at = f.fetched_at;

-- Historical hourly data (Open-Meteo Historical Weather API, default 'best match' reanalysis:
-- ERA5 family, with ECMWF IFS analysis filling the most recent days until ERA5 catches up).
-- Upsert: one row per location+hour; a later fetch overwrites (archive data can be revised).
CREATE TABLE IF NOT EXISTS observations_hourly (
    id              INTEGER PRIMARY KEY,
    location_id     INTEGER NOT NULL REFERENCES locations(id),
    time            TEXT NOT NULL,                 -- start of the hour, UTC
    temperature_c   REAL,
    precip_mm       REAL,
    wind_speed_kmh  REAL,
    wind_dir_deg    REAL,
    wind_gust_kmh   REAL,
    relative_humidity_pct REAL,
    pressure_msl_hpa REAL,
    source          TEXT NOT NULL DEFAULT 'open-meteo-archive',
    fetched_at      TEXT NOT NULL,
    raw_response_id INTEGER REFERENCES raw_responses(id),
    UNIQUE (location_id, time)
);

-- NWS active alerts. Upsert on the NWS alert id; last_seen_at tracks when it was still active.
CREATE TABLE IF NOT EXISTS nws_alerts (
    id              TEXT PRIMARY KEY,              -- NWS alert @id / urn
    location_id     INTEGER REFERENCES locations(id),
    event           TEXT,
    severity        TEXT,
    certainty       TEXT,
    urgency         TEXT,
    headline        TEXT,
    description     TEXT,
    instruction     TEXT,
    area_desc       TEXT,
    sent            TEXT,
    onset           TEXT,
    expires         TEXT,
    ends            TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    raw_json        TEXT
);

-- SPC convective outlooks (day 1 / day 2 text products).
CREATE TABLE IF NOT EXISTS spc_outlooks (
    id              INTEGER PRIMARY KEY,
    day             INTEGER NOT NULL CHECK (day IN (1,2,3)),
    issued          TEXT NOT NULL,                 -- issuance time as parsed from the product (UTC)
    product_url     TEXT,
    text            TEXT NOT NULL,
    max_risk        TEXT,                          -- highest category named in the text headline:
                                                   -- TSTM|MRGL|SLGT|ENH|MDT|HIGH, or NONE
    valid_from      TEXT,                          -- UTC, from the "Valid DDHHMMZ - DDHHMMZ" line
    valid_to        TEXT,
    wmo_header      TEXT,                          -- e.g. "ACUS01 KWNS 011954"
    layer_url       TEXT,                          -- categorical GeoJSON used for the point check
    layer_issued    TEXT,                          -- ISSUE_ISO from the GeoJSON (UTC)
    layer_status    TEXT,                          -- ok | unreachable: <reason> | mismatch: <reason>
    fetched_at      TEXT NOT NULL,                 -- last time this issuance was seen
    raw_response_id INTEGER REFERENCES raw_responses(id),
    UNIQUE (day, issued)
);

-- Per-location categorical risk for an outlook, from a stdlib point-in-polygon
-- (ray casting, holes respected) against SPC's categorical GeoJSON layer.
-- risk = highest containing category (TSTM|MRGL|SLGT|ENH|MDT|HIGH) or NONE.
-- No row is written when the layer was unreachable (text-only outlook).
CREATE TABLE IF NOT EXISTS spc_location_risk (
    outlook_id      INTEGER NOT NULL REFERENCES spc_outlooks(id),
    location_id     INTEGER NOT NULL REFERENCES locations(id),
    risk            TEXT NOT NULL,
    risk_dn         INTEGER,                       -- SPC DN code (2=TSTM ... 8=HIGH), 0 for NONE
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (outlook_id, location_id)
);

-- Backward walk for historical data: oldest_date_fetched moves toward 1940
-- in small paced chunks; newest_date_fetched records where the walk started.
CREATE TABLE IF NOT EXISTS backfill_progress (
    location_id         INTEGER PRIMARY KEY REFERENCES locations(id),
    newest_date_fetched TEXT,                      -- YYYY-MM-DD
    oldest_date_fetched TEXT,                      -- YYYY-MM-DD
    target_oldest_date  TEXT NOT NULL DEFAULT '1940-01-01',
    chunk_days          INTEGER NOT NULL DEFAULT 30,
    updated_at          TEXT,
    done                INTEGER NOT NULL DEFAULT 0,
    chunks_fetched      INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '4');

-- Seed locations (US Census Bureau 2024 Gazetteer place internal points).
INSERT OR IGNORE INTO locations (slug, name, state, latitude, longitude, timezone, coord_source) VALUES
 ('worthington-mn', 'Worthington', 'MN', 43.628733, -95.599702, 'America/Chicago',  'US Census 2024 Gazetteer (GEOID 2771734)'),
 ('brandon-sd',     'Brandon',     'SD', 43.592908, -96.583769, 'America/Chicago',  'US Census 2024 Gazetteer (GEOID 4606840)'),
 ('tampa-fl',       'Tampa',       'FL', 27.970086, -82.479673, 'America/New_York', 'US Census 2024 Gazetteer (GEOID 1271000)');

-- Worthington and Brandon are ~85 km apart: put their pin labels on opposite sides.
UPDATE locations SET map_label_side = CASE slug WHEN 'brandon-sd' THEN 'left' WHEN 'worthington-mn' THEN 'right'
                                                ELSE map_label_side END;

INSERT OR IGNORE INTO backfill_progress (location_id)
SELECT id FROM locations;

-- =====================================================================
-- Website support (milestone 4). The static site only runs simple SELECTs
-- against these; every bit of logic lives here or in fetch.py.
-- Views that use 'now' are evaluated by sql.js with the viewer's clock.
-- =====================================================================

-- Daily aggregates per LOCAL calendar day, rebuilt by `fetch.py derive`
-- (also at the end of `backfill` and `all`) using each location's IANA zone.
CREATE TABLE IF NOT EXISTS observations_daily (
    location_id     INTEGER NOT NULL REFERENCES locations(id),
    date            TEXT NOT NULL,                 -- YYYY-MM-DD, location-local
    hours           INTEGER NOT NULL,              -- hourly rows in that day (23-25 on DST days)
    temp_min_c      REAL,
    temp_max_c      REAL,
    temp_avg_c      REAL,
    precip_mm       REAL,
    wind_avg_kmh    REAL,
    wind_max_kmh    REAL,
    gust_max_kmh    REAL,
    PRIMARY KEY (location_id, date)
);

-- SPC categorical legend (colors from SPC's own GeoJSON fill values).
CREATE TABLE IF NOT EXISTS spc_categories (
    label TEXT PRIMARY KEY, dn INTEGER NOT NULL, name TEXT NOT NULL, color TEXT NOT NULL, text_color TEXT NOT NULL
);
INSERT OR REPLACE INTO spc_categories VALUES
 ('NONE', 0, 'No thunderstorms', '#9AA0A6', '#111111'),
 ('TSTM', 2, 'General thunderstorms', '#C1E9C1', '#111111'),
 ('MRGL', 3, 'Marginal', '#66A366', '#FFFFFF'),
 ('SLGT', 4, 'Slight', '#FFE066', '#111111'),
 ('ENH', 5, 'Enhanced', '#FFA366', '#111111'),
 ('MDT', 6, 'Moderate', '#E06666', '#FFFFFF'),
 ('HIGH', 8, 'High', '#EE99EE', '#111111');

DROP VIEW IF EXISTS site_data_status;
CREATE VIEW site_data_status AS
SELECT l.id AS location_id, l.slug, l.name, l.state,
       (SELECT MIN(time) FROM observations_hourly o WHERE o.location_id = l.id) AS history_from,
       (SELECT MAX(time) FROM observations_hourly o WHERE o.location_id = l.id) AS history_through,
       (SELECT MAX(date) FROM observations_daily d WHERE d.location_id = l.id) AS history_through_local_date,
       (SELECT MAX(fetched_at) FROM forecast_hourly f WHERE f.location_id = l.id) AS forecast_fetched_at,
       (SELECT MAX(finished_at) FROM fetch_runs WHERE status IN ('ok','partial')) AS last_run_finished_at
FROM locations l;

-- Latest NWS alerts that are still active: seen in the location's most recent alerts
-- fetch and not yet expired/ended.
DROP VIEW IF EXISTS site_active_alerts;
CREATE VIEW site_active_alerts AS
SELECT a.location_id, a.id, a.event, a.severity, a.urgency, a.certainty, a.headline,
       a.onset, a.expires, a.ends, a.last_seen_at,
       CASE a.severity WHEN 'Extreme' THEN 4 WHEN 'Severe' THEN 3 WHEN 'Moderate' THEN 2
                       WHEN 'Minor' THEN 1 ELSE 0 END AS severity_rank
FROM nws_alerts a
WHERE a.last_seen_at >= (SELECT MAX(r.fetched_at) FROM raw_responses r
                         WHERE r.source = 'nws' AND r.endpoint = 'alerts' AND r.location_id = a.location_id
                           AND r.http_status = 200) 
  AND julianday(COALESCE(a.ends, a.expires, '9999-12-31')) > julianday('now');

-- SPC risk per location for the latest Day 1 and Day 2 issuance.
DROP VIEW IF EXISTS site_spc_risk;
CREATE VIEW site_spc_risk AS
SELECT r.location_id, o.day, o.issued, o.valid_from, o.valid_to, o.max_risk AS national_max_risk,
       o.layer_status, r.risk, c.name AS risk_name, c.color AS risk_color, c.text_color AS risk_text_color
FROM spc_outlooks o
JOIN (SELECT day, MAX(issued) AS issued FROM spc_outlooks GROUP BY day) m ON m.day = o.day AND m.issued = o.issued
JOIN spc_location_risk r ON r.outlook_id = o.id
LEFT JOIN spc_categories c ON c.label = r.risk;

-- Forecast hours from the latest snapshot, ranked from the current hour (rank 0) onward.
DROP VIEW IF EXISTS site_forecast_upcoming;
CREATE VIEW site_forecast_upcoming AS
SELECT f.*, CAST(strftime('%s', f.valid_time) AS INTEGER) AS epoch,
       ROW_NUMBER() OVER (PARTITION BY f.location_id, f.source ORDER BY f.valid_time) - 1 AS hour_rank
FROM forecast_latest f
WHERE f.valid_time >= strftime('%Y-%m-%dT%H:00:00Z', 'now');


-- One row per location: everything a map pin needs.
DROP VIEW IF EXISTS site_current_conditions;
CREATE VIEW site_current_conditions AS
WITH om AS (
    SELECT * FROM (
        SELECT f.*, ROW_NUMBER() OVER (PARTITION BY f.location_id
               ORDER BY ABS(julianday(f.valid_time) + 1.0/48 - julianday('now'))) AS rn
        FROM forecast_latest f WHERE f.source = 'open-meteo') WHERE rn = 1),
nws AS (
    SELECT * FROM (
        SELECT f.*, ROW_NUMBER() OVER (PARTITION BY f.location_id
               ORDER BY ABS(julianday(f.valid_time) + 1.0/48 - julianday('now'))) AS rn
        FROM forecast_latest f WHERE f.source = 'nws') WHERE rn = 1)
SELECT l.id AS location_id, l.slug, l.name, l.state, l.latitude, l.longitude, l.timezone, l.map_label_side,
       om.valid_time, om.temperature_c, om.precip_prob_pct, om.precip_mm,
       om.wind_speed_kmh, om.wind_dir_deg, om.wind_gust_kmh, om.fetched_at AS om_fetched_at,
       nws.temperature_c AS nws_temperature_c, nws.short_forecast, nws.fetched_at AS nws_fetched_at,
       CASE
         WHEN nws.short_forecast IS NULL THEN 'unknown'
         WHEN lower(nws.short_forecast) LIKE '%thunder%' OR lower(nws.short_forecast) LIKE '%t-storm%' THEN 'storm'
         WHEN lower(nws.short_forecast) LIKE '%snow%' OR lower(nws.short_forecast) LIKE '%flurr%'
           OR lower(nws.short_forecast) LIKE '%sleet%' OR lower(nws.short_forecast) LIKE '%freezing%' THEN 'snow'
         WHEN lower(nws.short_forecast) LIKE '%rain%' OR lower(nws.short_forecast) LIKE '%shower%'
           OR lower(nws.short_forecast) LIKE '%drizzle%' THEN 'rain'
         WHEN lower(nws.short_forecast) LIKE '%fog%' OR lower(nws.short_forecast) LIKE '%haze%'
           OR lower(nws.short_forecast) LIKE '%smoke%' THEN 'fog'
         WHEN lower(nws.short_forecast) LIKE '%partly%' OR lower(nws.short_forecast) LIKE '%mostly sunny%'
           OR lower(nws.short_forecast) LIKE '%mostly clear%' THEN 'partly'
         WHEN lower(nws.short_forecast) LIKE '%cloudy%' OR lower(nws.short_forecast) LIKE '%overcast%' THEN 'cloudy'
         WHEN lower(nws.short_forecast) LIKE '%sunny%' THEN 'sunny'
         WHEN lower(nws.short_forecast) LIKE '%clear%' THEN 'clear'
         WHEN lower(nws.short_forecast) LIKE '%wind%' OR lower(nws.short_forecast) LIKE '%breezy%' THEN 'wind'
         ELSE 'cloudy' END AS condition_key,
       (SELECT COUNT(*) FROM site_active_alerts a WHERE a.location_id = l.id) AS alert_count,
       (SELECT MAX(severity_rank) FROM site_active_alerts a WHERE a.location_id = l.id) AS alert_max_severity_rank,
       d1.risk AS spc_day1_risk, d1.risk_name AS spc_day1_name, d1.risk_color AS spc_day1_color,
       d1.risk_text_color AS spc_day1_text_color, d1.issued AS spc_day1_issued,
       d2.risk AS spc_day2_risk, d2.risk_name AS spc_day2_name, d2.risk_color AS spc_day2_color,
       d2.risk_text_color AS spc_day2_text_color, d2.issued AS spc_day2_issued,
       (SELECT MAX(last_seen_at) FROM nws_alerts WHERE location_id = l.id) AS alerts_last_seen_at,
       (SELECT MAX(r.fetched_at) FROM raw_responses r WHERE r.source='nws' AND r.endpoint='alerts'
          AND r.location_id = l.id AND r.http_status = 200) AS alerts_checked_at
FROM locations l
LEFT JOIN om ON om.location_id = l.id
LEFT JOIN nws ON nws.location_id = l.id
LEFT JOIN site_spc_risk d1 ON d1.location_id = l.id AND d1.day = 1
LEFT JOIN site_spc_risk d2 ON d2.location_id = l.id AND d2.day = 2;
