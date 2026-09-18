# Databricks notebook source
# MAGIC %md
# MAGIC # Extract state rules from AHCCCS chapter PDFs
# MAGIC
# MAGIC PDF → sections → proposals (model) → **deterministic grounding** → drafts in `main.sedo.state_rules`.
# MAGIC
# MAGIC | stage | module | writes |
# MAGIC |---|---|---|
# MAGIC | 0 acquire + hash | `policy_docs.acquire` | `policy_document_inventory` |
# MAGIC | 1-2 parse + segment | `policy_docs.parse_pdf / segment` | `policy_sections` |
# MAGIC | 3 pre-filter | `policy_docs.rule_signal` | (flag on sections) |
# MAGIC | 4 propose | `extract_rules.propose` (STUB / DBFM / HFLOCAL) | `policy_extraction_cache` |
# MAGIC | 5 ground | `extract_rules.ground` | `policy_extraction_dropped` (what was refused, and why) |
# MAGIC | 6 draft → review | `state_rules.merge_drafts` | `state_rules` (review_status = draft) |
# MAGIC | 7 change tracking | `policy_changes.diff_chapter` | `policy_change_log` |
# MAGIC
# MAGIC The model only ever **proposes**. A proposal is kept only if its verbatim quote and every code are found in
# MAGIC the section. Drafts never compile: `build_kb` loads `approved` / `legacy_hand_authored` rows only.
# MAGIC Re-running is safe: approved rows are never downgraded, rejected rows are never resurrected.

# COMMAND ----------

# MAGIC %pip install -q pymupdf pypdf requests openpyxl

# COMMAND ----------

import os, sys, json, importlib, time
import pandas as pd
sys.path.insert(0, os.getcwd())
sys.path.append("/Workspace/Users/sedo.senou@azahcccs.gov/Projects/policy_intelligence")

import config, schema, kb_io, policy_docs, extract_rules, state_rules, policy_changes
for m in (config, schema, kb_io, policy_docs, extract_rules, state_rules, policy_changes):
    importlib.reload(m)

# ---- run settings -----------------------------------------------------------
CHAPTERS_TO_RUN = config.MVP_CHAPTERS            # ["4","10","13","14","19","22"]; use list(config.CHAPTERS) for all
LLM_BACKEND     = config.LLM_BACKEND             # "STUB" | "DBFM" | "HFLOCAL"  (set config.DBFM_ENDPOINT for DBFM)
DOWNLOAD        = True                           # False = read PDFs already uploaded to PDF_DIR
ONLY_CANDIDATES = True                           # skip sections with no rule signal (saves model calls)

KB   = f"{config.OUTPUT_CATALOG}.{config.OUTPUT_SCHEMA}"
STATE_RULES_TBL = config.STATE_RULES_TABLE
T_INV, T_SEC, T_LOG = f"{KB}.policy_document_inventory", f"{KB}.policy_sections", f"{KB}.policy_extraction_log"
T_DROP, T_CHG, T_CACHE = f"{KB}.policy_extraction_dropped", f"{KB}.policy_change_log", f"{KB}.policy_extraction_cache"

# ---- where PDFs live: a UC Volume if we can, else a local scratch dir (hashes still persist in the inventory)
PDF_DIR = config.PDF_DIR
try:
    cat, sch, vol = PDF_DIR.split("/")[2:5]
    spark.sql(f"CREATE VOLUME IF NOT EXISTS {cat}.{sch}.{vol}")
except Exception as e:
    PDF_DIR = "/tmp/policy_docs"
    print(f"could not use volume {config.PDF_DIR} ({type(e).__name__}); PDFs go to {PDF_DIR} (not persistent)")
os.makedirs(PDF_DIR, exist_ok=True)

run_id = pd.Timestamp.now().strftime("%Y%m%dT%H%M%S")
backend = extract_rules.make_backend(LLM_BACKEND)
print(f"run {run_id} | backend {backend.name}:{backend.model} | prompt {extract_rules.PROMPT_VERSION} | chapters {CHAPTERS_TO_RUN}")

# COMMAND ----------

# MAGIC %md ## Load prior state (cache, inventory, existing rules)

# COMMAND ----------

def _table_or_none(fqn):
    try:
        return spark.table(fqn).toPandas()
    except Exception:
        return None

cache_df = _table_or_none(T_CACHE)
cache = {} if cache_df is None else {r["cache_key"]: json.loads(r["proposals"]) for r in cache_df.to_dict("records")}
prev_inventory = _table_or_none(T_INV)
existing_rules = _table_or_none(STATE_RULES_TBL)
print(f"cache entries: {len(cache):,} | prior inventory rows: {0 if prev_inventory is None else len(prev_inventory)} "
      f"| existing state_rules: {0 if existing_rules is None else len(existing_rules)}")

# COMMAND ----------

# MAGIC %md ## Run — one chapter at a time, failures isolated

# COMMAND ----------

inventory_rows, section_rows, log_rows, dropped_rows, change_rows, all_drafts = [], [], [], [], [], []
for ch in CHAPTERS_TO_RUN:
    t0 = time.time()
    title = config.CHAPTERS[ch][0]
    print(f"\n=== Chapter {ch}: {title}")
    try:
        inv = policy_docs.acquire(ch, PDF_DIR, download=DOWNLOAD)
        pages = policy_docs.parse_pdf(inv["path"])
        secs, meta = policy_docs.segment(pages, ch, inv["doc_hash"])
        inv["revision_date"] = meta["revision_date"]
        inventory_rows.append(policy_docs.inventory_record(inv, meta))
        for s in secs:
            section_rows.append({k: (json.dumps(v) if isinstance(v, list) else v) for k, v in s.items()} | {"run_id": run_id})
        print(f"  {meta['pages']} pages, rev {meta['revision_date']}, {meta['sections']} sections, "
              f"{meta['candidate_sections']} candidates ({inv['source']})")

        drafts, dropped, stats = extract_rules.extract_chapter(secs, inv, backend, run_id, ONLY_CANDIDATES, cache)
        changes = policy_changes.diff_chapter(existing_rules, drafts, inv, prev_inventory, run_id)
        all_drafts += drafts; dropped_rows += dropped; change_rows += changes
        stats["seconds"] = round(time.time() - t0, 1); stats["error"] = None
        log_rows.append(stats)
        print(f"  proposals {stats['proposals']} -> grounded {stats['grounded']} (dropped {stats['dropped']}, "
              f"cache hits {stats['sections_from_cache']}) | drafts {stats['drafts_unique']} | "
              f"changes {policy_changes.summarize(changes)}")
    except Exception as e:
        log_rows.append({"chapter": ch, "run_id": run_id, "backend": f"{backend.name}:{backend.model}",
                         "prompt_version": extract_rules.PROMPT_VERSION, "seconds": round(time.time() - t0, 1),
                         "error": f"{type(e).__name__}: {str(e)[:400]}"})
        print(f"  FAILED: {type(e).__name__}: {e}")

print(f"\n{len(all_drafts)} drafts across {len(CHAPTERS_TO_RUN)} chapters; {len(dropped_rows)} proposals refused by grounding")

# COMMAND ----------

# MAGIC %md ## Merge drafts into `state_rules` and persist everything

# COMMAND ----------

merged, counts = state_rules.merge_drafts(existing_rules, all_drafts)
print("merge:", counts)
state_rules.write_state_rules(spark, STATE_RULES_TBL, merged.to_dict("records"), mode="overwrite")

kb_io.write_table(spark, inventory_rows, T_INV, mode="append")
kb_io.write_table(spark, section_rows, T_SEC, mode="append")
kb_io.write_table(spark, log_rows, T_LOG, mode="append")
kb_io.write_table(spark, dropped_rows, T_DROP, mode="append")
kb_io.write_table(spark, change_rows, T_CHG, mode="append")
kb_io.write_table(spark, [{"cache_key": k, "proposals": json.dumps(v)} for k, v in cache.items()], T_CACHE, mode="overwrite")

# COMMAND ----------

# MAGIC %md ## What a reviewer sees next

# COMMAND ----------

display(spark.sql(f"""
  SELECT chapter, review_status, COUNT(*) AS rules, ROUND(AVG(extraction_confidence),2) AS avg_conf,
         SUM(CASE WHEN ambiguity_flag THEN 1 ELSE 0 END) AS ambiguous
  FROM {STATE_RULES_TBL} GROUP BY chapter, review_status ORDER BY chapter, review_status"""))

# COMMAND ----------

# MAGIC %md ## Export the review workbook (drafts, lowest confidence first)

# COMMAND ----------

wb = os.path.join(PDF_DIR, f"rule_review_{run_id}.xlsx")
state_rules.export_review_workbook(spark, STATE_RULES_TBL, wb, statuses=("draft", "needs_review"))
print("after the SME fills decision/reviewer:  state_rules.ingest_review_decisions(spark, STATE_RULES_TBL, wb, "
      f"'{KB}.policy_review_log')  then run build_kb")
