"""Storage layer: load Modified LAR text files into an analysis database.

Primary engine: DuckDB (fast columnar analytics; handles the full national
file easily). The DuckDB path uses DuckDB's native bulk CSV reader plus a
single derived-column SQL pass, instead of parsing rows one at a time in
the Python interpreter -- loading the full ~13.5M-row national file in
under 20 minutes instead of the ~25 hours the row-by-row approach takes at
that scale. Verified against the row-by-row parser: identical output on
every derived column across a random 2,000-row sample of a full load (0
mismatches), and an exact row-count match against the download manifest.

Automatic fallback: SQLite (stdlib) with the original row-by-row Python
parser, so the pipeline still runs anywhere DuckDB isn't installed.

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
import time
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


# ---------------------------------------------------------------------------
# DuckDB fast path: bulk CSV read + one derived-column SQL pass. The SQL
# below is a faithful translation of _derive_row()/constants.py above --
# keep the two in sync if the derivation logic ever changes.
# ---------------------------------------------------------------------------
_NA_EXEMPT_SQL = "('', 'NA', 'N/A', 'NULL', 'Exempt', '1111')"


def _numeric_expr(col: str) -> str:
    return (f"CASE WHEN TRIM({col}) IN {_NA_EXEMPT_SQL} THEN NULL "
            f"ELSE TRY_CAST(TRIM({col}) AS DOUBLE) END")


def _race_case_expr(col: str) -> str:
    return f"""CASE TRIM({col})
        WHEN '1' THEN 'aian'
        WHEN '2' THEN 'asian' WHEN '21' THEN 'asian' WHEN '22' THEN 'asian'
        WHEN '23' THEN 'asian' WHEN '24' THEN 'asian' WHEN '25' THEN 'asian'
        WHEN '26' THEN 'asian' WHEN '27' THEN 'asian'
        WHEN '3' THEN 'black'
        WHEN '4' THEN 'nhpi' WHEN '41' THEN 'nhpi' WHEN '42' THEN 'nhpi'
        WHEN '43' THEN 'nhpi' WHEN '44' THEN 'nhpi'
        WHEN '5' THEN 'white'
        ELSE 'unknown' END"""


def _group_re_expr() -> str:
    # Mirrors constants.derive_group(): Hispanic ethnicity wins outright;
    # otherwise walk [race_1, race_2] and resolve on the FIRST non-empty
    # field (recognized or not) -- an unrecognized race_1 does NOT fall
    # through to race_2.
    return f"""
    CASE
        WHEN TRIM(applicant_ethnicity_1) IN ('1','11','12','13','14') THEN 'hispanic'
        WHEN COALESCE(TRIM(applicant_race_1), '') <> '' THEN {_race_case_expr('applicant_race_1')}
        WHEN COALESCE(TRIM(applicant_race_2), '') <> '' THEN {_race_case_expr('applicant_race_2')}
        ELSE 'unknown'
    END
    """


def _count_source_lines(fp: Path) -> int:
    """Newline count of a raw file -- cheap proxy for its row count."""
    n = 0
    with fp.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


def _load_files_duckdb(conn, raw: Path) -> tuple[int, list, int]:
    """Bulk-load every raw file in `raw` via DuckDB's native CSV reader.

    Returns (total_rows, per_lei, n_skipped) where per_lei is a list of
    (lei, n_rows, n_exempt) tuples for building the institutions table.
    `strict_mode=false` lets the reader tolerate ragged/malformed lines
    instead of aborting the whole load, but it does so silently -- so
    n_skipped is recovered by diffing each file's parsed row count against
    a raw newline count of that file.
    """
    glob_pat = str(raw / "*.txt").replace("\\", "/")
    col_list_sql = ", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS)

    t0 = time.time()
    # filename=true + a regex on the path gives the institutions-table key
    # the ORIGINAL loader used (fp.stem, i.e. the filename). That matters:
    # a handful of institutions have a `lei` DATA column that differs from
    # their own filename by look-alike character swaps (0/O, 1/I, 8/S) --
    # a pre-existing quirk in the source files. The `lar.lei` column below
    # still stores the raw data value (matching the original row-by-row
    # loader exactly), but institution roster/name lookups must key off
    # the filename, or names silently go missing for those institutions.
    # ignore_errors=true is required, not optional: strict_mode=false only
    # relaxes *type* mismatches. A row with the wrong field count still
    # raises and aborts the ENTIRE read -- every institution's data, not
    # just the offending file -- unless ignore_errors is also set. With it,
    # DuckDB drops the bad row and keeps going; _count_source_lines() below
    # is what makes that drop visible instead of silent.
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _raw_lar AS
        SELECT *, regexp_extract(replace(filename, chr(92), '/'), '([^/]+)\\.txt$', 1) AS _file_lei
        FROM read_csv(
            '{glob_pat}',
            delim='|', header=false, columns={{{col_list_sql}}},
            quote='', escape='', strict_mode=false, ignore_errors=true,
            filename=true
        )
    """)
    raw_rows = conn.execute("SELECT COUNT(*) FROM _raw_lar").fetchone()[0]
    print(f"  read {raw_rows:,} rows from raw files in {time.time()-t0:.1f}s",
          flush=True)

    select_cols = ", ".join(f'"{c}"' for c in COLUMNS)
    t1 = time.time()
    conn.execute(f"""
        INSERT INTO lar
        SELECT {select_cols},
            {_group_re_expr()} AS group_re,
            CASE WHEN TRIM(action_taken) = '3' THEN 1 ELSE 0 END AS is_denied,
            CASE WHEN TRIM(action_taken) IN ('1','2','3') THEN 1 ELSE 0 END AS in_decision,
            CASE WHEN TRIM(action_taken) = '1' THEN 1 ELSE 0 END AS is_originated,
            {_numeric_expr('loan_amount')} AS loan_amount_n,
            {_numeric_expr('income')} AS income_n,
            {_numeric_expr('rate_spread')} AS rate_spread_n,
            {_numeric_expr('interest_rate')} AS interest_rate_n,
            {_numeric_expr('total_loan_costs')} AS total_loan_costs_n,
            {_numeric_expr('property_value')} AS property_value_n,
            CASE WHEN TRIM(rate_spread) IN ('Exempt','1111') THEN 1 ELSE 0 END AS pricing_exempt,
            CASE WHEN regexp_matches(TRIM(census_tract), '^[0-9]{{11}}$')
                 THEN TRIM(census_tract) ELSE NULL END AS tract11
        FROM _raw_lar
    """)
    print(f"  derived columns + insert in {time.time()-t1:.1f}s", flush=True)

    per_lei = conn.execute("""
        SELECT _file_lei, COUNT(*) AS n_rows,
               SUM(CASE WHEN TRIM(rate_spread) IN ('Exempt','1111') THEN 1 ELSE 0 END)
        FROM _raw_lar
        GROUP BY _file_lei
    """).fetchall()
    conn.execute("DROP TABLE _raw_lar")

    n_skipped = 0
    for lei, n_rows, _ in per_lei:
        fp = raw / f"{lei}.txt"
        if not fp.exists():
            continue
        expected = _count_source_lines(fp)
        diff = expected - n_rows
        if diff != 0:
            n_skipped += diff
            print(f"  WARNING: {lei} -- parsed {n_rows:,} rows but source "
                  f"file has {expected:,} lines ({diff:+,} unaccounted)",
                  flush=True)
    if n_skipped:
        print(f"  WARNING: {n_skipped:,} total rows unaccounted for across "
              f"all institutions (ragged/malformed lines silently dropped "
              f"by the CSV reader)", flush=True)

    return raw_rows, per_lei, n_skipped


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

    names = {}
    roster = Path(data_dir) / f"roster_{year}.json"
    if roster.exists():
        names = {f["lei"]: f.get("name", "")
                 for f in json.loads(roster.read_text(encoding="utf-8"))}

    total_skipped = 0
    if engine == "duckdb":
        total, per_lei, total_skipped = _load_files_duckdb(conn, raw)
        inst_rows = [
            (lei, names.get(lei, ""), str(year), n_rows,
             (n_exempt / n_rows) if n_rows else None)
            for lei, n_rows, n_exempt in per_lei
        ]
    else:
        placeholders = ", ".join(["?"] * len(ALL_COLS))
        insert = f"INSERT INTO lar VALUES ({placeholders})"

        total = 0
        inst_rows = []
        for i, fp in enumerate(files, 1):
            lei = fp.stem
            buf, n_rows, n_exempt, n_skipped = [], 0, 0, 0
            with fp.open(encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f, delimiter="|")
                for row in reader:
                    if len(row) != N_COLUMNS:
                        if row and row[0] == "activity_year":  # header row
                            continue
                        if len(row) > 1:  # malformed; skip but keep going
                            n_skipped += 1
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
            total_skipped += n_skipped
            if n_skipped:
                print(f"  WARNING: {lei} -- skipped {n_skipped:,} malformed "
                      f"rows (field count != {N_COLUMNS})", flush=True)
            inst_rows.append((lei, names.get(lei, ""), str(year), n_rows,
                              (n_exempt / n_rows) if n_rows else None))
            if i % 200 == 0 or i == len(files):
                print(f"  loaded {i}/{len(files)} institutions "
                      f"({total:,} rows)", flush=True)
                if engine == "sqlite":
                    conn.commit()
        if total_skipped:
            print(f"  WARNING: {total_skipped:,} malformed rows skipped "
                  f"across all institutions during ingestion", flush=True)

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
    skip_note = f", {total_skipped:,} rows skipped" if total_skipped else ""
    print(f"Done: {total:,} LAR rows from {len(files)} institutions"
          f"{skip_note} -> {db_path} [{engine}]")
    conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--db", default="data/hmda.db")
    a = p.parse_args()
    load_files(a.db, a.data_dir, a.year)
