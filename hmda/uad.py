"""FHFA Uniform Appraisal Dataset (UAD) integration.

Sources (URLs verified against fhfa.gov, July 2026):
1. UAD Aggregate Statistics (v3.3): long-format CSVs by geography.
   Enterprise single-family reaches census tract; FHA bottoms out at county.
2. UAD Appraisal-Level PUF: 5% national samples (Enterprise 2013-2022,
   FHA 2017-2022).

Performance notes: the tract-level aggregate file expands to tens of
millions of rows. The loader therefore (a) keeps only rows relevant to the
valuation analyses (below-contract / count / median-value series, overall
characteristic rows, purchase or all purposes) and (b) bulk-inserts via a
DataFrame when the engine is DuckDB. Progress prints every chunk.

Loaders are header-driven; if FHFA changes a layout the error lists the
columns found. Update ALIASES/KEEP_SERIES_RE below rather than guessing.

Tables:
  uad_agg — channel, geolevel, geoid, series, purpose, group_name,
            group_value, year, quarter, value
  uad_puf — appraisal-level rows as-loaded (+ channel column)
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path

import requests

HEADERS = {"User-Agent": "hmda-fair-lending-research/1.0 (personal research use)"}

AGG_FILES = {
    "ent_sf_tract": "https://www.fhfa.gov/sites/default/files/2024-12/UADAggs_ent_sf_tract_v3_3.zip",
    "ent_sf_county": "https://www.fhfa.gov/sites/default/files/2024-12/UADAggs_ent_sf_county_v3_3.zip",
    "fha_sf_county": "https://www.fhfa.gov/sites/default/files/2024-12/UADAggs_fha_sf_county_v3_3.zip",
}
PUF_FILES = {
    "ent": "https://www.fhfa.gov/document/d/uad-al/ent_uad_puf_combined_csv_v2_1.zip",
    "fha": "https://www.fhfa.gov/document/d/uad-al/fha_uad_puf_combined_v1_0_csv.zip",
}

AGG_ALIASES = {
    "geoid": ["geoid", "fips", "geography_id", "geo_id", "tract", "county"],
    "geolevel": ["geolevel", "geo_level", "geographylevel", "geography"],
    "series": ["series", "seriesid", "series_id", "seriesname", "series_name", "metric"],
    "purpose": ["purpose", "appraisal_purpose", "loan_purpose"],
    "group_name": ["characteristic", "group", "dimension", "category_name"],
    "group_value": ["category", "subgroup", "group_value", "characteristic_value", "value_label"],
    "year": ["year", "yr"],
    "quarter": ["quarter", "qtr", "period"],
    "value": ["value", "estimate", "statistic"],
}
REQUIRED_AGG = ["geoid", "series", "year", "value"]

# Only these series families are used by the valuation module.
KEEP_SERIES_RE = re.compile(
    r"below|contract|count|number|volume|median", re.I)
# Keep only overall rows (no characteristic split) and purchase/all purpose.
KEEP_GROUP_RE = re.compile(r"^$|all|total|none", re.I)
KEEP_PURPOSE_RE = re.compile(r"^$|purchase|all|total", re.I)

AGG_COLS = ["channel", "geolevel", "geoid", "series", "purpose",
            "group_name", "group_value", "year", "quarter", "value"]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _map_columns(header, aliases):
    normed = [_norm(h) for h in header]
    out = {}
    for concept, cands in aliases.items():
        idx = None
        for c in cands:
            if c in normed:
                idx = normed.index(c)
                break
        if idx is None:
            for i, n in enumerate(normed):
                if any(c in n for c in cands):
                    idx = i
                    break
        if idx is not None:
            out[concept] = idx
    return out


def download(data_dir="data", include_puf=True):
    dest = Path(data_dir) / "uad"
    dest.mkdir(parents=True, exist_ok=True)
    files = dict(AGG_FILES)
    if include_puf:
        files.update({f"puf_{k}": v for k, v in PUF_FILES.items()})
    s = requests.Session()
    for key, url in files.items():
        out = dest / f"{key}.zip"
        if out.exists() and out.stat().st_size > 0:
            print(f"  {key}: cached")
            continue
        print(f"  {key}: downloading {url}", flush=True)
        with s.get(url, headers=HEADERS, timeout=1200, stream=True) as r:
            r.raise_for_status()
            tmp = out.with_suffix(".part")
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            tmp.replace(out)
        print(f"  {key}: {out.stat().st_size/1e6:.1f} MB")


def _iter_zip_csv(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.lower().endswith(".csv"):
                with z.open(name) as f:
                    text = io.TextIOWrapper(f, encoding="utf-8-sig",
                                            errors="replace")
                    yield name, csv.reader(text)


def _bulk_insert(conn, engine, table, cols, rows):
    """Fast path for DuckDB (DataFrame register), executemany for SQLite."""
    if not rows:
        return
    if engine == "duckdb":
        import pandas as pd
        df = pd.DataFrame(rows, columns=cols)
        conn.register("_bulk_tmp", df)
        conn.execute(f"INSERT INTO {table} SELECT * FROM _bulk_tmp")
        conn.unregister("_bulk_tmp")
    else:
        ph = ", ".join(["?"] * len(cols))
        conn.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)


def load_aggregates(db_path, data_dir="data"):
    from .db import connect
    conn, engine = connect(db_path)
    conn.execute("DROP TABLE IF EXISTS uad_agg")
    conn.execute(
        "CREATE TABLE uad_agg (channel TEXT, geolevel TEXT, geoid TEXT, "
        "series TEXT, purpose TEXT, group_name TEXT, group_value TEXT, "
        "year INTEGER, quarter TEXT, value DOUBLE)")
    dest = Path(data_dir) / "uad"
    total_kept = 0
    for key in AGG_FILES:
        zp = dest / f"{key}.zip"
        if not zp.exists():
            print(f"  {key}: zip missing, skipping (run the uad download)")
            continue
        channel = "fha" if key.startswith("fha") else "enterprise"
        for name, reader in _iter_zip_csv(zp):
            header = next(reader)
            cmap = _map_columns(header, AGG_ALIASES)
            missing = [c for c in REQUIRED_AGG if c not in cmap]
            if missing:
                raise SystemExit(
                    f"UAD aggregate layout changed in {name}: could not "
                    f"locate {missing}. Columns found: {header}. "
                    f"Update AGG_ALIASES in hmda/uad.py.")
            gi = {k: cmap.get(k) for k in AGG_ALIASES}

            def g(row, concept, default=""):
                i = gi.get(concept)
                return row[i].strip() if i is not None and i < len(row) else default

            buf, scanned, kept = [], 0, 0
            for row in reader:
                scanned += 1
                if scanned % 2_000_000 == 0:
                    print(f"    {name}: scanned {scanned:,} rows, "
                          f"kept {kept:,}", flush=True)
                series = g(row, "series")
                if not KEEP_SERIES_RE.search(series):
                    continue
                if not KEEP_GROUP_RE.search(g(row, "group_name")):
                    continue
                if not KEEP_PURPOSE_RE.search(g(row, "purpose")):
                    continue
                try:
                    val = float(g(row, "value"))
                    year = int(float(g(row, "year", "0")))
                except ValueError:
                    continue
                buf.append((channel, g(row, "geolevel"), g(row, "geoid"),
                            series, g(row, "purpose"), g(row, "group_name"),
                            g(row, "group_value"), year,
                            g(row, "quarter"), val))
                kept += 1
                if len(buf) >= 500_000:
                    _bulk_insert(conn, engine, "uad_agg", AGG_COLS, buf)
                    buf = []
            _bulk_insert(conn, engine, "uad_agg", AGG_COLS, buf)
            total_kept += kept
            print(f"  loaded {name} ({channel}): scanned {scanned:,}, "
                  f"kept {kept:,}", flush=True)
    if engine == "sqlite":
        conn.execute("CREATE INDEX IF NOT EXISTS ix_uadagg ON "
                     "uad_agg(geoid, series, year)")
        conn.commit()
    print(f"uad_agg: {total_kept:,} rows")
    conn.close()


def load_puf(db_path, data_dir="data"):
    from .db import connect
    conn, engine = connect(db_path)
    dest = Path(data_dir) / "uad"
    created = False
    created_header = None
    total = 0
    for channel, key in (("enterprise", "puf_ent"), ("fha", "puf_fha")):
        zp = dest / f"{key}.zip"
        if not zp.exists():
            print(f"  {key}: zip missing, skipping")
            continue
        for name, reader in _iter_zip_csv(zp):
            header = [_norm(h) for h in next(reader)]
            if not created:
                cols_sql = ", ".join(f'"{c}" TEXT' for c in header)
                conn.execute("DROP TABLE IF EXISTS uad_puf")
                conn.execute(f"CREATE TABLE uad_puf (channel TEXT, {cols_sql})")
                created_header = header
                created = True
            buf, n = [], 0
            hmap = {h: i for i, h in enumerate(header)}
            for row in reader:
                rec = [channel] + [
                    row[hmap[c]].strip() if c in hmap and hmap[c] < len(row)
                    else "" for c in created_header]
                buf.append(rec)
                n += 1
                if len(buf) >= 250_000:
                    _bulk_insert(conn, engine, "uad_puf",
                                 ["channel"] + created_header, buf)
                    buf = []
                    print(f"    {name}: {n:,} rows", flush=True)
            _bulk_insert(conn, engine, "uad_puf",
                         ["channel"] + created_header, buf)
            total += n
            print(f"  loaded {name} ({channel}): {n:,} rows", flush=True)
    if created and engine == "sqlite":
        conn.commit()
    print(f"uad_puf: {total:,} rows")
    conn.close()


def run_all(db_path, data_dir="data", include_puf=True):
    download(data_dir, include_puf)
    load_aggregates(db_path, data_dir)
    if include_puf:
        load_puf(db_path, data_dir)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--db", default="data/hmda.db")
    p.add_argument("--skip-puf", action="store_true")
    a = p.parse_args()
    run_all(a.db, a.data_dir, include_puf=not a.skip_puf)
