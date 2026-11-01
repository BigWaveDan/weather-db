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
    nws_points_checked_at TEXT
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
INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '3');

-- Seed locations (US Census Bureau 2024 Gazetteer place internal points).
INSERT OR IGNORE INTO locations (slug, name, state, latitude, longitude, timezone, coord_source) VALUES
 ('worthington-mn', 'Worthington', 'MN', 43.628733, -95.599702, 'America/Chicago',  'US Census 2024 Gazetteer (GEOID 2771734)'),
 ('brandon-sd',     'Brandon',     'SD', 43.592908, -96.583769, 'America/Chicago',  'US Census 2024 Gazetteer (GEOID 4606840)'),
 ('tampa-fl',       'Tampa',       'FL', 27.970086, -82.479673, 'America/New_York', 'US Census 2024 Gazetteer (GEOID 1271000)');

INSERT OR IGNORE INTO backfill_progress (location_id)
SELECT id FROM locations;
