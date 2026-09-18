# Databricks notebook source
# MAGIC %md
# MAGIC # Policy Knowledge Base — build
# MAGIC
# MAGIC Rebuilds `main.policy_kb` from scratch, deterministically, every run.
# MAGIC
# MAGIC | step | what |
# MAGIC |---|---|
# MAGIC | 1 | **Scope** = the distinct procedure codes actually paid in the claims table (no hand-picked list) |
# MAGIC | 2 | **NCCI** (federal) rules from the quarterly tables, filtered to scope in Spark |
# MAGIC | 3 | **State** (AHCCCS manual) rules from the reviewed table `main.sedo.state_rules` |
# MAGIC | 4 | Validation gate → normalise (stable ids, planes, provenance) → compile to SQL |
# MAGIC | 5 | Claims-weighted coverage and gap |
# MAGIC | 6 | Write `policy_rules`, `policy_detectors`, `policy_coverage`, `policy_gap` |
# MAGIC
# MAGIC No demo code lives here. See `demo_kb` (reads the published tables) and `run_detectors`.

# COMMAND ----------

import os, sys, json, time
sys.path.insert(0, os.getcwd())
sys.path.append("/Workspace/Users/sedo.senou@azahcccs.gov/Projects/policy_intelligence")

import importlib
import pandas as pd
import config

SOURCE_CATALOG, SOURCE_SCHEMA = config.CATALOG, config.SCHEMA            # main.sedo
OUTPUT_CATALOG, OUTPUT_SCHEMA = config.OUTPUT_CATALOG, config.OUTPUT_SCHEMA  # main.policy_kb
CLAIMS          = config.CLAIMS_TABLE                                    # main.prod_input.all_data_C_A
STATE_RULES_TBL = config.STATE_RULES_TABLE
PAID_COL        = config.CLAIM_COLS.get("paid_amt")                      # None = auto-detect
SCOPE           = "claims"                                               # "claims" (default) | "all"

# Once the DME form type is confirmed (see the routing cell below), map it in
# config.SERVICE_FORM_MAP, e.g. {"dme": ["A"]}.

import ingest_ncci_tables, state_rules, validate_gate, rule_normalize, detector_gen, gap, schema, kb_io
for m in (config, schema, kb_io, ingest_ncci_tables, state_rules, validate_gate, rule_normalize, detector_gen, gap):
    importlib.reload(m)
print("engine loaded")

# COMMAND ----------

# MAGIC %md ## 0. Preflight — resolve every column before building
# MAGIC Fails loudly on the first mismatch. Nothing downstream is trusted until this cell is green.

# COMMAND ----------

problems = []
# claims columns named in config
claims_cols = {c.upper() for c in spark.table(CLAIMS).columns}
need = [v for k, v in config.CLAIM_COLS.items() if k != "mods" and isinstance(v, str)] + list(config.CLAIM_COLS["mods"])
for c in dict.fromkeys(need):
    if c.upper() not in claims_cols:
        problems.append(f"claims column missing: {c}")

# NCCI tables: every column the ingest reads
metas = ingest_ncci_tables.discover_tables(spark, SOURCE_CATALOG, SOURCE_SCHEMA)
if not metas:
    problems.append(f"no medicaid_ncci_edit_* tables in {SOURCE_CATALOG}.{SOURCE_SCHEMA}")
fc = ingest_ncci_tables._find_col
for m in metas:
    cols = spark.table(m["fqn"]).columns
    if m["edit_type"] == "ptp":
        want = {"col1": fc(cols, "column1", "col1", "comprehensive code"),
                "col2": fc(cols, "column2", "col2", "component code"),
                "modifier": fc(cols, "modifier indicator", "modifier"),
                "effdt": fc(cols, "effective date", "effdt"),
                "deldt": fc(cols, "deletion date", "deldt")}
    else:
        want = {"code": fc(cols, "hcpcs/cpt code", "hcpcs cpt code", "procedure code", "code"),
                "value": fc(cols, "mue value", "mue values")}
    missing = [k for k, v in want.items() if v is None]
    if missing:
        problems.append(f"{m['table']}: cannot resolve {missing}; columns are {cols}")
    print(f"  {m['label']} {m['service']:12s} {m['edit_type']}  -> {want}")

# state rules table
try:
    st_cols = set(spark.table(STATE_RULES_TBL).columns)
    miss = [c for c in state_rules.STATE_RULE_COLUMNS if c not in st_cols]
    if miss:
        problems.append(f"{STATE_RULES_TBL} missing columns {miss} (re-run migrate_state_rules)")
except Exception as e:
    problems.append(f"{STATE_RULES_TBL} not readable: {e}  (run migrate_state_rules first)")

if problems:
    raise RuntimeError("PREFLIGHT FAILED:\n  - " + "\n  - ".join(problems))
print("preflight OK")

# COMMAND ----------

# MAGIC %md ## 1. Scope from paid claims

# COMMAND ----------

t0 = time.time()
if SCOPE == "claims":
    scope_codes = {r.code for r in spark.sql(
        f"SELECT DISTINCT UPPER(TRIM(PROC_CD)) AS code FROM {CLAIMS} WHERE PROC_CD IS NOT NULL").collect()}
    print(f"scope: {len(scope_codes):,} distinct paid procedure codes")
else:
    scope_codes = None
    print("scope: all codes (full federal library)")

# COMMAND ----------

# MAGIC %md ## 2. Federal rules — NCCI PTP + MUE

# COMMAND ----------

ncci_rules = ingest_ncci_tables.ingest_tables(spark, SOURCE_CATALOG, SOURCE_SCHEMA, code_filter=scope_codes)

# COMMAND ----------

# MAGIC %md ## 3. State rules — reviewed table

# COMMAND ----------

st_rules = state_rules.load_state_rules(spark, STATE_RULES_TBL, scope_codes)

# COMMAND ----------

# MAGIC %md ## 4. Gate → normalise → compile

# COMMAND ----------

rules = ncci_rules + st_rules                      # assembled once; nothing appended in place
accepted, rejected = validate_gate.gate(rules)
print("gate:", json.dumps(validate_gate.summarize(accepted, rejected)))
for r in rejected[:10]:
    print("   rejected:", r["reject_reason"], "|", (r.get("statement") or "")[:80])

accepted, norm_report = rule_normalize.normalize_rules(accepted)
print("normalise:", json.dumps(norm_report, indent=1))

detectors = detector_gen.build_detectors(accepted)
summary = detector_gen.summarize(detectors)
print("detectors:", json.dumps(summary, indent=1))

# COMMAND ----------

# MAGIC %md ### Routing check
# MAGIC NCCI editions are claim-form specific. Anything `UNROUTED` compiles but is not
# MAGIC restricted to a form type — confirm the form value for that service in the claims
# MAGIC table and add it to `config.SERVICE_FORM_MAP` at the top of this notebook.

# COMMAND ----------

print("claim form types in the claims table:")
display(spark.sql(f"SELECT FORM_TYP, COUNT(*) AS lines FROM {CLAIMS} GROUP BY FORM_TYP ORDER BY lines DESC"))
unrouted = sorted({d["service_category"] for d in detectors if d["compiled"] and str(d["routing"]).startswith("UNROUTED")})
print("unrouted service categories:", unrouted or "none")

# COMMAND ----------

# MAGIC %md ## 5. Coverage and gap (claims-weighted)

# COMMAND ----------

coverage = gap.coverage_table(spark, accepted, CLAIMS, PAID_COL)
gap_df   = gap.gap_rows(coverage)
print(gap.coverage_summary(coverage).to_string(index=False))
print(f"\ngap: {len(gap_df):,} paid codes with no state rule; top 15 by exposure:")
print(gap_df.head(15).to_string(index=False))

# COMMAND ----------

# MAGIC %md ## 6. Write outputs

# COMMAND ----------

from kb_io import write_table          # explicit Spark schema: all-None columns are safe

def write(rows, table, mode="overwrite"):
    return write_table(spark, rows, f"{OUTPUT_CATALOG}.{OUTPUT_SCHEMA}.{table}", mode)

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {OUTPUT_CATALOG}.{OUTPUT_SCHEMA}")
build_meta = {"built_at": pd.Timestamp.now().isoformat(timespec="seconds"), "scope": SCOPE,
              "scope_codes": len(scope_codes) if scope_codes else None, "claims_table": CLAIMS,
              "rules": len(accepted), "detectors_compiled": summary["compiled"], "leads": summary["leads"]}
for r in accepted:
    r["built_at"] = build_meta["built_at"]

print("writing outputs:")
write(accepted,  "policy_rules")      # every vetted rule: compiled AND leads
write(detectors, "policy_detectors")  # one row per rule: SQL or the reason there is none
write(coverage,  "policy_coverage")   # every paid code, its coverage and exposure
write(gap_df,    "policy_gap")        # paid codes with no state rule, ranked by exposure

# append-only build history: what scope and counts each published KB was built from
write([build_meta], "policy_build_log", mode="append")
print(f"\nbuild finished in {time.time()-t0:,.0f}s — {json.dumps(build_meta)}")
