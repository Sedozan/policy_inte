# Databricks notebook source
# MAGIC %md
# MAGIC # Policy Knowledge Base — walkthrough
# MAGIC
# MAGIC Reads the published `main.policy_kb` tables. Nothing here rebuilds anything, so what
# MAGIC is shown is exactly what the KB contains. Three beats:
# MAGIC
# MAGIC 1. **A policy sentence → a cited, runnable detector** (state rule, then the same machine on a federal edit)
# MAGIC 2. **Coverage**: how much of what AHCCCS pays has a governing rule
# MAGIC 3. **Gap**: paid codes with no state rule, ranked by dollars

# COMMAND ----------

import json, pandas as pd
KB = "main.policy_kb"
rules = spark.table(f"{KB}.policy_rules").toPandas()
dets  = spark.table(f"{KB}.policy_detectors").toPandas()
print(f"rules: {len(rules):,}   detectors compiled: {int(dets['compiled'].sum()):,}   review leads: {int((~dets['compiled']).sum()):,}")
print(rules.groupby(["origin", "plane"]).size().to_string())

# COMMAND ----------

def show(rule_id=None, contains=(), origin=None, run=False, limit=20):
    """Print one rule end to end: policy text, citation, structured predicate, SQL, and optionally run it."""
    df = dets[dets["compiled"] == True]
    if origin:
        df = df[df["origin"] == origin]
    for s in contains:
        df = df[df["statement"].str.contains(s, regex=False)]
    if rule_id:
        df = df[df["rule_id"] == rule_id]
    if df.empty:
        print("no compiled detector matches"); return None
    d = df.iloc[0]
    r = rules[rules["rule_id"] == d["rule_id"]].iloc[0]
    print("=" * 78)
    print("POLICY:", r["statement"])
    if r.get("notes"):
        print("NOTES :", str(r["notes"])[:400])
    print(f"CITED : {r['source_doc']} — {r['source_locator']}\n        {r['source_url']}")
    print("WINDOW:", r["effective_date"] or "—", "→", r["end_date"] or "open")
    print("RULE  :", r["predicate"])
    print("ROUTE :", d["routing"], "| severity:", d["finding_severity"])
    print("-" * 78); print(d["sql"]); print("=" * 78)
    if run:
        res = spark.sql(d["sql"])
        n = res.count()
        print(f"hits against paid claims: {n:,}")
        if n:
            display(res.limit(limit))
    return d

# COMMAND ----------

# MAGIC %md ## Beat 1 — a Chapter 19 sentence becomes a detector

# COMMAND ----------

show(contains=("H2025", "H2026"), origin="manual", run=True)

# COMMAND ----------

# MAGIC %md ### …and the same compiler on a federal NCCI edit (no new code)

# COMMAND ----------

show(contains=("90832", "90791"), origin="ncci", run=True)

# COMMAND ----------

# MAGIC %md ### A rule that correctly refused to become a detector
# MAGIC Ambiguous or unmechanisable policy is kept as a review lead with its reason — never silently dropped, never guessed at.

# COMMAND ----------

leads = dets[dets["compiled"] == False].merge(rules[["rule_id", "ambiguity_flag", "ambiguity_note"]], on="rule_id")
display(leads[leads["ambiguity_flag"] == True][["rule_id", "statement", "reason", "ambiguity_note", "source_doc", "source_locator"]])

# COMMAND ----------

# MAGIC %md ## Beat 2 — coverage of what AHCCCS actually pays

# COMMAND ----------

cov = spark.table(f"{KB}.policy_coverage").toPandas()
tot_lines, tot_paid = cov["lines"].sum(), cov["paid_dollars"].sum()
s = cov.groupby("coverage").agg(codes=("code", "count"), lines=("lines", "sum"), paid_dollars=("paid_dollars", "sum")).reset_index()
s["pct_lines"] = (100 * s["lines"] / tot_lines).round(1)
if pd.notna(tot_paid) and tot_paid:
    s["pct_paid"] = (100 * s["paid_dollars"] / tot_paid).round(1)
display(s.sort_values("lines", ascending=False))

# COMMAND ----------

# MAGIC %md ## Beat 3 — the gap: paid codes with no state rule, ranked by exposure

# COMMAND ----------

display(spark.sql(f"SELECT * FROM {KB}.policy_gap ORDER BY paid_dollars DESC NULLS LAST, lines DESC LIMIT 25"))

# COMMAND ----------

# MAGIC %md ## Beat 4 — the PDF → rule pipeline: proposals, grounding, review queue
# MAGIC The model *proposes*; every proposal must quote the document verbatim and name codes that exist in it,
# MAGIC or it is refused. What survives waits for a named reviewer. Nothing here can fire a detector yet.

# COMMAND ----------

try:
    display(spark.sql(f"""
      SELECT chapter, review_status, COUNT(*) AS rules, ROUND(AVG(extraction_confidence),2) AS avg_confidence,
             SUM(CASE WHEN ambiguity_flag THEN 1 ELSE 0 END) AS flagged_ambiguous
      FROM main.sedo.state_rules WHERE extraction_key IS NOT NULL
      GROUP BY chapter, review_status ORDER BY chapter, review_status"""))
    print("refused by grounding (what the model proposed that the document does not say):")
    display(spark.sql(f"SELECT chapter, heading, drop_reason, statement, verbatim_quote FROM {KB}.policy_extraction_dropped ORDER BY chapter LIMIT 25"))
    print("one draft, end to end:")
    display(spark.sql(f"""SELECT chapter, statement, verbatim_quote, source_locator, extraction_confidence, grounding
                          FROM main.sedo.state_rules WHERE review_status='draft' ORDER BY extraction_confidence DESC LIMIT 5"""))
except Exception as e:
    print("no extraction run yet — execute the extract_chapters notebook first")

# COMMAND ----------

# MAGIC %md ## Has it ever fired? (telemetry from `run_detectors`)

# COMMAND ----------

try:
    display(spark.sql(f"""
        SELECT d.origin, d.predicate_type, r.finding_severity,
               COUNT(*) AS detectors_run, SUM(CASE WHEN r.hits > 0 THEN 1 ELSE 0 END) AS detectors_with_hits,
               SUM(r.hits) AS total_hits
        FROM {KB}.policy_detector_runs r JOIN {KB}.policy_detectors d USING (rule_id)
        GROUP BY d.origin, d.predicate_type, r.finding_severity ORDER BY total_hits DESC"""))
except Exception as e:
    print("no runs yet — execute the run_detectors notebook first")
