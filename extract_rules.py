"""
extract_rules.py  -  Stages 3-5: pre-filter, propose (LLM), ground, draft.

The model PROPOSES rules into the closed predicate grammar; it never decides.
Every proposal passes a deterministic grounding check (the quoted sentence must
exist in the section, every code must exist in the section, thresholds must be
present or be marked derived) and then lands in main.sedo.state_rules as a
DRAFT for a named reviewer. Nothing reaches a detector without that approval.

Backends (config.LLM_BACKEND):
  STUB     deterministic pattern extractor. Runs the whole pipeline with no
           model; also a floor for the simplest rule shapes.
  DBFM     Databricks Foundation Model serving endpoint, OpenAI-compatible.
  HFLOCAL  transformers on the cluster (best effort; gpt-oss harmony output is
           handled by scanning for the final JSON object).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime

import config
import schema
from policy_docs import CODE_RE, WORD_NUMBERS, rule_signal

PROMPT_VERSION = config.PROMPT_VERSION

# --------------------------------------------------------------- prompt
SYSTEM_PROMPT = """You extract billing rules from Arizona Medicaid (AHCCCS) policy text into a fixed JSON grammar.
You are an extractor, not a decider: propose only what the text explicitly states, quote it verbatim, and
flag anything ambiguous instead of resolving it.

OUTPUT: a single JSON object: {"rules": [...], "no_rule_reason": string|null}
Each rule object:
  "statement"        one plain-English sentence stating the rule as written
  "predicate"        one of the types below, with its fields
  "codes"            list of CPT/HCPCS codes the rule applies to (may be empty)
  "verbatim_quote"   the EXACT sentence(s) from the text that state the rule. Copy it character for character.
  "derived"          true if any number in the predicate is computed rather than written (say how in derivation_note)
  "derivation_note"  string|null
  "ambiguous"        true if the text is contradictory, conditional on facts not in a claim, or unclear
  "ambiguity_note"   string|null
  "not_checkable_reason" string|null (required when predicate.type is "not_checkable")
  "population"       "all" unless the text restricts (e.g. "members under 21", "ALTCS members")
  "confidence"       0.0-1.0, your honest confidence that the rule is exactly what the text says

PREDICATE TYPES (closed grammar - use nothing else):
  {"type":"max_units_per_day","code":"98960","threshold":4}
  {"type":"max_units_per_period","code_set":["98960","98961","98962"],"threshold":24,"period":"month"}   period: month|year|benefit_year
  {"type":"code_pair_prohibited","code_a":"H2025","code_b":"H2026","modifier_indicator":"0","service_category":"state"}
  {"type":"code_not_covered","code":"11975"}
  {"type":"max_dollars_per_period","code_set":[...],"threshold":1000,"period":"benefit_year"}
  {"type":"frequency_limit","code_set":[...],"threshold":8,"period":"benefit_year","unit":"visits"}
  {"type":"modifier_required","code_set":[...],"modifier":"GT"}
  {"type":"provider_type_allowed","code_set":[...],"provider_types":["07"]}
  {"type":"not_checkable","reason":"<one of: requires_medical_record | requires_member_attribute | ambiguous_source |
        cross_source_conflict | unit_incommensurability | policy_reference | no_claim_field>"}

RULES OF EXTRACTION
1. One rule per distinct obligation. "Codes A, B and C cannot be billed together on the same day" -> one
   code_pair_prohibited per pair (A/B, A/C, B/C). "maximum of four units per day (codes X, Y)" -> one
   max_units_per_day per code.
2. Numbers written as words ("four units") are fine; put the integer in the predicate and quote the words.
3. If a limit depends on information not on a claim (diagnosis justification, medical necessity, member age
   or program, prior visits) emit not_checkable with the right reason. Do not guess.
4. If the text contradicts itself or is unclear, emit not_checkable with reason ambiguous_source and explain.
5. A code RANGE (99201-99499) is not an enumerated set: emit not_checkable (cross_source_conflict or
   no_claim_field) and name the range in the statement.
6. Policy adoption statements ("AHCCCS follows CCI") are not_checkable with reason policy_reference.
7. Never invent a code, a number, or a quote. If you cannot quote it, do not emit it.
8. If the section states no billing rule, return {"rules": [], "no_rule_reason": "..."}.
"""

USER_TEMPLATE = """CHAPTER {chapter}: {title}
SECTION: {heading}   (pages {page_start}-{page_end}, revision {revision})

TEXT:
\"\"\"
{text}
\"\"\"

Return the JSON object now."""

_REQUIRED_KEYS = {
    "code_pair_prohibited": ("code_a", "code_b"),
    "max_units_per_line": ("code", "threshold"),
    "max_units_per_dos": ("code", "threshold"),
    "max_units_per_day": ("code", "threshold"),
    "max_units_per_period": ("threshold", "period"),
    "code_not_covered": (),
    "max_dollars_per_period": ("threshold", "period"),
    "frequency_limit": ("threshold",),
    "modifier_required": ("modifier",),
    "provider_type_allowed": ("provider_types",),
    "not_checkable": ("reason",),
}
_FIELDS_BY_TYPE = {
    "code_pair_prohibited": ["proc_cd", "member_id", "provider_id", "srvc_bgn_dt", "mod_1", "mod_2", "mod_3", "mod_4"],
    "max_units_per_day": ["proc_cd", "units", "member_id", "srvc_bgn_dt"],
    "max_units_per_period": ["proc_cd", "units", "member_id", "srvc_bgn_dt"],
    "max_units_per_line": ["proc_cd", "units"],
    "max_units_per_dos": ["proc_cd", "units", "member_id", "provider_id", "srvc_bgn_dt"],
    "code_not_covered": ["proc_cd"],
}


# ------------------------------------------------------------- backends
class Backend:
    name = "base"
    model = ""

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class DBFMBackend(Backend):
    """Databricks Foundation Model serving endpoint (OpenAI-compatible)."""
    name = "DBFM"

    def __init__(self, endpoint: str | None = None):
        self.model = endpoint or config.DBFM_ENDPOINT
        self._client = None

    def _client_(self):
        if self._client is None:
            try:
                from databricks.sdk import WorkspaceClient
                self._client = WorkspaceClient().serving_endpoints.get_open_ai_client()
            except Exception:
                import os
                from openai import OpenAI
                host = os.environ["DATABRICKS_HOST"].rstrip("/")
                self._client = OpenAI(api_key=os.environ["DATABRICKS_TOKEN"], base_url=f"{host}/serving-endpoints")
        return self._client

    def complete(self, system: str, user: str) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kw = dict(model=self.model, messages=msgs, temperature=config.LLM_TEMPERATURE,
                  max_tokens=config.LLM_MAX_TOKENS)
        try:
            r = self._client_().chat.completions.create(**kw, response_format={"type": "json_object"})
        except Exception:
            r = self._client_().chat.completions.create(**kw)
        return r.choices[0].message.content or ""


class HFLocalBackend(Backend):
    """transformers on the cluster. Best effort; plug your harmony-parsing call in here if you have one."""
    name = "HFLOCAL"

    def __init__(self, model: str | None = None):
        self.model = model or config.HF_MODEL
        self._pipe = None

    def complete(self, system: str, user: str) -> str:
        if self._pipe is None:
            from transformers import pipeline
            self._pipe = pipeline("text-generation", model=self.model, torch_dtype="auto", device_map="auto")
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        out = self._pipe(msgs, max_new_tokens=config.LLM_MAX_TOKENS, do_sample=False)
        text = out[0]["generated_text"]
        if isinstance(text, list):                 # chat-format output
            text = text[-1].get("content", "")
        return str(text)


class StubBackend(Backend):
    """Deterministic pattern extractor. No model. Emits the same JSON contract."""
    name = "STUB"
    model = "pattern-v1"

    _SENT = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")

    def complete(self, system: str, user: str) -> str:
        m = re.search(r'TEXT:\n"""\n(.*?)\n"""', user, re.S)
        text = m.group(1) if m else user
        rules, prev_codes = [], []
        for sent in self._SENT.split(text.replace("\n", " ")):
            s = sent.strip()
            if not s:
                continue
            codes = sorted(set(CODE_RE.findall(s)))
            # "These codes cannot be billed together..." refers back to the previous sentence
            carry = prev_codes if (not codes and re.match(r"^(these|those|both|the above) codes?\b", s.lower())) else []
            rules += self._patterns(s, carry)
            if codes:
                prev_codes = codes
        rules += self._section_level(text)
        return json.dumps({"rules": rules, "no_rule_reason": None if rules else "no rule pattern matched"})

    @staticmethod
    def _num(tok: str) -> int | None:
        tok = tok.lower()
        if tok.isdigit():
            return int(tok)
        return WORD_NUMBERS.get(tok)

    def _patterns(self, s: str, carry: list[str] | None = None) -> list[dict]:
        out = []
        low = s.lower()
        codes = sorted(set(CODE_RE.findall(s))) or list(carry or [])
        base = {"verbatim_quote": s, "derived": False, "derivation_note": None, "ambiguous": False,
                "ambiguity_note": None, "not_checkable_reason": None, "population": "all", "confidence": 0.6}

        m = re.search(r"maximum of (\w+(?:-\w+)?) units? per day", low)
        if m and codes and self._num(m.group(1)):
            n = self._num(m.group(1))
            for c in codes:
                out.append({**base, "statement": f"{c}: maximum {n} unit(s) per day.",
                            "predicate": {"type": "max_units_per_day", "code": c, "threshold": n}, "codes": [c]})
        m = re.search(r"up to (\w+(?:-\w+)?) units? per month per member", low)
        if m and codes and self._num(m.group(1)):
            n = self._num(m.group(1))
            out.append({**base, "statement": f"{'/'.join(codes)}: maximum {n} units per month per member.",
                        "predicate": {"type": "max_units_per_period", "code_set": codes, "threshold": n,
                                      "period": "month"}, "codes": codes})
        if re.search(r"cannot be billed (together )?on the same day", low) and len(codes) >= 2:
            for i in range(len(codes)):
                for j in range(i + 1, len(codes)):
                    a, b = codes[i], codes[j]
                    out.append({**base, "statement": f"{a} and {b} may not be billed on the same day for the same member.",
                                "predicate": {"type": "code_pair_prohibited", "code_a": a, "code_b": b,
                                              "modifier_indicator": "0", "service_category": "state"}, "codes": [a, b]})
        if re.search(r"do not bill|is not a covered service|not an ahcccs[- ]covered service|not reimburs", low) and codes:
            for c in codes:
                out.append({**base, "statement": f"{c} is not a covered/reimbursable service.",
                            "predicate": {"type": "code_not_covered", "code": c}, "codes": [c]})
        if "modifier" in low and re.search(r"\d{5}\s*-\s*\d{5}", s):
            out.append({**base, "statement": s, "predicate": {"type": "not_checkable", "reason": "cross_source_conflict"},
                        "codes": [], "ambiguous": True,
                        "ambiguity_note": "modifier restriction over a code range; conflicts with NCCI bypass rules",
                        "not_checkable_reason": "cross_source_conflict", "confidence": 0.5})
        if re.search(r"follows medicare'?s correct coding initiative", low):
            out.append({**base, "statement": s, "predicate": {"type": "not_checkable", "reason": "policy_reference"},
                        "codes": [], "not_checkable_reason": "policy_reference", "confidence": 0.7})
        return out


def _stub_section_level(self, text: str) -> list[dict]:
    """Contradictions usually span sentences: 'not covered.' ... 'will be re-instated'."""
    low = text.lower()
    out = []
    if "not covered" in low and re.search(r"re-?instated", low):
        sents = [x for x in self._SENT.split(text.replace("\n", " ")) if re.search(r"re-?instated", x, re.I)]
        if sents:
            out.append({"statement": "Coverage statement is contradictory: the section says both 'not covered' and "
                                     "'re-instated' for the same service/population.",
                        "predicate": {"type": "not_checkable", "reason": "ambiguous_source"}, "codes": [],
                        "verbatim_quote": sents[0].strip(), "derived": False, "derivation_note": None,
                        "ambiguous": True, "ambiguity_note": "text states both 'not covered' and 're-instated'",
                        "not_checkable_reason": "ambiguous_source", "population": "all", "confidence": 0.4})
    return out


StubBackend._section_level = _stub_section_level


def make_backend(name: str | None = None) -> Backend:
    name = (name or config.LLM_BACKEND).upper()
    if name == "STUB":
        return StubBackend()
    if name == "DBFM":
        return DBFMBackend()
    if name == "HFLOCAL":
        return HFLocalBackend()
    raise ValueError(f"unknown LLM_BACKEND {name!r}")


# ------------------------------------------------------------- proposing
def _extract_json(text: str) -> dict:
    """Find the last well-formed JSON object in a model response (tolerates fences, harmony channels)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except Exception:
        pass
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for st in reversed(starts):
        depth = 0
        for i in range(st, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[st:i + 1])
                        if isinstance(obj, dict) and "rules" in obj:
                            return obj
                    except Exception:
                        break
    raise ValueError("no JSON object with a 'rules' key in model output")


def propose(section: dict, inv: dict, backend: Backend) -> tuple[list[dict], str | None]:
    user = USER_TEMPLATE.format(chapter=inv["chapter"], title=inv["title"], heading=section["heading"],
                                page_start=section["page_start"], page_end=section["page_end"],
                                revision=inv.get("revision_date") or "unknown", text=section["text"])
    raw = backend.complete(SYSTEM_PROMPT, user)
    try:
        obj = _extract_json(raw)
    except Exception as e:
        return [], f"unparseable model output: {e}"
    rules = obj.get("rules") or []
    clean = []
    for r in rules:
        if not isinstance(r, dict) or not isinstance(r.get("predicate"), dict):
            continue
        r.setdefault("codes", [])
        r.setdefault("confidence", 0.5)
        r.setdefault("population", "all")
        clean.append(r)
    return clean, obj.get("no_rule_reason")


# ------------------------------------------------------------- grounding
def _norm(s: str) -> str:
    s = (s or "").lower().replace("–", "-").replace("—", "-").replace("’", "'")
    s = re.sub(r"[\"“”]", "", s)
    return re.sub(r"\s+", " ", s).strip().rstrip(".;:")


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.;])\s+", text.replace("\n", " ")) if x.strip()]


def _threshold_present(n, text_norm: str) -> bool:
    if n is None:
        return True
    try:
        n = int(n)
    except (TypeError, ValueError):
        return False
    if re.search(rf"\b{n}\b", text_norm):
        return True
    words = [w for w, v in WORD_NUMBERS.items() if v == n]
    return any(re.search(rf"\b{re.escape(w)}\b", text_norm) for w in words)


def _predicate_codes(p: dict) -> list[str]:
    out = []
    for k in ("code_a", "code_b", "code"):
        if p.get(k):
            out.append(str(p[k]).upper())
    out += [str(c).upper() for c in (p.get("code_set") or [])]
    return list(dict.fromkeys(out))


def predicate_valid(p: dict) -> tuple[bool, str]:
    t = p.get("type")
    if t not in schema.PREDICATE_TYPES:
        return False, f"unknown predicate type {t!r}"
    for k in _REQUIRED_KEYS.get(t, ()):
        if p.get(k) in (None, "", []):
            return False, f"{t} missing {k}"
    if t == "code_pair_prohibited" and str(p["code_a"]).upper() == str(p["code_b"]).upper():
        return False, "self pair"
    if t in ("max_units_per_period", "code_not_covered", "max_dollars_per_period") and not _predicate_codes(p):
        return False, f"{t} needs code or code_set"
    if t == "max_units_per_period" and p.get("period") not in ("month", "year", "benefit_year"):
        return False, f"bad period {p.get('period')!r}"
    if "threshold" in p:
        try:
            if int(p["threshold"]) <= 0:
                return False, "threshold must be positive"
        except (TypeError, ValueError):
            return False, "threshold not an integer"
    return True, ""


def ground(proposal: dict, section: dict) -> dict:
    """Deterministic verification of one proposal against its source section."""
    text_norm = _norm(section["text"])
    quote = proposal.get("verbatim_quote") or ""
    qn = _norm(quote)
    if qn and qn in text_norm:
        quote_found = "exact"
    elif qn:
        best = max((difflib.SequenceMatcher(None, qn, _norm(s)).ratio() for s in _sentences(section["text"])), default=0)
        quote_found = "fuzzy" if best >= 0.85 else "none"
    else:
        quote_found = "none"

    p = proposal.get("predicate") or {}
    codes = _predicate_codes(p) + [str(c).upper() for c in proposal.get("codes") or []]
    codes = list(dict.fromkeys(codes))
    if codes:
        hit = [c for c in codes if c.lower() in text_norm]
        codes_found = "all" if len(hit) == len(codes) else "some" if hit else "none"
    else:
        codes_found = "n/a"
    thr_ok = _threshold_present(p.get("threshold"), text_norm) if "threshold" in p else None
    valid, why = predicate_valid(p)

    score = {"exact": 0.5, "fuzzy": 0.35, "none": 0.0}[quote_found]
    score += {"all": 0.25, "n/a": 0.25, "some": 0.10, "none": 0.0}[codes_found]
    score += 0.15 if thr_ok in (True, None) else 0.05
    score += 0.10 if valid else 0.0

    keep, reason = True, None
    if quote_found == "none":
        keep, reason = False, "quote not found in source"
    elif not valid:
        keep, reason = False, f"invalid predicate: {why}"
    elif codes_found == "none":
        keep, reason = False, "codes not in source"
    return {"quote_found": quote_found, "codes_found": codes_found, "threshold_found": thr_ok,
            "predicate_valid": valid, "score": round(score, 2), "keep": keep, "drop_reason": reason,
            "codes": codes}


# -------------------------------------------------------------- drafts
def semantic_key(chapter: str, p: dict, codes: list[str]) -> str:
    sig = json.dumps({"c": str(chapter), "p": p, "codes": sorted(codes)}, sort_keys=True, default=str)
    return hashlib.sha256(sig.encode()).hexdigest()[:16]


def _effective_from_quote(quote: str) -> str | None:
    m = re.search(r"effective\s+(\d{1,2})/(\d{1,2})/(\d{4})", quote or "", re.I)
    if not m:
        return None
    mo, d, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def to_draft_row(proposal: dict, g: dict, section: dict, inv: dict, backend: Backend, run_id: str) -> dict:
    from state_rules import rule_to_row
    p = dict(proposal["predicate"])
    t = p["type"]
    codes = g["codes"]
    ambiguous = bool(proposal.get("ambiguous")) or (g["threshold_found"] is False)
    anote = proposal.get("ambiguity_note")
    if g["threshold_found"] is False:
        anote = ((anote + " ") if anote else "") + "threshold not found verbatim in source; treat as derived."
    not_reason = proposal.get("not_checkable_reason") or (p.get("reason") if t == "not_checkable" else None)
    machine = t in schema.PREDICATE_TYPES_COMPILED and not not_reason
    pages = (f"page {section['page_start']}" if section["page_start"] == section["page_end"]
             else f"pages {section['page_start']}-{section['page_end']}")
    notes = f'Verbatim: "{proposal.get("verbatim_quote", "").strip()}"'
    if proposal.get("derived") and proposal.get("derivation_note"):
        notes += f" Derived: {proposal['derivation_note']}"
    if proposal.get("population") and proposal["population"] != "all":
        notes += f" Population: {proposal['population']}."

    rule = schema.Rule(
        origin="extracted", plane="coverage_scope", binding_status="state_policy",
        statement=(proposal.get("statement") or "").strip(),
        predicate=p, codes=codes, population=proposal.get("population") or "all",
        machine_checkable=machine, not_checkable_reason=not_reason,
        required_claim_fields=_FIELDS_BY_TYPE.get(t, []),
        ambiguity_flag=ambiguous, ambiguity_note=anote,
        source_doc=f"AHCCCS FFS Provider Billing Manual, Chapter {inv['chapter']} {inv['title']}",
        source_locator=f"{pages} — {section['heading']}", source_url=inv["url"],
        doc_version=f"rev {inv.get('revision_date') or 'unknown'}", doc_hash=inv["doc_hash"],
        effective_date=_effective_from_quote(proposal.get("verbatim_quote", "")),
        notes=notes,
    )
    row = rule_to_row(rule, chapter=str(inv["chapter"]), review_status="draft",
                      review_note=f"extracted by {backend.name}:{backend.model} prompt {PROMPT_VERSION}")
    key = semantic_key(inv["chapter"], p, codes)
    llm_conf = float(proposal.get("confidence") or 0.5)
    row.update({
        "rule_id": f"EXT-{key}",
        "extraction_key": key,
        "verbatim_quote": proposal.get("verbatim_quote"),
        "section_id": section["section_id"],
        "extraction_confidence": round(0.5 * llm_conf + 0.5 * g["score"], 2),
        "extraction_run_id": run_id,
        "extraction_backend": f"{backend.name}:{backend.model}",
        "grounding": json.dumps(g, default=str),
    })
    return row


# ---------------------------------------------------------- orchestrate
def extract_chapter(sections: list[dict], inv: dict, backend: Backend, run_id: str,
                    only_candidates: bool = True, cache: dict | None = None,
                    log_fn=print) -> tuple[list[dict], list[dict], dict]:
    """Returns (draft_rows, dropped, stats). `cache` maps cache_key -> proposals list."""
    cache = cache if cache is not None else {}
    drafts, dropped = [], []
    stats = {"chapter": inv["chapter"], "doc_hash": inv["doc_hash"], "run_id": run_id,
             "backend": f"{backend.name}:{backend.model}", "prompt_version": PROMPT_VERSION,
             "sections": len(sections), "sections_candidate": 0, "sections_from_cache": 0,
             "proposals": 0, "grounded": 0, "dropped": 0, "unparseable": 0,
             "started_at": datetime.now().isoformat(timespec="seconds")}
    for s in sections:
        if only_candidates and not s.get("has_rule_signal"):
            continue
        stats["sections_candidate"] += 1
        ck = f"{s['text_hash']}|{PROMPT_VERSION}|{backend.name}|{backend.model}"
        if ck in cache:
            proposals, why = cache[ck], None
            stats["sections_from_cache"] += 1
        else:
            proposals, why = propose(s, inv, backend)
            if why and "unparseable" in why:
                stats["unparseable"] += 1
                log_fn(f"    [{s['heading'][:40]}] {why}")
            cache[ck] = proposals
        stats["proposals"] += len(proposals)
        for pr in proposals:
            g = ground(pr, s)
            if g["keep"]:
                drafts.append(to_draft_row(pr, g, s, inv, backend, run_id))
                stats["grounded"] += 1
            else:
                dropped.append({"chapter": inv["chapter"], "section_id": s["section_id"], "heading": s["heading"],
                                "statement": pr.get("statement"), "predicate": json.dumps(pr.get("predicate")),
                                "verbatim_quote": pr.get("verbatim_quote"), "drop_reason": g["drop_reason"],
                                "run_id": run_id})
                stats["dropped"] += 1
    # de-duplicate within the run by semantic key (keep the highest-confidence copy)
    best: dict[str, dict] = {}
    for d in drafts:
        k = d["extraction_key"]
        if k not in best or d["extraction_confidence"] > best[k]["extraction_confidence"]:
            best[k] = d
    drafts = list(best.values())
    stats["drafts_unique"] = len(drafts)
    stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return drafts, dropped, stats
