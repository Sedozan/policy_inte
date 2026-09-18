"""Offline checks against the REAL package modules (no Spark). Run: cd pie_mvp && python -m pytest tests -q"""
import os, sys, json
import pandas as pd

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(HERE))

import ingest_ncci_tables as ing
import rule_normalize as rn
import detector_gen as dg
import state_rules as sr
import validate_gate as vg
from schema import Rule


def meta(service, edit, q, y):
    return {"table": f"medicaid_ncci_edit_{service}_{edit}_q{q}_{y}", "fqn": "x",
            "service": service, "edit_type": edit, "quarter": q, "year": y,
            "label": f"{y}Q{q}", "starts": f"{y}-{ing.QUARTER_START[q]}"}


# ------------------------------------------------------------------ PTP
def test_ptp_both_codes_and_indicator9_and_dates():
    df = pd.DataFrame({
        "Column 1": ["90791", "90791", "99213", "12345"],
        "Column 2": ["90832", "90833", "36415", "67890"],
        "Effective Date": ["20210101", "20210101", "20200101", "20200101"],
        "Deletion Date": ["*", "20241231", "*", "*"],
        "Modifier Indicator": ["0", "1", "9", "1"],
        "PTP Edit Rationale": ["Misuse", "Standards", "x", "y"],
    })
    scope = {"90791", "90832", "90833", "99213"}   # 12345/67890 out of scope
    rules = ing.ptp_rules(df, meta("practitioner", "ptp", 4, 2026), scope)
    assert [r.predicate["code_b"] for r in rules] == ["90832", "90833"]   # 9 skipped, pair out of scope skipped
    r0, r1 = rules
    assert r0.effective_date == "2021-01-01" and r0.end_date is None
    assert r1.end_date == "2024-12-31"
    assert r0.source_url and r0.doc_hash
    assert all(r.plane != "enforcement" for r in rules)


# ------------------------------------------------------------------ MUE
def test_mue_runs_close_on_change_and_absence():
    q2 = pd.DataFrame({"HCPCS/CPT Code": ["A", "B", "C"], "Practitioner Services MUE Values": [4, 2, 1], "MUE Rationale": ["", "", ""]})
    q3 = pd.DataFrame({"HCPCS/CPT Code": ["A", "B"], "Practitioner Services MUE Values": [4, 3], "MUE Rationale": ["", "", ][:2]})
    q4 = pd.DataFrame({"HCPCS/CPT Code": ["A", "B", "C"], "Practitioner Services MUE Values": [4, 3, 5], "MUE Rationale": ["", "", ""]})
    frames = [(meta("practitioner", "mue", 2, 2026), q2), (meta("practitioner", "mue", 3, 2026), q3), (meta("practitioner", "mue", 4, 2026), q4)]
    rules = ing.mue_history(frames)
    by = {}
    for r in rules:
        by.setdefault(r.predicate["code"], []).append((r.effective_date, r.end_date, r.predicate["threshold"]))
    assert by["A"] == [("2026-04-01", None, 4)]                              # unchanged across all quarters
    assert by["B"] == [("2026-04-01", "2026-06-30", 2), ("2026-07-01", None, 3)]  # value change
    assert by["C"] == [("2026-04-01", "2026-06-30", 1), ("2026-10-01", None, 5)]  # absent in Q3 -> closed, reopened Q4
    assert all(r.predicate["type"] == "max_units_per_line" for r in rules)     # no MAI column


def test_mue_mai_honoured_when_present():
    q = pd.DataFrame({"HCPCS/CPT Code": ["X", "Y"], "MUE Values": [2, 3], "MUE Adjudication Indicator": [1, 2]})
    rules = ing.mue_history([(meta("dme", "mue", 4, 2026), q)])
    t = {r.predicate["code"]: r.predicate["type"] for r in rules}
    assert t == {"X": "max_units_per_line", "Y": "max_units_per_dos"}


# ------------------------------------------------------- normalize + ids
def test_normalize_fixes_id_collisions_and_planes():
    from schema import Rule
    a = Rule(origin="manual", plane="coverage_scope", binding_status="state_policy", statement="rule one",
             predicate={"type": "not_checkable", "reason": "no_claim_field"}, codes=[], source_doc="Ch10",
             machine_checkable=False, not_checkable_reason="no_claim_field").assign_id()
    b = Rule(origin="manual", plane="coverage_scope", binding_status="state_policy", statement="rule two",
             predicate={"type": "not_checkable", "reason": "requires_medical_record"}, codes=[], source_doc="Ch10",
             machine_checkable=False, not_checkable_reason="requires_medical_record").assign_id()
    assert a.rule_id != b.rule_id                       # content-derived ids: no collision
    enf = {"origin": "ncci", "plane": "enforcement", "statement": "x", "predicate": {"type": "edit_failure"}}
    rows, rep = rn.normalize_rules([a.to_dict(), b.to_dict(), enf, a.to_dict()])
    assert len(rows) == 2 and len({r["rule_id"] for r in rows}) == 2
    assert rep["enforcement_rows_dropped"] == 1 and rep["duplicate_content_collapsed"] == 1
    assert rep["rule_ids_rederived"] == 0                # schema and normalize agree
    planes = {r["statement"]: r["plane"] for r in rows}
    assert planes == {"rule one": "coverage_scope", "rule two": "medical_necessity"}


# ------------------------------------------------------------ compilers
def _rule(pred, eff=None, end=None, mc=True, origin="ncci"):
    return {"rule_id": "T", "origin": origin, "statement": "s", "predicate": pred,
            "effective_date": eff, "end_date": end, "machine_checkable": mc}


def test_ptp_indicator0_has_no_bypass_clause_and_windows():
    d = dg.build_detectors([_rule({"type": "code_pair_prohibited", "code_a": "90791", "code_b": "90832",
                                   "modifier_indicator": "0", "service_category": "practitioner"},
                                  eff="2021-01-01", end="2024-12-31")])[0]
    assert d["compiled"] and d["finding_severity"] == "hard_denial"
    assert "PROC_MOD" not in d["sql"]
    assert "<= DATE '2024-12-31'" in d["sql"] and ">= DATE '2021-01-01'" in d["sql"]
    # NCCI editions route by code, not form (all three land on FORM_TYP 'A')
    assert "FORM_TYP" not in d["sql"] and d["routing"] == "all_forms:ncci"


def test_ptp_indicator1_uses_full_bypass_set():
    d = dg.build_detectors([_rule({"type": "code_pair_prohibited", "code_a": "1", "code_b": "2",
                                   "modifier_indicator": "1", "service_category": "outpatient"})])[0]
    assert d["finding_severity"] == "unbypassed_pair"
    for m in ("'LT'", "'RT'", "'F1'", "'59'", "'XU'", "'25'"):
        assert m in d["sql"]
    assert "AND NOT (" in d["sql"]


def test_state_all_forms_and_dme_routes_by_code():
    st = dg.build_detectors([_rule({"type": "code_pair_prohibited", "code_a": "H2025", "code_b": "H2026",
                                    "modifier_indicator": "0", "service_category": "state_bh"}, origin="manual")])[0]
    assert st["routing"] == "all_forms" and "FORM_TYP" not in st["sql"]
    # dme rides on FORM_TYP 'A' with practitioner, so it routes by code, not form
    dme = dg.build_detectors([_rule({"type": "max_units_per_line", "code": "E0601", "threshold": 1,
                                     "service_category": "dme"})])[0]
    assert dme["compiled"] and dme["routing"] == "all_forms:ncci" and "FORM_TYP" not in dme["sql"]


def test_mue_line_vs_dos_vs_state_day():
    line = dg.build_detectors([_rule({"type": "max_units_per_line", "code": "A", "threshold": 4, "service_category": "practitioner"})])[0]
    dos = dg.build_detectors([_rule({"type": "max_units_per_dos", "code": "A", "threshold": 4, "mai": 2, "service_category": "practitioner"})])[0]
    day = dg.build_detectors([_rule({"type": "max_units_per_day", "code": "S5150", "threshold": 48}, origin="manual")])[0]
    assert "GROUP BY" not in line["sql"] and "QUANTITY_PAID > 4" in line["sql"]
    assert "GROUP BY a.MEMBER_KEY, a.SProv_ID, a.Svc_Begin_Dt" in dos["sql"]
    assert "GROUP BY a.MEMBER_KEY, a.Svc_Begin_Dt" in day["sql"] and "SProv_ID" not in day["sql"]


def test_period_and_not_covered_and_leads():
    per = dg.build_detectors([_rule({"type": "max_units_per_period", "code_set": ["98960", "98961", "98962"],
                                     "threshold": 24, "period": "month"}, origin="manual")])[0]
    assert "DATE_TRUNC('MONTH'" in per["sql"] and "IN ('98960','98961','98962')" in per["sql"]
    nc = dg.build_detectors([_rule({"type": "code_not_covered", "code": "11975"}, origin="manual")])[0]
    assert nc["finding_severity"] == "not_covered_paid"
    lead = dg.build_detectors([_rule({"type": "not_checkable", "reason": "requires_medical_record"}, mc=False, origin="manual")])[0]
    assert not lead["compiled"] and lead["reason"] == "requires_medical_record"
    unk = dg.build_detectors([_rule({"type": "frequency_limit"}, origin="manual")])[0]
    assert not unk["compiled"] and "not in grammar" in unk["reason"]
    bad = dg.build_detectors([_rule({"type": "code_pair_prohibited", "code_a": "1; DROP", "code_b": "2"})])[0]
    assert not bad["compiled"] and "compile error" in bad["reason"]


# ---------------------------------------------------------- state rows
def test_state_row_roundtrip():
    from schema import Rule
    r = Rule(origin="manual", plane="coverage_scope", binding_status="state_policy", statement="s",
             predicate={"type": "max_units_per_day", "code": "S5150", "threshold": 48}, codes=["S5150"],
             source_doc="Ch19", source_locator="p30", alternates=[{"reading": "x"}]).assign_id()
    row = sr.rule_to_row(r, chapter="19", review_status="legacy_hand_authored")
    assert set(sr.STATE_RULE_COLUMNS) <= set(row)
    back = sr.row_to_rule(row)
    assert back.predicate == r.predicate and back.codes == ["S5150"] and back.alternates == [{"reading": "x"}]


# ------------------------------------------------------------------ gate
def _mk(**kw):
    base = dict(origin="manual", plane="coverage_scope", binding_status="state_policy", statement="s",
                predicate={"type": "max_units_per_day", "code": "s5150", "threshold": 48}, codes=["s5150"],
                source_doc="Ch19", required_claim_fields=["proc_cd", "quantity_paid", "member_id", "srvc_bgn_dt"])
    base.update(kw)
    return Rule(**base).assign_id()


def test_gate_statuses():
    clean      = _mk(codes=["S5150"])
    patched    = _mk(codes=[])                                             # derived from predicate
    downgraded = _mk(required_claim_fields=["proc_cd", "member_dob"])      # not in extract
    unknown    = _mk(predicate={"type": "made_up"})
    ambiguous  = _mk(ambiguity_flag=True, ambiguity_note="two readings")
    authored   = _mk(predicate={"type": "not_checkable", "reason": "requires_medical_record"}, machine_checkable=False)
    selfpair   = _mk(predicate={"type": "code_pair_prohibited", "code_a": "A", "code_b": "A"}, codes=["A"])
    rejected   = _mk(statement="")
    acc, rej = vg.gate([clean, patched, downgraded, unknown, ambiguous, authored, selfpair, rejected])
    assert len(rej) == 1 and "empty statement" in rej[0]["reject_reason"]
    st = [d["validation_status"] for d in acc]
    assert st == ["clean", "patched", "downgraded", "downgraded", "needs_review", "needs_review", "downgraded"]
    assert acc[1]["codes"] == ["S5150"]
    assert acc[2]["not_checkable_reason"] == "missing_claim_field" and acc[2]["machine_checkable"] is False
    assert acc[3]["not_checkable_reason"] == "predicate_not_in_grammar"
    assert acc[5]["not_checkable_reason"] == "requires_medical_record"
    # gate output feeds the compiler: downgraded rules become leads, clean ones compile
    dets = dg.build_detectors(acc)
    assert [d["compiled"] for d in dets] == [True, True, False, False, True, False, False]


def test_authorable_but_uncompiled_predicate_becomes_lead_not_error():
    r = _mk(predicate={"type": "max_dollars_per_period", "threshold": 1000, "period": "benefit_year"}, codes=[])
    acc, _ = vg.gate([r])
    assert acc[0]["validation_status"] == "downgraded"
    d = dg.build_detectors(acc)[0]
    assert not d["compiled"] and d["reason"] == "predicate_not_in_grammar"


# ------------------------------------------------------------------ kb_io
def test_kb_io_explicit_schema_handles_all_none_and_nullable_ints():
    import types as _t
    # stand-in for pyspark.sql.types: only the names kb_io uses
    T = _t.SimpleNamespace(
        BooleanType=lambda: "boolean", LongType=lambda: "long", DoubleType=lambda: "double",
        StringType=lambda: "string",
        StructField=lambda n, t, nullable: (n, t),
        StructType=lambda fields: list(fields))
    sys.modules["pyspark"] = _t.ModuleType("pyspark")
    sys.modules["pyspark.sql"] = _t.ModuleType("pyspark.sql")
    sys.modules["pyspark.sql.types"] = T
    import kb_io; import importlib; importlib.reload(kb_io)

    captured = {}
    class FakeSpark:
        def createDataFrame(self, rows, schema=None):
            captured["rows"], captured["schema"] = rows, schema
            return "df"
    pdf = pd.DataFrame({
        "flag": [True, False],
        "hits": pd.array([3, None], dtype="Int64"),
        "secs": [1.5, None],
        "err": [None, None],                      # all-None column: the inference killer
        "pred": [{"type": "x"}, ["a"]],           # nested -> JSON string
        "name": ["a", None],
    })
    kb_io.to_spark(FakeSpark(), pdf)
    assert captured["schema"] == [("flag", "boolean"), ("hits", "long"), ("secs", "double"),
                                  ("err", "string"), ("pred", "string"), ("name", "string")]
    assert captured["rows"][0] == [True, 3, 1.5, None, '{"type": "x"}', "a"]
    assert captured["rows"][1] == [False, None, None, None, '["a"]', None]


# ------------------------------------------------ real column names (SB's tables, 2026-09-18)
def test_real_ptp_and_mue_column_names_and_types():
    import numpy as np
    # exactly as main.sedo.medicaid_ncci_edit_outpatient_ptp_q2_2026 presents them:
    # ints for dates/indicator; NULL deletion dates surface as NaN (float) in pandas
    ptp = pd.DataFrame({
        "Col1": ["0001A", "0001A", "0001A"],
        "Col2": ["0591T", "90473", "99202"],
        "EffDt": [20220101, 20220101, 20220101.0],
        "DelDt": [20231231, 20220101, np.nan],
        "ModifierIndicator_0_not_allowed_1_allowed_9_not_applicable": [1, 9, 0.0],
        "PTP_Edit_Rationale": ["CPT Manual or CMS manual coding instruction"] * 3,
    })
    rules = ing.ptp_rules(ptp, meta("outpatient", "ptp", 2, 2026))
    assert [(r.predicate["code_b"], r.predicate["modifier_indicator"], r.effective_date, r.end_date) for r in rules] == [
        ("0591T", "1", "2022-01-01", "2023-12-31"),     # deleted edit kept, windowed
        ("99202", "0", "2022-01-01", None),             # NULL DelDt -> still active
    ]                                                   # indicator 9 dropped
    assert "CMS rationale: CPT Manual" in rules[0].notes
    for svc, col in (("outpatient", "Outpatient Hospital Services MUE Values"),
                     ("practitioner", "Practitioner Services MUE Values"),
                     ("dme", "DME Supplier Services MUE Values")):
        mue = pd.DataFrame({"HCPCS/CPT Code": ["A4218"], col: [20], "MUE Rationale": ["CMS Policy"]})
        r = ing.mue_history([(meta(svc, "mue", 3, 2026), mue)])[0]
        assert r.predicate == {"type": "max_units_per_line", "code": "A4218", "threshold": 20, "service_category": svc}
    # Spark-side scoping resolves the same names
    assert ing._find_col(ptp.columns, "column1", "col1") == "Col1"
    assert ing._find_col(mue.columns, "hcpcs/cpt code") == "HCPCS/CPT Code"


# ------------------------------------------------------------------ seed
def test_state_seed_is_complete_and_compiles():
    import state_seed
    rules = state_seed.seed_rules()
    assert len(rules) >= 18
    # every rule has a citation and a page
    for r in rules:
        assert r.source_doc and r.source_url and r.source_locator, r.statement
        assert state_seed.CHAPTER_BY_DOC.get(r.source_doc) in ("10", "19")
    # round-trips through the row path, passes the gate with zero rejects
    rows = [sr.rule_to_row(r, chapter=state_seed.CHAPTER_BY_DOC[r.source_doc]) for r in rules]
    back = [sr.row_to_rule(x) for x in rows]
    acc, rej = vg.gate(back)
    assert rej == []
    acc, _ = rn.normalize_rules(acc)
    assert len({a["rule_id"] for a in acc}) == len(rules)     # no id collisions
    dets = dg.build_detectors(acc)
    comp = [d for d in dets if d["compiled"]]
    assert len(comp) >= 15
    # state rules never compile UNROUTED (they apply to all forms)
    assert not any(str(d["routing"]).startswith("UNROUTED") for d in comp)
    # the three not_checkable rules are leads, not errors
    leads = [d for d in dets if not d["compiled"]]
    assert len(leads) == 3 and all("error" not in (d["reason"] or "") for d in leads)


# ================================================ chapter extraction (Stages 0-7)
def _fixture_pipeline(tmp_path="/tmp/pie_fixture.pdf"):
    import fixture_pdf, policy_docs as pdx, extract_rules as ex
    sys.path.insert(0, HERE)
    path = fixture_pdf.build(tmp_path)
    pages = pdx.parse_pdf(path)
    inv = {"chapter": "10", "title": "Individual Practitioner Services", "url": "https://x/FFS_Chap10.pdf",
           "doc_hash": "deadbeef", "revision_date": None}
    secs, meta = pdx.segment(pages, "10", inv["doc_hash"])
    inv["revision_date"] = meta["revision_date"]
    return inv, secs, meta, ex


def test_parse_and_segment_real_layout():
    sys.path.insert(0, HERE)
    inv, secs, meta, _ = _fixture_pipeline()
    assert meta["revision_date"] == "04/29/2026"
    assert meta["revision_dates"][:2] == ["04/29/2026", "05/02/2024"]
    heads = [s["heading"] for s in secs]
    assert heads == ["GENERAL INFORMATION", "CORRECT CODING INITIATIVE", "COMMUNITY HEALTH WORKER SERVICES",
                     "FAMILY PLANNING SERVICES", "ANESTHESIA SERVICES", "WELL EXAMS", "PROVIDER RESPONSIBILITIES"]
    # header line never leaks into section text
    assert not any("CHAPTER 10 INDIVIDUAL" in s["text"] for s in secs)
    # pre-filter: narrative sections are not candidates
    cand = {s["heading"] for s in secs if s["has_rule_signal"]}
    assert "GENERAL INFORMATION" not in cand and "PROVIDER RESPONSIBILITIES" not in cand
    assert "COMMUNITY HEALTH WORKER SERVICES" in cand


def test_stub_extraction_grounds_and_drafts():
    sys.path.insert(0, HERE)
    inv, secs, meta, ex = _fixture_pipeline()
    backend = ex.make_backend("STUB")
    drafts, dropped, stats = ex.extract_chapter(secs, inv, backend, run_id="t1", log_fn=lambda *_: None)
    assert stats["sections_candidate"] == 5 and stats["dropped"] == 0, stats
    types = sorted(d["predicate"] for d in drafts)
    preds = [json.loads(d["predicate"]) for d in drafts]
    kinds = {p["type"] for p in preds}
    assert {"max_units_per_day", "max_units_per_period", "code_pair_prohibited", "code_not_covered", "not_checkable"} <= kinds
    # word-number limit grounded: "four units per day" -> 4, threshold_found True
    day = [d for d in drafts if json.loads(d["predicate"]).get("type") == "max_units_per_day"]
    assert {json.loads(d["predicate"])["threshold"] for d in day} == {4}
    assert all(json.loads(d["grounding"])["threshold_found"] is True for d in day)
    # citations carry page + heading + revision
    chw = [d for d in day if "98960" in d["codes"]][0]
    assert chw["source_locator"].startswith("page 2") and "COMMUNITY HEALTH WORKER" in chw["source_locator"]
    assert chw["doc_version"] == "rev 04/29/2026" and chw["review_status"] == "draft"
    assert chw["rule_id"].startswith("EXT-") and chw["extraction_key"]
    # same-day prohibition expanded to 3 pairs; non-covered codes 11975/11977/00938 caught
    pairs = [p for p in preds if p["type"] == "code_pair_prohibited"]
    assert len(pairs) == 3
    nc = {json.loads(d["predicate"])["code"] for d in drafts if json.loads(d["predicate"])["type"] == "code_not_covered"}
    assert {"11975", "11977", "00938"} <= nc
    # the well-exam contradiction is a lead with ambiguity, not a detector
    amb = [d for d in drafts if d["ambiguity_flag"] and "re-instated" in (d["verbatim_quote"] or "")]
    assert amb and amb[0]["machine_checkable"] is False
    # drafts feed the existing gate + compiler unchanged
    back = [sr.row_to_rule(d) for d in drafts]
    acc, rej = vg.gate(back)
    assert rej == []
    acc, _ = rn.normalize_rules(acc)
    dets = dg.build_detectors(acc)
    assert sum(d["compiled"] for d in dets) >= 8


def test_grounding_drops_hallucinations_and_bad_predicates():
    sys.path.insert(0, HERE)
    inv, secs, meta, ex = _fixture_pipeline()
    sec = [s for s in secs if s["heading"] == "COMMUNITY HEALTH WORKER SERVICES"][0]
    good = {"statement": "s", "predicate": {"type": "max_units_per_day", "code": "98960", "threshold": 4},
            "codes": ["98960"], "verbatim_quote": "Claims can be submitted for a maximum of four units per day"}
    assert ex.ground(good, sec)["keep"]
    fake_quote = dict(good, verbatim_quote="Providers may bill up to nine units per day for CHW services.")
    g = ex.ground(fake_quote, sec)
    assert not g["keep"] and g["drop_reason"] == "quote not found in source"
    fake_code = dict(good, predicate={"type": "max_units_per_day", "code": "99999", "threshold": 4}, codes=["99999"])
    assert ex.ground(fake_code, sec)["drop_reason"] == "codes not in source"
    bad_pred = dict(good, predicate={"type": "code_pair_prohibited", "code_a": "98960", "code_b": "98960"})
    assert "invalid predicate" in ex.ground(bad_pred, sec)["drop_reason"]
    unknown = dict(good, predicate={"type": "made_up"})
    assert "invalid predicate" in ex.ground(unknown, sec)["drop_reason"]
    # threshold not in text -> kept but flagged ambiguous (derived)
    thr = dict(good, predicate={"type": "max_units_per_day", "code": "98960", "threshold": 7})
    g = ex.ground(thr, sec)
    assert g["keep"] and g["threshold_found"] is False
    row = ex.to_draft_row(thr, g, sec, inv, ex.make_backend("STUB"), "t")
    assert row["ambiguity_flag"] is True and "derived" in row["ambiguity_note"]


def test_merge_never_downgrades_approved_or_resurrects_rejected():
    sys.path.insert(0, HERE)
    inv, secs, meta, ex = _fixture_pipeline()
    drafts, _, _ = ex.extract_chapter(secs, inv, ex.make_backend("STUB"), run_id="r1", log_fn=lambda *_: None)
    merged, c = sr.merge_drafts(None, drafts)
    assert c["added"] == len(drafts) and len(merged) == len(drafts)
    # reviewer approves one, rejects one
    k_ok, k_no = drafts[0]["extraction_key"], drafts[1]["extraction_key"]
    merged.loc[merged["extraction_key"] == k_ok, "review_status"] = "approved"
    merged.loc[merged["extraction_key"] == k_no, "review_status"] = "rejected"
    # re-extract (same content, new run id)
    drafts2, _, _ = ex.extract_chapter(secs, inv, ex.make_backend("STUB"), run_id="r2", log_fn=lambda *_: None)
    merged2, c2 = sr.merge_drafts(merged, drafts2)
    assert c2["kept_reviewed"] == 1 and c2["kept_rejected"] == 1 and c2["added"] == 0
    assert c2["replaced_draft"] == len(drafts) - 2
    st = dict(zip(merged2["extraction_key"], merged2["review_status"]))
    assert st[k_ok] == "approved" and st[k_no] == "rejected"
    assert len(merged2) == len(drafts)
    # build loads only reviewed rows: approved yes, draft no
    loadable = merged2[merged2["review_status"].isin(sr.LOADABLE_STATUS)]
    assert len(loadable) == 1


def test_change_tracking():
    import policy_changes as pc
    sys.path.insert(0, HERE)
    inv, secs, meta, ex = _fixture_pipeline()
    drafts, _, _ = ex.extract_chapter(secs, inv, ex.make_backend("STUB"), run_id="r1", log_fn=lambda *_: None)
    first = pc.diff_chapter(None, drafts, inv, None, "r1")
    s1 = pc.summarize(first)
    assert s1["document_first_seen"] == 1 and s1["rule_added"] == len(drafts)
    merged, _ = sr.merge_drafts(None, drafts)
    prev_inv = pd.DataFrame([{"chapter": "10", "doc_hash": inv["doc_hash"], "fetched_at": "2026-01-01"}])
    # next quarter: doc changed, one rule gone
    inv2 = dict(inv, doc_hash="cafebabe")
    second = pc.diff_chapter(merged, drafts[1:], inv2, prev_inv, "r2")
    s2 = pc.summarize(second)
    assert s2["document_changed"] == 1 and s2["not_re_extracted"] == 1 and s2["rule_unchanged"] == len(drafts) - 1


# ============================================ routing + cross-edition collapse (real data finding)
def test_config_form_map_has_no_duplicate_key_and_no_O():
    import importlib, config; importlib.reload(config)
    assert config.FORM_TYPE_MAP == {"A": "practitioner", "D": "outpatient"}   # no dup "A", no "O"
    assert "practitioner" in config.FORM_TYPE_MAP.values()                    # not silently dropped


def test_ncci_editions_route_by_code_not_form():
    import importlib, config, detector_gen as dg2
    importlib.reload(config); importlib.reload(dg2)
    assert config.NCCI_ROUTE_BY_FORM is False
    for svc in ("practitioner", "outpatient", "dme"):
        d = dg2.build_detectors([_rule({"type": "max_units_per_line", "code": "A4218", "threshold": 20,
                                        "service_category": svc})])[0]
        assert d["compiled"] and d["routing"] == "all_forms:ncci", (svc, d["routing"])
        assert "FORM_TYP" not in d["sql"]        # no form filter; the code selects the claims


def test_collapse_mue_keeps_max_threshold_across_editions():
    # same code, three editions, three thresholds -> ONE rule at the max (no false positives on 'A')
    rules = [
        _rule({"type": "max_units_per_line", "code": "A4218", "threshold": 4, "service_category": "practitioner"}),
        _rule({"type": "max_units_per_line", "code": "A4218", "threshold": 20, "service_category": "dme"}),
        _rule({"type": "max_units_per_line", "code": "A4218", "threshold": 6, "service_category": "outpatient"}),
    ]
    out, rep = rn.normalize_rules(rules)
    assert rep["ncci_editions_collapsed"] == 2 and len(out) == 1
    p = out[0]["predicate"]
    assert p["threshold"] == 20 and p["service_category"] == "ncci"
    assert p["editions"] == {"practitioner": 4, "dme": 20, "outpatient": 6}
    assert out[0]["ambiguity_flag"] is True          # editions disagree -> flagged for review
    d = dg.build_detectors(out)[0]
    assert "QUANTITY_PAID > 20" in d["sql"] and d["routing"] == "all_forms:ncci"


def test_collapse_ptp_most_permissive_indicator_and_window():
    rules = [
        dict(_rule({"type": "code_pair_prohibited", "code_a": "11111", "code_b": "22222",
                    "modifier_indicator": "0", "service_category": "practitioner"}, eff="2021-01-01", end="2023-12-31")),
        dict(_rule({"type": "code_pair_prohibited", "code_a": "22222", "code_b": "11111",  # reversed order, same pair
                    "modifier_indicator": "1", "service_category": "outpatient"}, eff="2022-01-01", end=None)),
    ]
    out, rep = rn.normalize_rules(rules)
    assert rep["ncci_editions_collapsed"] == 1 and len(out) == 1
    p = out[0]["predicate"]
    assert p["modifier_indicator"] == "1"                       # any edition allows bypass -> permissive
    assert out[0]["effective_date"] == "2021-01-01" and out[0]["end_date"] is None   # widest window
    d = dg.build_detectors(out)[0]
    assert d["finding_severity"] == "unbypassed_pair"          # indicator 1 -> bypass-aware detector


def test_collapse_leaves_distinct_codes_and_nonncci_alone():
    rules = [
        _rule({"type": "max_units_per_line", "code": "A4218", "threshold": 4, "service_category": "practitioner"}),
        _rule({"type": "max_units_per_line", "code": "E0601", "threshold": 1, "service_category": "dme"}),
        _rule({"type": "max_units_per_day", "code": "S5150", "threshold": 48}, origin="manual"),
    ]
    out, rep = rn.normalize_rules(rules)
    assert rep["ncci_editions_collapsed"] == 0 and len(out) == 3   # different codes; manual untouched
