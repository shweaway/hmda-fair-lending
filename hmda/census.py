"""Census-tract demographics for redlining analysis.

Builds a `tracts` table from the Census Bureau ACS 5-year API:
  B03002_001E  total population
  B03002_003E  non-Hispanic White population
  B19013_001E  median household income

minority_pct = 100 * (1 - white_nh / total_pop). One API call per state.
Each state's result is cached to data/census_parts_{vintage}/{fips}.csv as
it completes, so an interrupted or rate-limited run resumes where it left
off. The merged file lands at data/census_tracts_{vintage}.csv.

The API requires a key (free, instant — anonymous access was disabled):
https://api.census.gov/data/key_signup.html — then
  setx CENSUS_API_KEY yourkey
and reopen the terminal.

Bands follow redlining-analysis convention: <10%, 10-30%, 30-50%,
50-80% (majority-minority), 80-100%. Income quintiles are within-state
(a pragmatic LMI proxy; see README).
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
    "54", "55", "56", "72",
]

API = ("https://api.census.gov/data/{vintage}/acs/acs5"
       "?get=B03002_001E,B03002_003E,B19013_001E"
       "&for=tract:*&in=state:{state}")

HEADERS = {"User-Agent": "hmda-fair-lending-research/1.0"}

CACHE_HEADER = ["tract11", "state_fips", "county_fips",
                "total_pop", "minority_pct", "median_income"]


def band(minority_pct: float | None) -> str | None:
    if minority_pct is None:
        return None
    if minority_pct < 10: return "<10%"
    if minority_pct < 30: return "10-30%"
    if minority_pct < 50: return "30-50%"
    if minority_pct < 80: return "50-80%"
    return "80-100%"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_state(st: str, vintage: int, key: str) -> list[list]:
    url = API.format(vintage=vintage, state=st)
    if key:
        url += f"&key={key}"
    last_err = None
    for attempt in range(5):
        try:
            r = requests.get(url, headers=HEADERS, timeout=120)
            r.raise_for_status()
            data = r.json()
            header, body = data[0], data[1:]
            idx = {h: i for i, h in enumerate(header)}
            rows = []
            for rec in body:
                total = _f(rec[idx["B03002_001E"]])
                white = _f(rec[idx["B03002_003E"]])
                mhi = _f(rec[idx["B19013_001E"]])
                if mhi is not None and mhi < 0:  # census sentinel
                    mhi = None
                tract11 = (rec[idx["state"]] + rec[idx["county"]]
                           + rec[idx["tract"]])
                pct = None
                if total and total > 0 and white is not None:
                    pct = round(100.0 * (1.0 - white / total), 2)
                rows.append([tract11, rec[idx["state"]],
                             rec[idx["state"]] + rec[idx["county"]],
                             total, pct, mhi])
            return rows
        except ValueError as e:  # JSON decode failure: API sent HTML/text
            body_snip = r.text[:300].replace("\n", " ")
            last_err = RuntimeError(
                f"Census API returned non-JSON for state {st} "
                f"(HTTP {r.status_code}): {body_snip!r}\n"
                "The API requires a key ('Missing Key' pages mean the "
                "request was anonymous); otherwise this is rate limiting "
                "or a temporary Census outage. Get a free key: "
                "https://api.census.gov/data/key_signup.html then "
                "`setx CENSUS_API_KEY yourkey` and reopen the terminal. "
                "Progress is cached per state — just rerun this step.")
        except requests.RequestException as e:
            last_err = e
        time.sleep(3 * (attempt + 1))
    raise last_err


def _cache_ok(cache: Path) -> bool:
    """A usable cache has the expected header row and at least one data row.

    Size checks reject the small bundled demo cache; checking the header
    also catches truncated/corrupt files from interrupted merges.
    """
    if not cache.exists():
        return False
    try:
        with cache.open(newline="", encoding="utf-8") as f:
            r = csv.reader(f)
            if next(r, None) != CACHE_HEADER:
                return False
            return next(r, None) is not None
    except OSError:
        return False


def fetch_tracts(vintage: int = 2023, cache_dir: str | Path = "data") -> Path:
    """Download tract demographics for all states; returns merged CSV path.

    Resumable: completed states are cached in census_parts_{vintage}/ and
    skipped on rerun.
    """
    cache = Path(cache_dir) / f"census_tracts_{vintage}.csv"
    if _cache_ok(cache):
        print(f"Census cache found: {cache}")
        return cache
    parts = Path(cache_dir) / f"census_parts_{vintage}"
    parts.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("CENSUS_API_KEY", "")
    if not key:
        print("Note: no CENSUS_API_KEY set — api.census.gov requires a key "
              "and anonymous requests fail with a 'Missing Key' error. "
              "Get a free key: https://api.census.gov/data/key_signup.html")
    for st in STATE_FIPS:
        part = parts / f"{st}.csv"
        if part.exists() and part.stat().st_size > 0:
            continue
        rows = _fetch_state(st, vintage, key)
        tmp = part.with_suffix(".part")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        tmp.replace(part)
        print(f"  state {st}: {len(rows)} tracts", flush=True)
        time.sleep(0.3)
    # merge
    n = 0
    with cache.open("w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(CACHE_HEADER)
        for st in STATE_FIPS:
            with (parts / f"{st}.csv").open(encoding="utf-8") as f:
                for row in csv.reader(f):
                    w.writerow(row)
                    n += 1
    print(f"Saved {n:,} tracts -> {cache}")
    return cache


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
    sub = df[["tract11", "state_fips", "county_fips", "total_pop",
              "minority_pct", "median_income", "minority_band",
              "income_quintile_state"]]
    # astype(object) BEFORE where(), otherwise NaN survives in float
    # columns and DuckDB refuses to cast nan -> INTEGER
    sub = sub.astype(object).where(pd.notna(sub), None)
    recs = sub.values.tolist()
    conn.executemany(
        "INSERT OR REPLACE INTO tracts VALUES (?,?,?,?,?,?,?,?)", recs)
    if engine == "sqlite":
        conn.commit()
    print(f"Loaded {len(recs):,} tracts into {db_path}")
    conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vintage", type=int, default=2023)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--db", default="data/hmda.db")
    a = p.parse_args()
    path = fetch_tracts(a.vintage, a.data_dir)
    load_tracts(a.db, path)
