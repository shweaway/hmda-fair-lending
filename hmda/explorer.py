"""Interactive tract explorer — a self-contained HTML file for analysts.

`build()` aggregates the LAR to census-tract level (overall and per
race/ethnicity group), joins tract demography, and embeds the result as
JSON in a single HTML page (template: explorer_template.html, adjacent to
this file). Everything runs client-side — no server, no CDN, works
offline and can be shared as one file.

The page offers: state/county/band/income-quintile filters, a group
metric selector, a minority-share vs denial-rate scatter, denial rate by
minority band, a sortable/searchable tract table, and a per-tract
drill-down (group breakdown, loan-type mix, county context).

Small-cell suppression: the page hides any rate computed from fewer than
10 applications — tiny denominators produce rates that mislead more than
they inform. The threshold is SUPPRESS_N below and is stated on the page.

County display names come from the `county_language` table when present
(market-insights branch); otherwise counties show as FIPS codes.
"""
from __future__ import annotations

import json
from pathlib import Path

from .constants import GROUP_ORDER, GROUP_LABELS, LOAN_TYPE

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
               None if r.minority_pct != r.minority_pct else
               round(float(r.minority_pct), 1),
               None if r.minority_band != r.minority_band
               else r.minority_band,
               None if r.income_quintile_state != r.income_quintile_state
               else int(r.income_quintile_state),
               None if r.median_income != r.median_income
               else int(r.median_income),
               None if r.total_pop != r.total_pop else int(r.total_pop),
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

    template = (Path(__file__).parent / "explorer_template.html").read_text(
        encoding="utf-8")
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", blob)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    mb = out_path.stat().st_size / 1e6
    print(f"Explorer: {len(rows):,} tracts -> {out_path} ({mb:.1f} MB)")
    return out_path
