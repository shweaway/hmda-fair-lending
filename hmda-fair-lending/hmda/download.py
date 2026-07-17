"""Download the full set of HMDA Modified LAR files for a filing year.

Endpoints (verified live, July 2026):
  Filer roster:  https://ffiec.cfpb.gov/v2/reporting/filers/{year}
  Modified LAR:  https://ffiec.cfpb.gov/file/modifiedLar/year/{year}/institution/{lei}/txt

Design:
  * Resumable — already-downloaded, non-empty files are skipped, so you can
    stop and restart at any time.
  * Threaded (default 6 workers) with retry + exponential backoff.
  * Polite: small delay per request; identifies itself with a User-Agent.
  * Writes data/raw/{year}/{lei}.txt and a manifest CSV with row counts.

Typical full-year footprint: ~4,000-5,000 institutions, ~8-10M records,
roughly 3-5 GB of text. Expect a few hours on a home connection.
"""
from __future__ import annotations

import csv
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = "https://ffiec.cfpb.gov"
ROSTER_URL = BASE + "/v2/reporting/filers/{year}"
MLAR_URL = BASE + "/file/modifiedLar/year/{year}/institution/{lei}/txt"

HEADERS = {"User-Agent": "hmda-fair-lending-research/1.0 (personal research use)"}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def get_roster(year: int, session: requests.Session | None = None) -> list[dict]:
    """Return the list of filers ({lei, name, ...}) for a year."""
    s = session or requests.Session()
    r = s.get(ROSTER_URL.format(year=year), headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    filers = data.get("institutions", data if isinstance(data, list) else [])
    if not filers:
        raise RuntimeError(f"Empty roster for {year}: {str(data)[:200]}")
    return filers


def _download_one(session: requests.Session, year: int, lei: str,
                  dest: Path, retries: int = 4, delay: float = 0.15) -> tuple[str, int]:
    """Download one institution's file. Returns (status, n_rows)."""
    if dest.exists() and dest.stat().st_size > 0:
        return "cached", sum(1 for _ in dest.open(encoding="utf-8", errors="replace"))
    url = MLAR_URL.format(year=year, lei=lei)
    for attempt in range(retries):
        try:
            time.sleep(delay)
            with session.get(url, headers=HEADERS, timeout=300, stream=True) as r:
                if r.status_code == 404:
                    return "missing", 0
                r.raise_for_status()
                tmp = dest.with_suffix(".part")
                n = 0
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        n += chunk.count(b"\n")
                if tmp.stat().st_size == 0:
                    tmp.unlink()
                    return "empty", 0
                tmp.replace(dest)
                return "ok", n
        except requests.RequestException as e:
            wait = 2 ** attempt
            log(f"  retry {lei} in {wait}s ({e.__class__.__name__})")
            time.sleep(wait)
    return "failed", 0


def download_year(year: int, data_dir: str | Path = "data",
                  workers: int = 6, limit: int | None = None,
                  leis: list[str] | None = None) -> Path:
    """Download Modified LAR files for every filer in `year`.

    Args:
        year: HMDA activity year (2018+).
        data_dir: root data directory; files land in data/raw/{year}/.
        workers: concurrent downloads (keep modest — this is a public API).
        limit: only download the first N filers (for testing).
        leis: restrict to specific LEIs instead of the full roster.

    Returns path to the manifest CSV.
    """
    raw = Path(data_dir) / "raw" / str(year)
    raw.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    roster = get_roster(year, session)
    name_by_lei = {f["lei"]: f.get("name", "") for f in roster}
    todo = leis if leis else [f["lei"] for f in roster]
    if limit:
        todo = todo[:limit]
    log(f"{year}: {len(todo)} institutions to fetch -> {raw}")

    manifest_path = Path(data_dir) / f"manifest_{year}.csv"
    results: dict[str, tuple[str, int]] = {}
    done = 0

    def work(lei: str):
        s = requests.Session()  # one session per thread
        return lei, _download_one(s, year, lei, raw / f"{lei}.txt")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, lei) for lei in todo]
        for fut in as_completed(futures):
            lei, (status, n) = fut.result()
            results[lei] = (status, n)
            done += 1
            if done % 100 == 0 or done == len(todo):
                ok = sum(1 for s, _ in results.values() if s in ("ok", "cached"))
                log(f"  {done}/{len(todo)} done ({ok} ok)")

    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["lei", "name", "status", "rows", "path"])
        for lei in todo:
            status, n = results.get(lei, ("unknown", 0))
            w.writerow([lei, name_by_lei.get(lei, ""), status, n,
                        str(raw / f"{lei}.txt")])

    failed = [l for l, (s, _) in results.items() if s == "failed"]
    if failed:
        log(f"WARNING: {len(failed)} downloads failed — rerun to retry "
            f"(completed files are skipped automatically).")
    total_rows = sum(n for _, n in results.values())
    log(f"Manifest: {manifest_path}  (total rows: {total_rows:,})")

    # Save roster for the institutions table
    roster_path = Path(data_dir) / f"roster_{year}.json"
    roster_path.write_text(json.dumps(roster), encoding="utf-8")
    return manifest_path


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--limit", type=int, default=None,
                   help="only first N filers (testing)")
    a = p.parse_args()
    download_year(a.year, a.data_dir, a.workers, a.limit)
