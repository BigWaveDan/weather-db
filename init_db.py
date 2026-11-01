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
    con.commit()
    return con


if __name__ == "__main__":
    con = init_db(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH)
    for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"):
        print(n)
