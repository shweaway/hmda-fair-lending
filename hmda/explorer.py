"""Interactive explorers — self-contained HTML files for analysts.

Three builders, one pattern: aggregate the LAR into a compact cube,
embed it as JSON in an HTML template (adjacent *_template.html files),
and let all slicing happen client-side. No server, no CDN, works
offline, shareable as single files.

  build()          tract explorer — geography x demography
  build_lenders()  lender explorer — institution performance across
                   tracts and applicant groups vs the market benchmark
  build_loans()    loan-parameter explorer — channel (loan type),
                   purpose, amount band, occupancy, group x denial and
                   pricing metrics

Small-cell suppression: the pages hide any rate computed from fewer than
10 applications — tiny denominators produce rates that mislead more than
they inform. The threshold is SUPPRESS_N below and is stated on-page.

County display names come from the `county_language` table when present
(market-insights branch); otherwise counties show as FIPS codes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .constants import (GROUP_ORDER, GROUP_LABELS, LOAN_TYPE, LOAN_PURPOSE,
                        OCCUPANCY)

DEC = "in_decision = 1 AND business_or_commercial_purpose != '1'"
SUPPRESS_N = 10

TRACT_COLS = ["tract", "minority_pct", "minority_band", "income_q",
              "median_income", "pop", "apps", "orig", "den", "vol_mn"]


def _q(conn, engine, sql, params=None):
    from .analysis import q
    return q(conn, engine, sql, params)


def _table_exists(conn, engine, name):
    if engine == "duckdb":
        r = _q(conn, engine, "SELECT table_name FROM information_schema."
                             "tables WHERE table_name = ?", [name])
    else:
        r = _q(conn, engine, "SELECT name AS table_name FROM sqlite_master "
                             "WHERE type='table' AND name = ?", [name])
    return len(r) > 0


def build(conn, engine: str, year: int, out_path: str | Path) -> Path:
    group_terms = []
    for g in GROUP_ORDER:
        group_terms.append(
            f"SUM(CASE WHEN group_re = '{g}' THEN 1 ELSE 0 END) AS a_{g}")
        group_terms.append(
            f"SUM(CASE WHEN group_re = '{g}' THEN is_denied ELSE 0 END)"
            f" AS d_{g}")
    lt_terms = [
        f"SUM(CASE WHEN loan_type = '{c}' THEN 1 ELSE 0 END) AS lt_{c}"
        for c in LOAN_TYPE]

    df = _q(conn, engine, f"""
        SELECT tract11,
               COUNT(*) AS apps, SUM(is_originated) AS orig,
               SUM(is_denied) AS den,
               SUM(loan_amount_n * is_originated) / 1e6 AS vol_mn,
               {', '.join(group_terms)}, {', '.join(lt_terms)}
        FROM lar
        WHERE activity_year = ? AND {DEC} AND tract11 IS NOT NULL
        GROUP BY tract11""", [str(year)])

    if _table_exists(conn, engine, "tracts"):
        tr = _q(conn, engine, """
            SELECT tract11, minority_pct, minority_band,
                   income_quintile_state, median_income, total_pop
            FROM tracts""")
        df = df.merge(tr, on="tract11", how="left")
    else:
        for c in ("minority_pct", "minority_band",
                  "income_quintile_state", "median_income", "total_pop"):
            df[c] = None

    county_names = {}
    if _table_exists(conn, engine, "county_language"):
        cn = _q(conn, engine,
                "SELECT county_fips, county_name FROM county_language")
        county_names = dict(zip(cn.county_fips, cn.county_name))

    cols = (TRACT_COLS
            + [f"{p}_{g}" for g in GROUP_ORDER for p in ("a", "d")]
            + [f"lt_{c}" for c in LOAN_TYPE])
    rows = []
    for r in df.itertuples(index=False):
        row = [r.tract11,
               None if pd.isna(r.minority_pct) else
               round(float(r.minority_pct), 1),
               None if pd.isna(r.minority_band)
               else r.minority_band,
               None if pd.isna(r.income_quintile_state)
               else int(r.income_quintile_state),
               None if pd.isna(r.median_income)
               else int(r.median_income),
               None if pd.isna(r.total_pop) else int(r.total_pop),
               int(r.apps), int(r.orig), int(r.den),
               round(float(r.vol_mn or 0), 2)]
        for g in GROUP_ORDER:
            row += [int(getattr(r, f"a_{g}")), int(getattr(r, f"d_{g}"))]
        row += [int(getattr(r, f"lt_{c}")) for c in LOAN_TYPE]
        rows.append(row)

    payload = {
        "year": year,
        "suppress_n": SUPPRESS_N,
        "cols": cols,
        "rows": rows,
        "groups": GROUP_ORDER,
        "group_labels": GROUP_LABELS,
        "loan_types": LOAN_TYPE,
        "county_names": county_names,
    }

    return _emit("explorer_template.html", payload, out_path,
                 f"Tract explorer: {len(rows):,} tracts")


def _emit(template_name: str, payload: dict, out_path, label: str) -> Path:
    template = (Path(__file__).parent / template_name).read_text(
        encoding="utf-8")
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", blob)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    mb = out_path.stat().st_size / 1e6
    print(f"{label} -> {out_path} ({mb:.1f} MB)")
    return out_path


BANDS = ["<10%", "10-30%", "30-50%", "50-80%", "80-100%"]
QUINTILES = [1, 2, 3, 4, 5]


def build_lenders(conn, engine: str, year: int,
                  out_path: str | Path) -> Path:
    """Lender x state cube: overall, per-group, per-minority-band, and
    per-income-quintile application/denial counts. The market benchmark
    is computed client-side by summing over the current scope."""
    terms = []
    for g in GROUP_ORDER:
        terms.append(
            f"SUM(CASE WHEN group_re = '{g}' THEN 1 ELSE 0 END) AS a_{g}")
        terms.append(
            f"SUM(CASE WHEN group_re = '{g}' THEN is_denied ELSE 0 END)"
            f" AS d_{g}")
    for i, b in enumerate(BANDS):
        terms.append(f"SUM(CASE WHEN t.minority_band = '{b}' THEN 1 "
                     f"ELSE 0 END) AS ba_{i}")
        terms.append(f"SUM(CASE WHEN t.minority_band = '{b}' THEN "
                     f"l.is_denied ELSE 0 END) AS bd_{i}")
    for qn in QUINTILES:
        terms.append(f"SUM(CASE WHEN t.income_quintile_state = {qn} THEN 1 "
                     f"ELSE 0 END) AS qa_{qn}")
        terms.append(f"SUM(CASE WHEN t.income_quintile_state = {qn} THEN "
                     f"l.is_denied ELSE 0 END) AS qd_{qn}")

    df = _q(conn, engine, f"""
        SELECT l.lei, l.state_code AS state,
               COUNT(*) AS apps, SUM(l.is_originated) AS orig,
               SUM(l.is_denied) AS den,
               SUM(l.loan_amount_n * l.is_originated) / 1e6 AS vol_mn,
               SUM(l.pricing_exempt) AS exempt_n,
               {', '.join(terms)}
        FROM lar l LEFT JOIN tracts t ON l.tract11 = t.tract11
        WHERE l.activity_year = ? AND {DEC}
        GROUP BY l.lei, l.state_code""", [str(year)])

    # Records outside the apps/orig/den scope above: withdrawn (4), closed
    # for incompleteness (5), purchased loans (6), and preapproval requests
    # (7,8). Reported alongside the decisioned counts so the page shows how
    # much volume the DEC scope leaves out, instead of hiding it silently.
    excl = _q(conn, engine, """
        SELECT lei, state_code AS state, COUNT(*) AS excl_4_8
        FROM lar
        WHERE activity_year = ? AND action_taken IN ('4','5','6','7','8')
        GROUP BY lei, state_code""", [str(year)])
    df = df.merge(excl, on=["lei", "state"], how="left")
    df["excl_4_8"] = df["excl_4_8"].fillna(0)

    names = {}
    if _table_exists(conn, engine, "institutions"):
        nm = _q(conn, engine,
                "SELECT lei, name FROM institutions WHERE activity_year = ?",
                [str(year)])
        names = {r.lei: (r.name or "") for r in nm.itertuples(index=False)}

    cols = (["lei", "state", "apps", "orig", "den", "vol_mn", "exempt_n",
             "excl_4_8"]
            + [f"{p}_{g}" for g in GROUP_ORDER for p in ("a", "d")]
            + [f"b{p}_{i}" for i in range(len(BANDS)) for p in ("a", "d")]
            + [f"q{p}_{qn}" for qn in QUINTILES for p in ("a", "d")])
    rows = []
    for r in df.itertuples(index=False):
        row = [r.lei, r.state or "", int(r.apps), int(r.orig), int(r.den),
               round(float(r.vol_mn or 0), 2), int(r.exempt_n),
               int(r.excl_4_8)]
        for g in GROUP_ORDER:
            row += [int(getattr(r, f"a_{g}")), int(getattr(r, f"d_{g}"))]
        for i in range(len(BANDS)):
            row += [int(getattr(r, f"ba_{i}")), int(getattr(r, f"bd_{i}"))]
        for qn in QUINTILES:
            row += [int(getattr(r, f"qa_{qn}")), int(getattr(r, f"qd_{qn}"))]
        rows.append(row)

    payload = {
        "year": year, "suppress_n": SUPPRESS_N, "cols": cols, "rows": rows,
        "groups": GROUP_ORDER, "group_labels": GROUP_LABELS,
        "bands": BANDS, "names": names,
    }
    return _emit("lender_template.html", payload, out_path,
                 f"Lender explorer: {df.lei.nunique():,} lenders, "
                 f"{len(rows):,} lender-state rows")


AMOUNT_BANDS = [(0, 100), (100, 200), (200, 300), (300, 400),
                (400, 600), (600, 1000), (1000, None)]


def _amount_case() -> str:
    parts = ["CASE"]
    for i, (lo, hi) in enumerate(AMOUNT_BANDS):
        if hi is None:
            parts.append(f"WHEN loan_amount_n >= {lo * 1000} THEN {i}")
        else:
            parts.append(f"WHEN loan_amount_n >= {lo * 1000} AND "
                         f"loan_amount_n < {hi * 1000} THEN {i}")
    parts.append("ELSE -1 END")
    return " ".join(parts)


def build_loans(conn, engine: str, year: int, out_path: str | Path) -> Path:
    """Loan-parameter cube: loan type x purpose x amount band x occupancy
    x group, with denial counts plus pricing stats (originated first-lien,
    reported pricing only — EGRRCPA-exempt records are counted so the
    page can show how much of the slice is dark)."""
    df = _q(conn, engine, f"""
        SELECT loan_type, loan_purpose, occupancy_type, group_re,
               {_amount_case()} AS amt_band,
               COUNT(*) AS apps, SUM(is_denied) AS den,
               SUM(is_originated) AS orig,
               SUM(pricing_exempt) AS exempt_n,
               SUM(CASE WHEN is_originated = 1 AND lien_status = '1'
                        AND rate_spread_n IS NOT NULL THEN 1 ELSE 0 END)
                   AS sp_n,
               SUM(CASE WHEN is_originated = 1 AND lien_status = '1'
                        AND rate_spread_n IS NOT NULL
                   THEN rate_spread_n ELSE 0 END) AS sp_sum,
               SUM(CASE WHEN is_originated = 1 AND lien_status = '1'
                        AND rate_spread_n >= 1.5 THEN 1 ELSE 0 END)
                   AS hi_n,
               SUM(CASE WHEN is_originated = 1 AND lien_status = '1'
                        AND interest_rate_n BETWEEN 0.1 AND 25
                   THEN 1 ELSE 0 END) AS ir_n,
               SUM(CASE WHEN is_originated = 1 AND lien_status = '1'
                        AND interest_rate_n BETWEEN 0.1 AND 25
                   THEN interest_rate_n ELSE 0 END) AS ir_sum,
               SUM(CASE WHEN hoepa_status = '1' THEN 1 ELSE 0 END)
                   AS hoepa_n
        FROM lar WHERE activity_year = ? AND {DEC}
        GROUP BY loan_type, loan_purpose, occupancy_type, group_re,
                 amt_band""", [str(year)])

    cols = ["lt", "purpose", "occ", "grp", "amt", "apps", "den", "orig",
            "exempt_n", "sp_n", "sp_sum", "hi_n", "ir_n", "ir_sum",
            "hoepa_n"]
    rows = []
    for r in df.itertuples(index=False):
        rows.append([
            str(r.loan_type or ""), str(r.loan_purpose or ""),
            str(r.occupancy_type or ""), r.group_re, int(r.amt_band),
            int(r.apps), int(r.den), int(r.orig), int(r.exempt_n),
            int(r.sp_n), round(float(r.sp_sum), 3), int(r.hi_n),
            int(r.ir_n), round(float(r.ir_sum), 3), int(r.hoepa_n)])

    amt_labels = [f"${lo}k–{hi}k" if hi else f"≥ ${lo / 1000:.0f}M"
                  for lo, hi in AMOUNT_BANDS]
    payload = {
        "year": year, "suppress_n": SUPPRESS_N, "cols": cols, "rows": rows,
        "groups": GROUP_ORDER, "group_labels": GROUP_LABELS,
        "loan_types": LOAN_TYPE, "purposes": LOAN_PURPOSE,
        "occupancy": OCCUPANCY, "amt_labels": amt_labels,
    }
    return _emit("loans_template.html", payload, out_path,
                 f"Loan-parameter explorer: {len(rows):,} cells")
