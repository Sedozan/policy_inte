"""
policy_docs.py  -  Stages 0-2 of chapter extraction: acquire, parse, segment.

  acquire()   download a chapter PDF into PDF_DIR (or read the copy already
              there), hash it, return an inventory record.
  parse_pdf() PDF -> pages of lines with font size / bold (PyMuPDF), with a
              plain-text fallback (pypdf).
  segment()   strip the repeated page header, detect ALL-CAPS section headings,
              and cut the chapter into sections that carry heading + page range.
  rule_signal() cheap pre-filter: does this text look like it states a billing
              rule? (codes, modifiers, limit/prohibition phrases)

Layout facts these heuristics were validated against (FFS Chapter 10, rev
04/29/2026):
  * page header:  "04/29/2026 CHAPTER 10 INDIVIDUAL PRACTITIONER SERVICES 1 | 59"
    - date first, page counter varies per page  -> compare with digits removed
  * headings are ALL CAPS and unnumbered: "CORRECT CODING INITIATIVE"
  * limits are often written as words: "a maximum of four units per day"
  * revision history: "REVISION DATES: 04/29/2026; 05/02/2024; ..."
"""
from __future__ import annotations

import hashlib
import os
import re
import statistics
from datetime import datetime

import config

# ------------------------------------------------------------ constants
_HEADER_RE = re.compile(r"^\s*\d{2}/\d{2}/\d{4}\s+CHAPTER\s+\d+[A-Z]?\b.*\b\d+\s*\|\s*\d+\s*$", re.I)
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_REVISION_RE = re.compile(r"REVISION\s+DATES?\s*:\s*(.+)", re.I)

CODE_RE = re.compile(r"\b(?:\d{4}[A-Z0-9]|[A-Z]\d{4})\b")          # CPT 5-digit/4+letter, HCPCS letter+4
CODE_RANGE_RE = re.compile(r"\b(\d{5})\s*[-–]\s*(\d{5})\b")
MODIFIER_RE = re.compile(r"\bmodifiers?\b", re.I)
TRIGGER_PHRASES = (
    "maximum", "per day", "per month", "per year", "per benefit year", "per contract year",
    "units", "not covered", "non-covered", "not a covered", "not reimburs", "shall not",
    "cannot be billed", "may not be billed", "must not be billed", "do not bill", "limited to",
    "not to exceed", "prior authorization", "same day", "same date of service", "once per",
    "only when", "only if", "is not an ahcccs", "not an ahcccs-covered", "will be denied",
    "must be billed", "must use", "requires",
)

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty-four": 24, "thirty": 30, "forty-eight": 48, "sixty": 60, "ninety": 90,
}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------- acquire
def acquire(chapter: str, pdf_dir: str | None = None, download: bool = True,
            timeout: int = 60) -> dict:
    """Return an inventory record for one chapter and make sure its PDF is on disk."""
    chapter = str(chapter)
    if chapter not in config.CHAPTERS:
        raise KeyError(f"chapter {chapter!r} not in config.CHAPTERS")
    title, url = config.CHAPTERS[chapter]
    pdf_dir = pdf_dir or config.PDF_DIR
    os.makedirs(pdf_dir, exist_ok=True)
    path = os.path.join(pdf_dir, os.path.basename(url))
    source = "volume"
    if download:
        try:
            import requests
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "policy-kb/1.0"})
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise ValueError("response is not a PDF")
            with open(path, "wb") as f:
                f.write(r.content)
            source = "download"
        except Exception as e:
            if not os.path.exists(path):
                raise RuntimeError(
                    f"chapter {chapter}: download failed ({type(e).__name__}: {e}) and no copy at {path}. "
                    f"Upload the PDF to {pdf_dir} and re-run with download=False.") from e
            print(f"  chapter {chapter}: download failed ({type(e).__name__}); using {path}")
    elif not os.path.exists(path):
        raise FileNotFoundError(f"chapter {chapter}: {path} not found; set download=True or upload it")
    with open(path, "rb") as f:
        data = f.read()
    return {"chapter": chapter, "title": title, "url": url, "path": path,
            "doc_hash": _sha(data), "bytes": len(data), "source": source,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}


# ----------------------------------------------------------------- parse
def parse_pdf(path: str) -> list[dict]:
    """[{page, lines:[{text,size,bold}], tables:[[row,...],...]}] (1-based pages)."""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # older name
        except ImportError:
            fitz = None
    if fitz is not None:
        return _parse_pymupdf(fitz, path)
    return _parse_pypdf(path)


def _parse_pymupdf(fitz, path: str) -> list[dict]:
    pages = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, 1):
            lines = []
            d = page.get_text("dict")
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = "".join(s.get("text", "") for s in spans).strip()
                    if not text:
                        continue
                    size = max((s.get("size", 0) for s in spans), default=0)
                    bold = any((s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower() for s in spans)
                    lines.append({"text": text, "size": round(size, 1), "bold": bool(bold)})
            tables = []
            try:
                for t in page.find_tables().tables:
                    rows = [[(c or "").strip() for c in row] for row in t.extract()]
                    rows = [r for r in rows if any(r)]
                    if rows:
                        tables.append(rows)
            except Exception:
                pass
            pages.append({"page": i, "lines": lines, "tables": tables})
    return pages


def _parse_pypdf(path: str) -> list[dict]:
    from pypdf import PdfReader
    pages = []
    for i, p in enumerate(PdfReader(path).pages, 1):
        text = p.extract_text() or ""
        lines = [{"text": ln.strip(), "size": 0.0, "bold": False}
                 for ln in text.splitlines() if ln.strip()]
        pages.append({"page": i, "lines": lines, "tables": []})
    return pages


# --------------------------------------------------------------- segment
def _norm_header(s: str) -> str:
    return re.sub(r"[\d|]+", "", s).strip().lower()


def find_headers(pages: list[dict]) -> tuple[set[str], str | None]:
    """Lines that repeat on most pages (page counter ignored) + the revision date."""
    n = max(len(pages), 1)
    counts: dict[str, int] = {}
    for p in pages:
        seen = set()
        for ln in p["lines"]:
            k = _norm_header(ln["text"])
            if 3 <= len(k) <= 120 and k not in seen:
                seen.add(k)
                counts[k] = counts.get(k, 0) + 1
    headers = {k for k, c in counts.items() if c >= max(2, int(0.5 * n))}
    revision = None
    for p in pages[:3]:
        for ln in p["lines"]:
            if _HEADER_RE.match(ln["text"]) or _norm_header(ln["text"]) in headers:
                m = _DATE_RE.search(ln["text"])
                if m:
                    revision = m.group(1)
                    break
        if revision:
            break
    return headers, revision


def _is_heading(ln: dict, body_size: float) -> bool:
    t = ln["text"].strip()
    letters = re.sub(r"[^A-Za-z]", "", t)
    if len(letters) < 3 or len(t) > 120 or len(t.split()) > 14:
        return False
    if t.endswith((".", ";", ",")) and not t.endswith("..."):
        return False
    if _REVISION_RE.match(t):
        return False
    all_caps = letters.isupper()
    big = body_size and ln["size"] >= body_size + 1.5
    return all_caps or (big and ln["bold"])


def segment(pages: list[dict], chapter: str, doc_hash: str) -> tuple[list[dict], dict]:
    """Cut a parsed chapter into sections. Returns (sections, meta)."""
    headers, revision = find_headers(pages)
    sizes = [ln["size"] for p in pages for ln in p["lines"] if ln["size"] > 0]
    body_size = statistics.median(sizes) if sizes else 0.0

    revision_dates: list[str] = []
    sections: list[dict] = []
    cur = {"heading": "PREAMBLE", "page_start": 1, "page_end": 1, "lines": [], "tables": []}

    def _close(fallback_page: int):
        text = _join_lines(cur["lines"])
        for tbl in cur["tables"]:
            text += "\n" + "\n".join(" | ".join(r) for r in tbl)
        if text.strip():
            sections.append({"heading": cur["heading"], "page_start": cur["page_start"],
                             "page_end": cur.get("page_end") or fallback_page, "text": text.strip()})

    for p in pages:
        for ln in p["lines"]:
            t = ln["text"].strip()
            if _HEADER_RE.match(t) or _norm_header(t) in headers:
                continue
            m = _REVISION_RE.match(t)
            if m:
                revision_dates += _DATE_RE.findall(m.group(1))
                continue
            if _is_heading(ln, body_size):
                _close(p["page"])
                cur = {"heading": t, "page_start": p["page"], "page_end": p["page"],
                       "lines": [], "tables": []}
                continue
            cur["lines"].append(t)
            cur["page_end"] = p["page"]              # last page that contributed text
        if p.get("tables"):
            cur["tables"].extend(p["tables"])
            cur["page_end"] = p["page"]
    _close(pages[-1]["page"] if pages else 1)

    if not revision and revision_dates:
        revision = revision_dates[0]
    for s in sections:
        sig = rule_signal(s["text"])
        s.update({
            "chapter": chapter, "doc_hash": doc_hash,
            "section_id": hashlib.sha256(f"{chapter}|{doc_hash}|{s['heading']}|{s['page_start']}".encode()).hexdigest()[:16],
            "text_hash": hashlib.sha256(s["text"].encode()).hexdigest()[:16],
            "n_chars": len(s["text"]),
            "rule_signal": sig["score"], "codes_seen": sig["codes"],
            "has_rule_signal": sig["score"] > 0,
        })
    meta = {"pages": len(pages), "revision_date": revision, "revision_dates": revision_dates,
            "headers_stripped": sorted(headers), "sections": len(sections),
            "candidate_sections": sum(1 for s in sections if s["has_rule_signal"])}
    return sections, meta


def _join_lines(lines: list[str]) -> str:
    """Physical PDF lines -> paragraphs. Keeps bullets on their own line."""
    out, buf = [], []
    for t in lines:
        if re.match(r"^[•\-•\*]\s", t) or re.match(r"^\(?[a-z0-9]{1,2}[\.\)]\s", t):
            if buf:
                out.append(" ".join(buf)); buf = []
            out.append(t)
            continue
        buf.append(t)
        if t.endswith((".", ":", ";")):
            out.append(" ".join(buf)); buf = []
    if buf:
        out.append(" ".join(buf))
    return "\n".join(re.sub(r"\s+", " ", x).strip() for x in out)


# ------------------------------------------------------------ pre-filter
def rule_signal(text: str) -> dict:
    """Cheap, deterministic 'does this look like a billing rule?' score."""
    codes = sorted(set(CODE_RE.findall(text)))
    ranges = CODE_RANGE_RE.findall(text)
    low = text.lower()
    phrases = [p for p in TRIGGER_PHRASES if p in low]
    score = len(codes) + 2 * len(ranges) + len(phrases) + (1 if MODIFIER_RE.search(text) else 0)
    return {"score": score, "codes": codes, "ranges": ranges, "phrases": phrases}


def inventory_record(acq: dict, meta: dict) -> dict:
    return {**{k: acq[k] for k in ("chapter", "title", "url", "path", "doc_hash", "bytes", "source", "fetched_at")},
            "pages": meta["pages"], "revision_date": meta["revision_date"],
            "revision_history": "; ".join(meta["revision_dates"]),
            "sections": meta["sections"], "candidate_sections": meta["candidate_sections"]}
