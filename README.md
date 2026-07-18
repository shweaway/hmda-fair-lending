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

## Demo without downloading anything

```
python tests/make_test_data.py --data-dir data_test
python run.py load    --year 2025 --data-dir data_test --db data_test/hmda.db
python run.py census  --data-dir data_test --db data_test/hmda.db  # uses bundled synthetic census cache
python run.py analyze --year 2025 --db data_test/hmda.db --out demo_out
python run.py market  --year 2025 --data-dir data_test --db data_test/hmda.db --out demo_out  # marketing brief
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

## Marketing insights (`market-insights` branch)

The same database can answer inclusive-marketing questions: where is
mortgage demand, which communities drive it, what languages do they
speak, and where is the market underserved or thinly competed.

```
python run.py market --year 2025
```

writes `outputs/market_insights_2025.xlsx` + `market_insights.html` with:

| Sheet | What it tells a marketing team |
|---|---|
| `guardrails` | Permitted vs. prohibited uses — always the first sheet |
| `county_market_size` | Largest county markets: applications, originations, volume, denial rate |
| `county_group_mix` | Demographic mix of application demand per county |
| `language_national` / `language_by_county` | Speakers and limited-English (LEP) population per language group (ACS C16001) — which languages to localize creative in, and where |
| `underserved_tracts` | Majority-minority tracts originating below their county rate — the inclusive-outreach opportunity list |
| `product_mix_by_group` | Conventional/FHA/VA/USDA shares within each community |
| `demand_trend_by_group` | Application demand by community across all loaded years |
| `thin_competition` | High-volume counties with concentrated lending (HHI ≥ 2500) |

The language sheets need a free Census API key
(`setx CENSUS_API_KEY yourkey`); without one the step still runs and
marks those sheets with a note.

**Fair-lending guardrails — read before using any of this.** ECOA and
the Fair Housing Act permit *inclusive* marketing: adding languages,
channels, community partnerships, and outreach that welcome underserved
groups (the same logic behind CRA performance and Special Purpose Credit
Programs, Reg B § 1002.8). They prohibit the reverse: using neighborhood
or group demographics to **exclude, discourage, or avoid** marketing to
anyone (redlining / digital redlining), to target **less favorable
products or terms** at protected groups (reverse redlining), to feed
protected-class data into **credit or pricing decisions**, or to build
ad-platform audiences that proxy protected classes (housing ads face
special targeting restrictions on major platforms since the 2019
HUD/Facebook settlement). Route campaigns built on these outputs through
fair-lending/compliance review and document the inclusive intent.

## Alternative bulk source

If you want one big file instead of per-institution files, the CFPB also
publishes the **Snapshot National Loan-Level Dataset** (all filers combined,
with derived census fields included). The Modified LAR approach used here is
what updates continuously and matches the per-institution publication, but
for pure speed the snapshot is worth knowing about:
https://ffiec.cfpb.gov/data-publication/snapshot-national-loan-level-dataset
