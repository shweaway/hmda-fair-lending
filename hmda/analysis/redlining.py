"""Redlining / geographic analysis.

Requires the `tracts` table (run the census step first). Follows the
screening logic used in DOJ/CFPB redlining matters: compare a lender's
application and origination footprint in majority-minority census tracts
against the aggregate market in the same counties.

Outputs:
  volume_by_minority_band   market-wide apps/origs/denial rate by tract
                            minority band
  volume_by_income_quintile same by tract income quintile (LMI proxy)
  lender_mmt_screen         per-lender majority-minority-tract share vs
                            the market benchmark in the counties where the
                            lender operates (min volume threshold applies)
  county_gaps               counties with the largest denial-rate gaps
"""
from __future__ import annotations

import pandas as pd

from . import q

DEC = "in_decision = 1 AND business_or_commercial_purpose != '1'"
MIN_LENDER_APPS = 500   # screen lenders with at least this many decisions
MIN_COUNTY_APPS = 250


def run(conn, engine: str, year: int) -> dict[str, pd.DataFrame]:
    out = {}
    y = [str(year)]

    have_tracts = q(conn, engine, "SELECT COUNT(*) AS n FROM tracts")["n"][0]
    if not have_tracts:
        out["NOTE"] = pd.DataFrame({"note": [
            "tracts table is empty — run `python run.py census` first "
            "to enable redlining analysis"]})
        return out

    band = q(conn, engine, f"""
        SELECT t.minority_band, COUNT(*) AS apps,
               SUM(l.is_denied) AS denials,
               SUM(l.is_originated) AS originations,
               SUM(l.loan_amount_n * l.is_originated) AS orig_volume
        FROM lar l JOIN tracts t ON l.tract11 = t.tract11
        WHERE l.activity_year = ? AND {DEC}
              AND t.minority_band IS NOT NULL
        GROUP BY t.minority_band ORDER BY t.minority_band""", y)
    band["Denial rate %"] = (100 * band.denials / band.apps).round(2)
    out["volume_by_minority_band"] = band.rename(columns={
        "minority_band": "Tract minority share"})

    inc = q(conn, engine, f"""
        SELECT t.income_quintile_state AS quintile, COUNT(*) AS apps,
               SUM(l.is_denied) AS denials,
               SUM(l.is_originated) AS originations
        FROM lar l JOIN tracts t ON l.tract11 = t.tract11
        WHERE l.activity_year = ? AND {DEC}
              AND t.income_quintile_state IS NOT NULL
        GROUP BY t.income_quintile_state ORDER BY quintile""", y)
    inc["Denial rate %"] = (100 * inc.denials / inc.apps).round(2)
    out["volume_by_income_quintile"] = inc.rename(columns={
        "quintile": "Tract income quintile (1=lowest, in-state)"})

    # ---- Per-lender majority-minority tract screen -----------------------
    lender = q(conn, engine, f"""
        SELECT l.lei,
               COUNT(*) AS apps,
               SUM(CASE WHEN t.minority_pct >= 50 THEN 1 ELSE 0 END) AS mmt_apps
        FROM lar l JOIN tracts t ON l.tract11 = t.tract11
        WHERE l.activity_year = ? AND {DEC}
        GROUP BY l.lei HAVING COUNT(*) >= {MIN_LENDER_APPS}""", y)

    # market benchmark per county, then lender-specific expected share
    market = q(conn, engine, f"""
        SELECT l.county_code,
               COUNT(*) AS apps,
               SUM(CASE WHEN t.minority_pct >= 50 THEN 1 ELSE 0 END) AS mmt_apps
        FROM lar l JOIN tracts t ON l.tract11 = t.tract11
        WHERE l.activity_year = ? AND {DEC}
        GROUP BY l.county_code""", y).set_index("county_code")

    mix = q(conn, engine, f"""
        SELECT l.lei, l.county_code, COUNT(*) AS apps
        FROM lar l JOIN tracts t ON l.tract11 = t.tract11
        WHERE l.activity_year = ? AND {DEC}
        GROUP BY l.lei, l.county_code""", y)
    mix = mix[mix.lei.isin(set(lender.lei))]
    mix["mkt_share_mmt"] = mix.county_code.map(
        lambda c: (market.loc[c, "mmt_apps"] / market.loc[c, "apps"])
        if c in market.index and market.loc[c, "apps"] else None)
    mix = mix.dropna(subset=["mkt_share_mmt"])
    expected = (mix.assign(w=mix.apps * mix.mkt_share_mmt)
                   .groupby("lei").agg(apps_w=("apps", "sum"),
                                       exp_mmt=("w", "sum")))
    expected["expected_share"] = expected.exp_mmt / expected.apps_w

    names = q(conn, engine,
              "SELECT lei, name FROM institutions").set_index("lei")["name"]
    scr = lender.set_index("lei").join(expected[["expected_share"]])
    scr["actual_share"] = scr.mmt_apps / scr.apps
    scr["shortfall_ratio"] = (scr.actual_share / scr.expected_share).round(3)
    scr["Institution"] = scr.index.map(lambda l: names.get(l, l))
    scr = scr.reset_index()[[
        "Institution", "lei", "apps", "actual_share", "expected_share",
        "shortfall_ratio"]]
    scr["actual_share"] = (100 * scr.actual_share).round(2)
    scr["expected_share"] = (100 * scr.expected_share).round(2)
    scr = scr.rename(columns={
        "apps": "Applications",
        "actual_share": "Majority-minority tract share % (actual)",
        "expected_share": "Market-expected share %",
        "shortfall_ratio": "Actual/expected ratio"})
    out["lender_mmt_screen"] = scr.sort_values("Actual/expected ratio")

    # ---- Counties with biggest Black/Hispanic vs White denial gaps -------
    cg = q(conn, engine, f"""
        SELECT county_code, group_re, COUNT(*) AS apps,
               SUM(is_denied) AS denials
        FROM lar WHERE activity_year = ? AND {DEC}
              AND group_re IN ('white', 'black', 'hispanic')
        GROUP BY county_code, group_re
        HAVING COUNT(*) >= {MIN_COUNTY_APPS}""", y)
    piv = cg.pivot_table(index="county_code", columns="group_re",
                         values=["apps", "denials"], aggfunc="sum")
    rows = []
    for county, r in piv.iterrows():
        try:
            wa, wd = r[("apps", "white")], r[("denials", "white")]
        except KeyError:
            continue
        if pd.isna(wa) or pd.isna(wd) or wa <= 0 or wd <= 0:
            continue  # no (or no denied) White apps in county: no benchmark
        wr = wd / wa
        for g in ("black", "hispanic"):
            try:
                ga, gd = r[("apps", g)], r[("denials", g)]
            except KeyError:
                continue
            # counties where a group has no applications leave NaN holes
            if pd.isna(ga) or pd.isna(gd) or ga < MIN_COUNTY_APPS:
                continue
            gr = gd / ga
            rows.append({"County FIPS": county, "Group": g,
                         "Group denial %": round(100 * gr, 2),
                         "White denial %": round(100 * wr, 2),
                         "Ratio": round(gr / wr, 2),
                         "Group apps": int(ga)})
    gaps = pd.DataFrame(rows)
    if len(gaps):
        gaps = gaps.sort_values("Ratio", ascending=False).head(100)
    out["county_denial_gaps"] = gaps

    return out
