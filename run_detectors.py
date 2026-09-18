# Databricks notebook source
# MAGIC %md
# MAGIC # Run detectors against paid claims
# MAGIC
# MAGIC Executes the compiled detectors in `main.policy_kb.policy_detectors` and writes
# MAGIC
# MAGIC * `policy_detector_runs` — one row per detector per run: hit count, seconds, error (append-only). This is the
# MAGIC   enforcement telemetry the PoC lacked: it answers "has this rule ever fired?" with data.
# MAGIC * `policy_findings` — the flagged claim lines / member-days, tagged with `rule_id` and `finding_severity`.
# MAGIC
# MAGIC Run order: state rules first (few, high-value), then NCCI. `MAX_DETECTORS` caps a run so it
# MAGIC stays affordable on a small warehouse; raise it once you know the runtime.

# COMMAND ----------

import time, json
import pandas as pd
from pyspark.sql import functions as F

KB              = "main.policy_kb"
MAX_DETECTORS   = 300          # None = all
MAX_ROWS_PER_DETECTOR = 20_000 # findings kept per detector per run
INCLUDE_UNROUTED = False       # UNROUTED detectors (e.g. dme before its form type is mapped) over-flag

det = spark.table(f"{KB}.policy_detectors").toPandas()
det = det[det["compiled"] == True]
if not INCLUDE_UNROUTED:
    det = det[~det["routing"].astype(str).str.startswith("UNROUTED")]
det["prio"] = (det["origin"] != "ncci").astype(int)            # state first
det = det.sort_values(["prio", "predicate_type"], ascending=[False, True])
if MAX_DETECTORS:
    det = det.head(MAX_DETECTORS)
print(f"running {len(det)} detectors")

# COMMAND ----------

run_id = pd.Timestamp.now().strftime("%Y%m%dT%H%M%S")
runs, findings = [], []
for i, d in enumerate(det.to_dict("records"), 1):
    t0 = time.time()
    rec = {"run_id": run_id, "rule_id": d["rule_id"], "predicate_type": d["predicate_type"],
           "service_category": d["service_category"], "finding_severity": d["finding_severity"],
           "hits": None, "seconds": None, "error": None}
    try:
        sdf = spark.sql(d["sql"])
        n = sdf.count()
        rec["hits"] = n
        if n:
            f = (sdf.limit(MAX_ROWS_PER_DETECTOR)
                    .withColumn("rule_id", F.lit(d["rule_id"]))
                    .withColumn("run_id", F.lit(run_id)))
            # normalise to one schema: line-level and aggregate detectors differ
            for c in ("claim_id", "line_no", "servicing_provider", "dos", "period_start"):
                if c not in f.columns:
                    f = f.withColumn(c, F.lit(None).cast("string"))
            f = (f.withColumn("claim_id", F.col("claim_id").cast("string"))
                  .withColumn("line_no", F.col("line_no").cast("string"))
                  .withColumn("member_id", F.col("member_id").cast("string"))
                  .withColumn("servicing_provider", F.col("servicing_provider").cast("string"))
                  .withColumn("dos", F.col("dos").cast("string"))
                  .withColumn("period_start", F.col("period_start").cast("string"))
                  .withColumn("observed_units", F.col("observed_units").cast("double")))
            findings.append(f.select("run_id", "rule_id", "claim_id", "line_no", "member_id",
                                     "servicing_provider", "dos", "period_start", "finding",
                                     "observed_units", "allowed_units", "finding_severity"))
    except Exception as e:
        rec["error"] = str(e)[:500]
    rec["seconds"] = round(time.time() - t0, 1)
    runs.append(rec)
    if i % 25 == 0:
        print(f"  {i}/{len(det)}  hits so far: {sum(r['hits'] or 0 for r in runs):,}")

runs_df = pd.DataFrame(runs)
runs_df["hits"] = pd.to_numeric(runs_df["hits"]).astype("Int64")
runs_df["seconds"] = pd.to_numeric(runs_df["seconds"]).astype(float)
print(runs_df[["hits", "seconds"]].describe().to_string())
print("errors:", int(runs_df["error"].notna().sum()))

# COMMAND ----------

import os, sys
sys.path.insert(0, os.getcwd()); sys.path.append("/Workspace/Users/sedo.senou@azahcccs.gov/Projects/policy_intelligence")
from kb_io import write_table
write_table(spark, runs_df, f"{KB}.policy_detector_runs", mode="append")
if findings:
    out = findings[0]
    for f in findings[1:]:
        out = out.unionByName(f, allowMissingColumns=True)
    out.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(f"{KB}.policy_findings")
    print(f"findings written: {out.count():,}")
print(f"run {run_id} logged to {KB}.policy_detector_runs")

# COMMAND ----------

# MAGIC %md ## What fired

# COMMAND ----------

display(spark.sql(f"""
SELECT r.rule_id, d.predicate_type, d.service_category, r.finding_severity, r.hits, r.seconds, d.statement
FROM {KB}.policy_detector_runs r JOIN {KB}.policy_detectors d USING (rule_id)
WHERE r.run_id = '{run_id}' AND r.hits > 0
ORDER BY r.hits DESC
"""))
