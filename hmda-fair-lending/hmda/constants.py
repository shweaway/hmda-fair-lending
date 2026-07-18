"""HMDA Modified LAR (2018+) schema, code decodes, and derivation logic.

Column order verified against the live HMDA File API header endpoint
(https://ffiec.cfpb.gov/file/modifiedLar/year/2025/institution/{lei}/txt/header)
in July 2026. Codes per the official 2018 Public LAR Code Sheet:
https://files.ffiec.cfpb.gov/documentation/2018-public-LAR-code-sheet.pdf
"""

# ---------------------------------------------------------------------------
# The pipe-delimited columns of a Modified LAR file (2018 onward), in order.
# ---------------------------------------------------------------------------
COLUMNS = [
    "activity_year", "lei", "loan_type", "loan_purpose", "preapproval",
    "construction_method", "occupancy_type", "loan_amount", "action_taken",
    "state_code", "county_code", "census_tract",
    "applicant_ethnicity_1", "applicant_ethnicity_2", "applicant_ethnicity_3",
    "applicant_ethnicity_4", "applicant_ethnicity_5",
    "co_applicant_ethnicity_1", "co_applicant_ethnicity_2",
    "co_applicant_ethnicity_3", "co_applicant_ethnicity_4",
    "co_applicant_ethnicity_5",
    "applicant_ethnicity_observed", "co_applicant_ethnicity_observed",
    "applicant_race_1", "applicant_race_2", "applicant_race_3",
    "applicant_race_4", "applicant_race_5",
    "co_applicant_race_1", "co_applicant_race_2", "co_applicant_race_3",
    "co_applicant_race_4", "co_applicant_race_5",
    "applicant_race_observed", "co_applicant_race_observed",
    "applicant_sex", "co_applicant_sex",
    "applicant_sex_observed", "co_applicant_sex_observed",
    "applicant_age", "applicant_age_above_62",
    "co_applicant_age", "co_applicant_age_above_62",
    "income", "purchaser_type", "rate_spread", "hoepa_status", "lien_status",
    "applicant_credit_scoring_model", "co_applicant_credit_scoring_model",
    "denial_reason_1", "denial_reason_2", "denial_reason_3", "denial_reason_4",
    "total_loan_costs", "total_points_and_fees", "origination_charges",
    "discount_points", "lender_credits", "interest_rate",
    "prepayment_penalty_term", "debt_to_income_ratio",
    "combined_loan_to_value_ratio", "loan_term", "intro_rate_period",
    "balloon_payment", "interest_only_payment", "negative_amortization",
    "other_non_amortizing_features", "property_value",
    "manufactured_home_secured_property_type",
    "manufactured_home_land_property_interest",
    "total_units", "multifamily_affordable_units",
    "submission_of_application", "initially_payable_to_institution",
    "aus_1", "aus_2", "aus_3", "aus_4", "aus_5",
    "reverse_mortgage", "open_end_line_of_credit",
    "business_or_commercial_purpose",
]

N_COLUMNS = len(COLUMNS)

# Sentinels used in public files
EXEMPT_CODES = {"Exempt", "1111"}
NA_CODES = {"", "NA", "N/A", "NULL", None}

# ---------------------------------------------------------------------------
# Code decodes (the values used in analysis/reporting)
# ---------------------------------------------------------------------------
ACTION_TAKEN = {
    "1": "Loan originated",
    "2": "Approved but not accepted",
    "3": "Application denied",
    "4": "Application withdrawn",
    "5": "File closed for incompleteness",
    "6": "Purchased loan",
    "7": "Preapproval request denied",
    "8": "Preapproval approved but not accepted",
}

LOAN_TYPE = {"1": "Conventional", "2": "FHA", "3": "VA", "4": "USDA/RHS-FSA"}

LOAN_PURPOSE = {
    "1": "Home purchase", "2": "Home improvement", "31": "Refinancing",
    "32": "Cash-out refinancing", "4": "Other purpose", "5": "Not applicable",
}

OCCUPANCY = {"1": "Principal residence", "2": "Second residence",
             "3": "Investment property"}

DENIAL_REASONS = {
    "1": "Debt-to-income ratio", "2": "Employment history",
    "3": "Credit history", "4": "Collateral",
    "5": "Insufficient cash", "6": "Unverifiable information",
    "7": "Credit application incomplete", "8": "Mortgage insurance denied",
    "9": "Other", "10": "Not applicable", "1111": "Exempt",
}

PURCHASER_TYPE = {
    "0": "Not sold (or NA)", "1": "Fannie Mae", "2": "Ginnie Mae",
    "3": "Freddie Mac", "4": "Farmer Mac", "5": "Private securitizer",
    "6": "Bank/S&L/CU purchaser", "71": "Credit union/mtg co/finance co",
    "72": "Life insurance co", "8": "Affiliate institution",
    "9": "Other purchaser",
}

HOEPA = {"1": "High-cost mortgage", "2": "Not a high-cost mortgage",
         "3": "Not applicable"}

SEX = {"1": "Male", "2": "Female", "3": "Not provided", "4": "Not applicable",
       "6": "Both male and female"}

AGE_BINS = ["<25", "25-34", "35-44", "45-54", "55-64", "65-74", ">74"]

AUS = {"1": "Desktop Underwriter", "2": "Loan Prospector/LPA", "3": "TOTAL",
       "4": "GUS", "5": "Other", "6": "Not applicable", "7": "Internal system",
       "1111": "Exempt"}

# Hispanic/Latino ethnicity codes (1 = H/L; 11-14 = subcategories)
_HISPANIC = {"1", "11", "12", "13", "14"}
_NOT_HISPANIC = {"2"}

# Race code roll-ups: 2x -> Asian, 4x -> Native Hawaiian/Pacific Islander
_RACE_PARENT = {
    "1": "aian",
    "2": "asian", "21": "asian", "22": "asian", "23": "asian", "24": "asian",
    "25": "asian", "26": "asian", "27": "asian",
    "3": "black",
    "4": "nhpi", "41": "nhpi", "42": "nhpi", "43": "nhpi", "44": "nhpi",
    "5": "white",
}

# Display names for derived groups
GROUP_LABELS = {
    "hispanic": "Hispanic or Latino",
    "aian": "American Indian/Alaska Native",
    "asian": "Asian",
    "black": "Black or African American",
    "nhpi": "Native Hawaiian/Pacific Islander",
    "white": "White (non-Hispanic)",
    "unknown": "Race/ethnicity not available",
}

# Ordered for tables: white is the conventional reference group
GROUP_ORDER = ["white", "black", "hispanic", "asian", "aian", "nhpi", "unknown"]


def derive_group(eth1, race_fields):
    """Collapse applicant ethnicity + race codes into one analysis group.

    Methodology (mirrors the approach in CFPB data point publications and
    most academic HMDA research):
      1. If the applicant reports any Hispanic/Latino ethnicity code
         (1, 11-14) -> 'hispanic', regardless of race.
      2. Otherwise use the first reported race code, rolled up to its parent
         category (Asian subcategories 21-27 -> Asian, etc.).
         Note: 'white' here therefore means non-Hispanic White.
      3. Codes 6/7/8 (info not provided / NA / no co-applicant) -> 'unknown'.

    Simplification vs. the CFPB 'derived_race' field: multiracial applicants
    are classified by their first-listed race, and co-applicant data is not
    used ('Joint' categories are omitted). Documented in the report.
    """
    e = (eth1 or "").strip()
    if e in _HISPANIC:
        return "hispanic"
    for r in race_fields:
        r = (r or "").strip()
        if not r:
            continue
        if r in _RACE_PARENT:
            return _RACE_PARENT[r]
        # 6 = info not provided, 7 = not applicable, 8 = no co-applicant
        return "unknown"
    return "unknown"


def parse_numeric(value):
    """Parse a Modified LAR numeric field. Returns float or None.

    Handles NA and Exempt/1111 sentinels.
    """
    if value is None:
        return None
    v = str(value).strip()
    if v in NA_CODES or v in EXEMPT_CODES:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def is_exempt(value):
    return str(value).strip() in EXEMPT_CODES
