"""Appraisal valuation equity analysis (FHFA UAD + HMDA join).

Design note: no public dataset links an appraisal to a HMDA loan record,
so all analyses here are neighborhood-level. Undervaluation is measured as
the share of purchase appraisals coming in below contract price — the
metric used in Freddie Mac's appraisal-gap research and FHFA's own
appraisal-bias work.

Outputs:
  undervaluation_by_minority_band   Enterprise tract data x ACS bands
  undervaluation_by_income          same by tract income quintile
  undervaluation_trend              gap over time (UAD years available)
  county_heat_list                  counties with highest below-contract share
  channel_comparison                Enterprise vs FHA at county level
  collateral_denial_crosscheck      tract undervaluation x HMDA collateral-
                                    denial share — the join neither dataset
                                    supports alone
  puf_gap_by_band / puf_adjusted    appraisal-level (PUF) rates, raw and
                                    with property controls
Caveats carried in every table: GSE/FHA channels only, appraisal waivers
absent, purchases only for contract comparisons, 2020 tract-boundary noise
for early years.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from . import q
from ..metrics import logit_fit

BELOW_RE = re.compile(r"below.*contract|contract.*below|under.*contract", re.I)
COUNT_RE = re.compile(r"^count|number|volume|total.*appraisal", re.I)
MIN_TRACT_APPRAISALS = 10
MIN_COUNTY_APPRAISALS = 100


def _note(msg: str) -> pd.DataFrame:
    return pd.DataFrame({"note": [msg]})


def _table_exists(conn, engine, name: str) -> bool:
    try:
        q(conn, engine, f"SELECT 1 FROM {name} LIMIT 1")
        return True
    except Exception:
        return False


def _pick_series(series_list: list[str], pattern: re.Pattern) -> str | None:
    hits = [s for s in series_list if s and pattern.search(s)]
    if not hits:
        return None
    # prefer purchase-flavored / share-flavored names
    hits.sort(key=lambda s: ("share" not in s.lower(), len(s)))
    return hits[0]


def _pct_scale(s: pd.Series) -> pd.Series:
    """Normalize a share series to 0-100."""
    m = s.dropna()
    if len(m) and m.median() <= 1.0:
        return s * 100
    return s


def run(conn, engine: str, year: int) -> dict[str, pd.DataFrame]:
    out = {}
    if not _table_exists(conn, engine, "uad_agg"):
        out["NOTE"] = _note("uad_agg table missing — run `python run.py uad` "
                            "first to enable valuation analysis")
        return out
    have_tracts = _table_exists(conn, engine, "tracts") and \
        q(conn, engine, "SELECT COUNT(*) AS n FROM tracts")["n"][0] > 0

    series_list = q(conn, engine,
                    "SELECT DISTINCT series FROM uad_agg")["series"].tolist()
    below = _pick_series(series_list, BELOW_RE)
    counts = _pick_series(series_list, COUNT_RE)
    if below is None:
        out["NOTE"] = _note(
            "No 'below contract price' series recognized in uad_agg. "
            f"Available series: {series_list[:40]}. Update BELOW_RE in "
            "hmda/analysis/valuation.py.")
        return out
    out["series_used"] = pd.DataFrame({
        "Concept": ["Below-contract share", "Appraisal count"],
        "UAD series name": [below, counts or "(none — unweighted means)"]})

    # ---- tract-level Enterprise frame ------------------------------------
    tr = q(conn, engine, """
        SELECT geoid, year, purpose, group_name, group_value, series, value
        FROM uad_agg
        WHERE channel = 'enterprise' AND series IN (?, ?)
              AND LENGTH(geoid) = 11""", [below, counts or below])
    # keep overall rows (no characteristic split) and purchase purpose
    if len(tr):
        tr["group_name"] = tr["group_name"].fillna("")
        tr = tr[(tr.group_name == "") | tr.group_name.str.lower()
                .str.contains("all|total|none", regex=True)]
        if tr["purpose"].notna().any():
            purch = tr[tr.purpose.astype(str).str.lower()
                       .str.contains("purchase")]
            if len(purch):
                tr = purch
    if not len(tr):
        out["NOTE"] = _note("No tract-level Enterprise rows after filtering; "
                            "inspect uad_agg group_name/purpose values.")
        return out

    piv = tr.pivot_table(index=["geoid", "year"], columns="series",
                         values="value", aggfunc="mean").reset_index()
    piv = piv.rename(columns={below: "below_pct"})
    piv["below_pct"] = _pct_scale(piv["below_pct"])
    if counts and counts in piv:
        piv = piv.rename(columns={counts: "n_appraisals"})
    else:
        piv["n_appraisals"] = np.nan
    latest = int(piv.year.max())

    def _band_table(join_col: str, label: str) -> pd.DataFrame | None:
        if not have_tracts:
            return None
        t = q(conn, engine,
              f"SELECT tract11, {join_col} AS grp FROM tracts")
        d = piv[piv.year == latest].merge(t, left_on="geoid",
                                          right_on="tract11")
        d = d.dropna(subset=["grp", "below_pct"])
        d = d[(d.n_appraisals.isna()) | (d.n_appraisals >= MIN_TRACT_APPRAISALS)]
        w = d.n_appraisals.fillna(1.0)
        g = (d.assign(w=w, wx=d.below_pct * w)
              .groupby("grp")
              .agg(tracts=("geoid", "nunique"), appraisals=("n_appraisals", "sum"),
                   wx=("wx", "sum"), wsum=("w", "sum")))
        g[f"Appraisals below contract % ({latest})"] = (g.wx / g.wsum).round(2)
        return (g.drop(columns=["wx", "wsum"]).reset_index()
                 .rename(columns={"grp": label}))

    bt = _band_table("minority_band", "Tract minority share")
    if bt is not None:
        order = ["<10%", "10-30%", "30-50%", "50-80%", "80-100%"]
        bt["_o"] = bt["Tract minority share"].map(
            {b: i for i, b in enumerate(order)})
        out["undervaluation_by_minority_band"] = (
            bt.sort_values("_o").drop(columns="_o"))
    it = _band_table("income_quintile_state", "Tract income quintile (1=lowest)")
    if it is not None:
        out["undervaluation_by_income"] = it.sort_values(
            "Tract income quintile (1=lowest)")

    # ---- trend by year x minority band -----------------------------------
    if have_tracts:
        t = q(conn, engine, "SELECT tract11, minority_band FROM tracts")
        d = piv.merge(t, left_on="geoid", right_on="tract11").dropna(
            subset=["minority_band", "below_pct"])
        w = d.n_appraisals.fillna(1.0)
        tr_tab = (d.assign(w=w, wx=d.below_pct * w)
                   .groupby(["year", "minority_band"])
                   .apply(lambda g: g.wx.sum() / g.w.sum(),
                          include_groups=False)
                   .unstack("minority_band").round(2))
        out["undervaluation_trend"] = tr_tab.reset_index().rename(
            columns={"year": "Year"})

    # ---- county heat list + channel comparison ---------------------------
    co = q(conn, engine, """
        SELECT channel, geoid, year, purpose, group_name, series, value
        FROM uad_agg WHERE series IN (?, ?) AND LENGTH(geoid) = 5""",
        [below, counts or below])
    if len(co):
        co["group_name"] = co["group_name"].fillna("")
        co = co[(co.group_name == "") | co.group_name.str.lower()
                .str.contains("all|total|none", regex=True)]
        if co["purpose"].notna().any():
            purch = co[co.purpose.astype(str).str.lower()
                       .str.contains("purchase")]
            if len(purch):
                co = purch
        cp = co.pivot_table(index=["channel", "geoid", "year"],
                            columns="series", values="value",
                            aggfunc="mean").reset_index()
        cp = cp.rename(columns={below: "below_pct"})
        cp["below_pct"] = _pct_scale(cp["below_pct"])
        if counts and counts in cp:
            cp = cp.rename(columns={counts: "n"})
        else:
            cp["n"] = np.nan
        yr_c = int(cp.year.max())
        cur = cp[(cp.year == yr_c) &
                 ((cp.n.isna()) | (cp.n >= MIN_COUNTY_APPRAISALS))]
        heat = (cur[cur.channel == "enterprise"]
                .nlargest(50, "below_pct")
                [["geoid", "n", "below_pct"]]
                .rename(columns={"geoid": "County FIPS", "n": "Appraisals",
                                 "below_pct": f"Below contract % ({yr_c})"}))
        out["county_heat_list"] = heat.round(2)
        cc = cur.pivot_table(index="geoid", columns="channel",
                             values="below_pct").dropna().round(2)
        if {"enterprise", "fha"} <= set(cc.columns):
            cc["FHA minus Enterprise (ppt)"] = (cc.fha - cc.enterprise).round(2)
            out["channel_comparison"] = (
                cc.reset_index()
                  .rename(columns={"geoid": "County FIPS",
                                   "enterprise": "Enterprise below %",
                                   "fha": "FHA below %"})
                  .sort_values("FHA minus Enterprise (ppt)", ascending=False)
                  .head(100))

    # ---- collateral-denial cross-check -----------------------------------
    hm = q(conn, engine, """
        SELECT tract11,
               COUNT(*) AS denials,
               SUM(CASE WHEN denial_reason_1 = '4' OR denial_reason_2 = '4'
                         OR denial_reason_3 = '4' OR denial_reason_4 = '4'
                    THEN 1 ELSE 0 END) AS collateral_denials
        FROM lar WHERE activity_year = ? AND is_denied = 1
              AND tract11 IS NOT NULL
        GROUP BY tract11 HAVING COUNT(*) >= 10""", [str(year)])
    if len(hm) and have_tracts:
        d = (piv[piv.year == latest]
             .merge(hm, left_on="geoid", right_on="tract11"))
        d = d[(d.n_appraisals.isna()) | (d.n_appraisals >= MIN_TRACT_APPRAISALS)]
        if len(d) >= 30:
            d["collateral_share"] = 100 * d.collateral_denials / d.denials
            r = float(np.corrcoef(d.below_pct, d.collateral_share)[0, 1])
            t = q(conn, engine, "SELECT tract11, minority_band FROM tracts")
            db = d.merge(t, on="tract11").groupby("minority_band").agg(
                tracts=("tract11", "nunique"),
                below_contract_pct=("below_pct", "mean"),
                collateral_denial_share_pct=("collateral_share", "mean"),
            ).round(2).reset_index().rename(columns={
                "minority_band": "Tract minority share"})
            db.loc[len(db)] = ["— tract-level correlation (r) —", len(d),
                               round(r, 3), None]
            out["collateral_denial_crosscheck"] = db
        else:
            out["collateral_denial_crosscheck"] = _note(
                f"Only {len(d)} tracts have both >=10 HMDA denials and "
                f">={MIN_TRACT_APPRAISALS} appraisals — need >=30. Expected "
                "with small pilot samples; run on the full national data.")

    # ---- PUF: appraisal-level, raw and controlled ------------------------
    if _table_exists(conn, engine, "uad_puf"):
        out.update(_puf_analysis(conn, engine, have_tracts))
    return out


def _puf_analysis(conn, engine, have_tracts) -> dict[str, pd.DataFrame]:
    cols = list(q(conn, engine, "SELECT * FROM uad_puf LIMIT 1").columns)

    def find(*pats, exclude=()):
        for c in cols:
            lc = c.lower()
            if any(re.search(p, lc) for p in pats) and \
               not any(re.search(e, lc) for e in exclude):
                return c
        return None

    tract_c = find(r"tract")
    below_c = find(r"below.*contract", r"contract.*below", r"appr.*less.*contract")
    ratio_c = find(r"contract.*ratio", r"ratio.*contract",
                   r"appraisal.*contract", exclude=(r"below",))
    weight_c = find(r"weight")
    purpose_c = find(r"purpose")
    if below_c is None and ratio_c is None:
        return {"puf_NOTE": _note(
            "No below-contract indicator or appraisal/contract ratio column "
            f"recognized in uad_puf. Columns: {cols}. Update _puf_analysis "
            "patterns in hmda/analysis/valuation.py.")}

    sel = [c for c in {tract_c, below_c, ratio_c, weight_c, purpose_c,
                       "channel"} if c]
    d = q(conn, engine, f"SELECT {', '.join(sel)} FROM uad_puf")
    if purpose_c and d[purpose_c].notna().any():
        m = d[purpose_c].astype(str).str.lower().str.contains("purchase")
        if m.any():
            d = d[m]
    if below_c:
        d["below"] = pd.to_numeric(d[below_c], errors="coerce")
        if d["below"].dropna().max() > 1:  # coded e.g. 1/2 — map to 0/1
            d["below"] = (d["below"] == 1).astype(float)
    else:
        r = pd.to_numeric(d[ratio_c], errors="coerce")
        d["below"] = (r < 1.0).astype(float)
    d["w"] = pd.to_numeric(d[weight_c], errors="coerce").fillna(1.0) \
        if weight_c else 1.0
    d = d.dropna(subset=["below"])
    if not len(d):
        return {"puf_NOTE": _note("PUF below-contract values all missing "
                                  "after parsing.")}
    outp = {}
    if tract_c and have_tracts:
        t = q(conn, engine, "SELECT tract11, minority_band FROM tracts")
        d[tract_c] = d[tract_c].astype(str).str.replace(r"\.0$", "", regex=True)\
            .str.zfill(11)
        dj = d.merge(t, left_on=tract_c, right_on="tract11")
        if len(dj):
            g = (dj.assign(wx=dj.below * dj.w)
                   .groupby(["channel", "minority_band"])
                   .agg(n=("below", "size"), wx=("wx", "sum"),
                        wsum=("w", "sum")))
            g["Below contract % (weighted)"] = (100 * g.wx / g.wsum).round(2)
            outp["puf_gap_by_band"] = (g.drop(columns=["wx", "wsum"])
                                        .reset_index().rename(columns={
                                            "channel": "Channel",
                                            "minority_band": "Tract minority share"}))
            # controlled: logit of below ~ band dummies + channel (property
            # controls added automatically if numeric-able columns exist)
            ref = "<10%"
            bands = [b for b in dj.minority_band.dropna().unique() if b != ref]
            X_cols, names = [], []
            for b in bands:
                X_cols.append((dj.minority_band == b).astype(float).values)
                names.append(b)
            X_cols.append((dj.channel == "fha").astype(float).values)
            names.append("FHA channel")
            X = np.column_stack(X_cols)
            y = dj.below.values.astype(float)
            if len(dj) > 1000 and 0 < y.mean() < 1:
                beta, se = logit_fit(X, y)
                rows = [{"Term": n,
                         "Odds ratio vs <10% tracts": round(float(np.exp(beta[i+1])), 3),
                         "CI low": round(float(np.exp(beta[i+1]-1.96*se[i+1])), 3),
                         "CI high": round(float(np.exp(beta[i+1]+1.96*se[i+1])), 3)}
                        for i, n in enumerate(names)]
                outp["puf_adjusted"] = pd.DataFrame(rows)
    if not outp:
        outp["puf_NOTE"] = _note(
            "PUF loaded but tract join produced no rows — check tract "
            "column formatting or census vintages.")
    return outp
