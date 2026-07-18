"""ACS language data for campaign localization (markets module).

Builds a `county_language` table from ACS 5-year table C16001 — language
spoken at home for the population 5 and over, by ability to speak English.
Variable IDs verified against the live metadata endpoint
(https://api.census.gov/data/2023/acs/acs5/groups/C16001.json), July 2026.

For each language group the table stores total speakers and LEP speakers
(those who speak English less than "very well") — the standard
limited-English-proficiency measure used in language-access planning.
One API call covers every county in the country; the result is cached to
data/county_language_{vintage}.csv.

The Census data API requires a (free, instant) key:
https://api.census.gov/data/key_signup.html then
  setx CENSUS_API_KEY yourkey
and reopen the terminal.
"""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "hmda-fair-lending-research/1.0"}

# key -> (label, total speakers variable, LEP variable)
LANGUAGES = {
    "spanish":         ("Spanish", "C16001_003E", "C16001_005E"),
    "french_haitian":  ("French/Haitian/Cajun", "C16001_006E", "C16001_008E"),
    "german":          ("German/West Germanic", "C16001_009E", "C16001_011E"),
    "slavic":          ("Russian/Polish/Slavic", "C16001_012E", "C16001_014E"),
    "other_indo_euro": ("Other Indo-European", "C16001_015E", "C16001_017E"),
    "korean":          ("Korean", "C16001_018E", "C16001_020E"),
    "chinese":         ("Chinese (Mandarin/Cantonese)",
                        "C16001_021E", "C16001_023E"),
    "vietnamese":      ("Vietnamese", "C16001_024E", "C16001_026E"),
    "tagalog":         ("Tagalog (incl. Filipino)",
                        "C16001_027E", "C16001_029E"),
    "other_asian_pi":  ("Other Asian/Pacific Island",
                        "C16001_030E", "C16001_032E"),
    "arabic":          ("Arabic", "C16001_033E", "C16001_035E"),
    "other":           ("Other/unspecified", "C16001_036E", "C16001_038E"),
}

_BASE_VARS = ["NAME", "C16001_001E", "C16001_002E"]  # name, pop 5+, English only


def _all_vars() -> list[str]:
    out = list(_BASE_VARS)
    for _, total, lep in LANGUAGES.values():
        out += [total, lep]
    return out


def _f(v):
    try:
        x = float(v)
        return None if x < 0 else x  # negative = census sentinel
    except (TypeError, ValueError):
        return None


def fetch_language(vintage: int = 2023,
                   cache_dir: str | Path = "data") -> Path:
    """Download county-level language data for the whole country (one call).

    Returns the cached CSV path; reuses the cache when present.
    """
    cache = Path(cache_dir) / f"county_language_{vintage}.csv"
    # size > 100 (not more): the synthetic demo cache is only a few rows
    if cache.exists() and cache.stat().st_size > 100:
        print(f"Language cache found: {cache}")
        return cache
    key = os.environ.get("CENSUS_API_KEY", "")
    if not key:
        raise SystemExit(
            "The Census data API requires an API key (free, instant): "
            "https://api.census.gov/data/key_signup.html — then "
            "`setx CENSUS_API_KEY yourkey` and reopen the terminal.")
    url = (f"https://api.census.gov/data/{vintage}/acs/acs5"
           f"?get={','.join(_all_vars())}&for=county:*&key={key}")
    last_err = None
    for attempt in range(5):
        try:
            r = requests.get(url, headers=HEADERS, timeout=180)
            r.raise_for_status()
            data = r.json()
            break
        except ValueError:
            snip = r.text[:300].replace("\n", " ")
            last_err = RuntimeError(
                f"Census API returned non-JSON (HTTP {r.status_code}): "
                f"{snip!r} — usually rate limiting or an invalid key.")
        except requests.RequestException as e:
            last_err = e
        time.sleep(3 * (attempt + 1))
    else:
        raise last_err

    header, body = data[0], data[1:]
    idx = {h: i for i, h in enumerate(header)}
    missing = [v for v in _all_vars() if v not in idx]
    if missing:
        raise RuntimeError(
            f"Census response missing variables {missing}; columns found: "
            f"{header}. Update LANGUAGES in hmda/language.py.")

    cols = ["county_fips", "county_name", "pop5plus", "english_only"]
    for k in LANGUAGES:
        cols += [f"{k}_total", f"{k}_lep"]
    tmp = cache.with_suffix(".part")
    n = 0
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for rec in body:
            row = [rec[idx["state"]] + rec[idx["county"]],
                   rec[idx["NAME"]],
                   _f(rec[idx["C16001_001E"]]),
                   _f(rec[idx["C16001_002E"]])]
            for _, total, lep in LANGUAGES.values():
                row += [_f(rec[idx[total]]), _f(rec[idx[lep]])]
            w.writerow(row)
            n += 1
    tmp.replace(cache)
    print(f"Saved language data for {n:,} counties -> {cache}")
    return cache


def load_language(db_path: str | Path, cache_csv: str | Path) -> None:
    """Load the language CSV into the `county_language` table."""
    from .db import connect
    import pandas as pd

    df = pd.read_csv(cache_csv, dtype={"county_fips": str})
    conn, engine = connect(db_path)
    num_cols = [c for c in df.columns
                if c not in ("county_fips", "county_name")]
    cols_sql = ('"county_fips" TEXT PRIMARY KEY, "county_name" TEXT, '
                + ", ".join(f'"{c}" DOUBLE' for c in num_cols))
    conn.execute(f"CREATE TABLE IF NOT EXISTS county_language ({cols_sql})")
    conn.execute("DELETE FROM county_language")
    sub = df.astype(object).where(pd.notna(df), None)
    ph = ",".join("?" * len(df.columns))
    conn.executemany(
        f"INSERT OR REPLACE INTO county_language VALUES ({ph})",
        sub.values.tolist())
    if engine == "sqlite":
        conn.commit()
    print(f"Loaded {len(sub):,} counties into county_language ({db_path})")
    conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vintage", type=int, default=2023)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--db", default="data/hmda.db")
    a = p.parse_args()
    path = fetch_language(a.vintage, a.data_dir)
    load_language(a.db, path)
