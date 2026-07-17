"""Statistical helpers: rate ratios, confidence intervals, two-proportion
tests, and a dependency-free logistic regression (IRLS on numpy) used for
adjusted denial-disparity estimates.

These are screening statistics in the fair lending sense: they identify
patterns warranting investigation. HMDA public data lacks credit score,
full DTI, and LTV detail, so disparities here are *not* proof of
discrimination — the report discusses this at length.
"""
from __future__ import annotations

import math

import numpy as np


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-proportion z-test p-value (two-sided, normal approx)."""
    if min(n1, n2) == 0:
        return float("nan")
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return float("nan")
    z = (p1 - p2) / se
    return 2 * (1 - _phi(abs(z)))


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def rate_ratio(k1, n1, k0, n0):
    """Rate for group 1 relative to reference group 0 (e.g. denial-rate
    ratio Black/White). Returns nan if undefined."""
    if not n1 or not n0 or not k0:
        return float("nan")
    return (k1 / n1) / (k0 / n0)


# ---------------------------------------------------------------------------
# Logistic regression via iteratively reweighted least squares (numpy only)
# ---------------------------------------------------------------------------
def logit_fit(X: np.ndarray, y: np.ndarray, max_iter: int = 60,
              tol: float = 1e-8, ridge: float = 1e-6):
    """Fit logistic regression; returns (beta, se).

    X: (n, p) design matrix WITHOUT intercept (added here).
    y: (n,) 0/1 outcomes.
    Ridge term stabilizes separation in sparse cells.
    """
    n, p = X.shape
    Xd = np.column_stack([np.ones(n), X])
    beta = np.zeros(p + 1)
    for _ in range(max_iter):
        eta = np.clip(Xd @ beta, -30, 30)
        mu = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(mu * (1 - mu), 1e-10, None)
        z = eta + (y - mu) / w
        XtW = Xd.T * w
        H = XtW @ Xd + ridge * np.eye(p + 1)
        beta_new = np.linalg.solve(H, XtW @ z)
        if np.max(np.abs(beta_new - beta)) < tol:
            beta = beta_new
            break
        beta = beta_new
    eta = np.clip(Xd @ beta, -30, 30)
    mu = 1.0 / (1.0 + np.exp(-eta))
    w = np.clip(mu * (1 - mu), 1e-10, None)
    H = (Xd.T * w) @ Xd + ridge * np.eye(p + 1)
    cov = np.linalg.inv(H)
    se = np.sqrt(np.diag(cov))
    return beta, se


def adjusted_odds_ratios(df, group_col: str, outcome_col: str,
                         controls: dict, reference: str = "white"):
    """Adjusted odds ratios of `outcome` by group vs reference.

    controls: {name: kind} where kind is 'numeric' (standardized) or
    'categorical' (one-hot, first level dropped).
    Returns list of dicts: group, odds_ratio, ci_low, ci_high, n.
    """
    import pandas as pd

    d = df.dropna(subset=[group_col, outcome_col]).copy()
    groups = [g for g in d[group_col].unique() if g != reference]
    cols = []
    names = []
    for g in groups:
        cols.append((d[group_col] == g).astype(float).values)
        names.append(("group", g))
    for c, kind in controls.items():
        if kind == "numeric":
            v = pd.to_numeric(d[c], errors="coerce")
            v = v.fillna(v.median())
            v = np.log1p(np.clip(v, 0, None))
            sd = v.std() or 1.0
            cols.append(((v - v.mean()) / sd).values)
            names.append(("ctl", c))
        else:
            levels = sorted(d[c].astype(str).unique())[1:]
            for lv in levels:
                cols.append((d[c].astype(str) == lv).astype(float).values)
                names.append(("ctl", f"{c}={lv}"))
    if not cols:
        return []
    X = np.column_stack(cols)
    y = d[outcome_col].astype(float).values
    beta, se = logit_fit(X, y)
    out = []
    for i, (kind, label) in enumerate(names):
        if kind != "group":
            continue
        b, s = beta[i + 1], se[i + 1]
        out.append({
            "group": label,
            "odds_ratio": math.exp(b),
            "ci_low": math.exp(b - 1.96 * s),
            "ci_high": math.exp(b + 1.96 * s),
            "n": int((d[group_col] == label).sum()),
        })
    return out
