"""
config.py  -  environment and claims-schema mapping for the Policy KB.

Everything that names a table or a column lives here. Nothing else in the
package hard-codes a physical column name.
"""
import os

MODE = os.environ.get("PKB_MODE", "DATABRICKS")

# ---------------------------------------------------------------- sources
CATALOG = "main"
SCHEMA = "sedo"                                   # NCCI tables + state_rules live here
STATE_RULES_TABLE = f"{CATALOG}.{SCHEMA}.state_rules"

# ---------------------------------------------------------------- outputs
OUTPUT_CATALOG = "main"
OUTPUT_SCHEMA = "policy_kb"

# ---------------------------------------------------------------- claims
CLAIMS_TABLE = os.environ.get("PKB_CLAIMS", "main.prod_input.all_data_C_A")

# logical name -> physical column (verified against DESCRIBE all_data_C_A)
CLAIM_COLS = {
    "claim_id":            "ClaimID",
    "line_no":             "LN_NO",
    "member":              "MEMBER_KEY",
    "servicing_provider":  "SProv_ID",
    "billing_provider":    "BProv_ID",
    "dos":                 "Svc_Begin_Dt",
    "code":                "PROC_CD",
    "units":               "QUANTITY_PAID",      # confirmed: units of service, decimal(15,4)
    "paid_amt":            "PMT_AMT",
    "pos":                 "POS_CODE",
    "form":                "FORM_TYP",
    "mods":                ["PROC_MOD_1", "PROC_MOD_2", "PROC_MOD_3", "PROC_MOD_4"],
}

# Logical claim fields a rule may require. The gate downgrades a rule that
# needs anything not listed here to a review lead (reason=missing_claim_field)
# instead of emitting SQL that silently returns nothing.
AVAILABLE_CLAIM_FIELDS = {
    "proc_cd", "member_id", "provider_id", "billing_provider_id", "srvc_bgn_dt",
    "units", "quantity_paid", "mod_1", "mod_2", "mod_3", "mod_4",
    "claim_id", "line_no", "pos_cd", "paid_amt", "paid_ind", "form_type",
}

# FORM_TYP value -> service category. Observed values in all_data_C_A (2026-09-18):
# 'A' (professional; carries practitioner AND DME AND ~96% of outpatient-edition codes)
# and 'D' (a small facility form). There is no 'O'.
FORM_TYPE_MAP = {"A": "practitioner", "D": "outpatient"}
SERVICE_FORM_MAP: dict = {}

# The three NCCI editions all land on FORM_TYP 'A', so form type CANNOT separate
# them (verified: DME 162k lines on 'A', outpatient 5.66M on 'A' vs 61k on 'D',
# practitioner on 'A'). NCCI detectors therefore route by procedure code, not by
# form, and codes that appear in more than one edition are collapsed to the most
# permissive limit (see rule_normalize.collapse_ncci_editions) so a shared 'A'
# claim is never false-positived by the stricter edition.
# Flip to True ONLY once a real claim-type field distinguishes practitioner /
# facility / DME-supplier claims; form type does not.
NCCI_ROUTE_BY_FORM = False
NCCI_SERVICES = {"practitioner", "outpatient", "dme", "ncci"}

# ------------------------------------------------------- policy documents
# AHCCCS FFS Provider Billing Manual. URLs verified against
# azahcccs.gov/PlansProviders/RatesAndBilling/FFS/providermanual.html on 2026-09-18.
# The file-name pattern is NOT uniform across chapters, so every URL is explicit.
_FFS = "https://www.azahcccs.gov/PlansProviders/Downloads/FFSProviderManual/"
CHAPTERS = {
    "1":  ("Introduction to AHCCCS",                          _FFS + "FFSChapter1IntroductiontoAHCCCS.pdf"),
    "2":  ("Eligibility",                                     _FFS + "FFSChapter2Eligibility.pdf"),
    "3":  ("Provider Records and Registration",               _FFS + "FFSChapter3ProviderRecordsandRegistration.pdf"),
    "4":  ("General Billing Rules",                           _FFS + "FFS_Chap04GeneralBillingRules.pdf"),
    "5":  ("Billing on the CMS 1500 Claim Form",              _FFS + "FFS_Chap05.pdf"),
    "6":  ("Billing on the UB-04 Claim Form",                 _FFS + "FFS_Chap06.pdf"),
    "7":  ("Billing on the ADA 2012 Claim Form",              _FFS + "FFS_Chap07.pdf"),
    "8":  ("Prior Authorizations",                            _FFS + "FFSChap08PriorAuthorizations.pdf"),
    "9":  ("Medicare/Other Insurance Liability",              _FFS + "FFS_Chap09Medicare.pdf"),
    "10": ("Individual Practitioner Services",                _FFS + "FFS_Chap10.pdf"),
    "10A": ("FQHC/RHC (Chapter 10 Addendum)",                 _FFS + "FFS_Chap_10AddendumFQHC.pdf"),
    "11": ("Hospital Services",                               _FFS + "FFSChap11_HospitalServices.pdf"),
    "11A": ("APR-DRG Payment Policies (Chapter 11 Addendum)", _FFS + "FFS_Chap11_Addendum.pdf"),
    "12": ("Pharmacy Services",                               _FFS + "FFS_Chap12Pharmacy.pdf"),
    "13": ("DME, Orthotics, Prosthetics, Medical Supplies",   _FFS + "FFS_Chap13DME.pdf"),
    "14": ("Transportation Services",                         _FFS + "FFS_Chap14Transportation.pdf"),
    "15": ("Dialysis Services",                               _FFS + "FFS_Chap15Dialysis.pdf"),
    "16": ("Free-Standing Ambulatory Surgery Centers",        _FFS + "FFS_Chap16AmbulatorySurgeryCenters.pdf"),
    "17": ("Free Standing Birthing Centers",                  _FFS + "FFS_Chap17BirthingCenters.pdf"),
    "18": ("Federal Emergency Services Program",              _FFS + "FFS_Chap18EmergencyServicesProgram.pdf"),
    "19": ("Behavioral Health Services",                      _FFS + "FFS_Chap19BehavioralHealth.pdf"),
    "20": ("Home Health Care Services",                       _FFS + "FFS_Chap20HomeHealthCare.pdf"),
    "21": ("ALTCS Services",                                  _FFS + "FFS_Chap21ALTCS.pdf"),
    "22": ("Nursing Facility Services",                       _FFS + "FFS_Chap22NursingFacility.pdf"),
    "23": ("Hospice Services",                                _FFS + "FFS_Chap23Hospice.pdf"),
    "24": ("Transplants",                                     _FFS + "FFS_Chap24Transplants.pdf"),
    "25": ("Claims Processing",                               _FFS + "FFS_Chap25ClaimsProcessing.pdf"),
    "26": ("Correcting Claim Errors",                         _FFS + "FFS_Chap26ClaimErrors.pdf"),
    "27": ("Understanding the Remittance Advice",             _FFS + "FFS_Chap27RemittanceAdvice.pdf"),
    "28": ("Claim Disputes",                                  _FFS + "FFS_Chap28ClaimDisputes.pdf"),
    "29": ("Housing and Health Opportunities (H2O) Services", _FFS + "FFS_Chap29H2OServices.pdf"),
}
# Rule-dense chapters for the MVP. Chapters 1-3, 5-9, 25-28 are process/eligibility
# text and yield few billing rules; run them once the pipeline is trusted.
MVP_CHAPTERS = ["4", "10", "13", "14", "19", "22"]

# Where PDFs are stored on Databricks (a Unity Catalog Volume). The extractor
# downloads into it when the workspace has internet egress; otherwise upload the
# PDFs here by hand and it reads them.
PDF_DIR = os.environ.get("PKB_PDF_DIR", "/Volumes/main/sedo/policy_docs")

# ------------------------------------------------------------- extraction
# STUB  : deterministic pattern extractor - runs the whole pipeline with no model
#         (tests, smoke runs, and a floor for simple rule shapes).
# DBFM  : Databricks Foundation Model serving endpoint (OpenAI-compatible).
# HFLOCAL: transformers on the cluster (gpt-oss with harmony parsing).
LLM_BACKEND = os.environ.get("PKB_LLM", "STUB")
DBFM_ENDPOINT = os.environ.get("PKB_DBFM_ENDPOINT", "databricks-gpt-oss-120b")
HF_MODEL = os.environ.get("PKB_HF_MODEL", "openai/gpt-oss-20b")
LLM_MAX_TOKENS = 2048
LLM_TEMPERATURE = 0.0          # extraction must be repeatable
PROMPT_VERSION = "v1"          # bump when the prompt changes; keys the cache

# ---------------------------------------------------------------- legacy
# Kept so the PoC authoring modules (ingest_ahcccs, demo_*, ch10_full_extract)
# still import cleanly. Not used by the MVP build.
CLAIM_COLS["provider"] = CLAIM_COLS["servicing_provider"]
CLAIM_COLS["claim"] = CLAIM_COLS["claim_id"]
CLAIM_COLS["member_id"] = CLAIM_COLS["member"]
POC_CODE_SET = None                                # None = scope from claims
AHCCCS_PDF_DIR = PDF_DIR
