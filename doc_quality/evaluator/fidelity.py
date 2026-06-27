"""
Fidelity dimension: verifies that each piece of structured content is grounded in the preprocessed source text the LLM actually saw. Deterministic; no LLM.

Unlike the other evaluators, this one compares against the source text rather than code truth, so it has its own signature: evaluate(view, source_text, cfg).
Comparison happens in placeholder space (preprocessed text + structured JSON both contain url_placeholder_X), so URLs never cause spurious mismatches.
"""

from __future__ import annotations


import re
import os
import difflib
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple, Iterator

try:
    from rapidfuzz import fuzz as _rf_fuzz
except ImportError:                       # pragma: no cover
    _rf_fuzz = None

from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import DocView
from doc_quality.issue_types import IssueType
from doc_quality.models import Dimension, DimensionScore, Issue, MaintainerStrategy, Severity
from doc_quality.presence import is_present



_WS = re.compile(r"\s+")
_DEHYPHEN = re.compile(r"(\w)-\s*\n\s*(\w)")          # "De-\nfault" -> "Default"
_PDF_MARKERS = re.compile(r"\(continues on next page\)|\(continued from previous page\)|˓→")
# Treat [url_placeholder_N] and (url_placeholder_N) as identical. Only matches brackets wrapping just a placeholder, so real type brackets like "Iterator[bytes(url_placeholder_2)]" are left intact
_PLACEHOLDER_BRACKET = re.compile(r"[\(\[]\s*(url_placeholder_\d+)\s*[\)\]]")

_BLOCK_KEYWORDS = r"(?:async\s+|function\s+|method\s+|class\s+|exception\s+|property\s+|classmethod\s+|staticmethod\s+)*"
_DEF_LINE = re.compile(rf"^{_BLOCK_KEYWORDS}(?:[\w.]+\.)?[A-Za-z_]\w*\s*[\(:\[]")


@dataclass
class _SourceUnit:
    text: str        # raw source span (for reporting)
    norm: str        # prose-canonical (for matching)
    line_start: int
    line_end: int
    char_start: int
    char_end: int
    section: str     # nearest preceding source heading


# Headings seen in preprocessed RST/Sphinx text; used for source-section labels and to skip the heading lines themselves.
_SRC_HEADINGS = frozenset({
    "parameters", "returns", "return type", "yields", "raises", "warns",
    "examples", "example", "notes", "note", "see also", "warnings",
    "warning", "attributes", "methods", "references"
})
_RST_UNDERLINE = re.compile(r"^[=\-~^\"'#*+.`]{3,}$")
_BOILERPLATE_SUBSTR = (
    "created using sphinx", "copyright", "toggle", "on this page",
    "edit on github", "back to top", "previous", "next",
    # web chrome (e.g. PyTorch docs nav / footer)
    "pytorch libraries", "show source", "pytorch on xla devices",
    "access comprehensive developer documentation", "view tutorials",
    "get in-depth tutorials", "find development resources",
)


_SEG_SEP = r"[\s:=,;.\-\u2013\u2014()\[\]|/]*"   # tolerant inter-segment separator


def _segments_present(segments: List[str], source_norm: str) -> Tuple[float, str]:
    """Match an ORDERED concatenation of present segments, tolerant of whatever separator the raw doc used between them. Absent / 'N/A' segments are dropped."""
    segs = [c for c in (_canonicalize(s) for s in segments if is_present(s)) if c]
    if not segs:
        return 1.0, ""
    m = re.search(_SEG_SEP.join(re.escape(s) for s in segs), source_norm)   # Tier A: contiguous
    if m:
        return 1.0, m.group(0)
    return _best_ratio(" ".join(segs), source_norm)                          # Tier B: fuzzy


def _grounding_checks(view: DocView):
    """(label, json_path, segments, is_code). Only each parameter's name+type is united; purpose, additional_information items, returns subfields, and everything else are checked individually."""
    checks = []
    checks.append(("module_member_signature", "$.module_member_signature",
                   [view.get_signature()], True))

    # purpose and each additional_information item checked independently
    checks.append(("module_member_description.purpose",
                   "$.module_member_description.purpose",
                   [view.get_purpose()], False))
    for i, info in enumerate(view.get_purpose_additional_info()):
        checks.append((f"module_member_description.additional_information[{i}]",
                       f"$.module_member_description.additional_information[{i}]",
                       [info], False))

    # parameters: unite name and type/annotation; check description and additional_information independently
    for p in view.get_parameters():
        nm = p.get("name", "?")
        base = f"$.parameters[?name=='{nm}']"
        checks.append((f"parameters[{nm}].name_type", base,
                       [p.get("name"), p.get("type")], False))
        checks.append((f"parameters[{nm}].description", f"{base}.description",
                       [p.get("description")], False))
        checks.append((f"parameters[{nm}].additional_information",
                       f"{base}.additional_information",
                       [p.get("additional_information")], False))

    # returns: type, description, and additional_information checked independently
    ret = view.get_returns() or {}
    if ret:
        checks.append(("returns.type", "$.returns.type",
                       [ret.get("type")], False))
        checks.append(("returns.description", "$.returns.description",
                       [ret.get("description")], False))
        checks.append(("returns.additional_information",
                       "$.returns.additional_information",
                       [ret.get("additional_information")], False))

    # INDIVIDUAL: attributes
    for a in view.get_attributes():
        ident = a.get("identifier", "?")
        base = f"$.attributes[?identifier=='{ident}']"
        checks.append((f"attributes[{ident}].description", f"{base}.description",
                       [a.get("description")], False))
        checks.append((f"attributes[{ident}].additional_information",
                       f"{base}.additional_information", [a.get("additional_information")], False))

    # INDIVIDUAL: methods
    for mth in view.get_methods():
        mn = mth.get("name", "?")
        base = f"$.methods[?name=='{mn}']"
        checks.append((f"methods[{mn}].description", f"{base}.description",
                       [mth.get("description")], False))
        checks.append((f"methods[{mn}].additional_information",
                       f"{base}.additional_information", [mth.get("additional_information")], False))

    # INDIVIDUAL: examples (code) + their additional_information (prose)
    for i, ex in enumerate(view.get_examples()):
        checks.append((f"examples[{i}].example", f"$.examples[{i}].example",
                       [ex.get("example")], True))
        checks.append((f"examples[{i}].additional_information",
                       f"$.examples[{i}].additional_information",
                       [ex.get("additional_information")], False))

    # INDIVIDUAL: additional_notes items
    notes = view.get_additional_notes() or {}
    for i, item in enumerate(notes.get("supplementary_information", []) or []):
        checks.append((f"additional_notes.supplementary_information[{i}]",
                       f"$.additional_notes.supplementary_information[{i}]", [item], False))
    for i, item in enumerate(notes.get("edge_cases", []) or []):
        checks.append((f"additional_notes.edge_cases[{i}]",
                       f"$.additional_notes.edge_cases[{i}]", [item], False))
    return checks


def _is_boilerplate(raw: str) -> bool:
    low = raw.strip().lower()
    if not low:
        return True
    key = low.rstrip(":").strip()        # "return type:" -> "return type"
    if key in _SRC_HEADINGS or _RST_UNDERLINE.match(low):
        return True
    return any(s in low for s in _BOILERPLATE_SUBSTR)


def _segment_source(text: str) -> List[_SourceUnit]:
    """
    Split source into sentence-ish units while tracking line/char offsets.
    
    Offsets index into the raw source_text so reports can point a human straight at the omitted span; line numbers are 1-based.
    """
    units: List[_SourceUnit] = []
    section = "(top)"
    buf: List[Tuple[int, str]] = []     # (lineno, original line incl. newline)
    buf_start = None                    # char offset of block start
    offset = 0
    
    def flush():
        nonlocal buf, buf_start
        if not buf:
            return
        block_raw = "".join(orig for _, orig in buf)
        joined = _DEHYPHEN.sub(r"\1\2", block_raw).replace("\n", " ")
        l0, l1, cursor = buf[0][0], buf[-1][0], 0
        for sm in re.finditer(r".+?(?:[.;:!?](?=\s|$)|$)", joined):
            seg = sm.group().strip()
            if not seg:
                continue
            anchor = seg.split()[0]
            idx = block_raw.find(anchor, cursor)        # approx char offset; line range is exact
            cs = buf_start + (idx if idx >= 0 else 0)
            units.append(_SourceUnit(seg, _canonicalize(seg), l0, l1, cs, cs + len(seg), section))
            cursor = idx + 1 if idx >= 0 else cursor
        buf, buf_start = [], None
    
    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        raw = line.rstrip("\r\n"); low = raw.strip().lower()
        if not low:
            flush()
        elif low in _SRC_HEADINGS:
            flush(); section = raw.strip()
        elif _RST_UNDERLINE.match(low):
            pass
        else:
            if buf_start is None:
                buf_start = offset
            buf.append((lineno, line))
        offset += len(line)
    flush()
    return units


def _is_def_line(stripped: str) -> bool:
    """A column-0 line that introduces another member (vs. a heading/prose)."""
    if stripped.lower().rstrip(":") in _SRC_HEADINGS:
        return False
    return bool(_DEF_LINE.match(stripped))


def _member_block(source_text: str, signature: str, max_lines: int):
    """(start_char, end_char) of the member's own block. Tolerates leading keywords (property/class/def), a dotted qualifier (requests.get), and ends at the next *definition-like* column-0 line (headings/prose don't end it)."""
    if not is_present(signature):
        return None
    m = re.match(rf"\s*{_BLOCK_KEYWORDS}([A-Za-z_][\w.]*)", signature)
    if not m:
        return None
    head = m.group(1).split(".")[-1]
    head_pat = re.compile(rf"^{_BLOCK_KEYWORDS}(?:[\w.]+\.)?{re.escape(head)}\b")
    start_off = end_off = None
    off = 0
    for i, line in enumerate(source_text.splitlines(keepends=True)):
        stripped = line.strip()
        at_col0 = bool(stripped) and line[:1] not in (" ", "\t")
        if start_off is None:
            if i >= max_lines:
                return None
            if at_col0 and head_pat.match(stripped):
                start_off = off
        elif at_col0 and _is_def_line(stripped):     # next sibling def ends the block
            end_off = off
            break
        off += len(line)
    if start_off is None:
        return None
    return start_off, (end_off if end_off is not None else len(source_text))


def _check_omissions(source_units: List[_SourceUnit], structured_all_norm: str, config: EvaluatorConfig, source_name: str) -> Tuple[List[Issue], float, int]:
    """Check for source content that is not represented in the structured doc."""
    
    allowed = {s.lower().rstrip(":") for s in config.fidelity_omission_sections}
    issues: List[Issue] = []
    covered = checked = 0
    for u in source_units:
        if u.section.lower().rstrip(":") not in allowed:      # section scoping
            continue
        
        if len(u.norm) < config.fidelity_min_unit_chars or _is_boilerplate(u.text):
            continue
        checked += 1
        
        score, span = _best_ratio(u.norm, structured_all_norm)
        if score >= config.fidelity_omission_exact_min:        # omission-specific bar
            covered += 1
            continue
        
        category = ("partially_omitted" if score >= config.fidelity_omission_partial_min else "omitted")
        issues.append(Issue(
            issue_type=IssueType.FID_SOURCE_OMITTED,
            dimension=Dimension.FIDELITY,
            severity=(Severity.LOW if category == "partially_omitted" else Severity.MEDIUM),
            section=f"source:{u.section}",
            target=None,
            json_path="",                       # absent from the doc; no doc-side path
            detail=(f"Source content at {source_name}:{u.line_start} not represented in the structured doc (best match {score:.2f})."),
            doc_value=(span.strip()[:300] or None),   # closest thing the doc did say
            code_value=u.text.strip()[:300],          # source = ground-truth side
            metadata={
                "category": category,
                "similarity": round(score, 3),
                "omitted_text": u.text.strip(),
                "source_file": source_name,
                "source_section": u.section,
                "source_line_start": u.line_start,
                "source_line_end": u.line_end,
                "source_char_start": u.char_start,
                "source_char_end": u.char_end
            },
            maintainer_strategy=MaintainerStrategy.MANUAL
        ))
    coverage = (covered / checked) if checked else 1.0
    return issues, coverage, checked


def _canonicalize(text: str, *, code: bool = False) -> str:
    """Normalize both sides identically before comparison."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _PDF_MARKERS.sub(" ", t)
    t = _DEHYPHEN.sub(r"\1\2", t)
    t = _PLACEHOLDER_BRACKET.sub(r"(\1)", t)   # unify placeholder bracket style
    # remove control/DEL corruption chars
    t = "".join(c for c in t if not ((ord(c) < 0x20 and c not in "\t\n\r") or ord(c) == 0x7f))
    t = (t.replace("\u2018", "'").replace("\u2019", "'")
           .replace("\u201c", '"').replace("\u201d", '"')
           .replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " "))
    t = _WS.sub(" ", t).strip()
    return t if code else t.lower()


def _iter_all_strings(obj) -> "Iterator[str]":
    """Yield every string leaf in a nested api_reference (dict/list/str)."""
    if isinstance(obj, str):
        if obj:
            yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_all_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_all_strings(v)


def _best_ratio(unit: str, source_norm: str) -> Tuple[float, str]:
    """Best local similarity of `unit` against any span of `source_norm`, plus that span."""
    if not unit:
        return 1.0, ""
    if unit in source_norm:                                   # Tier A: exact normalized substring
        return 1.0, unit
    
    if _rf_fuzz is not None:                                  # Tier B: fast partial alignment
        al = _rf_fuzz.partial_ratio_alignment(unit, source_norm, score_cutoff=0)
        if al is None:
            return 0.0, ""
        return al.score / 100.0, source_norm[al.dest_start:al.dest_end]
    
    sm = difflib.SequenceMatcher(None, unit, source_norm, autojunk=False)  # Tier B fallback
    longest = max(sm.get_matching_blocks(), key=lambda b: b.size)
    if longest.size == 0:
        return 0.0, ""
    start = max(0, longest.b - longest.a)
    span = source_norm[start:start + len(unit)]
    return difflib.SequenceMatcher(None, unit, span, autojunk=False).ratio(), span


def _is_empty_extraction(view: DocView) -> bool:
    """True only if EVERY field (including the signature) is missing or 'N/A'."""
    return not any(is_present(s) for s in _iter_all_strings(view.raw))

def evaluate(view: DocView, source_text: str, config: EvaluatorConfig, *, source_path: Optional[str] = None) -> DimensionScore:
    """Evaluate the fidelity of the structured doc against the source text."""
    if not source_text or not source_text.strip():
        return DimensionScore(score=0.0, issues=[], metric_breakdown={"source_missing": 1.0})
    
    if config.fidelity_check_empty_extraction and _is_empty_extraction(view):
        return DimensionScore(
            score=0.0,
            issues=[Issue(
                issue_type=IssueType.FID_EMPTY_EXTRACTION,
                dimension=Dimension.FIDELITY,
                severity=IssueType.FID_EMPTY_EXTRACTION.value.default_severity,
                section="document",
                target=None,
                json_path="$",
                detail="Structured extraction is empty: every field (including the signature) is missing or 'N/A' (failed extraction).",
                doc_value=None,
                code_value=None,
                metadata={"category": "empty_extraction"},
                maintainer_strategy=MaintainerStrategy.MANUAL
            )],
            metric_breakdown={"empty_extraction": 1.0}
        )
    
    source_prose = _canonicalize(source_text)
    source_code = _canonicalize(source_text, code=True)
    source_name = os.path.basename(source_path) if source_path else "(source)"

    issues: List[Issue] = []
    grounded_units = 0
    total_units = 0
    
    for label, jp, segments, is_code in _grounding_checks(view):
        segs = [s for s in segments if is_present(s)]    # drop None / "" / "N/A" before matching
        if not segs:
            continue
        joined = " ".join(s.strip() for s in segs)
        norm = _canonicalize(joined, code=is_code)
        if len(norm) < config.fidelity_min_unit_chars:
            continue
        
        total_units += 1
        haystack = source_code if is_code else source_prose
        score, span = (_best_ratio(norm, haystack) if is_code else _segments_present(segs, haystack))
        
        if score >= config.fidelity_exact_min:
            grounded_units += 1
            continue
        itype = (IssueType.FID_PARTIAL_SUPPORT if score >= config.fidelity_partial_min else IssueType.FID_UNSUPPORTED_CONTENT)
        issues.append(Issue(
            issue_type=itype,
            dimension=Dimension.FIDELITY,
            severity=itype.value.default_severity,
            section=label,
            target=None,
            json_path=jp,
            detail=f"'{label}' content has similarity {score:.2f} to its closest source span.",
            doc_value=joined[:300],
            code_value=span.strip()[:300],
            metadata={"similarity": round(score, 3),
                      "category": ("partial_support" if score >= config.fidelity_partial_min else "unsupported_content")},
            maintainer_strategy=MaintainerStrategy.MANUAL
        ))

    score = (grounded_units / total_units) if total_units else 1.0
    
    breakdown = {"grounded_unit_ratio": score, "units_checked": float(total_units)}
    if config.fidelity_check_omissions:
        span = _member_block(source_text, view.get_signature(), config.fidelity_signature_search_lines)
        if span is None:
            breakdown["omission_skipped"] = 1.0
        else:
            structured_all_norm = _canonicalize(" \n ".join(s for s in _iter_all_strings(view.raw) if is_present(s)))
            s0, s1 = span
            units = [u for u in _segment_source(source_text) if s0 <= u.char_start < s1]
            omit_issues, coverage, src_checked = _check_omissions(units, structured_all_norm, config, source_name)
            issues.extend(omit_issues)
            breakdown["omission_skipped"] = 0.0
            breakdown["source_coverage_ratio"] = coverage
            breakdown["source_units_checked"] = float(src_checked)
            breakdown["omitted_units"] = float(len(omit_issues))
    
    return DimensionScore(score=score, issues=issues, metric_breakdown=breakdown)


