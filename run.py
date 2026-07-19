#!/usr/bin/env python3
"""HMDA Fair Lending Analysis Framework — orchestrator.

Steps (run in order, or `all`):
  python run.py download   --year 2025            # fetch every filer's file
  python run.py load       --year 2025            # build the database
  python run.py census                            # tract demographics (ACS)
  python run.py uad                               # FHFA appraisal data
  python run.py analyze    --year 2025            # run all five modules
  python run.py all        --year 2025

Interactive output (separate deliverable, not part of `all`):
  python run.py explore    --year 2025            # analyst tract explorer
                                                  # (self-contained HTML)

Useful flags:
  --limit N        download only the first N institutions (smoke test)
  --skip-puf       uad step: skip the appraisal-level PUF
  --data-dir DIR   where raw files + db live (default ./data)
  --out DIR        analysis outputs (default ./outputs)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hmda import download as dl          # noqa: E402
from hmda import db as hdb               # noqa: E402
from hmda import census as hcensus       # noqa: E402
from hmda import report as hreport       # noqa: E402
from hmda import uad as huad             # noqa: E402
from hmda import explorer as hexplorer   # noqa: E402
from hmda.analysis import (denials, pricing, redlining, institutions,  # noqa: E402
                           valuation)


def cmd_download(a):
    dl.download_year(a.year, a.data_dir, a.workers, a.limit)


def cmd_load(a):
    hdb.load_files(a.db, a.data_dir, a.year)


def cmd_census(a):
    path = hcensus.fetch_tracts(a.vintage, a.data_dir)
    hcensus.load_tracts(a.db, path)


def cmd_uad(a):
    huad.run_all(a.db, a.data_dir, include_puf=not a.skip_puf)


def cmd_analyze(a):
    conn, engine = hdb.connect(a.db)
    print(f"Engine: {engine}")
    results = {}
    for name, mod in [("denials", denials), ("pricing", pricing),
                      ("redlining", redlining),
                      ("institutions", institutions),
                      ("valuation", valuation)]:
        print(f"Running {name} ...", flush=True)
        try:
            results[name] = mod.run(conn, engine, a.year)
        except Exception as e:
            print(f"  {name} failed: {e!r}")
            import traceback; traceback.print_exc()
    out = Path(a.out)
    xlsx = hreport.write_excel(results, out / f"hmda_{a.year}_analysis.xlsx")
    charts = hreport.make_charts(results, out / "charts")
    html = hreport.write_html(results, charts, a.year, out / "summary.html")
    print(f"\nWrote:\n  {xlsx}\n  {html}\n  {len(charts)} charts in {out/'charts'}")


def cmd_explore(a):
    conn, engine = hdb.connect(a.db)
    print(f"Engine: {engine}")
    hexplorer.build(conn, engine, a.year,
                    Path(a.out) / f"explorer_{a.year}.html")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["download", "load", "census", "uad",
                                    "analyze", "explore", "all"])
    p.add_argument("--skip-puf", action="store_true",
                   help="uad step: aggregate statistics only, no "
                        "appraisal-level PUF")
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--db", default=None)
    p.add_argument("--out", default="outputs")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--vintage", type=int, default=2023,
                   help="ACS 5-year vintage for census step")
    a = p.parse_args()
    if a.db is None:
        a.db = str(Path(a.data_dir) / "hmda.db")
    Path(a.data_dir).mkdir(parents=True, exist_ok=True)

    steps = {"download": cmd_download, "load": cmd_load,
             "census": cmd_census, "uad": cmd_uad, "analyze": cmd_analyze,
             "explore": cmd_explore}
    if a.step == "all":
        for s in ("download", "load", "census", "uad", "analyze"):
            print(f"\n=== {s.upper()} ===")
            steps[s](a)
    else:
        steps[a.step](a)


if __name__ == "__main__":
    main()
