"""
state_rules.py  -  the reviewed authoring source for AHCCCS (state) rules.

State rules live in a Delta table, not in Python modules. "Add a chapter" means
"add reviewed rows", never "edit the build notebook". Every row carries a
review_status; the build loads only rows a named human has approved (or the
hand-authored PoC rules migrated as 'legacy_hand_authored').

Table: main.sedo.state_rules  (see STATE_RULE_COLUMNS)

Review round-trip (the human gate, made auditable):
  export_review_workbook()  -> Excel of rows needing a decision
  ingest_review_decisions() <- the same file with decision columns filled;
                               appends to policy_review_log and updates
                               review_status on state_rules.
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from schema import Rule

RULE_FIELDS = [
    "rule_id", "origin", "plane", "binding_status", "statement", "predicate", "codes",
    "population", "machine_checkable", "not_checkable_reason", "required_claim_fields",
    "ambiguity_flag", "ambiguity_note", "alternates", "source_doc", "source_locator",
    "source_url", "doc_version", "doc_hash", "effective_date", "end_date", "deactivated",
    "supersedes", "notes",
]
_JSON_FIELDS = {"predicate", "codes", "required_claim_fields", "alternates", "supersedes"}
REVIEW_FIELDS = ["chapter", "review_status", "reviewer", "reviewed_at", "review_note"]
# filled only for rows produced by extract_rules (None for hand-authored rows)
EXTRACTION_FIELDS = ["extraction_key", "verbatim_quote", "section_id", "extraction_confidence",
                     "extraction_run_id", "extraction_backend", "grounding"]
STATE_RULE_COLUMNS = RULE_FIELDS + REVIEW_FIELDS + EXTRACTION_FIELDS

LOADABLE_STATUS = ("approved", "legacy_hand_authored")


# ------------------------------------------------------------ row <-> rule
def rule_to_row(rule, chapter: str, review_status: str = "draft",
                reviewer: str | None = None, review_note: str | None = None) -> dict:
    d = rule.to_dict() if hasattr(rule, "to_dict") else dict(rule)
    row = {}
    for f in RULE_FIELDS:
        v = d.get(f)
        if f in _JSON_FIELDS:
            v = json.dumps(v if v is not None else ([] if f != "predicate" else {}), default=str)
        row[f] = v
    row.update(chapter=chapter, review_status=review_status, reviewer=reviewer,
               reviewed_at=datetime.now().isoformat(timespec="seconds") if reviewer else None,
               review_note=review_note)
    for f in EXTRACTION_FIELDS:
        row.setdefault(f, None)
    return row


def row_to_rule(row: dict) -> Rule:
    kw = {}
    for f in RULE_FIELDS:
        v = row.get(f)
        if f in _JSON_FIELDS and isinstance(v, str):
            v = json.loads(v) if v else ([] if f != "predicate" else {})
        if f in ("machine_checkable", "ambiguity_flag", "deactivated") and v is not None:
            v = bool(v)
        if isinstance(v, float) and pd.isna(v):
            v = None
        kw[f] = v
    if not kw.get("supersedes"):
        kw["supersedes"] = None
    kw.pop("rule_id", None)              # re-derived by rule_normalize
    return Rule(**kw)


# ----------------------------------------------------------------- loading
def load_state_rules(spark, table: str, scope_codes: set[str] | None = None,
                     statuses=LOADABLE_STATUS) -> list[Rule]:
    df = spark.table(table).toPandas()
    if df.empty:
        print(f"  state: {table} is empty")
        return []
    df = df[df["review_status"].isin(statuses) & ~df["deactivated"].fillna(False).astype(bool)]
    out, skipped_scope = [], 0
    for row in df.to_dict("records"):
        codes = json.loads(row["codes"]) if isinstance(row.get("codes"), str) and row["codes"] else []
        codes = [str(c).strip().upper() for c in codes]
        # a coded rule must touch a paid code; a policy-level rule (no codes) always loads
        if scope_codes and codes and not any(c in scope_codes for c in codes):
            skipped_scope += 1
            continue
        out.append(row_to_rule(row))
    by_ch = df["chapter"].value_counts().to_dict()
    print(f"  state: {len(out)} reviewed rules loaded {by_ch}"
          + (f"; {skipped_scope} out of claims scope" if skipped_scope else ""))
    return out


# ---------------------------------------------------------------- writing
def write_state_rules(spark, table: str, rows: list[dict], mode: str = "overwrite"):
    from kb_io import write_table
    pdf = pd.DataFrame(rows, columns=STATE_RULE_COLUMNS)
    for b in ("machine_checkable", "ambiguity_flag", "deactivated"):
        pdf[b] = pdf[b].fillna(False).astype(bool)
    write_table(spark, pdf, table, mode)


# ----------------------------------------------------------------- merge
REVIEWED = ("approved", "legacy_hand_authored")


def merge_drafts(existing: pd.DataFrame | None, drafts: list[dict]) -> tuple[pd.DataFrame, dict]:
    """Merge freshly extracted drafts into the state_rules table, by extraction_key.

      * a key already reviewed (approved / legacy) is left untouched - never downgraded
      * a key previously rejected stays rejected - never resurrected by re-extraction
      * a key already in draft is replaced by the new draft (fresh quote, confidence, run)
      * a new key is added as a draft
    Returns (merged_df, counts)."""
    cols = STATE_RULE_COLUMNS
    if existing is None or len(existing) == 0:
        existing = pd.DataFrame(columns=cols)
    for c in cols:
        if c not in existing.columns:
            existing[c] = None
    existing = existing[cols].copy()
    by_key = {k: i for i, k in existing["extraction_key"].items() if isinstance(k, str) and k}
    by_id = {k: i for i, k in existing["rule_id"].items() if isinstance(k, str) and k}
    counts = {"added": 0, "replaced_draft": 0, "kept_reviewed": 0, "kept_rejected": 0}
    keep_rows = existing.to_dict("records")
    idx_to_drop = set()
    new_rows = []
    for d in drafts:
        key = d.get("extraction_key")
        i = by_key.get(key, by_id.get(d.get("rule_id")))
        if i is None:
            new_rows.append(d); counts["added"] += 1; continue
        status = existing.at[i, "review_status"]
        if status in REVIEWED:
            counts["kept_reviewed"] += 1
        elif status == "rejected":
            counts["kept_rejected"] += 1
        else:
            idx_to_drop.add(i); new_rows.append(d); counts["replaced_draft"] += 1
    merged = [r for j, r in enumerate(keep_rows) if j not in idx_to_drop] + new_rows
    out = pd.DataFrame(merged, columns=cols)
    for b in ("machine_checkable", "ambiguity_flag", "deactivated"):
        out[b] = out[b].fillna(False).astype(bool)
    return out, counts


# --------------------------------------------------------- review workbook
def export_review_workbook(spark, table: str, path: str,
                           statuses=("draft", "needs_review")) -> str:
    df = spark.table(table).toPandas()
    need = df[df["review_status"].isin(statuses)].copy()
    out = pd.DataFrame({
        "rule_id": need["rule_id"],
        "chapter": need["chapter"],
        "policy_text_and_notes": need["notes"],
        "proposed_rule": need["statement"],
        "predicate": need["predicate"],
        "citation": need["source_doc"].astype(str) + " | " + need["source_locator"].astype(str),
        "source_url": need["source_url"],
        "verbatim_quote": need["verbatim_quote"] if "verbatim_quote" in need else None,
        "confidence": need["extraction_confidence"] if "extraction_confidence" in need else None,
        "open_question": need["ambiguity_note"],
        "decision": "",                 # Approve | Reject | Fix
        "corrected_rule": "",
        "reviewer": "",
        "review_note": "",
    })
    if "confidence" in out.columns:
        out = out.sort_values("confidence", na_position="last")
    out.to_excel(path, index=False, engine="openpyxl")
    print(f"  exported {len(out)} rules for review -> {path}")
    return path


def ingest_review_decisions(spark, table: str, path: str, log_table: str):
    filled = pd.read_excel(path, engine="openpyxl")
    filled["decision"] = filled["decision"].astype(str).str.strip().str.lower()
    filled = filled[filled["decision"].isin(["approve", "reject", "fix"])].copy()
    if filled.empty:
        print("  no decisions found in workbook")
        return
    filled["ingested_at"] = datetime.now().isoformat(timespec="seconds")
    log_cols = ["rule_id", "decision", "corrected_rule", "reviewer", "review_note", "ingested_at"]
    from kb_io import write_table
    write_table(spark, filled[log_cols], log_table, mode="append")

    status = {"approve": "approved", "reject": "rejected", "fix": "approved"}
    df = spark.table(table).toPandas()
    for rec in filled.to_dict("records"):
        m = df["rule_id"] == rec["rule_id"]
        if not m.any():
            continue
        df.loc[m, "review_status"] = status[rec["decision"]]
        df.loc[m, "reviewer"] = rec.get("reviewer")
        df.loc[m, "reviewed_at"] = rec["ingested_at"]
        df.loc[m, "review_note"] = rec.get("review_note")
        if rec["decision"] == "fix" and isinstance(rec.get("corrected_rule"), str) and rec["corrected_rule"].strip():
            df.loc[m, "statement"] = rec["corrected_rule"].strip()
    write_state_rules(spark, table, df.to_dict("records"))
    print(f"  applied {len(filled)} decisions; log -> {log_table}")
