"""Market insights for inclusive multicultural marketing.

Repurposes the fair-lending database to answer marketing questions —
where is mortgage demand, which communities drive it, what languages do
those communities speak, and where is the market underserved or thinly
competed. All outputs are for INCLUSIVE outreach (deciding where to show
up and in what language), never for screening or exclusion; see the
`guardrails` sheet, which leads every report.

  guardrails                permitted vs. prohibited uses (read first)
  county_market_size        applications, originations, volume, denial
                            rate for the largest county markets
  county_group_mix          demographic mix of application demand in the
                            largest counties — which communities are
                            actively seeking mortgages where
  language_national         national speakers + limited-English (LEP)
                            totals per language group (ACS C16001)
  language_by_county        counties ranked by LEP population — where
                            localized creative and staffing matter most
  underserved_tracts        majority-minority tracts whose origination
                            rate per resident trails their county — the
                            inclusive-outreach opportunity list
  product_mix_by_group      loan product (conventional/FHA/VA/USDA)
                            shares within each community
  demand_trend_by_group     applications and originations by group across
                            every year loaded in the database
  thin_competition          high-volume counties with concentrated
                            lending (HHI >= 2500) — fewer incumbents
                            competing for the same audience

Language sheets require `python run.py market` to have loaded the ACS
language table (needs a free CENSUS_API_KEY); tract sheets require the
census step. Missing inputs produce a NOTE row, not a failure.
"""
from __future__ import annotations

import pandas as pd

from . import q
from ..constants import GROUP_LABELS, GROUP_ORDER, LOAN_TYPE

DEC = "in_decision = 1 AND business_or_commercial_purpose != '1'"
TOP_COUNTIES = 100
MIN_TRACT_POP = 500
MIN_TRACT_APPS = 10

GUARDRAILS = [
    ("Purpose", "Inclusive market analysis: find underserved communities, "
     "size demand, and plan language/channel localization so outreach "
     "REACHES more of the market."),
    ("Permitted", "Adding languages, channels, community partnerships, and "
     "creative that welcome underserved groups; supporting CRA performance "
     "and Special Purpose Credit Programs (ECOA / Reg B 1002.8)."),
    ("Prohibited", "Using any output to exclude, discourage, or avoid "
     "marketing to a neighborhood or group based on race, ethnicity, or "
     "language (redlining / digital redlining), or to target less "
     "favorable products or terms at protected groups (reverse "
     "redlining)."),
    ("Prohibited", "Feeding race/ethnicity/language data into credit, "
     "pricing, or prescreening decisions. Marketing analytics only."),
    ("Prohibited", "Ad-platform audience targeting that proxies protected "
     "classes. Housing-related ads face special restrictions on major "
     "platforms (post-2019 HUD/Facebook settlement)."),
    ("Process", "Route campaigns built on these outputs through "
     "fair-lending / compliance review and document the inclusive intent."),
]


def _note(msg: str) -> pd.DataFrame:
    return pd.DataFrame({"NOTE": [msg]})


def _table_exists(conn, engine: str, name: str) -> bool:
    if engine == "duckdb":
        r = q(conn, engine, "SELECT table_name FROM information_schema.tables "
                            "WHERE table_name = ?", [name])
    else:
        r = q(conn, engine, "SELECT name AS table_name FROM sqlite_master "
                            "WHERE type='table' AND name = ?", [name])
    return len(r) > 0


def _county_names(conn, engine: str) -> pd.DataFrame | None:
    if not _table_exists(conn, engine, "county_language"):
        return None
    return q(conn, engine,
             "SELECT county_fips, county_name FROM county_language")


def run(conn, engine: str, year: int) -> dict[str, pd.DataFrame]:
    out = {}
    y = [str(year)]
    out["guardrails"] = pd.DataFrame(GUARDRAILS, columns=["Rule", "Detail"])
    names = _county_names(conn, engine)

    # ---- county market size ---------------------------------------------
    cm = q(conn, engine, f"""
        SELECT county_code, COUNT(*) AS apps,
               SUM(is_originated) AS originations,
               SUM(loan_amount_n * is_originated) / 1e6 AS orig_vol_mn,
               AVG(is_denied) * 100 AS denial_pct
        FROM lar WHERE activity_year = ? AND {DEC}
              AND county_code IS NOT NULL AND county_code != ''
        GROUP BY county_code ORDER BY apps DESC LIMIT {TOP_COUNTIES}""", y)
    cm["orig_vol_mn"] = cm.orig_vol_mn.round(1)
    cm["denial_pct"] = cm.denial_pct.round(2)
    if names is not None:
        cm = cm.merge(names, left_on="county_code",
                      right_on="county_fips", how="left")
        cm = cm.drop(columns=["county_fips"])
        cm.insert(1, "County", cm.pop("county_name"))
    out["county_market_size"] = cm.rename(columns={
        "county_code": "County FIPS", "apps": "Applications",
        "originations": "Originations",
        "orig_vol_mn": "Origination volume ($mn)",
        "denial_pct": "Denial rate %"})

    # ---- demographic mix of demand in the biggest counties --------------
    top_fips = cm["county_code"].head(50).tolist()
    gm = q(conn, engine, f"""
        SELECT county_code, group_re, COUNT(*) AS n
        FROM lar WHERE activity_year = ? AND {DEC}
        GROUP BY county_code, group_re""", y)
    gm = gm[gm.county_code.isin(top_fips)]
    piv = gm.pivot_table(index="county_code", columns="group_re",
                         values="n", aggfunc="sum", fill_value=0)
    pct = (100 * piv.div(piv.sum(axis=1), axis=0)).round(2)
    pct = pct[[c for c in GROUP_ORDER if c in pct.columns]]
    pct.columns = [GROUP_LABELS.get(c, c) for c in pct.columns]
    pct = pct.reindex(top_fips).reset_index()
    if names is not None:
        pct = pct.merge(names, left_on="county_code",
                        right_on="county_fips", how="left")
        pct = pct.drop(columns=["county_fips"])
        pct.insert(1, "County", pct.pop("county_name"))
    out["county_group_mix"] = pct.rename(columns={
        "county_code": "County FIPS (row % of applications)"})

    # ---- language localization (ACS C16001) -----------------------------
    if _table_exists(conn, engine, "county_language"):
        from ..language import LANGUAGES
        cl = q(conn, engine, "SELECT * FROM county_language")
        nat_rows = []
        for k, (label, _, _) in LANGUAGES.items():
            nat_rows.append({
                "Language": label,
                "Speakers (5+)": int(cl[f"{k}_total"].sum()),
                "Limited English (LEP)": int(cl[f"{k}_lep"].sum())})
        nat = pd.DataFrame(nat_rows).sort_values(
            "Limited English (LEP)", ascending=False).reset_index(drop=True)
        nat["LEP % of speakers"] = (100 * nat["Limited English (LEP)"]
                                    / nat["Speakers (5+)"]).round(1)
        out["language_national"] = nat

        lep_cols = [f"{k}_lep" for k in LANGUAGES]
        cl["lep_total"] = cl[lep_cols].sum(axis=1)
        cl["lep_pct"] = (100 * cl.lep_total / cl.pop5plus).round(2)
        top = cl.sort_values("lep_total", ascending=False).head(150)
        keep = {"county_fips": "County FIPS", "county_name": "County",
                "pop5plus": "Population 5+",
                "lep_total": "LEP population", "lep_pct": "LEP %"}
        for k, (label, _, _) in LANGUAGES.items():
            if k in ("other", "other_indo_euro", "other_asian_pi"):
                continue
            keep[f"{k}_lep"] = f"{label} LEP"
        out["language_by_county"] = (
            top[list(keep)].rename(columns=keep).reset_index(drop=True))
    else:
        msg = ("county_language table missing — run `python run.py market` "
               "with a CENSUS_API_KEY set to enable language sheets")
        out["language_national"] = _note(msg)
        out["language_by_county"] = _note(msg)

    # ---- underserved majority-minority tracts ---------------------------
    have_tracts = _table_exists(conn, engine, "tracts") and \
        q(conn, engine, "SELECT COUNT(*) AS n FROM tracts")["n"][0] > 0
    if have_tracts:
        tr = q(conn, engine, f"""
            SELECT t.tract11, t.county_fips, t.minority_pct, t.median_income,
                   t.total_pop, COUNT(l.tract11) AS apps,
                   SUM(l.is_originated) AS originations
            FROM tracts t
            LEFT JOIN lar l ON l.tract11 = t.tract11
                 AND l.activity_year = ? AND {DEC}
            GROUP BY t.tract11, t.county_fips, t.minority_pct,
                     t.median_income, t.total_pop""", y)
        tr = tr[tr.total_pop >= MIN_TRACT_POP].copy()
        tr["orig_per_1k"] = (1000 * tr.originations.fillna(0)
                             / tr.total_pop).round(2)
        county_rate = (tr.groupby("county_fips")
                         .apply(lambda g: 1000 * g.originations.sum()
                                / g.total_pop.sum(), include_groups=False)
                         .rename("county_orig_per_1k"))
        tr = tr.merge(county_rate, on="county_fips")
        mm = tr[(tr.minority_pct >= 50)
                & (tr.county_orig_per_1k > 0)].copy()
        mm["gap_ratio"] = (mm.orig_per_1k
                           / mm.county_orig_per_1k).round(2)
        mm = mm[mm.apps >= MIN_TRACT_APPS].sort_values(
            ["gap_ratio", "total_pop"], ascending=[True, False]).head(200)
        mm["county_orig_per_1k"] = mm.county_orig_per_1k.round(2)
        if names is not None:
            mm = mm.merge(names, left_on="county_fips",
                          right_on="county_fips", how="left")
            mm.insert(2, "County", mm.pop("county_name"))
        out["underserved_tracts"] = mm.rename(columns={
            "tract11": "Tract", "county_fips": "County FIPS",
            "minority_pct": "Minority %", "median_income": "Median income",
            "total_pop": "Population", "apps": "Applications",
            "originations": "Originations",
            "orig_per_1k": "Originations per 1k residents",
            "county_orig_per_1k": "County rate per 1k",
            "gap_ratio": "Tract/county ratio"})
    else:
        out["underserved_tracts"] = _note(
            "tracts table missing — run `python run.py census` first")

    # ---- product mix by community ---------------------------------------
    pm = q(conn, engine, f"""
        SELECT group_re, loan_type, COUNT(*) AS n
        FROM lar WHERE activity_year = ? AND {DEC}
        GROUP BY group_re, loan_type""", y)
    pm["loan_type"] = pm["loan_type"].astype(str).map(
        lambda v: LOAN_TYPE.get(v, v))
    piv = pm.pivot_table(index="group_re", columns="loan_type",
                         values="n", aggfunc="sum", fill_value=0)
    pct = (100 * piv.div(piv.sum(axis=1), axis=0)).round(2)
    pct = pct.reindex([g for g in GROUP_ORDER if g in pct.index])
    pct.index = [GROUP_LABELS.get(g, g) for g in pct.index]
    out["product_mix_by_group"] = pct.reset_index().rename(
        columns={"index": "Group (row % of applications)"})

    # ---- multi-year demand trend ----------------------------------------
    tr = q(conn, engine, f"""
        SELECT activity_year, group_re, COUNT(*) AS apps,
               SUM(is_originated) AS originations
        FROM lar WHERE {DEC}
        GROUP BY activity_year, group_re ORDER BY activity_year""")
    piv = tr.pivot_table(index="activity_year", columns="group_re",
                         values="apps", aggfunc="sum", fill_value=0)
    piv = piv[[c for c in GROUP_ORDER if c in piv.columns]]
    piv.columns = [GROUP_LABELS.get(c, c) for c in piv.columns]
    out["demand_trend_by_group"] = piv.reset_index().rename(
        columns={"activity_year": "Year (applications)"})

    # ---- thin competition ------------------------------------------------
    hh = q(conn, engine, f"""
        SELECT county_code, lei, COUNT(*) AS apps
        FROM lar WHERE activity_year = ? AND {DEC}
        GROUP BY county_code, lei""", y)
    def _hhi(g):
        s = g.apps / g.apps.sum()
        return 10_000 * (s ** 2).sum()
    ch = (hh.groupby("county_code")
            .apply(_hhi, include_groups=False).round(0)
            .rename("HHI").reset_index())
    tot = hh.groupby("county_code")["apps"].sum().rename("apps")
    nlenders = hh.groupby("county_code")["lei"].nunique().rename("lenders")
    ch = ch.merge(tot, on="county_code").merge(nlenders, on="county_code")
    ch = ch[(ch.apps >= 500) & (ch.HHI >= 2500)].sort_values(
        "apps", ascending=False).head(100)
    if names is not None:
        ch = ch.merge(names, left_on="county_code",
                      right_on="county_fips", how="left")
        ch = ch.drop(columns=["county_fips"])
        ch.insert(1, "County", ch.pop("county_name"))
    out["thin_competition"] = ch.rename(columns={
        "county_code": "County FIPS", "apps": "Applications",
        "lenders": "Lenders active"})

    return out
