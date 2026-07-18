"""Census-tract demographics for redlining analysis.

The Modified LAR gives each loan's 11-digit census tract but no tract
demographics (unlike the CFPB Snapshot dataset, which ships derived census
fields). This module builds a `tracts` table from the Census Bureau ACS
5-year API:

  B03002_001E  total population
  B03002_003E  non-Hispanic White population
  B19013_001E  median household income

minority_pct = 100 * (1 - white_nh / total_pop). One API call per state
(free; a census.gov API key is optional but raises rate limits — set env
CENSUS_API_KEY). Results cached to data/census_tracts_{vintage}.csv.

Bands follow the conventions used in redlining analysis:
  <10%, 10-30%, 30-50%, 50-80% (majority-minority), 80-100%.

Income: tracts are ranked into quintiles *within their state* as a
pragmatic LMI proxy. The FFIEC's official LMI definition compares tract
median family income to MSA/MD median family income; if you need exam-grade
LMI flags, import the FFIEC Census flat file instead (see README).
"""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path

import requests

STATE_FIPS = [
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12", "13", "15",
    "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27",
    "28", "29", "30", "31", "32", "33", "34", "35", "36", "37", "38", "39",
    "40", "41", "42", "44", "45", "46", "47", "48", "49", "50", "51", "53",
    "54", "55", "56", "72",  # includes DC (11) and Puerto Rico (72)
]

API = ("https://api.census.gov/data/{vintage}/acs/acs5"
       "?get=B03002_001E,B03002_003E,B19013_001E"
       "&for=tract:*&in=state:{state}")


def band(minority_pct: float | None) -> str | None:
    if minority_pct is None:
        return None
    if minority_pct < 10: return "<10%"
    if minority_pct < 30: return "10-30%"
    if minority_pct < 50: return "30-50%"
    if minority_pct < 80: return "50-80%"
    return "80-100%"


def fetch_tracts(vintage: int = 2023, cache_dir: str | Path = "data") -> Path:
    """Download tract demographics for all states; returns cached CSV path."""
    cache = Path(cache_dir) / f"census_tracts_{vintage}.csv"
    if cache.exists() and cache.stat().st_size > 1000:
        print(f"Census cache found: {cache}")
        return cache
    key = os.environ.get("CENSUS_API_KEY", "")
    rows = []
    for st in STATE_FIPS:
        url = API.format(vintage=vintage, state=st)
        if key:
            url += f"&key={key}"
        for attempt in range(4):
            try:
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        header, body = data[0], data[1:]
        idx = {h: i for i, h in enumerate(header)}
        for rec in body:
            total = _f(rec[idx["B03002_001E"]])
            white = _f(rec[idx["B03002_003E"]])
            mhi = _f(rec[idx["B19013_001E"]])
            if mhi is not None and mhi < 0:  # census sentinel -666666666
                mhi = None
            tract11 = rec[idx["state"]] + rec[idx["county"]] + rec[idx["tract"]]
            pct = None
            if total and total > 0 and white is not None:
                pct = round(100.0 * (1.0 - white / total), 2)
            rows.append([tract11, rec[idx["state"]],
                         rec[idx["state"]] + rec[idx["county"]],
                         total, pct, mhi])
        print(f"  state {st}: {len(body)} tracts", flush=True)
        time.sleep(0.2)
    with cache.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tract11", "state_fips", "county_fips",
                    "total_pop", "minority_pct", "median_income"])
        w.writerows(rows)
    print(f"Saved {len(rows):,} tracts -> {cache}")
    return cache


def _f(v):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def load_tracts(db_path: str | Path, cache_csv: str | Path) -> None:
    """Load the census CSV into the `tracts` table with bands + quintiles."""
    from .db import connect, _create_tables
    import pandas as pd

    df = pd.read_csv(cache_csv, dtype={"tract11": str, "state_fips": str,
                                       "county_fips": str})
    df["minority_band"] = df["minority_pct"].map(
        lambda p: band(p) if p == p else None)
    df["income_quintile_state"] = (
        df.groupby("state_fips")["median_income"]
          .transform(lambda s: pd.qcut(s.rank(method="first"), 5,
                                       labels=False, duplicates="drop") + 1)
    )
    conn, engine = connect(db_path)
    _create_tables(conn, engine)
    conn.execute("DELETE FROM tracts")
    recs = df[["tract11", "state_fips", "county_fips", "total_pop",
               "minority_pct", "median_income", "minority_band",
               "income_quintile_state"]].where(df.notna(), None).values.tolist()
    conn.executemany(
        "INSERT OR REPLACE INTO tracts VALUES (?,?,?,?,?,?,?,?)", recs)
    if engine == "sqlite":
        conn.commit()
    print(f"Loaded {len(recs):,} tracts into {db_path}")
    conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vintage", type=int, default=2023,
                   help="ACS 5-year vintage (2023 = 2019-2023)")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--db", default="data/hmda.db")
    a = p.parse_args()
    path = fetch_tracts(a.vintage, a.data_dir)
    load_tracts(a.db, path)
