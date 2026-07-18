"""Institution behavior analysis.

How lenders differ and how they respond to the rules:
  top_lenders            volume, denial rate, demographic mix per lender
  exemption_usage        which lenders invoke the EGRRCPA partial exemption
                         (pricing fields reported 'Exempt') and how much of
                         the market is dark as a result
  purchaser_channels     where loans go after origination (GSE, Ginnie,
                         private, portfolio) by group — securitization mix
  aus_usage              automated underwriting system usage
  market_concentration   county-level HHI of application volume
  lender_group_mix       demographic application shares of the largest
                         lenders vs the market
"""
from __future__ import annotations

import pandas as pd

from . import q
from ..constants import GROUP_LABELS, GROUP_ORDER, PURCHASER_TYPE, AUS

DEC = "in_decision = 1 AND business_or_commercial_purpose != '1'"
TOP_N = 50


def run(conn, engine: str, year: int) -> dict[str, pd.DataFrame]:
    out = {}
    y = [str(year)]

    top = q(conn, engine, f"""
        SELECT l.lei, i.name, COUNT(*) AS apps,
               SUM(l.is_originated) AS originations,
               SUM(l.is_denied) AS denials,
               SUM(l.loan_amount_n * l.is_originated) / 1e9 AS orig_vol_bn,
               AVG(l.pricing_exempt) * 100 AS pricing_exempt_pct
        FROM lar l LEFT JOIN institutions i ON l.lei = i.lei
        WHERE l.activity_year = ? AND {DEC}
        GROUP BY l.lei, i.name
        ORDER BY apps DESC LIMIT {TOP_N}""", y)
    top["Denial rate %"] = (100 * top.denials / top.apps).round(2)
    top["orig_vol_bn"] = top.orig_vol_bn.round(2)
    top["pricing_exempt_pct"] = top.pricing_exempt_pct.round(1)
    out["top_lenders"] = top.rename(columns={
        "name": "Institution", "apps": "Applications",
        "originations": "Originations", "denials": "Denials",
        "orig_vol_bn": "Origination volume ($bn)",
        "pricing_exempt_pct": "Pricing-exempt records %"})

    ex = q(conn, engine, """
        SELECT i.name, i.lei, i.rows AS records,
               i.pricing_exempt_share * 100 AS exempt_pct
        FROM institutions i
        WHERE i.activity_year = ? AND i.rows >= 25
        ORDER BY i.pricing_exempt_share DESC, i.rows DESC""", y)
    ex["exempt_pct"] = ex["exempt_pct"].round(1)
    summary = pd.DataFrame({
        "Metric": [
            "Institutions (>=25 records)",
            "Institutions with >50% pricing-exempt records",
            "Share of ALL records that are pricing-exempt %",
        ],
        "Value": [
            len(ex),
            int((ex.exempt_pct > 50).sum()),
            round(q(conn, engine,
                    "SELECT AVG(pricing_exempt)*100 AS v FROM lar "
                    "WHERE activity_year = ?", y)["v"][0], 2),
        ]})
    out["exemption_summary"] = summary
    out["exemption_by_lender"] = ex.head(200).rename(columns={
        "name": "Institution", "records": "Records",
        "exempt_pct": "Pricing-exempt %"})

    pc = q(conn, engine, """
        SELECT purchaser_type, group_re, COUNT(*) AS n
        FROM lar WHERE activity_year = ? AND is_originated = 1
        GROUP BY purchaser_type, group_re""", y)
    pc["purchaser_type"] = pc["purchaser_type"].astype(str).map(
        lambda v: PURCHASER_TYPE.get(v, v))
    piv = pc.pivot_table(index="purchaser_type", columns="group_re",
                         values="n", aggfunc="sum", fill_value=0)
    piv = piv[[c for c in GROUP_ORDER if c in piv.columns]]
    pct = (100 * piv / piv.sum()).round(2)
    pct.columns = [GROUP_LABELS.get(c, c) for c in pct.columns]
    out["purchaser_channel_pct"] = pct.reset_index().rename(
        columns={"purchaser_type": "Purchaser (column % within group)"})

    aus = q(conn, engine, f"""
        SELECT aus_1, COUNT(*) AS n, AVG(is_denied)*100 AS denial_pct
        FROM lar WHERE activity_year = ? AND {DEC}
        GROUP BY aus_1 ORDER BY n DESC""", y)
    aus["aus_1"] = aus["aus_1"].astype(str).map(lambda v: AUS.get(v, v))
    aus["denial_pct"] = aus["denial_pct"].round(2)
    out["aus_usage"] = aus.rename(columns={
        "aus_1": "AUS", "n": "Applications", "denial_pct": "Denial rate %"})

    hhi = q(conn, engine, f"""
        SELECT county_code, lei, COUNT(*) AS apps
        FROM lar WHERE activity_year = ? AND {DEC}
        GROUP BY county_code, lei""", y)
    def _hhi(g):
        s = g.apps / g.apps.sum()
        return (10_000 * (s ** 2).sum())
    ch = (hhi.groupby("county_code")
             .apply(_hhi, include_groups=False).round(0)
             .rename("HHI").reset_index())
    tot = hhi.groupby("county_code")["apps"].sum().rename("apps")
    ch = ch.merge(tot, on="county_code")
    ch = ch[ch.apps >= 500].sort_values("HHI", ascending=False)
    out["county_hhi"] = ch.rename(columns={
        "county_code": "County FIPS", "apps": "Applications"}).head(100)

    mkt = q(conn, engine, f"""
        SELECT group_re, COUNT(*) AS n FROM lar
        WHERE activity_year = ? AND {DEC} GROUP BY group_re""", y)
    mkt_share = (mkt.set_index("group_re")["n"] /
                 mkt["n"].sum() * 100).round(2)
    lg = q(conn, engine, f"""
        SELECT l.lei, i.name, l.group_re, COUNT(*) AS n
        FROM lar l LEFT JOIN institutions i ON l.lei = i.lei
        WHERE l.activity_year = ? AND {DEC}
        GROUP BY l.lei, i.name, l.group_re""", y)
    top_leis = set(out["top_lenders"]["lei"].head(25))
    lg = lg[lg.lei.isin(top_leis)]
    piv = lg.pivot_table(index="name", columns="group_re", values="n",
                         aggfunc="sum", fill_value=0)
    pct = (100 * piv.div(piv.sum(axis=1), axis=0)).round(2)
    pct = pct[[c for c in GROUP_ORDER if c in pct.columns]]
    pct.loc["— MARKET OVERALL —"] = [
        mkt_share.get(c, 0) for c in [x for x in GROUP_ORDER if x in piv.columns]]
    pct.columns = [GROUP_LABELS.get(c, c) for c in pct.columns]
    out["lender_group_mix_pct"] = pct.reset_index().rename(
        columns={"name": "Institution (row % of applications)"})

    return out
