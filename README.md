# Policy Knowledge Base — MVP

Makes AHCCCS policy computable: federal NCCI edits + AHCCCS manual chapters → one
cited, versioned rule table → deterministic SQL detectors → findings, coverage, gap.

```
config.py               tables, claims columns, routing, the 29-chapter registry (URLs verified 2026-09-18), LLM settings
schema.py               Rule + closed predicate grammar + content-derived ids
validate_gate.py        clean / patched / downgraded / needs_review / rejected — with reasons
ingest_ncci_tables.py   NCCI PTP + MUE -> rules (Spark-scoped, latest PTP quarter, dated MUE runs)
policy_docs.py          PDF -> sections   (acquire, hash, parse, strip headers, ALL-CAPS headings, pre-filter)
extract_rules.py        sections -> proposals -> GROUNDING -> drafts   (STUB / DBFM / HFLOCAL backends)
policy_changes.py       what changed since last extraction (by meaning, not wording)
state_rules.py          the reviewed state-rule table, merge rules, Excel review round-trip
state_seed.py           19 hand-authored, cited Ch10/Ch19 rules (legacy_hand_authored)
rule_normalize.py       planes, provenance, de-dup
detector_gen.py         rules -> SQL, severity, form routing
gap.py                  claims-weighted coverage + gap
kb_io.py                pandas -> Delta with explicit schemas (all-None columns are safe)
notebooks/
  migrate_state_rules   once   seed state_rules from state_seed
  extract_chapters      PDFs -> drafts in state_rules (+ inventory, sections, dropped, change log)
  build_kb              policy_rules / policy_detectors / policy_coverage / policy_gap
  run_detectors         executes detectors -> policy_findings + policy_detector_runs
  demo_kb               walkthrough of the published tables (read-only)
tests/                  20 offline checks incl. a fixture PDF in the real AHCCCS layout:  python -m pytest tests -q
```

## Run order

1. Copy every `.py` into the `policy_intelligence` folder (overwrite). Import the 5 notebooks.
2. `migrate_state_rules` — seeds `main.sedo.state_rules`.
3. `extract_chapters` — default `MVP_CHAPTERS = 4, 10, 13, 14, 19, 22`, backend `STUB`.
   First run proves the pipeline with no model. Then set `config.LLM_BACKEND = "DBFM"` and
   `config.DBFM_ENDPOINT` to a Foundation Model endpoint that exists in your workspace
   (Serving → Foundation Models; e.g. `databricks-gpt-oss-120b`), and re-run: cached sections
   are skipped, new proposals are grounded and merged as drafts.
4. Review: the notebook exports `rule_review_<run>.xlsx` (lowest confidence first, verbatim
   quote + citation beside each proposal). SME fills **decision / corrected_rule / reviewer**;
   `state_rules.ingest_review_decisions(spark, table, path, "main.policy_kb.policy_review_log")`.
5. `build_kb` — loads `approved` + `legacy_hand_authored` only. Drafts never compile.
6. `run_detectors`, then `demo_kb`.

If the workspace has no internet egress, upload the chapter PDFs to the Volume
`/Volumes/main/sedo/policy_docs` using the file names from `config.CHAPTERS` and set
`DOWNLOAD = False` in `extract_chapters`.

## The extraction contract (what makes an LLM safe here)

| stage | guarantee |
|---|---|
| segment | every section carries chapter, heading, page range, document hash → every rule has a real citation |
| pre-filter | only sections with rule signals (codes, "maximum", "not covered", "same day", …) reach the model |
| propose | the model must emit into the **closed grammar** and quote the source sentence verbatim; ambiguity → `not_checkable`, never a guess |
| ground | deterministic: quote must exist in the section; every code must exist in the section; predicate must be well-formed; a threshold not in the text is kept but flagged *derived*. Anything else is **refused** and written to `policy_extraction_dropped` with the reason |
| draft | lands in `state_rules` as `draft` with `extraction_confidence` (½ model self-report + ½ grounding score), verbatim quote, section id, run id |
| merge | keyed by **meaning** (chapter + predicate + codes): approved rows are never downgraded, rejected rows never resurrected, re-runs replace drafts in place |
| change tracking | document hash changes and rules not re-extracted are logged for a reviewer — nothing is deactivated automatically |
| detection | the LLM is never in the detection path; only reviewed rules compile |

## Tables

| table | one row per |
|---|---|
| `main.sedo.state_rules` | state rule: hand-authored or extracted, with `review_status` |
| `policy_document_inventory` | chapter × fetch: hash, revision date, pages, sections |
| `policy_sections` | section: heading, pages, text, rule signal |
| `policy_extraction_log` | chapter × run: proposals, grounded, dropped, cache hits, backend, prompt version |
| `policy_extraction_dropped` | refused proposal: what and why |
| `policy_extraction_cache` | section text-hash × prompt × model → proposals (re-runs are free) |
| `policy_change_log` | document / rule change events per run |
| `policy_review_log` | reviewer decisions (append) |
| `policy_rules`, `policy_detectors`, `policy_coverage`, `policy_gap`, `policy_build_log` | the KB build |
| `policy_detector_runs`, `policy_findings` | execution telemetry and findings |

## What was validated, and how

* Chapter list and every PDF URL: read from the live AHCCCS manual page (patterns are not uniform).
* Page header format, ALL-CAPS headings, word-number limits, `REVISION DATES:` line: read from the live
  Chapter 10 PDF; the test fixture reproduces them and the parser is asserted against it.
* NCCI column names and int/NULL date types: from your tables.
* 20 offline tests run the real modules end-to-end: parse → segment → pre-filter → STUB propose → ground
  (incl. hallucinated quote / wrong code / self-pair refused) → drafts → gate → normalize → compile;
  merge never downgrades approved / resurrects rejected; change tracking; Delta schema typing.

## NCCI routing — resolved from the data (2026-09-18)

A diagnostic join of each edition's codes to `all_data_C_A` by `FORM_TYP` showed:
DME → `A` (162k lines), outpatient → `A` (5.66M) and `D` (61k), practitioner → `A`; **no `O`**.
So `FORM_TYP` does **not** encode NCCI edition — `A` is a catch-all professional form carrying
all three. Consequences, now handled in code:

* `FORM_TYPE_MAP = {"A": "practitioner", "D": "outpatient"}` (the old `"O"` matched nothing).
* NCCI detectors route by **procedure code, not form** (`config.NCCI_ROUTE_BY_FORM = False`), so
  no edition is dropped or misrouted. The code in the `WHERE` clause selects the claims.
* A code in more than one edition is collapsed to the **most permissive limit**
  (`rule_normalize.collapse_ncci_editions`): MUE → max threshold, PTP → bypass-allowed if any
  edition allows it. Per-edition values are kept in `predicate['editions']` and flagged for review.
  This is what stops the stricter edition from false-positiving a shared `A` claim.

The real fix, when you have it, is a claim-type field (provider type / bill type) that distinguishes
practitioner vs facility vs DME-supplier claims; flip `NCCI_ROUTE_BY_FORM` to True then. Ask Audie
whether such a field exists in the extract — `FORM_TYP` is not it.

## Not yet verified (needs your workspace)

* Foundation Model endpoint name and whether `response_format=json_object` is honoured (the backend
  falls back to plain completion + JSON scan if not).
* Internet egress from the cluster to `azahcccs.gov` (fallback: upload PDFs to the Volume).
