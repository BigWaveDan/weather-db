#!/usr/bin/env python3
"""Create/upgrade data/weather.db from schema.sql (idempotent)."""
import pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "weather.db"


def init_db(db_path=DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript((ROOT / "schema.sql").read_text())
    con.commit()
    return con


if __name__ == "__main__":
    con = init_db(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH)
    for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"):
        print(n)
