# HMDA Fair Lending Analysis Framework

Download the complete HMDA **Modified LAR** (every reporting institution's
loan/application register) for any year 2018+, build a local analysis
database, and run four fair-lending analysis modules:

| Module | What it measures |
|---|---|
| `denials` | Denial rates and denial reasons by race/ethnicity, sex, age, income — raw, stratified, and regression-adjusted |
| `pricing` | Rate spread, note rate, loan costs, HOEPA high-cost share by group; tracks how much data the EGRRCPA partial exemption removes |
| `redlining` | Lending by census-tract minority share and income; per-lender majority-minority-tract screen vs. market benchmark (the method used in DOJ/CFPB redlining matters) |
| `institutions` | Top lenders, exemption usage, purchaser/securitization channels by group, AUS usage, county market concentration (HHI) |

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

## Demo without downloading anything

```
python tests/make_test_data.py --data-dir data_test
python run.py load    --year 2025 --data-dir data_test --db data_test/hmda.db
python run.py census  --data-dir data_test --db data_test/hmda.db  # uses bundled synthetic census cache
python run.py analyze --year 2025 --db data_test/hmda.db --out demo_out
```

(`example_output/` in this folder is exactly that demo's result.)

## Data sources (verified July 2026)

- Filer roster: `https://ffiec.cfpb.gov/v2/reporting/filers/{year}`
- Modified LAR: `https://ffiec.cfpb.gov/file/modifiedLar/year/{year}/institution/{lei}/txt`
- Schema: https://ffiec.cfpb.gov/documentation/publications/modified-lar/modified-lar-schema
- Code sheet: https://files.ffiec.cfpb.gov/documentation/2018-public-LAR-code-sheet.pdf
- Census: ACS 5-year API (`api.census.gov`), variables B03002, B19013

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

## Alternative bulk source

If you want one big file instead of per-institution files, the CFPB also
publishes the **Snapshot National Loan-Level Dataset** (all filers combined,
with derived census fields included). The Modified LAR approach used here is
what updates continuously and matches the per-institution publication, but
for pure speed the snapshot is worth knowing about:
https://ffiec.cfpb.gov/data-publication/snapshot-national-loan-level-dataset
