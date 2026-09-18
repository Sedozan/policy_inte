"""
rule_normalize.py  -  post-gate, pre-compile normalisation of rule dicts.

Runs on Rule.to_dict() output after validate_gate.gate(). Pure and idempotent.

  stable ids     rule_id is re-derived from CONTENT (origin, source, statement,
                 predicate, dates). The legacy assign_id() collided badly - one
                 id was shared by nine different Chapter 10 rules - which makes
                 policy_rules unusable as a keyed table.
  dedupe         identical content collapses to one row.
  plane          derived deterministically from predicate type; never hand-set,
                 never 'enforcement' (a claims outcome is not a policy plane).
  provenance     source_url / doc_hash filled for NCCI rows if missing.
"""
from __future__ import annotations

from typing import Iterable

MEDICAID_NCCI_URL = ("https://www.cms.gov/medicare/coding-billing/"
                     "ncci-medicaid/medicaid-ncci-edit-files")

# ----------------------------------------------------------------- plane
_PLANE_BY_PREDICATE = {
    "code_pair_prohibited":   "coding_edit",
    "max_units_per_line":     "coding_edit",
    "max_units_per_dos":      "coding_edit",
    "max_units_per_day":      "coding_edit",
    "max_units_per_period":   "coverage_scope",
    "max_dollars_per_period": "coverage_scope",
    "frequency_limit":        "coverage_scope",
    "code_not_covered":       "coverage_scope",
}
_PLANE_BY_REASON = {
    "requires_medical_record":   "medical_necessity",
    "requires_member_attribute": "coverage_scope",
    "cross_source_conflict":     "coverage_scope",
    "ambiguous_source":          "coverage_scope",
}


def derive_plane(rule: dict) -> str:
    pred = rule.get("predicate") or {}
    t = pred.get("type")
    if t == "not_checkable":
        reason = pred.get("reason") or rule.get("not_checkable_reason")
        return _PLANE_BY_REASON.get(reason, "coverage_scope")
    return _PLANE_BY_PREDICATE.get(t, "coverage_scope")


# ------------------------------------------------------------- stable id
from schema import content_signature, stable_rule_id, PLANES  # noqa: E402


# ------------------------------------------------------------ provenance
def enrich_provenance(rule: dict) -> dict:
    if rule.get("origin") != "ncci":
        return rule
    if not rule.get("source_url"):
        rule["source_url"] = MEDICAID_NCCI_URL
    if not rule.get("doc_hash"):
        rule["doc_hash"] = content_signature(rule)[:16]
    return rule


# ------------------------------------------------- cross-edition collapse
# NCCI publishes practitioner / outpatient / dme editions separately, and in this
# claims table all three land on FORM_TYP 'A' (form cannot tell them apart). So a
# code that appears in >1 edition would otherwise produce multiple detectors on the
# same 'A' claims, and the stricter one would false-positive the other edition's
# claims. We collapse per (predicate shape + codes) to the MOST PERMISSIVE limit:
#   MUE  -> the maximum threshold  (a finding then violates every edition)
#   PTP  -> indicator 1 if any edition allows a bypass, else 0
# and widen the date window to any edition's active span. The per-edition values
# are kept in predicate['editions'] and the note, for audit.
_MUE_TYPES = {"max_units_per_line", "max_units_per_dos", "max_units_per_day"}


def _edition_key(r: dict):
    if r.get("origin") != "ncci":
        return None
    p = r.get("predicate") or {}
    t = p.get("type")
    if t == "code_pair_prohibited":
        a, b = str(p.get("code_a")).upper(), str(p.get("code_b")).upper()
        return ("ptp", t, tuple(sorted((a, b))))
    if t in _MUE_TYPES:
        return (t, str(p.get("code")).upper())
    return None


def _widen_window(members: list[dict]) -> tuple:
    effs = [m.get("effective_date") for m in members]
    ends = [m.get("end_date") for m in members]
    eff = None if any(e in (None, "") for e in effs) else min(effs)
    end = None if any(e in (None, "") for e in ends) else max(ends)
    return eff, end


def collapse_ncci_editions(rules: list[dict]) -> tuple[list[dict], int]:
    groups: dict = {}
    passthrough: list[dict] = []
    for r in rules:
        k = _edition_key(r)
        (groups.setdefault(k, []).append(r) if k is not None else passthrough.append(r))
    collapsed = 0
    out = list(passthrough)
    for k, members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        svc_of = lambda m: (m.get("predicate") or {}).get("service_category")
        if k[0] == "ptp":
            winner = dict(max(members, key=lambda m: str((m.get("predicate") or {}).get("modifier_indicator")) == "1"))
            p = dict(winner["predicate"])
            any_bypass = any(str((m["predicate"]).get("modifier_indicator")) == "1" for m in members)
            p["modifier_indicator"] = "1" if any_bypass else "0"
            p["editions"] = {svc_of(m): str(m["predicate"].get("modifier_indicator")) for m in members}
            note_bits = ", ".join(f"{s}={i}" for s, i in p["editions"].items())
            detail = f"Collapsed across NCCI editions ({note_bits}); most-permissive indicator kept."
        else:
            winner = dict(max(members, key=lambda m: int((m.get("predicate") or {}).get("threshold", 0))))
            p = dict(winner["predicate"])
            p["editions"] = {svc_of(m): int(m["predicate"].get("threshold", 0)) for m in members}
            p["threshold"] = max(p["editions"].values())
            note_bits = ", ".join(f"{s}={v}" for s, v in p["editions"].items())
            detail = f"Collapsed across NCCI editions ({note_bits}); maximum (most-permissive) threshold kept."
        p["service_category"] = "ncci"
        winner["predicate"] = p
        eff, end = _widen_window(members)
        winner["effective_date"], winner["end_date"] = eff, end
        winner["notes"] = ((winner.get("notes") or "") + " " + detail).strip()
        if len(p["editions"]) > 1 and len(set(p["editions"].values())) > 1:
            winner["ambiguity_flag"] = True
            winner["ambiguity_note"] = ((winner.get("ambiguity_note") or "")
                                        + f" NCCI editions disagree ({note_bits}); using most permissive.").strip()
        out.append(winner)
        collapsed += len(members) - 1
    return out, collapsed


# ---------------------------------------------------------- orchestrator
def normalize_rules(rules: Iterable[dict], collapse_editions: bool = True) -> tuple[list[dict], dict]:
    rules = [dict(r) for r in rules]
    dropped_enforcement = sum(1 for r in rules
                              if r.get("plane") == "enforcement" or (r.get("predicate") or {}).get("type") == "edit_failure")
    rules = [r for r in rules
             if not (r.get("plane") == "enforcement" or (r.get("predicate") or {}).get("type") == "edit_failure")]
    editions_collapsed = 0
    if collapse_editions:
        rules, editions_collapsed = collapse_ncci_editions(rules)

    out: dict[str, dict] = {}
    collapsed = 0
    id_changed = 0
    for r in rules:
        r = dict(r)
        new_id = stable_rule_id(r)
        if r.get("rule_id") != new_id:
            id_changed += 1
        r["rule_id"] = new_id
        r["plane"] = derive_plane(r)
        r = enrich_provenance(r)
        if new_id in out:
            collapsed += 1
            continue
        out[new_id] = r

    rows = list(out.values())
    planes: dict[str, int] = {}
    for r in rows:
        planes[r["plane"]] = planes.get(r["plane"], 0) + 1
    report = {
        "rules_out": len(rows),
        "enforcement_rows_dropped": dropped_enforcement,
        "ncci_editions_collapsed": editions_collapsed,
        "duplicate_content_collapsed": collapsed,
        "rule_ids_rederived": id_changed,
        "plane_counts": planes,
    }
    return rows, report
