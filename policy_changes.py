"""
policy_changes.py  -  Stage 7: what changed since the last extraction?

Compares this run's grounded drafts (by extraction_key = chapter + predicate +
codes, i.e. by MEANING not wording) against what state_rules already holds for
the chapter, and compares the document hash against the inventory.

Nothing is deactivated automatically: a rule that was not re-extracted is
flagged 'not_re_extracted' for a reviewer, because the cause may be a policy
removal, a model miss, or a parser change - and only a human can tell which.
"""
from __future__ import annotations

import pandas as pd


def diff_chapter(existing: pd.DataFrame | None, drafts: list[dict], inv: dict,
                 prev_inventory: pd.DataFrame | None, run_id: str) -> list[dict]:
    ch = str(inv["chapter"])
    rows = []

    # --- document level
    prev_hash = None
    if prev_inventory is not None and len(prev_inventory):
        p = prev_inventory[prev_inventory["chapter"].astype(str) == ch]
        if len(p):
            p = p.sort_values("fetched_at")
            prev_hash = p.iloc[-1]["doc_hash"]
    if prev_hash is None:
        rows.append(_row(run_id, ch, "document_first_seen", None, None, None, None, inv["doc_hash"]))
    elif prev_hash != inv["doc_hash"]:
        rows.append(_row(run_id, ch, "document_changed", None, None, None, prev_hash, inv["doc_hash"]))
    else:
        rows.append(_row(run_id, ch, "document_unchanged", None, None, None, prev_hash, inv["doc_hash"]))

    # --- rule level
    new_keys = {d["extraction_key"]: d for d in drafts}
    old = pd.DataFrame(columns=["extraction_key", "rule_id", "statement", "origin", "review_status"]) \
        if existing is None or len(existing) == 0 else existing
    old = old[old["chapter"].astype(str) == ch] if "chapter" in old.columns else old
    old_keys = {k: r for k, r in zip(old.get("extraction_key", pd.Series(dtype=object)), old.to_dict("records"))
                if isinstance(k, str) and k}

    for k, d in new_keys.items():
        if k not in old_keys:
            rows.append(_row(run_id, ch, "rule_added", k, d["rule_id"], d["statement"], prev_hash, inv["doc_hash"]))
        else:
            rows.append(_row(run_id, ch, "rule_unchanged", k, d["rule_id"], d["statement"], prev_hash, inv["doc_hash"]))
    for k, r in old_keys.items():
        if k not in new_keys and r.get("origin") == "extracted":
            rows.append(_row(run_id, ch, "not_re_extracted", k, r["rule_id"], r["statement"], prev_hash, inv["doc_hash"]))
    return rows


def _row(run_id, ch, change, key, rule_id, statement, old_hash, new_hash):
    return {"run_id": run_id, "chapter": ch, "change_type": change, "extraction_key": key,
            "rule_id": rule_id, "statement": statement, "old_doc_hash": old_hash, "new_doc_hash": new_hash}


def summarize(change_rows: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in change_rows:
        out[r["change_type"]] = out.get(r["change_type"], 0) + 1
    return out
