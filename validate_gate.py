"""
validate_gate.py  -  nothing enters the KB unvetted.

gate(rules) -> (accepted: list[dict], rejected: list[dict])

  rejected     structurally unusable (no statement, no source, no predicate type)
  accepted     everything else, as dicts, with:
      validation_status  clean        nothing changed
                         patched      the gate normalised something (codes upper-cased,
                                      codes derived from the predicate, ...)
                         downgraded   machine_checkable was turned off because the
                                      predicate is not in the grammar or a required
                                      claim field is not in the extract
                         needs_review ambiguity_flag set, or authored not-checkable
      gate_notes         what was changed / why, one line per action

The gate never resolves ambiguity and never deletes a rule: a rule that cannot
be checked by machine is kept as a lead so the coverage finding survives.
"""
from __future__ import annotations

import config
from schema import Rule, PREDICATE_TYPES, PREDICATE_TYPES_COMPILED

_CODE_KEYS = ("code_a", "code_b", "code")


def _codes_from_predicate(p: dict) -> list[str]:
    out = []
    for k in _CODE_KEYS:
        if p.get(k):
            out.append(str(p[k]).strip().upper())
    for c in p.get("code_set") or []:
        out.append(str(c).strip().upper())
    return list(dict.fromkeys(out))


def _check(rule: Rule) -> tuple[Rule, list[str], str]:
    notes: list[str] = []
    status = "clean"
    p = rule.predicate or {}

    # ---- codes: normalise, derive from predicate if the author left them out
    codes = [str(c).strip().upper() for c in (rule.codes or []) if str(c).strip()]
    if codes != list(rule.codes or []):
        notes.append("codes normalised")
        status = "patched"
    derived = _codes_from_predicate(p)
    if not codes and derived:
        codes = derived
        notes.append("codes derived from predicate")
        status = "patched"
    rule.codes = codes

    # ---- predicate type must be in the grammar
    t = p.get("type")
    if t not in PREDICATE_TYPES:
        if rule.machine_checkable:
            rule.machine_checkable = False
            rule.not_checkable_reason = "predicate_not_in_grammar"
            notes.append(f"downgraded: predicate '{t}' not in grammar")
            status = "downgraded"
    elif t not in PREDICATE_TYPES_COMPILED and t != "not_checkable" and rule.machine_checkable:
        rule.machine_checkable = False
        rule.not_checkable_reason = rule.not_checkable_reason or "predicate_not_in_grammar"
        notes.append(f"downgraded: predicate '{t}' authorable but not yet compiled")
        status = "downgraded"

    # ---- authored not_checkable must say why
    if t == "not_checkable":
        rule.machine_checkable = False
        rule.not_checkable_reason = rule.not_checkable_reason or p.get("reason") or "no_claim_field"

    # ---- required claim fields must exist in the extract
    if rule.machine_checkable:
        missing = sorted(set(rule.required_claim_fields or []) - config.AVAILABLE_CLAIM_FIELDS)
        if missing:
            rule.machine_checkable = False
            rule.not_checkable_reason = "missing_claim_field"
            notes.append("downgraded: claim fields not in extract: " + ", ".join(missing))
            status = "downgraded"

    # ---- code-pair sanity: a pair needs two distinct codes
    if t == "code_pair_prohibited" and rule.machine_checkable:
        a, b = str(p.get("code_a", "")).upper(), str(p.get("code_b", "")).upper()
        if not a or not b or a == b:
            rule.machine_checkable = False
            rule.not_checkable_reason = "ambiguous_source"
            notes.append("downgraded: code pair incomplete or self-pair")
            status = "downgraded"

    if rule.ambiguity_flag or not rule.machine_checkable:
        status = "needs_review" if status in ("clean", "patched") else status
    return rule, notes, status


def gate(rules: list[Rule]) -> tuple[list[dict], list[dict]]:
    accepted, rejected = [], []
    for r in rules:
        probs = r.problems()
        if probs:
            d = r.to_dict()
            d["reject_reason"] = "; ".join(probs)
            rejected.append(d)
            continue
        r, notes, status = _check(r)
        d = r.to_dict()
        d["validation_status"] = status
        d["gate_notes"] = "; ".join(notes)
        accepted.append(d)
    return accepted, rejected


def summarize(accepted: list[dict], rejected: list[dict]) -> dict:
    by = {}
    for d in accepted:
        by[d["validation_status"]] = by.get(d["validation_status"], 0) + 1
    return {"accepted": len(accepted), "rejected": len(rejected), "by_status": by}
