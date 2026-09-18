"""
state_seed.py  -  a self-contained, cited seed of AHCCCS state rules.

These are the real Chapter 10 / Chapter 19 rules from the PoC, transcribed with
their citations. The migration notebook writes them to main.sedo.state_rules so
the build does NOT depend on importing the old hand-authored Python modules
(ingest_ahcccs, demo_*, ch10_full_extract) — which is what failed.

Every rule carries origin='manual'. The grammar upgrades are already applied:
non-covered codes use code_not_covered; the CHW monthly cap uses
max_units_per_period. rule_id is left blank; the build derives a stable one.
"""
from __future__ import annotations

from schema import Rule

CH19 = "AHCCCS FFS Provider Billing Manual, Chapter 19 Behavioral Health Services"
CH19_URL = "https://www.azahcccs.gov/PlansProviders/Downloads/FFSProviderManual/FFS_Chap19BehavioralHealth.pdf"
CH10 = "AHCCCS FFS Provider Billing Manual, Chapter 10 Individual Practitioner Services"
CH10_URL = "https://www.azahcccs.gov/PlansProviders/Downloads/FFSProviderManual/FFS_Chap10.pdf"

_PAIR_FIELDS = ["proc_cd", "member_id", "provider_id", "srvc_bgn_dt", "mod_1", "mod_2", "mod_3", "mod_4"]
_UNIT_FIELDS = ["proc_cd", "units", "member_id", "srvc_bgn_dt"]


def _pair(a, b, svc, doc, url, loc, note):
    return Rule(origin="manual", plane="coverage_scope", binding_status="state_policy",
                statement=f"{a} and {b} may not be billed on the same day for the same member.",
                predicate={"type": "code_pair_prohibited", "code_a": a, "code_b": b,
                           "modifier_indicator": "0", "service_category": svc},
                codes=[a, b], population="all", machine_checkable=True,
                required_claim_fields=_PAIR_FIELDS,
                source_doc=doc, source_locator=loc, source_url=url, notes=note)


def _cap_day(code, n, doc, url, loc, note):
    return Rule(origin="manual", plane="coverage_scope", binding_status="state_policy",
                statement=f"{code}: maximum {n} unit(s) per day.",
                predicate={"type": "max_units_per_day", "code": code, "threshold": n},
                codes=[code], population="all", machine_checkable=True,
                required_claim_fields=_UNIT_FIELDS,
                source_doc=doc, source_locator=loc, source_url=url, notes=note)


def _not_covered(code, doc, url, loc, note):
    return Rule(origin="manual", plane="coverage_scope", binding_status="state_policy",
                statement=f"{code} is not a covered/reimbursable service; a paid claim is a finding.",
                predicate={"type": "code_not_covered", "code": code},
                codes=[code], population="all", machine_checkable=True,
                required_claim_fields=["proc_cd"],
                source_doc=doc, source_locator=loc, source_url=url, notes=note)


def _lead(statement, codes, reason, doc, url, loc, note, ambiguous=False, anote=None):
    return Rule(origin="manual", plane="coverage_scope", binding_status="state_policy",
                statement=statement,
                predicate={"type": "not_checkable", "reason": reason},
                codes=codes, population="all", machine_checkable=False,
                not_checkable_reason=reason, ambiguity_flag=ambiguous, ambiguity_note=anote,
                required_claim_fields=[],
                source_doc=doc, source_locator=loc, source_url=url, notes=note)


def seed_rules() -> list[Rule]:
    R: list[Rule] = []

    # ---- Chapter 19 (Behavioral Health) ----
    R.append(_cap_day("S5150", 48, CH19, CH19_URL, "page 30",
                      "Unskilled respite: up to 12 hours/day x 4 quarter-hour units = 48. The number 48 is derived, not verbatim."))
    R.append(_pair("S5151", "H2016", "state_bh", CH19, CH19_URL, "page 30",
                   "H2016 is a per-diem code (Ch.19 p.31). Resolved from open class 'or any other per diem code' (p.30)."))
    R.append(_pair("S5151", "S9485", "state_bh", CH19, CH19_URL, "page 30",
                   "S9485 is a per-diem code (Ch.19 p.23). Resolved from open class 'or any other per diem code' (p.30)."))
    # H2025/H2026/H2027 cannot be billed on the same day -> three pairs
    for a, b in [("H2025", "H2026"), ("H2025", "H2027"), ("H2026", "H2027")]:
        R.append(_pair(a, b, "state_bh", CH19, CH19_URL, "page 30",
                       'Verbatim: "Service codes H2025, H2026, and H2027 cannot be billed on the same day."'))
    R.append(_lead(
        "Respite services are limited to 600 hours per benefit year (Oct 1 - Sep 30) per member, "
        "inclusive of behavioral health and ALTCS respite.",
        ["S5150", "S5151"], "unit_incommensurability", CH19, CH19_URL, "page 30",
        "Cannot be mechanized: S5150 bills in 15-min units, S5151 per diem with no hours-per-diem conversion in policy. "
        "A policy finding, not a detector.",
        ambiguous=True,
        anote="S5150 hours are countable (units x 0.25); S5151 per-diem hours are undefined in any AHCCCS source."))

    # ---- Chapter 10 (Individual Practitioner) ----
    R.append(_lead(
        "AHCCCS follows Medicare's Correct Coding Initiative (CCI) and performs CCI edits/audits on FFS claims "
        "for the same provider, member, and date of service.",
        [], "policy_reference", CH10, CH10_URL, "page 2",
        "The legal basis that connects the NCCI quarterly ingest to Arizona Medicaid authority. Not itself a detector."))
    for code in ("98960", "98961", "98962"):
        R.append(_cap_day(code, 4, CH10, CH10_URL, "page 9",
                          "CHW/CHR daily cap. Source: 'a maximum of four units per day' (Ch.10 p.9)."))
    R.append(_pair("98960", "98961", "practitioner", CH10, CH10_URL, "page 9",
                   "CHW/CHR codes cannot be billed together on the same day for the same member (Ch.10 p.9)."))
    R.append(Rule(
        origin="manual", plane="coverage_scope", binding_status="state_policy",
        statement="CHW/CHR (98960/98961/98962): maximum 24 units per month per member, inclusive of all three codes.",
        predicate={"type": "max_units_per_period", "code_set": ["98960", "98961", "98962"],
                   "threshold": 24, "period": "month", "service_category": "practitioner"},
        codes=["98960", "98961", "98962"], population="all", machine_checkable=True,
        required_claim_fields=_UNIT_FIELDS,
        source_doc=CH10, source_locator="page 9", source_url=CH10_URL,
        notes="Monthly cross-code aggregation. Source: 'up to 24 units per month per member (inclusive of all 3 codes)' (Ch.10 p.9)."))
    R.append(_pair("99238", "99213", "practitioner", CH10, CH10_URL, "page 13",
                   "Discharge-management same-day prohibition. Sample of the 99201-99499 E/M range (Ch.10 p.13)."))
    R.append(_not_covered("00938", CH10, CH10_URL, "page 6", "Anesthesia for penile prosthesis insertion; non-covered."))
    R.append(_not_covered("99070", CH10, CH10_URL, "page 47", "Supplies/materials by physician; not reimbursed on FFS."))
    R.append(_not_covered("11975", CH10, CH10_URL, "page 17", "Norplant insertion; no longer distributed."))
    R.append(_not_covered("11977", CH10, CH10_URL, "page 17", "Norplant removal-with-reinsertion; no longer distributed."))
    R.append(_lead(
        "Well exams for adults 21+: the source states both 'not covered' and 'coverage reinstated effective 10/1/2013' "
        "in the same section.",
        [], "ambiguous_source", CH10, CH10_URL, "page 22",
        "In-place edit contradiction preserved for the policy owner to resolve.",
        ambiguous=True,
        anote="Both the old and new rule are present in the current revision without one superseding the other."))
    return R


CHAPTER_BY_DOC = {CH19: "19", CH10: "10"}
