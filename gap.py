"""
gap.py  -  claims-weighted coverage and gap analysis.

The OIG question is "which PAID codes have no governing rule?" - so the gap
table starts from what was actually paid and weights every code by exposure.
No rule + $8M paid is a finding; no rule + $0 paid is noise. The PoC's
'risk' column was derived from rule presence alone (circular) and carried
editorial strings; both are gone.

coverage values:
  both          NCCI and state rule(s) cover the code
  federal_only  NCCI edit exists, no state (AHCCCS manual) rule loaded
  state_only    state rule exists, no NCCI edit
  none          paid code with no rule of either kind
"""
from __future__ import annotations

import pandas as pd


def _paid_col(spark, claims: str, candidates=("PMT_AMT", "PAID_AMT", "AMT_PAID", "PAID_AMOUNT")) -> str | None:
    cols = {c.upper(): c for c in spark.table(claims).columns}
    for c in candidates:
        if c.upper() in cols:
            return cols[c.upper()]
    return None


def exposure_by_code(spark, claims: str, paid_col: str | None = None,
                     units_col: str = "QUANTITY_PAID", proc_col: str = "PROC_CD") -> pd.DataFrame:
    cols = {c.upper() for c in spark.table(claims).columns}
    if paid_col and paid_col.upper() not in cols:
        print(f"  gap: configured paid column {paid_col!r} not in {claims}; auto-detecting")
        paid_col = None
    paid_col = paid_col or _paid_col(spark, claims)
    paid_expr = f"SUM({paid_col}) AS paid_dollars" if paid_col else "CAST(NULL AS DOUBLE) AS paid_dollars"
    if not paid_col:
        print("  gap: no paid-amount column found; ranking by line volume only "
              "(set PAID_COL in the notebook to fix)")
    df = spark.sql(f"""
        SELECT UPPER(TRIM({proc_col})) AS code,
               COUNT(*)                AS lines,
               SUM({units_col})        AS units,
               {paid_expr}
        FROM {claims}
        WHERE {proc_col} IS NOT NULL
        GROUP BY UPPER(TRIM({proc_col}))
    """).toPandas()
    # Spark DECIMAL sums arrive as Python Decimal objects; make them plain floats
    df["units"] = pd.to_numeric(df["units"], errors="coerce").astype(float)
    df["paid_dollars"] = pd.to_numeric(df["paid_dollars"], errors="coerce").astype(float)
    df["lines"] = df["lines"].astype("int64")
    return df


def coverage_table(spark, rules: list[dict], claims: str, paid_col: str | None = None) -> pd.DataFrame:
    exp = exposure_by_code(spark, claims, paid_col)

    ncci: dict[str, int] = {}
    state: dict[str, int] = {}
    for r in rules:
        bucket = ncci if r.get("origin") == "ncci" else state
        for c in (r.get("codes") or []):
            c = str(c).strip().upper()
            bucket[c] = bucket.get(c, 0) + 1

    exp["ncci_rules"] = exp["code"].map(ncci).fillna(0).astype(int)
    exp["state_rules"] = exp["code"].map(state).fillna(0).astype(int)

    def _cov(row):
        n, s = row["ncci_rules"] > 0, row["state_rules"] > 0
        return "both" if n and s else "federal_only" if n else "state_only" if s else "none"

    exp["coverage"] = exp.apply(_cov, axis=1)
    sort_cols = ["paid_dollars", "lines"] if exp["paid_dollars"].notna().any() else ["lines"]
    exp = exp.sort_values(sort_cols, ascending=False).reset_index(drop=True)
    return exp[["code", "coverage", "ncci_rules", "state_rules", "lines", "units", "paid_dollars"]]


def gap_rows(coverage: pd.DataFrame) -> pd.DataFrame:
    """Paid codes with no state rule, ranked by exposure."""
    return coverage[coverage["coverage"].isin(["none", "federal_only"])].reset_index(drop=True)


def coverage_summary(coverage: pd.DataFrame) -> pd.DataFrame:
    g = coverage.groupby("coverage").agg(codes=("code", "count"), lines=("lines", "sum"),
                                         paid_dollars=("paid_dollars", "sum")).reset_index()
    tot_lines = coverage["lines"].sum()
    g["pct_lines"] = (100 * g["lines"] / tot_lines).round(1)
    if coverage["paid_dollars"].notna().any():
        tot_paid = coverage["paid_dollars"].sum()
        g["pct_paid"] = (100 * g["paid_dollars"] / tot_paid).round(1)
    return g.sort_values("lines", ascending=False).reset_index(drop=True)
