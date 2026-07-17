"""Pricing disparity analysis.

Universe: originated (action_taken = 1), first-lien loans with reported
(non-exempt) pricing. Note: filers using the EGRRCPA partial exemption
report 'Exempt' for pricing fields — their loans drop out here, and the
share of such records is itself reported (a key data-gap finding).

Outputs:
  rate_spread_by_group    mean/median rate spread + share of high-priced
                          loans (spread >= 1.5ppt over APOR) by group
  interest_rate_by_group  mean/median note rate by group
  loan_costs_by_group     median total loan costs by group and purpose
  high_cost_by_group      HOEPA high-cost loan share
  pricing_exemption       share of records with exempt pricing, by year
"""
from __future__ import annotations

import pandas as pd

from . import q
from ..constants import GROUP_LABELS, GROUP_ORDER

ORIG = ("is_originated = 1 AND lien_status = '1' "
        "AND business_or_commercial_purpose != '1'")


def _group_stats(df: pd.DataFrame, value: str) -> pd.DataFrame:
    rows = []
    for g in GROUP_ORDER:
        sub = df.loc[df.group_re == g, value].dropna()
        if not len(sub):
            continue
        rows.append({
            "Group": GROUP_LABELS.get(g, g),
            "N": len(sub),
            "Mean": round(sub.mean(), 3),
            "Median": round(sub.median(), 3),
            "P75": round(sub.quantile(0.75), 3),
        })
    return pd.DataFrame(rows)


def run(conn, engine: str, year: int) -> dict[str, pd.DataFrame]:
    out = {}
    y = [str(year)]

    rs = q(conn, engine, f"""
        SELECT group_re, rate_spread_n FROM lar
        WHERE activity_year = ? AND {ORIG} AND rate_spread_n IS NOT NULL""", y)
    t = _group_stats(rs, "rate_spread_n")
    hp = (rs.assign(hp=(rs.rate_spread_n >= 1.5).astype(int))
            .groupby("group_re")["hp"].mean() * 100)
    t["High-priced share %"] = t["Group"].map(
        {GROUP_LABELS.get(k, k): round(v, 2) for k, v in hp.items()})
    out["rate_spread_by_group"] = t

    ir = q(conn, engine, f"""
        SELECT group_re, interest_rate_n FROM lar
        WHERE activity_year = ? AND {ORIG} AND interest_rate_n IS NOT NULL
              AND interest_rate_n BETWEEN 0.1 AND 25""", y)
    out["interest_rate_by_group"] = _group_stats(ir, "interest_rate_n")

    tc = q(conn, engine, f"""
        SELECT group_re, loan_purpose, total_loan_costs_n FROM lar
        WHERE activity_year = ? AND {ORIG}
              AND total_loan_costs_n IS NOT NULL""", y)
    med = (tc.groupby(["loan_purpose", "group_re"])["total_loan_costs_n"]
             .median().reset_index())
    piv = med.pivot(index="loan_purpose", columns="group_re",
                    values="total_loan_costs_n")
    piv = piv[[c for c in GROUP_ORDER if c in piv.columns]].round(0)
    piv.columns = [GROUP_LABELS.get(c, c) for c in piv.columns]
    out["median_loan_costs"] = piv.reset_index()

    hc = q(conn, engine, f"""
        SELECT group_re,
               SUM(CASE WHEN hoepa_status = '1' THEN 1 ELSE 0 END) AS high_cost,
               COUNT(*) AS originations
        FROM lar WHERE activity_year = ? AND {ORIG} AND hoepa_status IN ('1','2')
        GROUP BY group_re""", y)
    hc["High-cost (HOEPA) %"] = (100 * hc.high_cost / hc.originations).round(3)
    hc["group_re"] = hc["group_re"].map(lambda g: GROUP_LABELS.get(g, g))
    out["high_cost_by_group"] = hc.rename(columns={"group_re": "Group"})

    ex = q(conn, engine, """
        SELECT activity_year,
               AVG(pricing_exempt) * 100 AS pct_records_pricing_exempt,
               COUNT(*) AS records
        FROM lar GROUP BY activity_year ORDER BY activity_year""")
    ex["pct_records_pricing_exempt"] = ex["pct_records_pricing_exempt"].round(2)
    out["pricing_exemption"] = ex

    return out
