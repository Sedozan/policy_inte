"""
detector_gen.py  -  compile rule dicts into SQL detectors. Deterministic, no LLM.

One row per rule. Machine-checkable rules with a known predicate type compile
to SQL against the claims table; everything else is a review lead with a reason.

Correctness rules baked in here:
  * PTP modifier bypass is gated on the edit's modifier indicator. Indicator 0
    emits NO bypass clause at all (no modifier can clear a hard denial).
    Indicator 1 checks the FULL NCCI PTP-associated modifier set - the PoC used
    six of ~30, so legitimately bypassed pairs (LT/RT, F1..) were flagged.
  * Every detector is windowed on ITS OWN effective_date / end_date. The
    deletion date is inclusive (CMS DelDt is the last active day). There is no
    global date floor.
  * MUE per-line vs per-date-of-service is honoured from the predicate type.
  * Detectors route on the claim's form type from the rule's service_category.
    A service category with no form mapping compiles UNROUTED and says so.
  * finding_severity travels with the detector so the findings table can be
    triaged without re-reading policy.
"""
from __future__ import annotations

import config

# ---------------------------------------------------------------- claims
CLAIMS = getattr(config, "CLAIMS_TABLE", "main.prod_input.all_data_C_A")

# physical column names come from config.CLAIM_COLS; nothing is hard-coded here
_CC = config.CLAIM_COLS
COL = {
    "claim_id":  _CC["claim_id"],
    "line_no":   _CC["line_no"],
    "member":    _CC["member"],
    "provider":  _CC["servicing_provider"],   # NCCI: same servicing provider
    "dos":       _CC["dos"],
    "proc":      _CC["code"],
    "units":     _CC["units"],
    "form":      _CC["form"],
    "mods":      list(_CC["mods"]),
}

# service_category -> FORM_TYP values. Derived from config.FORM_TYPE_MAP
# ({'A': 'practitioner', 'O': 'outpatient'}); extend with config.SERVICE_FORM_MAP
# e.g. {"dme": ["A"]} once the DME form type is confirmed in the claims table.
def _form_by_service() -> dict[str, list[str]]:
    m: dict[str, list[str]] = {}
    for form, svc in config.FORM_TYPE_MAP.items():
        m.setdefault(svc, []).append(form)
    m.update(getattr(config, "SERVICE_FORM_MAP", {}) or {})
    return m

# state policy applies to every FFS claim regardless of form
_UNROUTED_STATE = {"state_bh", "state", "all", None, ""}

# Full NCCI PTP-associated modifier set (Medicaid NCCI Policy Manual, Ch. 1).
NCCI_BYPASS_MODS = (
    "E1", "E2", "E3", "E4", "FA", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
    "TA", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9",
    "LT", "RT", "LC", "LD", "RC", "LM", "RI",          # anatomic
    "24", "25", "57", "58", "78", "79",                # global surgery
    "27", "59", "91", "XE", "XS", "XP", "XU",          # other
)


# --------------------------------------------------------------- helpers
def _q(code) -> str:
    s = str(code).strip().upper()
    if not s or not all(ch.isalnum() for ch in s):
        raise ValueError(f"unsafe procedure code {code!r}")
    return s


def _window(alias: str, rule: dict) -> str:
    conds = []
    eff, end = rule.get("effective_date"), rule.get("end_date")
    if eff:
        conds.append(f"CAST({alias}.{COL['dos']} AS DATE) >= DATE '{eff}'")
    if end:
        conds.append(f"CAST({alias}.{COL['dos']} AS DATE) <= DATE '{end}'")
    return ("\n  AND " + "\n  AND ".join(conds)) if conds else ""


def _routing(alias: str, service: str | None) -> tuple[str, str]:
    """Return (sql_clause, routing_label). NCCI editions route by CODE, not form,
    because all three land on FORM_TYP 'A' (config.NCCI_ROUTE_BY_FORM)."""
    if service in _UNROUTED_STATE:
        return "", "all_forms"
    if service in getattr(config, "NCCI_SERVICES", set()) and not getattr(config, "NCCI_ROUTE_BY_FORM", False):
        return "", "all_forms:ncci"
    forms = _form_by_service().get(service)
    if not forms:
        return "", f"UNROUTED:{service}"
    lst = ",".join(f"'{f}'" for f in forms)
    return f"\n  AND {alias}.{COL['form']} IN ({lst})", f"form:{','.join(forms)}"


def _line_cols(alias: str) -> str:
    return (f"{alias}.{COL['claim_id']} AS claim_id, {alias}.{COL['line_no']} AS line_no,\n"
            f"       {alias}.{COL['member']} AS member_id, {alias}.{COL['provider']} AS servicing_provider,\n"
            f"       {alias}.{COL['dos']} AS dos")


# -------------------------------------------------------------- compilers
def compile_code_pair(rule: dict) -> tuple[str, str, str]:
    p = rule["predicate"]
    a, b = _q(p["code_a"]), _q(p["code_b"])
    mi = str(p.get("modifier_indicator", "0"))
    route_sql, route = _routing("a", p.get("service_category"))
    if mi == "0":
        mod_clause, severity = "", "hard_denial"
    else:
        mods = ",".join(f"'{m}'" for m in NCCI_BYPASS_MODS)
        checks = " OR ".join(f"COALESCE(b.{m}, '') IN ({mods})" for m in COL["mods"])
        mod_clause, severity = f"\n  AND NOT ({checks})", "unbypassed_pair"
    sql = f"""SELECT {_line_cols('a')},
       '{b} billed with {a}' AS finding,
       1 AS observed_units, 0 AS allowed_units,
       '{severity}' AS finding_severity
FROM {CLAIMS} a
JOIN {CLAIMS} b
  ON  a.{COL['member']} = b.{COL['member']}
  AND a.{COL['provider']} = b.{COL['provider']}
  AND a.{COL['dos']} = b.{COL['dos']}
  AND NOT (a.{COL['claim_id']} = b.{COL['claim_id']} AND a.{COL['line_no']} = b.{COL['line_no']})
WHERE a.{COL['proc']} = '{a}' AND b.{COL['proc']} = '{b}'{mod_clause}{route_sql}{_window('a', rule)}"""
    return sql, severity, route


def compile_units_per_line(rule: dict) -> tuple[str, str, str]:
    p = rule["predicate"]
    code, val = _q(p["code"]), int(p["threshold"])
    route_sql, route = _routing("a", p.get("service_category"))
    severity = "units_over_limit"
    sql = f"""SELECT {_line_cols('a')},
       '{code} units per line > {val}' AS finding,
       a.{COL['units']} AS observed_units, {val} AS allowed_units,
       '{severity}' AS finding_severity
FROM {CLAIMS} a
WHERE a.{COL['proc']} = '{code}' AND a.{COL['units']} > {val}{route_sql}{_window('a', rule)}"""
    return sql, severity, route


def _compile_units_per_day(rule: dict, group_provider: bool) -> tuple[str, str, str]:
    p = rule["predicate"]
    code, val = _q(p["code"]), int(p["threshold"])
    route_sql, route = _routing("a", p.get("service_category"))
    mai = p.get("mai")
    severity = "documentation_reviewable" if mai == 3 else "units_over_limit"
    grp = [f"a.{COL['member']}"] + ([f"a.{COL['provider']}"] if group_provider else []) + [f"a.{COL['dos']}"]
    sel = (f"a.{COL['member']} AS member_id, "
           + (f"a.{COL['provider']} AS servicing_provider, " if group_provider else "")
           + f"a.{COL['dos']} AS dos")
    sql = f"""SELECT {sel},
       '{code} units per day > {val}' AS finding,
       SUM(a.{COL['units']}) AS observed_units, {val} AS allowed_units,
       '{severity}' AS finding_severity
FROM {CLAIMS} a
WHERE a.{COL['proc']} = '{code}'{route_sql}{_window('a', rule)}
GROUP BY {', '.join(grp)}
HAVING SUM(a.{COL['units']}) > {val}"""
    return sql, severity, route


def compile_units_per_dos(rule: dict):      # NCCI MAI 2/3: same provider, same member, same DOS
    return _compile_units_per_day(rule, group_provider=True)


def compile_units_per_day(rule: dict):      # state daily benefit cap: per member, per DOS
    return _compile_units_per_day(rule, group_provider=False)


_PERIOD_SQL = {
    "month":        "DATE_TRUNC('MONTH', CAST(a.{dos} AS DATE))",
    "year":         "YEAR(CAST(a.{dos} AS DATE))",
    # AHCCCS contract/benefit year runs Oct 1 - Sep 30
    "benefit_year": "YEAR(ADD_MONTHS(CAST(a.{dos} AS DATE), 3))",
}


def compile_units_per_period(rule: dict) -> tuple[str, str, str]:
    p = rule["predicate"]
    codes = [_q(c) for c in (p.get("code_set") or ([p["code"]] if p.get("code") else []))]
    if not codes:
        raise ValueError("max_units_per_period needs code or code_set")
    period = p.get("period", "month")
    if period not in _PERIOD_SQL:
        raise ValueError(f"unsupported period {period!r}")
    val = int(p["threshold"])
    bucket = _PERIOD_SQL[period].format(dos=COL["dos"])
    route_sql, route = _routing("a", p.get("service_category"))
    in_list = ",".join(f"'{c}'" for c in codes)
    severity = "units_over_limit"
    sql = f"""SELECT a.{COL['member']} AS member_id, {bucket} AS period_start,
       '{"/".join(codes)} units per {period} > {val}' AS finding,
       SUM(a.{COL['units']}) AS observed_units, {val} AS allowed_units,
       '{severity}' AS finding_severity
FROM {CLAIMS} a
WHERE a.{COL['proc']} IN ({in_list}){route_sql}{_window('a', rule)}
GROUP BY a.{COL['member']}, {bucket}
HAVING SUM(a.{COL['units']}) > {val}"""
    return sql, severity, route


def compile_code_not_covered(rule: dict) -> tuple[str, str, str]:
    p = rule["predicate"]
    codes = [_q(c) for c in (p.get("code_set") or [p["code"]])]
    route_sql, route = _routing("a", p.get("service_category"))
    in_list = ",".join(f"'{c}'" for c in codes)
    severity = "not_covered_paid"
    sql = f"""SELECT {_line_cols('a')},
       'non-covered code {"/".join(codes)} paid' AS finding,
       a.{COL['units']} AS observed_units, 0 AS allowed_units,
       '{severity}' AS finding_severity
FROM {CLAIMS} a
WHERE a.{COL['proc']} IN ({in_list}){route_sql}{_window('a', rule)}"""
    return sql, severity, route


_COMPILERS = {
    "code_pair_prohibited":  compile_code_pair,
    "max_units_per_line":    compile_units_per_line,
    "max_units_per_dos":     compile_units_per_dos,
    "max_units_per_day":     compile_units_per_day,
    "max_units_per_period":  compile_units_per_period,
    "code_not_covered":      compile_code_not_covered,
}

SUPPORTED_PREDICATES = tuple(_COMPILERS)


# ---------------------------------------------------------------- driver
def build_detectors(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules:
        p = r.get("predicate") or {}
        t = p.get("type")
        row = {
            "rule_id": r.get("rule_id"),
            "origin": r.get("origin"),
            "plane": r.get("plane"),
            "predicate_type": t,
            "service_category": p.get("service_category"),
            "statement": r.get("statement"),
            "source_doc": r.get("source_doc"),
            "source_locator": r.get("source_locator"),
            "source_url": r.get("source_url"),
            "effective_date": r.get("effective_date"),
            "end_date": r.get("end_date"),
            "routing": None,
            "compiled": False,
            "sql": None,
            "finding_severity": None,
            "reason": None,
        }
        if not r.get("machine_checkable", True) or t == "not_checkable":
            row["reason"] = r.get("not_checkable_reason") or p.get("reason") or "not_checkable"
            out.append(row)
            continue
        fn = _COMPILERS.get(t)
        if fn is None:
            row["reason"] = f"predicate '{t}' not in grammar"
            out.append(row)
            continue
        try:
            sql, severity, route = fn(r)
        except Exception as e:  # bad predicate payload -> lead, never a crash
            row["reason"] = f"compile error: {e}"
            out.append(row)
            continue
        row.update(compiled=True, sql=sql, finding_severity=severity, routing=route)
        out.append(row)
    return out


def summarize(detectors: list[dict]) -> dict:
    s = {"compiled": 0, "leads": 0, "unrouted": 0, "by_predicate": {}, "lead_reasons": {}}
    for d in detectors:
        key = d["predicate_type"]
        s["by_predicate"][key] = s["by_predicate"].get(key, 0) + 1
        if d["compiled"]:
            s["compiled"] += 1
            if str(d["routing"]).startswith("UNROUTED"):
                s["unrouted"] += 1
        else:
            s["leads"] += 1
            s["lead_reasons"][d["reason"]] = s["lead_reasons"].get(d["reason"], 0) + 1
    return s
