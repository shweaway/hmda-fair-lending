"""Generate synthetic Modified LAR files with KNOWN disparities, so the
whole pipeline can be validated without downloading anything.

Injected ground truth (what the analysis must recover):
  * Denial rates: white 10%, asian 12%, hispanic 18%, black 22%
  * Rate spread (originated): white ~0.45, black ~0.75, hispanic ~0.70
  * Lender GOODBANK00000000001 mirrors market minority-tract share;
    lender REDLINEBK0000000001 under-serves majority-minority tracts
    (redlining signal)
  * Lender EXEMPTBANK000000001 reports pricing fields as Exempt
  * 3 synthetic counties x 8 tracts with minority_pct from 5% to 95%

Also writes a matching synthetic census CSV and roster JSON.

Usage:  python tests/make_test_data.py --data-dir data_test --n 40000
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from hmda.constants import COLUMNS  # noqa: E402

LENDERS = {
    "GOODBANK00000000001": {"share": 0.45, "redline": False, "exempt": False,
                            "name": "Good Bank NA"},
    "REDLINEBK0000000001": {"share": 0.25, "redline": True, "exempt": False,
                            "name": "Redline Bancorp"},
    "EXEMPTBANK000000001": {"share": 0.15, "redline": False, "exempt": True,
                            "name": "Exempt Community Bank"},
    "BIGLENDER0000000001": {"share": 0.15, "redline": False, "exempt": False,
                            "name": "Big Lender Mortgage LLC"},
}

# 3 counties x 8 tracts; minority pct rises within county
COUNTIES = ["28089", "28049", "28121"]
TRACTS = []
for c in COUNTIES:
    for i in range(8):
        pct = [5, 12, 25, 38, 55, 68, 82, 95][i]
        TRACTS.append((f"{c}9{i:05d}", c, pct))

GROUPS = [
    ("white",    0.55, ("2", "5"),  0.10, 0.45),
    ("black",    0.15, ("2", "3"),  0.22, 0.75),
    ("hispanic", 0.15, ("1", "5"),  0.18, 0.70),
    ("asian",    0.15, ("2", "2"),  0.12, 0.40),
]


def blank_row():
    return {c: "" for c in COLUMNS}


def make_row(rng, year, lei, cfg):
    g = rng.choices(GROUPS, weights=[w for _, w, _, _, _ in GROUPS])[0]
    name, _, (eth, race), p_deny, spread_mu = g

    if cfg["redline"]:
        # under-serve high-minority tracts
        weights = [max(0.05, 1.0 - pct / 100 * 1.6) for _, _, pct in TRACTS]
    else:
        weights = [1.0] * len(TRACTS)
    tract, county, pct = rng.choices(TRACTS, weights=weights)[0]

    denied = rng.random() < p_deny
    action = "3" if denied else ("1" if rng.random() < 0.9 else "2")

    r = blank_row()
    r.update({
        "activity_year": str(year), "lei": lei,
        "loan_type": rng.choice(["1", "1", "1", "2", "3"]),
        "loan_purpose": rng.choice(["1", "1", "31", "32", "2"]),
        "preapproval": "2", "construction_method": "1",
        "occupancy_type": rng.choice(["1", "1", "1", "2", "3"]),
        "loan_amount": str(rng.randrange(10, 80) * 10000 + 5000),
        "action_taken": action,
        "state_code": "MS", "county_code": county, "census_tract": tract,
        "applicant_ethnicity_1": eth, "co_applicant_ethnicity_1": "5",
        "applicant_ethnicity_observed": "2",
        "co_applicant_ethnicity_observed": "4",
        "applicant_race_1": race, "co_applicant_race_1": "8",
        "applicant_race_observed": "2", "co_applicant_race_observed": "4",
        "applicant_sex": rng.choice(["1", "2"]), "co_applicant_sex": "5",
        "applicant_sex_observed": "2", "co_applicant_sex_observed": "4",
        "applicant_age": rng.choice(["25-34", "35-44", "45-54", "55-64"]),
        "applicant_age_above_62": "No",
        "co_applicant_age": "9999", "co_applicant_age_above_62": "NA",
        "income": str(int(max(15, rng.gauss(95 if name != 'black' else 78, 40)))),
        "purchaser_type": rng.choice(["0", "1", "2", "3", "6"]),
        "hoepa_status": "3" if action != "1" else
                        ("1" if rng.random() < 0.002 else "2"),
        "lien_status": "1",
        "denial_reason_1": (rng.choice(["1", "3", "3", "4", "5"])
                            if denied else "10"),
        "total_units": "1", "submission_of_application": "1",
        "initially_payable_to_institution": "1",
        "aus_1": rng.choice(["1", "2", "6"]),
        "reverse_mortgage": "2", "open_end_line_of_credit": "2",
        "business_or_commercial_purpose": "2",
    })
    if cfg["exempt"]:
        for c in ("rate_spread", "total_loan_costs", "origination_charges",
                  "discount_points", "lender_credits", "interest_rate",
                  "debt_to_income_ratio", "combined_loan_to_value_ratio",
                  "property_value"):
            r[c] = "Exempt"
        r["denial_reason_1"] = "1111"
        r["aus_1"] = "1111"
    elif action == "1":
        r["rate_spread"] = f"{max(0.0, rng.gauss(spread_mu, 0.35)):.3f}"
        r["interest_rate"] = f"{rng.gauss(6.6 + (spread_mu - 0.45), 0.5):.3f}"
        r["total_loan_costs"] = f"{max(500, rng.gauss(4800, 1500)):.2f}"
        r["property_value"] = str(rng.randrange(15, 90) * 10000 + 5000)
    else:
        r["rate_spread"] = "NA"
        r["interest_rate"] = "NA"
    return [r[c] for c in COLUMNS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data_test")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--n", type=int, default=40_000)
    a = ap.parse_args()

    rng = random.Random(42)
    raw = Path(a.data_dir) / "raw" / str(a.year)
    raw.mkdir(parents=True, exist_ok=True)

    per = {lei: int(a.n * cfg["share"]) for lei, cfg in LENDERS.items()}
    for lei, cfg in LENDERS.items():
        with (raw / f"{lei}.txt").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="|")
            for _ in range(per[lei]):
                w.writerow(make_row(rng, a.year, lei, cfg))
        print(f"  {lei}: {per[lei]} rows")

    roster = [{"lei": lei, "name": cfg["name"]}
              for lei, cfg in LENDERS.items()]
    (Path(a.data_dir) / f"roster_{a.year}.json").write_text(
        json.dumps(roster), encoding="utf-8")

    with (Path(a.data_dir) / "census_tracts_2023.csv").open(
            "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tract11", "state_fips", "county_fips",
                    "total_pop", "minority_pct", "median_income"])
        for t, c, pct in TRACTS:
            w.writerow([t, "28", c, 4000,
                        pct, int(75000 - 450 * pct)])

    make_language_fixture(rng, Path(a.data_dir))
    make_uad_fixtures(rng, Path(a.data_dir), a.year)
    print(f"Synthetic data in {a.data_dir}/ "
          f"(ground truth documented in this file's docstring)")


def make_language_fixture(rng, data_dir: Path) -> None:
    """County-level ACS C16001 stand-in for the markets module demo."""
    from hmda.language import LANGUAGES

    counties = {c for _, c, _ in TRACTS}
    cols = ["county_fips", "county_name", "pop5plus", "english_only"]
    for k in LANGUAGES:
        cols += [f"{k}_total", f"{k}_lep"]
    with (data_dir / "county_language_2023.csv").open(
            "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for c in sorted(counties):
            pop = rng.randint(80_000, 200_000)
            row = [c, f"Test County {c}, Mississippi",
                   pop, int(pop * 0.82)]
            for k in LANGUAGES:
                total = rng.randint(200, int(pop * 0.06))
                row += [total, int(total * rng.uniform(0.2, 0.6))]
            w.writerow(row)


def make_uad_fixtures(rng, data_dir: Path, year: int) -> None:
    """Synthetic UAD zips with a PLANTED valuation gap.

    Ground truth: below-contract share rises linearly with tract minority
    share, from ~6% (<10% minority) to ~14% (80-100%); FHA counties run
    ~2ppt above Enterprise. The valuation module must recover a monotonic
    gradient and a positive FHA-minus-Enterprise difference.
    """
    import io as _io
    import zipfile as _zip

    uad_dir = data_dir / "uad"
    uad_dir.mkdir(parents=True, exist_ok=True)

    def below_for(pct):  # planted relationship
        return 6.0 + 8.0 * (pct / 100.0)

    years = list(range(year - 3, year + 1))

    def agg_rows(geolevel):
        rows = [["GEOLEVEL", "GEOID", "SERIES", "PURPOSE", "CHARACTERISTIC",
                 "CATEGORY", "YEAR", "QUARTER", "VALUE"]]
        for y in years:
            if geolevel == "tract":
                for t, c, pct in TRACTS:
                    b = below_for(pct) + rng.gauss(0, 0.4) + 0.3 * (y - year)
                    rows.append(["Tract", t,
                                 "Share of Appraisals Below Contract Price",
                                 "Purchase", "All", "All", y, "Annual",
                                 round(b, 2)])
                    rows.append(["Tract", t, "Count of Appraisals",
                                 "Purchase", "All", "All", y, "Annual",
                                 rng.randrange(40, 400)])
            else:
                for c in COUNTIES:
                    pct = sum(p for _, cc, p in TRACTS if cc == c) / 8
                    bump = 2.0 if geolevel == "county_fha" else 0.0
                    b = below_for(pct) + bump + rng.gauss(0, 0.3)
                    rows.append(["County", c,
                                 "Share of Appraisals Below Contract Price",
                                 "Purchase", "All", "All", y, "Annual",
                                 round(b, 2)])
                    rows.append(["County", c, "Count of Appraisals",
                                 "Purchase", "All", "All", y, "Annual",
                                 rng.randrange(500, 3000)])
        return rows

    def write_zip(name, rows):
        buf = _io.StringIO()
        csv.writer(buf).writerows(rows)
        with _zip.ZipFile(uad_dir / name, "w", _zip.ZIP_DEFLATED) as z:
            z.writestr(name.replace(".zip", ".csv"), buf.getvalue())

    write_zip("ent_sf_tract.zip", agg_rows("tract"))
    write_zip("ent_sf_county.zip", agg_rows("county"))
    write_zip("fha_sf_county.zip", agg_rows("county_fha"))

    # PUF: appraisal-level with the same planted gradient
    for channel, fname in (("ent", "puf_ent.zip"), ("fha", "puf_fha.zip")):
        rows = [["APPRAISAL_YEAR", "PURPOSE", "CENSUS_TRACT",
                 "APPRAISAL_BELOW_CONTRACT_FLAG", "WEIGHT"]]
        for _ in range(6000):
            t, c, pct = rng.choices(TRACTS)[0]
            p_below = (below_for(pct) + (2.0 if channel == "fha" else 0)) / 100
            rows.append([rng.choice(years), "Purchase", t,
                         1 if rng.random() < p_below else 0,
                         round(rng.uniform(15, 25), 1)])
        write_zip(fname, rows)
    print(f"  UAD fixtures in {uad_dir}")


if __name__ == "__main__":
    main()
