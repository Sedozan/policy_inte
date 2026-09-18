"""
schema.py  -  the rule object. This IS the product.

Everything downstream (detector SQL, gap analysis, review workbook, narratives)
reads this and nothing else. Interpretation happens here, once, and is
human-reviewable; downstream is mechanical translation only.

MVP changes vs the PoC:
  * rule_id is derived from CONTENT (source + statement + predicate + dates).
    The PoC hashed codes + predicate type + source, so nine different Chapter 10
    rules with codes=[] shared one id.
  * 'enforcement' is no longer a plane. A claims outcome ("a hard-denial pair
    was paid") is a finding the detector labels, not a policy authority. It is
    still accepted on read so old authoring modules do not crash; the build
    re-derives plane from the predicate anyway.
  * Predicate grammar gained max_units_per_dos, max_units_per_period,
    code_not_covered (compiled) and keeps the not-yet-compiled types so rules
    can be authored ahead of the compiler and land as leads, never as errors.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------- vocab
PLANES = {"coding_edit", "coverage_scope", "medical_necessity"}
LEGACY_PLANES = {"enforcement"}          # accepted on read, never written

BINDING = {"mandatory", "state_plan", "state_policy", "contract", "guidance", "advisory"}
BINDING_RANK = {"mandatory": 0, "state_plan": 1, "state_policy": 2,
                "contract": 3, "guidance": 4, "advisory": 5}

# Closed predicate grammar. COMPILED types have a SQL compiler in detector_gen;
# AUTHORABLE types are legal to write but land as review leads until compiled.
PREDICATE_TYPES_COMPILED = {
    "code_pair_prohibited",    # code_a, code_b, modifier_indicator, service_category
    "max_units_per_line",      # code, threshold                       (NCCI MUE / MAI 1)
    "max_units_per_dos",       # code, threshold, mai                  (NCCI MUE MAI 2/3)
    "max_units_per_day",       # code, threshold                       (state daily cap, per member)
    "max_units_per_period",    # code|code_set, threshold, period      (month|year|benefit_year)
    "code_not_covered",        # code|code_set
}
PREDICATE_TYPES_AUTHORABLE = {
    "max_dollars_per_period",  # needs paid amount + benefit year + often age
    "frequency_limit",
    "provider_type_allowed",
    "modifier_required",
    "not_checkable",           # reason
    "edit_failure",            # legacy; dropped by the build
}
PREDICATE_TYPES = PREDICATE_TYPES_COMPILED | PREDICATE_TYPES_AUTHORABLE

NOT_CHECKABLE_REASONS = {
    "no_claim_field", "missing_claim_field", "requires_member_attribute",
    "requires_medical_record", "ambiguous_source", "cross_source_conflict",
    "unit_incommensurability", "policy_reference", "monthly_aggregation_not_in_grammar",
    "predicate_not_in_grammar", "process_obligation",
}


# ------------------------------------------------------------ stable id
def content_signature(d: dict) -> str:
    pred = d.get("predicate") or {}
    parts = [d.get("origin"), d.get("source_doc"), d.get("source_locator"),
             (d.get("statement") or "").strip(),
             json.dumps(pred, sort_keys=True, default=str),
             d.get("effective_date"), d.get("end_date")]
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def stable_rule_id(d: dict) -> str:
    prefix = "NCCI" if d.get("origin") == "ncci" else "MAN"
    return f"{prefix}-{content_signature(d)[:16]}"


# ------------------------------------------------------------------ Rule
@dataclass
class Rule:
    origin: str                          # "ncci" | "manual" (hand-authored) | "extracted" (PDF pipeline)
    plane: str                           # see PLANES
    binding_status: str                  # see BINDING
    statement: str                       # one plain-English sentence, the rule as interpreted
    predicate: dict                      # structured form; type in PREDICATE_TYPES
    codes: list = field(default_factory=list)
    population: str = "all"
    machine_checkable: bool = True
    not_checkable_reason: Optional[str] = None
    required_claim_fields: list = field(default_factory=list)
    ambiguity_flag: bool = False
    ambiguity_note: Optional[str] = None
    alternates: list = field(default_factory=list)      # [{"reading":..., "locator":...}]
    source_doc: str = ""
    source_locator: str = ""             # page / section / row
    source_url: str = ""
    doc_version: str = ""
    doc_hash: str = ""
    effective_date: Optional[str] = None # ISO; None = from the beginning of the data
    end_date: Optional[str] = None       # ISO, inclusive; None = still in force
    deactivated: bool = False
    supersedes: Optional[list] = None
    notes: str = ""
    rule_id: str = ""

    # --- lifecycle -----------------------------------------------------
    def assign_id(self) -> "Rule":
        self.rule_id = stable_rule_id(self.to_dict())
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    # --- validation ----------------------------------------------------
    def problems(self) -> list[str]:
        """Structural problems that make a rule unusable (-> rejected by the gate)."""
        p = []
        if not (self.statement or "").strip():
            p.append("empty statement")
        if not (self.source_doc or "").strip():
            p.append("no source_doc")
        if not isinstance(self.predicate, dict) or not self.predicate.get("type"):
            p.append("predicate has no type")
        if self.plane not in PLANES | LEGACY_PLANES:
            p.append(f"unknown plane {self.plane!r}")
        if self.binding_status not in BINDING:
            p.append(f"unknown binding_status {self.binding_status!r}")
        return p
