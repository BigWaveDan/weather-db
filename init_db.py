#!/usr/bin/env python3
"""Create/upgrade data/weather.db from schema.sql (idempotent)."""
import pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "weather.db"

# Columns added after schema v1, applied to existing databases with ALTER TABLE.
MIGRATIONS = {
    "spc_outlooks": [("valid_from", "TEXT"), ("valid_to", "TEXT"), ("wmo_header", "TEXT"),
                     ("layer_url", "TEXT"), ("layer_issued", "TEXT"), ("layer_status", "TEXT")],
    "spc_location_risk": [("risk_dn", "INTEGER"), ("checked_at", "TEXT")],
    "observations_hourly": [("relative_humidity_pct", "REAL"), ("pressure_msl_hpa", "REAL")],
    "backfill_progress": [("chunks_fetched", "INTEGER NOT NULL DEFAULT 0"), ("last_error", "TEXT")],
    "raw_responses": [("body_pruned_at", "TEXT")],
    "locations": [("map_label_side", "TEXT NOT NULL DEFAULT 'center'")],
}


def init_db(db_path=DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if have:
            for name, typ in cols:
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    con.executescript((ROOT / "schema.sql").read_text())
    create_wide_views(con)
    con.commit()
    return con


def create_wide_views(con):
    """Pivot views for the site's charts: one row per timestamp, one column group per location
    (columns named <slug with _>_<metric>), so the frontend never has to join or align series.
    Regenerated from the locations table every time init_db runs."""
    locs = con.execute("SELECT id, slug FROM locations ORDER BY id").fetchall()
    hourly, daily = [], []
    for lid, slug in locs:
        k = slug.replace("-", "_")
        for col, name in [("temperature_c", "temp_c"), ("precip_mm", "precip_mm"),
                          ("wind_speed_kmh", "wind_kmh"), ("wind_gust_kmh", "gust_kmh")]:
            hourly.append(f"MAX(CASE WHEN location_id={lid} THEN {col} END) AS {k}_{name}")
        for col in ["temp_min_c", "temp_max_c", "temp_avg_c", "precip_mm", "wind_avg_kmh",
                    "wind_max_kmh", "gust_max_kmh"]:
            daily.append(f"MAX(CASE WHEN location_id={lid} THEN {col} END) AS {k}_{col}")
    con.execute("DROP VIEW IF EXISTS site_hourly_wide")
    con.execute("CREATE VIEW site_hourly_wide AS SELECT time, CAST(strftime('%s', time) AS INTEGER) AS epoch, "
                + ", ".join(hourly) + " FROM observations_hourly GROUP BY time")
    con.execute("DROP VIEW IF EXISTS site_daily_wide")
    con.execute("CREATE VIEW site_daily_wide AS SELECT date, CAST(strftime('%s', date || 'T12:00:00Z') AS INTEGER) AS epoch, "
                + ", ".join(daily) + " FROM observations_daily GROUP BY date")


if __name__ == "__main__":
    con = init_db(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH)
    for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"):
        print(n)
