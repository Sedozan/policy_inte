# Databricks notebook source
# MAGIC %md
# MAGIC # One-time: seed `main.sedo.state_rules`
# MAGIC
# MAGIC Writes the reviewed AHCCCS state rules the build reads from. The rules come from
# MAGIC `state_seed.py` — a self-contained, cited set (Chapters 10 & 19) — so this notebook
# MAGIC does **not** depend on importing the old hand-authored modules. Grammar upgrades
# MAGIC (`code_not_covered`, `max_units_per_period`) are already applied.
# MAGIC
# MAGIC Optionally it will also pull any PoC modules that still import, but that path is
# MAGIC additive and fully guarded — if they fail, the seed alone still produces a table.
# MAGIC
# MAGIC Re-running overwrites the table with the same content (idempotent).

# COMMAND ----------

import os, sys, importlib, re
sys.path.insert(0, os.getcwd())
sys.path.append("/Workspace/Users/sedo.senou@azahcccs.gov/Projects/policy_intelligence")

import config, schema, state_rules, state_seed, kb_io
for m in (config, schema, state_seed, state_rules, kb_io):
    importlib.reload(m)

STATE_RULES_TBL = config.STATE_RULES_TABLE       # main.sedo.state_rules
MODE = "overwrite"                                # "append" to keep rows already there
PULL_POC_MODULES = False                          # True to also try the old modules (additive)

# COMMAND ----------

# MAGIC %md ## Collect rules — seed first, PoC modules optional

# COMMAND ----------

raw = list(state_seed.seed_rules())
print(f"  state_seed.seed_rules()          {len(raw):4d} rules")

if PULL_POC_MODULES:
    def _try(modname, fn):
        try:
            m = importlib.import_module(modname); importlib.reload(m)
            rs = list(getattr(m, fn)())
            raw.extend(rs)
            print(f"  {modname}.{fn:26s} {len(rs):4d} rules")
        except Exception as e:
            print(f"  {modname}.{fn:26s} SKIPPED  ({type(e).__name__}: {e})")
    _try("ingest_ahcccs",     "sample_rules")
    _try("demo_three_beats",  "all_demo_rules")
    _try("demo_ch10_beats",   "all_ch10_rules")
    _try("ch10_full_extract", "all_ch10_extended_rules")

if not raw:
    raise RuntimeError("no rules collected — check state_seed imports")     # fail loud, never write empty
print(f"\n{len(raw)} rule objects collected")

# COMMAND ----------

# MAGIC %md ## Chapter tag, stable ids, de-duplicate, write

# COMMAND ----------

def chapter_of(r):
    ch = state_seed.CHAPTER_BY_DOC.get(getattr(r, "source_doc", ""))
    if ch:
        return ch
    m = re.search(r"Chapter\s+(\d+)", getattr(r, "source_doc", "") or "")
    return m.group(1) if m else "unknown"

rows, seen = [], set()
for r in raw:
    row = state_rules.rule_to_row(r, chapter=chapter_of(r), review_status="legacy_hand_authored",
                                  reviewer=None, review_note="seeded during MVP build")
    row["rule_id"] = schema.stable_rule_id(r.to_dict())     # content-derived: no collisions
    if row["rule_id"] in seen:
        continue
    seen.add(row["rule_id"])
    rows.append(row)

import pandas as pd
if rows:
    summary = (pd.DataFrame(rows).groupby(["chapter", "machine_checkable"])
                 .size().reset_index(name="rules"))
    print(summary.to_string(index=False))
else:
    print("no rows to write")

state_rules.write_state_rules(spark, STATE_RULES_TBL, rows, mode=MODE)

# COMMAND ----------

# MAGIC %md ## Verify it reads back

# COMMAND ----------

display(spark.sql(f"""
  SELECT chapter, machine_checkable, COUNT(*) AS rules
  FROM {STATE_RULES_TBL} GROUP BY chapter, machine_checkable ORDER BY chapter
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Adding a chapter later
# MAGIC Add rows with `review_status='draft'`, export the review workbook, have a named SME
# MAGIC fill decision / reviewer, then `state_rules.ingest_review_decisions(...)`. Only
# MAGIC `approved` / `legacy_hand_authored` rows enter the KB. See `state_rules.py`.
