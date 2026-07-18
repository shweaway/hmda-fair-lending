"""Report generator: one Excel workbook + charts + an HTML summary."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def write_excel(results: dict[str, dict[str, pd.DataFrame]],
                out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as xl:
        toc = []
        for module, sheets in results.items():
            for name, df in sheets.items():
                sheet = f"{module[:10]}_{name}"[:31]
                df.to_excel(xl, sheet_name=sheet, index=False)
                toc.append({"Module": module, "Table": name, "Sheet": sheet,
                            "Rows": len(df)})
        pd.DataFrame(toc).to_excel(xl, sheet_name="_contents", index=False)
    return out_path


def make_charts(results: dict, charts_dir: str | Path) -> list[Path]:
    charts_dir = Path(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    made = []

    d = results.get("denials", {}).get("denial_by_group")
    if d is not None and len(d):
        fig, ax = plt.subplots(figsize=(9, 5))
        sub = d[d["Group"] != "Race/ethnicity not available"]
        ax.barh(sub["Group"], sub["Denial rate %"], color="#33546A")
        ax.errorbar(
            sub["Denial rate %"], range(len(sub)),
            xerr=[sub["Denial rate %"] - sub["CI low %"],
                  sub["CI high %"] - sub["Denial rate %"]],
            fmt="none", ecolor="#C86B52", capsize=3)
        ax.set_xlabel("Denial rate (%)")
        ax.set_title("Mortgage denial rates by race/ethnicity")
        ax.invert_yaxis()
        fig.tight_layout()
        p = charts_dir / "denial_rates.png"
        fig.savefig(p, dpi=150); plt.close(fig); made.append(p)

    r = results.get("redlining", {}).get("volume_by_minority_band")
    if r is not None and len(r) and "Denial rate %" in r:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.bar(r["Tract minority share"].astype(str), r["Denial rate %"],
               color="#33546A")
        ax.set_xlabel("Census tract minority population share")
        ax.set_ylabel("Denial rate (%)")
        ax.set_title("Denial rate by neighborhood minority share")
        fig.tight_layout()
        p = charts_dir / "denial_by_tract_minority.png"
        fig.savefig(p, dpi=150); plt.close(fig); made.append(p)

    pr = results.get("pricing", {}).get("rate_spread_by_group")
    if pr is not None and len(pr):
        fig, ax = plt.subplots(figsize=(9, 5))
        sub = pr[pr["Group"] != "Race/ethnicity not available"]
        ax.barh(sub["Group"], sub["Median"], color="#5A7D9A")
        ax.set_xlabel("Median rate spread over APOR (ppt)")
        ax.set_title("Median rate spread by race/ethnicity (reported loans)")
        ax.invert_yaxis()
        fig.tight_layout()
        p = charts_dir / "rate_spread.png"
        fig.savefig(p, dpi=150); plt.close(fig); made.append(p)

    return made


def write_html(results: dict, charts: list[Path], year: int,
               out_path: str | Path) -> Path:
    out_path = Path(out_path)
    parts = [f"""<!doctype html><html><head><meta charset="utf-8">
<title>HMDA {year} Fair Lending Analysis</title>
<style>
 body{{font-family:Georgia,serif;max-width:1100px;margin:2em auto;
      color:#1a1a1a;line-height:1.45}}
 h1{{border-bottom:3px solid #33546A}} h2{{color:#33546A;margin-top:2em}}
 table{{border-collapse:collapse;font-size:13px;font-family:Arial}}
 th,td{{border:1px solid #ccc;padding:4px 8px;text-align:right}}
 th{{background:#33546A;color:#fff}} td:first-child,th:first-child{{text-align:left}}
 .note{{background:#f6f1e7;padding:1em;border-left:4px solid #C86B52}}
 img{{max-width:100%}}
</style></head><body>
<h1>HMDA Modified LAR {year} — Fair Lending Analysis</h1>
<p class="note"><b>Read this first.</b> These are screening statistics from
public HMDA data. The public files exclude credit scores, precise DTI/LTV,
and other underwriting detail, so disparities shown here are evidence of
patterns that warrant scrutiny — not, by themselves, proof of unlawful
discrimination. Loans from partially-exempt filers drop out of pricing
tables entirely.</p>"""]
    for ch in charts:
        parts.append(f'<img src="charts/{ch.name}" alt="{ch.stem}">')
    for module, sheets in results.items():
        parts.append(f"<h2>{module.title()}</h2>")
        for name, df in sheets.items():
            show = df.head(40)
            parts.append(f"<h3>{name.replace('_', ' ')}</h3>")
            parts.append(show.to_html(index=False, border=0))
            if len(df) > 40:
                parts.append(f"<p><i>{len(df) - 40} more rows in the Excel "
                             "workbook.</i></p>")
    parts.append("</body></html>")
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path
