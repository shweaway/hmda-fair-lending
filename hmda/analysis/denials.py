"""Approval/denial disparity analysis.

Universe: applications with a credit decision (action_taken 1, 2, 3),
first-lien, site-built and manufactured, all purposes unless noted.
Purchased loans (6), withdrawals (4), and incomplete files (5) are excluded
per standard practice.

Outputs:
  denial_by_group          national denial rates, ratios vs White, CIs, p
  denial_by_group_purpose  same split by loan purpose
  denial_by_group_income   same within income quintile bands (rough control)
  denial_reasons           distribution of primary denial reason by group
  adjusted_odds            logistic-regression adjusted odds ratios
  denial_by_sex, denial_by_age
"""
from __future__ import annotations

import pandas as pd

from . import q
from ..constants import GROUP_LABELS, GROUP_ORDER, DENIAL_REASONS, LOAN_PURPOSE, SEX
from ..metrics import wilson_ci, two_prop_z, rate_ratio, adjusted_odds_ratios

BASE_WHERE = "in_decision = 1 AND business_or_commercial_purpose != '1'"


def _rates_table(df: pd.DataFrame) -> pd.DataFrame:
    """df has columns group_re, apps, denials. Adds rates/ratios vs white."""
    df = df.set_index("group_re").reindex(
        [g for g in GROUP_ORDER if g in set(df["group_re"])]).reset_index()
    wk = df.loc[df.group_re == "white", ["denials", "apps"]]
    wd, wn = (int(wk.iloc[0, 0]), int(wk.iloc[0, 1])) if len(wk) else (0, 0)
    rows = []
    for _, r in df.iterrows():
        k, n = int(r.denials), int(r.apps)
        lo, hi = wilson_ci(k, n)
        rows.append({
            "Group": GROUP_LABELS.get(r.group_re, r.group_re),
            "Applications": n,
            "Denials": k,
            "Denial rate %": round(100 * k / n, 2) if n else None,
            "CI low %": round(100 * lo, 2),
            "CI high %": round(100 * hi, 2),
            "Ratio vs White": (round(rate_ratio(k, n, wd, wn), 2)
                               if r.group_re != "white" else 1.0),
            "p vs White": (round(two_prop_z(k, n, wd, wn), 4)
                           if r.group_re != "white" else None),
        })
    return pd.DataFrame(rows)


def run(conn, engine: str, year: int) -> dict[str, pd.DataFrame]:
    out = {}
    y = [str(year)]

    base = q(conn, engine, f"""
        SELECT group_re, COUNT(*) AS apps, SUM(is_denied) AS denials
        FROM lar WHERE activity_year = ? AND {BASE_WHERE}
        GROUP BY group_re""", y)
    out["denial_by_group"] = _rates_table(base)

    bp = q(conn, engine, f"""
        SELECT loan_purpose, group_re, COUNT(*) AS apps,
               SUM(is_denied) AS denials
        FROM lar WHERE activity_year = ? AND {BASE_WHERE}
        GROUP BY loan_purpose, group_re""", y)
    frames = []
    for purpose, sub in bp.groupby("loan_purpose"):
        t = _rates_table(sub.drop(columns="loan_purpose"))
        t.insert(0, "Loan purpose", LOAN_PURPOSE.get(str(purpose), purpose))
        frames.append(t)
    out["denial_by_group_purpose"] = (pd.concat(frames, ignore_index=True)
                                      if frames else pd.DataFrame())

    inc = q(conn, engine, f"""
        SELECT group_re, income_n, is_denied
        FROM lar WHERE activity_year = ? AND {BASE_WHERE}
              AND income_n IS NOT NULL AND income_n > 0""", y)
    if len(inc):
        inc["income_band"] = pd.qcut(inc["income_n"], 5, duplicates="drop")
        gb = (inc.groupby(["income_band", "group_re"], observed=True)
                 .agg(apps=("is_denied", "size"), denials=("is_denied", "sum"))
                 .reset_index())
        frames = []
        for band_, sub in gb.groupby("income_band", observed=True):
            t = _rates_table(sub.drop(columns="income_band"))
            t.insert(0, "Income quintile (000s)", str(band_))
            frames.append(t)
        out["denial_by_group_income"] = pd.concat(frames, ignore_index=True)

    dr = q(conn, engine, f"""
        SELECT group_re, denial_reason_1 AS reason, COUNT(*) AS n
        FROM lar WHERE activity_year = ? AND is_denied = 1
        GROUP BY group_re, denial_reason_1""", y)
    dr["reason"] = dr["reason"].astype(str).map(
        lambda r: DENIAL_REASONS.get(r, r))
    piv = dr.pivot_table(index="reason", columns="group_re", values="n",
                         aggfunc="sum", fill_value=0)
    piv = piv[[c for c in GROUP_ORDER if c in piv.columns]]
    piv_pct = (100 * piv / piv.sum()).round(2)
    piv_pct.columns = [GROUP_LABELS.get(c, c) for c in piv_pct.columns]
    out["denial_reasons_pct"] = piv_pct.reset_index()

    # Adjusted odds ratios (controls: income, loan amount, purpose, type,
    # occupancy, state). Public data lacks credit score/LTV/DTI detail, so
    # these are partial controls — see report caveats.
    micro = q(conn, engine, f"""
        SELECT group_re, is_denied, income_n, loan_amount_n,
               loan_purpose, loan_type, occupancy_type, state_code
        FROM lar WHERE activity_year = ? AND {BASE_WHERE}
              AND group_re != 'unknown'""", y)
    if len(micro) > 500:
        if len(micro) > 2_000_000:  # keep IRLS memory bounded
            micro = micro.sample(2_000_000, random_state=7)
        res = adjusted_odds_ratios(
            micro, "group_re", "is_denied",
            controls={"income_n": "numeric", "loan_amount_n": "numeric",
                      "loan_purpose": "categorical", "loan_type": "categorical",
                      "occupancy_type": "categorical"})
        adj = pd.DataFrame(res)
        if len(adj):
            adj["group"] = adj["group"].map(lambda g: GROUP_LABELS.get(g, g))
            adj = adj.rename(columns={
                "group": "Group", "odds_ratio": "Adjusted odds ratio",
                "ci_low": "CI low", "ci_high": "CI high", "n": "N"})
            for c in ("Adjusted odds ratio", "CI low", "CI high"):
                adj[c] = adj[c].round(3)
            out["adjusted_odds"] = adj

    sex = q(conn, engine, f"""
        SELECT applicant_sex AS sex, COUNT(*) AS apps,
               SUM(is_denied) AS denials
        FROM lar WHERE activity_year = ? AND {BASE_WHERE}
        GROUP BY applicant_sex""", y)
    sex["sex"] = sex["sex"].astype(str).map(lambda s: SEX.get(s, s))
    sex["Denial rate %"] = (100 * sex["denials"] / sex["apps"]).round(2)
    out["denial_by_sex"] = sex.rename(columns={
        "sex": "Applicant sex", "apps": "Applications", "denials": "Denials"})

    age = q(conn, engine, f"""
        SELECT applicant_age AS age, COUNT(*) AS apps,
               SUM(is_denied) AS denials
        FROM lar WHERE activity_year = ? AND {BASE_WHERE}
              AND applicant_age NOT IN ('8888', '9999')
        GROUP BY applicant_age""", y)
    age["Denial rate %"] = (100 * age["denials"] / age["apps"]).round(2)
    out["denial_by_age"] = age.rename(columns={
        "age": "Applicant age", "apps": "Applications", "denials": "Denials"})

    return out
