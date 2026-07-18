"""FHFA Uniform Appraisal Dataset (UAD) integration.

Two public sources (URLs verified against fhfa.gov, July 2026):

1. UAD **Aggregate Statistics** (v3.3, Dec 2024): long-format CSVs of
   appraisal statistics by geography. Enterprise single-family goes down to
   census tract; FHA single-family bottoms out at county.
2. UAD **Appraisal-Level PUF**: 5% national random samples of appraisal
   records. Enterprise v2.1 covers 2013-2022; FHA v1.0 covers 2017-2022.

Because FHFA revises file layouts between versions, the loaders are
header-driven: they normalize column names and locate *concepts* (geoid,
year, series, value, ...) through alias lists. If a concept can't be found
the loader stops with a message listing the columns it saw — update
ALIASES below rather than guessing.

Tract-vintage caveat: census tract boundaries changed with the 2020
census. Joins between UAD tract records and the ACS tracts table are exact
for recent years but noisy for earlier vintages; trend analyses therefore
prefer UAD's own neighborhood groupings where present.

Tables created:
  uad_agg  — long format: channel, geolevel, geoid, series, purpose,
             group_name, group_value, year, quarter, value
  uad_puf  — appraisal-level records as-loaded (normalized lowercase
             columns), plus channel
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path

import requests

HEADERS = {"User-Agent": "hmda-fair-lending-research/1.0 (personal research use)"}

# ---------------------------------------------------------------------------
# File catalog. If FHFA bumps a version, update these URLs (see
# https://www.fhfa.gov/data/uad and https://www.fhfa.gov/data/uad/puf).
# ---------------------------------------------------------------------------
AGG_FILES = {
    "ent_sf_tract": "https://www.fhfa.gov/sites/default/files/2024-12/UADAggs_ent_sf_tract_v3_3.zip",
    "ent_sf_county": "https://www.fhfa.gov/sites/default/files/2024-12/UADAggs_ent_sf_county_v3_3.zip",
    "fha_sf_county": "https://www.fhfa.gov/sites/default/files/2024-12/UADAggs_fha_sf_county_v3_3.zip",
}
PUF_FILES = {
    "ent": "https://www.fhfa.gov/document/d/uad-al/ent_uad_puf_combined_csv_v2_1.zip",
    "fha": "https://www.fhfa.gov/document/d/uad-al/fha_uad_puf_combined_v1_0_csv.zip",
}

# Concept -> candidate normalized column names (checked in order, then by
# substring). Normalization: lowercase, non-alphanumeric -> underscore.
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


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _map_columns(header: list[str], aliases: dict) -> dict:
    """Return concept -> actual column index, using exact then substring
    matches on normalized names."""
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


def download(data_dir: str | Path = "data", include_puf: bool = True) -> None:
    """Fetch the UAD zips (skips files already present)."""
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
        print(f"  {key}: downloading {url}")
        with s.get(url, headers=HEADERS, timeout=1200, stream=True) as r:
            r.raise_for_status()
            tmp = out.with_suffix(".part")
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            tmp.replace(out)
        print(f"  {key}: {out.stat().st_size/1e6:.1f} MB")


def _iter_zip_csv(zip_path: Path):
    """Yield (name, csv.reader) for each CSV inside a zip."""
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.lower().endswith(".csv"):
                with z.open(name) as f:
                    text = io.TextIOWrapper(f, encoding="utf-8-sig",
                                            errors="replace")
                    yield name, csv.reader(text)


def load_aggregates(db_path: str | Path, data_dir: str | Path = "data") -> None:
    from .db import connect
    conn, engine = connect(db_path)
    conn.execute("DROP TABLE IF EXISTS uad_agg")
    conn.execute(
        "CREATE TABLE uad_agg (channel TEXT, geolevel TEXT, geoid TEXT, "
        "series TEXT, purpose TEXT, group_name TEXT, group_value TEXT, "
        "year INTEGER, quarter TEXT, value DOUBLE)")
    insert = "INSERT INTO uad_agg VALUES (?,?,?,?,?,?,?,?,?,?)"

    dest = Path(data_dir) / "uad"
    total = 0
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
            buf = []
            for row in reader:
                def g(concept, default=None):
                    i = cmap.get(concept)
                    return row[i].strip() if i is not None and i < len(row) else default
                raw_val = g("value", "")
                try:
                    val = float(raw_val)
                except (TypeError, ValueError):
                    continue  # suppressed / non-numeric cells
                try:
                    year = int(float(g("year", "0")))
                except ValueError:
                    continue
                buf.append((channel, g("geolevel"), g("geoid"), g("series"),
                            g("purpose"), g("group_name"), g("group_value"),
                            year, g("quarter"), val))
                if len(buf) >= 50_000:
                    conn.executemany(insert, buf)
                    total += len(buf)
                    buf = []
            if buf:
                conn.executemany(insert, buf)
                total += len(buf)
            print(f"  loaded {name} ({channel})", flush=True)
    if engine == "sqlite":
        conn.execute("CREATE INDEX IF NOT EXISTS ix_uadagg ON "
                     "uad_agg(geoid, series, year)")
        conn.commit()
    print(f"uad_agg: {total:,} rows")
    conn.close()


def load_puf(db_path: str | Path, data_dir: str | Path = "data") -> None:
    from .db import connect
    conn, engine = connect(db_path)
    dest = Path(data_dir) / "uad"
    created = False
    total = 0
    for channel, key in (("enterprise", "puf_ent"), ("fha", "puf_fha")):
        zp = dest / f"{key}.zip"
        if not zp.exists():
            print(f"  {key}: zip missing, skipping")
            continue
        for name, reader in _iter_zip_csv(zp):
            header = [_norm(h) for h in next(reader)]
            if not created:
                cols = ", ".join(f'"{c}" TEXT' for c in header)
                conn.execute("DROP TABLE IF EXISTS uad_puf")
                conn.execute(f"CREATE TABLE uad_puf (channel TEXT, {cols})")
                created_header = header
                created = True
            elif header != created_header:
                # different layout between channels: align on shared columns
                header = [h if h in created_header else None for h in header]
            ph = ", ".join(["?"] * (len(created_header) + 1))
            insert = f"INSERT INTO uad_puf VALUES ({ph})"
            buf = []
            for row in reader:
                rec = dict(zip([h for h in header], row))
                buf.append([channel] + [rec.get(c, "") for c in created_header])
                if len(buf) >= 50_000:
                    conn.executemany(insert, buf)
                    total += len(buf)
                    buf = []
            if buf:
                conn.executemany(insert, buf)
                total += len(buf)
            print(f"  loaded {name} ({channel})", flush=True)
    if created and engine == "sqlite":
        conn.commit()
    print(f"uad_puf: {total:,} rows")
    conn.close()


def run_all(db_path: str | Path, data_dir: str | Path = "data",
            include_puf: bool = True) -> None:
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
