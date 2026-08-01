# HMDA Fair Lending Analysis Framework

Download the complete HMDA **Modified LAR** (every reporting institution's
loan/application register) for any year 2018+, build a local analysis
database, and run five fair-lending analysis modules — including an
appraisal-equity module built on FHFA's Uniform Appraisal Dataset (UAD):

| Module | What it measures |
|---|---|
| `denials` | Denial rates and denial reasons by race/ethnicity, sex, age, income — raw, stratified, and regression-adjusted |
| `pricing` | Rate spread, note rate, loan costs, HOEPA high-cost share by group; tracks how much data the EGRRCPA partial exemption removes |
| `redlining` | Lending by census-tract minority share and income; per-lender majority-minority-tract screen vs. market benchmark (the method used in DOJ/CFPB redlining matters) |
| `institutions` | Top lenders, exemption usage, purchaser/securitization channels by group, AUS usage, county market concentration (HHI) |
| `valuation` | Appraisal equity via FHFA UAD: undervaluation (appraisals below contract price) by tract minority share and income, trend over time, FHA vs Enterprise, collateral-denial cross-check with HMDA |

Everything was validated end-to-end against a synthetic dataset with known
injected disparities (see `tests/make_test_data.py`) — the pipeline recovers
the planted denial-rate ratios, pricing gaps, and redlining signal — and the
parser was verified against live 2025 files from the HMDA File API.

## Setup (Windows)

1. Install Python 3.10+ from https://python.org (check "Add to PATH").
2. In a terminal, from this folder:

```
pip install -r requirements.txt
```

DuckDB is the analysis engine. If it isn't installed the code silently
falls back to SQLite (works, but slower on the full national file).

## Run

```
python run.py all --year 2025
```

or step by step:

```
python run.py download --year 2025     # ~4,000-5,000 files, 3-5 GB, a few hours
python run.py load     --year 2025     # parse + build data/hmda.db
python run.py census                   # ACS tract demographics (redlining)
python run.py uad                      # FHFA appraisal data (valuation module)
python run.py analyze  --year 2025     # -> outputs/hmda_2025_analysis.xlsx + summary.html
```

Notes:

- **Resumable.** Stop the download anytime; rerunning skips finished files.
- **Smoke test first:** `python run.py download --year 2025 --limit 50`
  then `load` + `analyze` to see the whole thing work in minutes.
- **Multi-year trends:** run download/load for each year 2018-2024 too;
  the database accumulates years side by side.
- Optional: set a free Census API key (`setx CENSUS_API_KEY yourkey`)
  if the census step gets rate-limited.

## Interactive explorers (`analyst-explorer` branch)

For pattern-hunting that a static workbook can't do:

```
python run.py explore --year 2025
```

writes three **single self-contained HTML files** (no server, no
internet, shareable by email) to `outputs/`:

### `explorer_{year}.html` — tracts × demography

- Filters for state, county, tract minority-share band, state income
  quintile, applicant group, and minimum volume — every chart, stat, and
  table re-renders against the same slice.
- A denial-rate vs. minority-share scatter (dot = tract, size = volume)
  and a denial-by-band chart for spotting gradients.
- A sortable tract table with a **Δ vs county** column — each tract's
  denial rate against its county's overall rate for the selected group —
  plus FIPS search to track down specific tracts.
- Click any tract for a drill-down: demography, volumes, denial rate by
  applicant group within that tract, and loan-type mix.

### `lenders_{year}.html` — institutions × tracts × demography

How each lender performs against the market benchmark (every lender in
the current state scope):

- A footprint scatter: each lender's share of applications from
  majority-minority tracts vs. its denial rate, with the market's share
  as a reference line — the interactive version of the DOJ/CFPB-style
  redlining screen.
- A sortable lender table: volume, denial rate, MM-tract share and Δ vs
  market, Black–White denial gap, and EGRRCPA pricing-exempt share.
- Click a lender for a drill-down: side-by-side lender-vs-market bars
  for application share by tract minority band and denial rate by
  applicant group, plus a fact sheet.

### `loans_{year}.html` — loan parameters × pricing

Slice by loan type (channel), purpose, occupancy, loan-amount band, and
applicant group; every stat, chart, and table follows the slice:

- KPIs: denial rate, mean rate spread over APOR, high-priced share
  (≥1.5 ppt), mean note rate, and **pricing visible %** — how much of
  the slice the EGRRCPA exemption leaves dark.
- Denial rate by amount band and mean rate spread by group, recomputed
  within the slice.
- A pivot table: choose the breakdown dimension (amount, type, purpose,
  occupancy, group) and read denial, origination, spread, high-priced,
  HOEPA, and pricing-visibility per row. Pricing stats cover originated
  first-lien loans with reported pricing, matching the `pricing` module.

### `run.py serve` — unified slice explorer (every filter combined)

The three files above embed pre-aggregated cubes, which caps how many
dimensions can cross. For arbitrary combinations, run the local web app:

```
python run.py serve --year 2025          # opens http://127.0.0.1:8600
```

It queries the DuckDB/SQLite database live (localhost only, stdlib
HTTP server, no new dependencies), so **any** combination of geography
(state, county, tract minority band, income quintile), lender, loan
parameters (type, purpose, occupancy, amount band), and applicant group
works — with any of those as the "split by" dimension. KPIs, denial and
rate-spread charts, and a full-metric breakdown table follow the slice.
Example: Redline-screen a single lender's conventional lending inside
80-100% minority tracts, split by applicant group — one query.

Rates from fewer than 10 applications are suppressed (shown as ·) —
tiny denominators mislead more than they inform. The full-national file
embeds every tract with activity (roughly 85k), so expect a file in the
~10 MB range; it loads locally in any modern browser. County display
names appear automatically if the `county_language` table from the
market-insights branch is present; otherwise counties show as FIPS codes.

## Demo without downloading anything

```
python tests/make_test_data.py --data-dir data_test
python run.py load    --year 2025 --data-dir data_test --db data_test/hmda.db
python run.py census  --data-dir data_test --db data_test/hmda.db  # uses bundled synthetic census cache
python run.py analyze --year 2025 --db data_test/hmda.db --out demo_out
```

(`example_output/` in this folder is exactly that demo's result.)

Note: there's no bundled synthetic UAD fixture, so the demo's `valuation`
sheet will just show a note to run `python run.py uad` first — everything
else (denials, pricing, redlining, institutions) runs normally. To see
`valuation` populated, run the real `uad` step against a loaded database:
`python run.py uad --data-dir data && python run.py analyze --year 2025`.

## Data sources (verified July 2026)

- Filer roster: `https://ffiec.cfpb.gov/v2/reporting/filers/{year}`
- Modified LAR: `https://ffiec.cfpb.gov/file/modifiedLar/year/{year}/institution/{lei}/txt`
- Schema: https://ffiec.cfpb.gov/documentation/publications/modified-lar/modified-lar-schema
- Code sheet: https://files.ffiec.cfpb.gov/documentation/2018-public-LAR-code-sheet.pdf
- Census: ACS 5-year API (`api.census.gov`), variables B03002, B19013
- Appraisals: FHFA UAD Aggregate Statistics v3.3 + Appraisal-Level PUF (Enterprise v2.1, FHA v1.0) from fhfa.gov — `--skip-puf` to skip the large appraisal-level files

## Interpreting results — important caveats

1. **Screening, not proof.** Public HMDA data omits credit score, exact
   DTI/LTV, and reserves. Disparities here identify patterns that merit
   investigation; they are not by themselves evidence of illegal
   discrimination.
2. **Modified-LAR privacy edits.** Loan amount is rounded to the midpoint
   of a $10k bin, age is binned, and income is rounded — small
   measurement error is expected.
3. **Partial exemption blind spot.** Filers under the 2018 EGRRCPA
   exemption report `Exempt` for pricing, DTI, LTV, denial reasons, and
   AUS. The `pricing` and `institutions` modules quantify exactly how much
   of the market is invisible — this is a headline policy finding, not
   just a nuisance.
4. **Derived race/ethnicity** uses the applicant's first-listed race and
   Hispanic ethnicity precedence (standard research practice); joint and
   multiracial detail is collapsed.
5. **Appraisal-HMDA join is neighborhood-level, not loan-level.** No public
   dataset links an individual appraisal to a HMDA record, so `valuation`
   measures undervaluation (below-contract-price share) by census tract and
   correlates it with HMDA collateral-denial share at the tract level —
   never at the individual-loan level. It also covers GSE/FHA channels
   only (no portfolio/private loans), excludes appraisal waivers, compares
   purchase-money contracts only, and early UAD years carry some
   2020-tract-boundary noise.

## Alternative bulk source

If you want one big file instead of per-institution files, the CFPB also
publishes the **Snapshot National Loan-Level Dataset** (all filers combined,
with derived census fields included). The Modified LAR approach used here is
what updates continuously and matches the per-institution publication, but
for pure speed the snapshot is worth knowing about:
https://ffiec.cfpb.gov/data-publication/snapshot-national-loan-level-dataset
