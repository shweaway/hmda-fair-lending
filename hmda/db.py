"""Storage layer: load Modified LAR text files into an analysis database.

Primary engine: DuckDB (fast columnar analytics; handles the full national
file easily). Automatic fallback: SQLite (stdlib) so the pipeline runs
anywhere. The loader parses each pipe-delimited file, adds derived analysis
columns, and bulk-inserts.

Derived columns added at load time:
  group_re        derived race/ethnicity analysis group (see constants)
  is_denied       action_taken == 3
  in_decision     action_taken in (1,2,3)  -> standard denial-rate universe
  is_originated   action_taken == 1
  loan_amount_n, income_n, rate_spread_n, interest_rate_n,
  total_loan_costs_n, property_value_n     numeric parses (None if NA/Exempt)
  pricing_exempt  1 if rate_spread is Exempt (partial-exemption filer)
  tract11         cleaned 11-digit census tract key for census joins
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from .constants import COLUMNS, N_COLUMNS, derive_group, parse_numeric, is_exempt

DERIVED = [
    "group_re", "is_denied", "in_decision", "is_originated",
    "loan_amount_n", "income_n", "rate_spread_n", "interest_rate_n",
    "total_loan_costs_n", "property_value_n", "pricing_exempt", "tract11",
]
ALL_COLS = COLUMNS + DERIVED


def connect(db_path: str | Path):
    """Return (conn, engine) where engine is 'duckdb' or 'sqlite'."""
    db_path = str(db_path)
    try:
        import duckdb  # noqa
        conn = duckdb.connect(db_path)
        return conn, "duckdb"
    except ImportError:
        conn = sqlite3.connect(db_path)
        try:  # WAL is faster but unsupported on some network filesystems
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        return conn, "sqlite"


def _create_tables(conn, engine: str) -> None:
    cols_sql = ", ".join(f'"{c}" TEXT' for c in COLUMNS)
    num = ["loan_amount_n", "income_n", "rate_spread_n", "interest_rate_n",
           "total_loan_costs_n", "property_value_n"]
    derived_sql = (
        '"group_re" TEXT, "is_denied" INTEGER, "in_decision" INTEGER, '
        '"is_originated" INTEGER, '
        + ", ".join(f'"{c}" DOUBLE' for c in num)
        + ', "pricing_exempt" INTEGER, "tract11" TEXT'
    )
    conn.execute(f"CREATE TABLE IF NOT EXISTS lar ({cols_sql}, {derived_sql})")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS institutions ("
        "lei TEXT PRIMARY KEY, name TEXT, activity_year TEXT, "
        "rows INTEGER, pricing_exempt_share DOUBLE)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tracts ("
        "tract11 TEXT PRIMARY KEY, state_fips TEXT, county_fips TEXT, "
        "total_pop DOUBLE, minority_pct DOUBLE, median_income DOUBLE, "
        "minority_band TEXT, income_quintile_state INTEGER)"
    )


def _derive_row(row: list[str]) -> list:
    d = dict(zip(COLUMNS, row))
    action = (d["action_taken"] or "").strip()
    group = derive_group(
        d["applicant_ethnicity_1"],
        [d["applicant_race_1"], d["applicant_race_2"]],
    )
    tract = (d["census_tract"] or "").strip()
    tract11 = tract if len(tract) == 11 and tract.isdigit() else None
    return row + [
        group,
        1 if action == "3" else 0,
        1 if action in ("1", "2", "3") else 0,
        1 if action == "1" else 0,
        parse_numeric(d["loan_amount"]),
        parse_numeric(d["income"]),
        parse_numeric(d["rate_spread"]),
        parse_numeric(d["interest_rate"]),
        parse_numeric(d["total_loan_costs"]),
        parse_numeric(d["property_value"]),
        1 if is_exempt(d["rate_spread"]) else 0,
        tract11,
    ]


def load_files(db_path: str | Path, data_dir: str | Path = "data",
               year: int = 2025, batch: int = 50_000) -> None:
    """Parse every raw file for `year` and load into the database."""
    raw = Path(data_dir) / "raw" / str(year)
    files = sorted(raw.glob("*.txt"))
    if not files:
        raise SystemExit(f"No raw files in {raw} — run the download step first.")

    conn, engine = connect(db_path)
    _create_tables(conn, engine)
    conn.execute("DELETE FROM lar WHERE activity_year = ?", [str(year)])

    placeholders = ", ".join(["?"] * len(ALL_COLS))
    insert = f"INSERT INTO lar VALUES ({placeholders})"

    names = {}
    roster = Path(data_dir) / f"roster_{year}.json"
    if roster.exists():
        names = {f["lei"]: f.get("name", "")
                 for f in json.loads(roster.read_text(encoding="utf-8"))}

    total = 0
    inst_rows = []
    for i, fp in enumerate(files, 1):
        lei = fp.stem
        buf, n_rows, n_exempt = [], 0, 0
        with fp.open(encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter="|")
            for row in reader:
                if len(row) != N_COLUMNS:
                    if row and row[0] == "activity_year":  # header row
                        continue
                    if len(row) > 1:  # malformed; skip but keep going
                        continue
                    continue
                full = _derive_row(row)
                buf.append(full)
                n_rows += 1
                n_exempt += full[-2]  # pricing_exempt
                if len(buf) >= batch:
                    conn.executemany(insert, buf)
                    buf = []
        if buf:
            conn.executemany(insert, buf)
        total += n_rows
        inst_rows.append((lei, names.get(lei, ""), str(year), n_rows,
                          (n_exempt / n_rows) if n_rows else None))
        if i % 200 == 0 or i == len(files):
            print(f"  loaded {i}/{len(files)} institutions "
                  f"({total:,} rows)", flush=True)
            if engine == "sqlite":
                conn.commit()

    conn.execute("DELETE FROM institutions WHERE activity_year = ?", [str(year)])
    conn.executemany(
        "INSERT OR REPLACE INTO institutions VALUES (?,?,?,?,?)"
        if engine == "sqlite" else
        "INSERT OR REPLACE INTO institutions VALUES (?,?,?,?,?)",
        inst_rows,
    )
    if engine == "sqlite":
        conn.commit()
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_lar_lei ON lar(lei)",
            "CREATE INDEX IF NOT EXISTS ix_lar_group ON lar(group_re)",
            "CREATE INDEX IF NOT EXISTS ix_lar_tract ON lar(tract11)",
            "CREATE INDEX IF NOT EXISTS ix_lar_county ON lar(county_code)",
        ):
            conn.execute(stmt)
        conn.commit()
    print(f"Done: {total:,} LAR rows from {len(files)} institutions "
          f"-> {db_path} [{engine}]")
    conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--db", default="data/hmda.db")
    a = p.parse_args()
    load_files(a.db, a.data_dir, a.year)
