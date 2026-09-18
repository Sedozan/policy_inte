"""
kb_io.py  -  pandas -> Delta without schema-inference surprises.

spark.createDataFrame(pdf) infers types from values. A column that is all None
(common: end_date, reviewer, error, sql for leads on a small run) cannot be
inferred and the write fails with "Can not merge type" / "can not infer schema".
This helper builds an explicit schema from pandas dtypes instead: bool -> boolean,
int -> long, float -> double, everything else -> string (None preserved).
"""
from __future__ import annotations

import json

import pandas as pd


def _spark_types():
    from pyspark.sql import types as T
    return T


def to_spark(spark, pdf: pd.DataFrame):
    T = _spark_types()
    pdf = pdf.copy()
    fields = []
    for c in pdf.columns:
        s = pdf[c]
        if pd.api.types.is_bool_dtype(s):
            t = T.BooleanType()
            pdf[c] = s.astype(bool)
        elif pd.api.types.is_integer_dtype(s):
            t = T.LongType()
        elif pd.api.types.is_float_dtype(s):
            t = T.DoubleType()
            pdf[c] = s.astype(float)
        else:
            t = T.StringType()
            pdf[c] = s.map(lambda v: None if v is None or (isinstance(v, float) and pd.isna(v))
                           else (json.dumps(v, default=str) if isinstance(v, (list, dict)) else str(v)))
        fields.append(T.StructField(str(c), t, True))
    pdf = pdf.astype(object).where(pdf.notna(), None)
    return spark.createDataFrame(pdf.values.tolist(), schema=T.StructType(fields))


def write_table(spark, rows_or_df, fqn: str, mode: str = "overwrite") -> int:
    pdf = rows_or_df if isinstance(rows_or_df, pd.DataFrame) else pd.DataFrame(rows_or_df)
    if pdf.empty:
        print(f"  (skipped {fqn}: no rows)")
        return 0
    w = to_spark(spark, pdf).write.format("delta").mode(mode)
    w = w.option("overwriteSchema", "true") if mode == "overwrite" else w.option("mergeSchema", "true")
    w.saveAsTable(fqn)
    print(f"  wrote {fqn}  ({len(pdf):,} rows, {mode})")
    return len(pdf)
