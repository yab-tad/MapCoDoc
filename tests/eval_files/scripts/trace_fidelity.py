"""
Trace-unit-aligned, section-aware fidelity analysis.

Decomposes structured-doc fidelity into THREE sub-constructs, each scoped to the
trace units that are genuinely shared between source code and the API-reference
documentation (signature, parameters, returns, purpose/docstring, examples) -- not
arbitrary prose. This avoids the false-positive "omissions" that broad prose
matching produces (see-also / notes / nav chrome), which is why omission detection
was disabled in the production checker.

  F1  Faithfulness (precision)  : each structured trace unit is grounded in the
                                  member's own source block.
  F2  Categorization (placement): a structured trace unit that IS grounded in the
                                  block is grounded in its EXPECTED source section
                                  (not lifted from a different field).
  F3  Completeness (recall)     : each source-side trace unit (signature param
                                  names, a documented Returns/Parameters/summary/
                                  examples section) is represented in the structured
                                  doc.

Design choices (deliberate, for defensibility):
  * The parameter trace unit is anchored on the SOURCE SIGNATURE's parameter names
    -- the true code<->doc trace link -- not on a brittle prose-grammar parse, since
    the prose Parameters grammar varies wildly across libraries/sources
    (numpy "name : type", torch "- name (type) -", pdf "name\\n[type] desc").
  * Section detection is heading-based and colon-tolerant (handles "Parameters:").
  * Everything is reported as graded similarity + counts so bins can be chosen from
    the distributions; nothing is hard pass/fail here.

Reuses the validated normalization/alignment primitives from the production checker
(doc_quality.evaluator.fidelity) so scores are comparable.

Outputs:
  tests/trace_fidelity_metrics.csv : one row per (member x source) with the three
                                     construct scores + trace-link adjudication labels.
  tests/trace_fidelity_units.csv   : one row per trace unit (for triage / case studies).

Usage:
  python tests/trace_fidelity.py                 # full sweep + distributions
  python tests/trace_fidelity.py --debug numpy web numpy.append   # dump one member
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from doc_quality.config import EvaluatorConfig
from doc_quality.doc_views import doc_view
from doc_quality.evaluator.fidelity import (
    _canonicalize, _best_ratio, _member_block, _is_boilerplate, _segments_present,
)
from doc_quality.presence import is_present

TESTS = os.path.dirname(os.path.abspath(__file__))
ARTIFACTS = os.path.join(PROJECT_ROOT, "doc_processor", "doc_artifacts")

LIB_VERSIONS = {
    "numpy": "2.4.0", "pandas": "2.3.3", "requests": "2.32.5", "sklearn": "1.8.0",
    "sqlalchemy": "2.0.45", "torch": "2.9.1", "xgboost": "3.2.0",
}
SOURCES = ["web", "pdf"]

CFG = EvaluatorConfig()
EXACT_MIN = CFG.fidelity_exact_min       # 0.995  grounded
PLACE_MIN = 0.75                          # section-match bar for "well placed"
RECALL_MIN = 0.90                         # source unit considered represented

# heading sets (lowercased, colon-stripped)
PARAM_SECS = {"parameters", "arguments", "other parameters", "keyword arguments", "args"}
RETURN_SECS = {"returns", "return", "return type", "yields", "yield"}
EXAMPLE_SECS = {"examples", "example"}
# Only STRUCTURAL field headings break a section. Admonitions (note/warning/see
# also/references) are NOT breaks: they appear embedded inside a Parameters block
# (e.g. xgboost) and would otherwise fragment it, producing false "misplacement".
STRUCT_HEADINGS = PARAM_SECS | RETURN_SECS | EXAMPLE_SECS | {"raises", "attributes", "methods"}


# Interactive/code markers. Primary signal: REPL prompts (doctest ">>>"/"...",
# IPython "In [n]:", shell "$ "). Secondary: a fenced ``` block whose body carries a
# genuine Python STATEMENT, which catches plain fenced examples that lack a REPL prompt
# (e.g. torch.func.stack_module_state). The statement test is deliberately strict so it
# does NOT fire on fenced non-code that PDF wraps in fences: math like "L(l1) = l2",
# Note/Warning/See-also admonitions, or bare signature snippets like "hook(opt) -> None".
_REPL_RE = re.compile(r">>>|^\s*\.\.\.\s|In \[\d+\]:|^\s*\$ ", re.M)
_CODE_LINE_RE = re.compile(
    r"^\s*(?:import\s+\w|from\s+[\w.]+\s+import\b|def\s+\w+\s*\(|class\s+\w+\b|@[\w.]+|"
    r"return\s+\S|with\s+\S+\s+as\s|for\s+\w+\s+in\s+\S|while\s+\S|print\s*\(|"
    r"[A-Za-z_][\w.]*\s*=\s*[^=\s]|"                # assignment (not ==, not bare math RHS)
    r"[A-Za-z_][\w.]*\.[A-Za-z_]\w*\s*\([^)]*\)\s*$)",  # a method call statement
    re.M)


def has_repl_code(text) -> bool:
    if not text:
        return False
    if _REPL_RE.search(text):
        return True
    return "```" in text and _CODE_LINE_RE.search(text) is not None


def _mmd_additional_info(view):
    """module_member_description.additional_information, read schema-agnostically
    (the callable view hides it, but some callable docs use the object shape)."""
    mmd = view.raw.get("module_member_description")
    if isinstance(mmd, dict):
        return mmd.get("additional_information") or []
    return view.get_purpose_additional_info()


def iter_struct_prose(view):
    """(field_id, text) for every PROSE field that should not normally hold a code
    block. Used to detect usage examples misplaced into description/notes fields."""
    out = []
    if is_present(view.get_purpose()):
        out.append(("module_member_description.purpose", view.get_purpose()))
    for i, info in enumerate(_mmd_additional_info(view)):
        if is_present(info):
            out.append((f"module_member_description.additional_information[{i}]", info))
    for prm in view.get_parameters():
        nm = prm.get("name", "?")
        if is_present(prm.get("description")):
            out.append((f"parameters[{nm}].description", prm.get("description")))
    ret = view.get_returns() or {}
    if is_present(ret.get("description")):
        out.append(("returns.description", ret.get("description")))
    for a in view.get_attributes():
        if is_present(a.get("description")):
            out.append((f"attributes[{a.get('identifier','?')}].description", a.get("description")))
    for m in view.get_methods():
        if is_present(m.get("description")):
            out.append((f"methods[{m.get('name','?')}].description", m.get("description")))
    notes = view.get_additional_notes() or {}
    for i, it in enumerate(notes.get("supplementary_information", []) or []):
        if is_present(it):
            out.append((f"additional_notes.supplementary_information[{i}]", it))
    for i, it in enumerate(notes.get("edge_cases", []) or []):
        if is_present(it):
            out.append((f"additional_notes.edge_cases[{i}]", it))
    for i, ex in enumerate(view.get_examples()):
        if is_present(ex.get("additional_information")):
            out.append((f"examples[{i}].additional_information", ex.get("additional_information")))
    return out


# --------------------------------------------------------------------------- #
# Source-side parsing
# --------------------------------------------------------------------------- #
def short_name(api_name: str) -> str:
    base = re.split(r"-(?:class|function|method)$", api_name)[0]
    return base.split(".")[-1]


def split_top_level(arglist: str):
    """Split a signature arg list on top-level commas (respect [] () {})."""
    out, depth, cur = [], 0, []
    for ch in arglist:
        if ch in "([{":
            depth += 1; cur.append(ch)
        elif ch in ")]}":
            depth -= 1; cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def param_names_from_signature(sig: str):
    """Extract declared parameter names from a signature string."""
    if not sig:
        return []
    i = sig.find("(")
    if i < 0:
        return []
    depth, j = 0, None
    for k in range(i, len(sig)):
        if sig[k] == "(":
            depth += 1
        elif sig[k] == ")":
            depth -= 1
            if depth == 0:
                j = k
                break
    if j is None:
        return []
    inner = sig[i + 1:j]
    names = []
    for tok in split_top_level(inner):
        t = tok.strip()
        if not t or t in ("*", "/", "**"):
            continue
        t = re.split(r"[=:]", t, maxsplit=1)[0].strip()
        t = t.lstrip("*").strip()
        if (t and t not in ("self", "cls") and re.match(r"^[A-Za-z_]\w*$", t)
                and not t.startswith("url_placeholder")):
            names.append(t)
    return names


def find_source_signature(block_text: str, name: str):
    """First line in the member block that looks like the member's signature."""
    for line in block_text.splitlines():
        s = line.strip()
        if not s:
            continue
        # def-like line that mentions the short name and opens a paren/colon
        if re.search(rf"(?:^|[.\s]){re.escape(name)}\b", s) and ("(" in s or s.endswith(":")):
            return s
    # fallback: first non-empty line
    for line in block_text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def sectionize(block_text: str):
    """Return {section_key: canonical_text}. section_key in
    {'top','parameters','returns','examples','other'}; 'top' is everything before
    the first recognized heading (minus the signature line)."""
    buckets = defaultdict(list)
    cur = "top"
    for line in block_text.splitlines():
        stripped = line.strip()
        low = stripped.lower().rstrip(":")
        if low in STRUCT_HEADINGS:
            if low in PARAM_SECS:
                cur = "parameters"
            elif low in RETURN_SECS:
                cur = "returns"
            elif low in EXAMPLE_SECS:
                cur = "examples"
            else:
                cur = "other"
            continue
        buckets[cur].append(line)
    return {k: " ".join(v) for k, v in buckets.items()}   # raw text per section


def section_documented(raw_lines):
    """Does a section carry real (non-boilerplate, non-trivial) content?"""
    txt = " ".join(l for l in raw_lines if not _is_boilerplate(l)).strip()
    return len(_canonicalize(txt)) >= CFG.fidelity_min_unit_chars


# --------------------------------------------------------------------------- #
# Per-member evaluation
# --------------------------------------------------------------------------- #
def grade_text(unit_text, haystack, section_norm, is_code=False):
    """Return (global_score, section_score) for a plain-text unit."""
    norm = _canonicalize(unit_text, code=is_code)
    if len(norm) < CFG.fidelity_min_unit_chars:
        return None, None
    g, _ = _best_ratio(norm, haystack)
    s, _ = _best_ratio(norm, section_norm) if section_norm else (0.0, "")
    return g, s


def grade_segments(segs, haystack, section_norm):
    """Return (global_score, section_score) for an ordered name+type unit
    (separator-tolerant, mirroring the production checker)."""
    norm = _canonicalize(" ".join(s.strip() for s in segs if is_present(s)))
    if len(norm) < CFG.fidelity_min_unit_chars:
        return None, None
    g, _ = _segments_present(segs, haystack)
    s, _ = _segments_present(segs, section_norm) if section_norm else (0.0, "")
    return g, s


def evaluate_member(api_name, member_type, source_text, api_ref):
    view = doc_view(api_ref, member_type)
    name = short_name(api_name)
    struct_sig = view.get_signature() or f"{name}()"

    # Each preprocessed file is a single member's page, so faithfulness matches
    # against the WHOLE file (as the production precision check does). _member_block
    # is unreliable on numpydoc ("name : type" lines look like sibling defs), so it
    # is used only as an informational scope flag.
    span = _member_block(source_text, struct_sig, CFG.fidelity_signature_search_lines)
    block_scoped = span is not None
    block_text = source_text

    block_norm = _canonicalize(block_text)
    block_code = _canonicalize(block_text, code=True)
    secs = sectionize(block_text)
    src_sig_line = find_source_signature(block_text, name)
    src_params = param_names_from_signature(src_sig_line)

    units = []   # (unit_id, kind, global_score, section_score, is_code, expected_sec)

    def add_text(uid, kind, text, expected_sec, is_code=False):
        sec_norm = _canonicalize(secs.get(expected_sec, ""), code=is_code)
        g, s = grade_text(text, block_code if is_code else block_norm, sec_norm, is_code)
        if g is not None:
            units.append((uid, kind, g, s, is_code, expected_sec))

    def add_segs(uid, kind, segs, expected_sec):
        g, s = grade_segments(segs, block_norm, _canonicalize(secs.get(expected_sec, "")))
        if g is not None:
            units.append((uid, kind, g, s, False, expected_sec))

    # F1/F2 structured trace units -> expected source section
    if is_present(struct_sig):
        add_text("signature", "signature", struct_sig, "top", is_code=True)
    if is_present(view.get_purpose()):
        add_text("purpose", "purpose", view.get_purpose(), "top")
    for p in view.get_parameters():
        nm = p.get("name", "?")
        add_segs(f"param[{nm}].name_type", "param_name_type",
                 [p.get("name"), p.get("type")], "parameters")
        if is_present(p.get("description")):
            add_text(f"param[{nm}].description", "param_desc", p.get("description"), "parameters")
    ret = view.get_returns() or {}
    if is_present(ret.get("type")):
        add_text("returns.type", "return_type", ret.get("type"), "returns")
    if is_present(ret.get("description")):
        add_text("returns.description", "return_desc", ret.get("description"), "returns")
    for i, ex in enumerate(view.get_examples()):
        if is_present(ex.get("example")):
            add_text(f"examples[{i}]", "example", ex.get("example"), "examples", is_code=True)

    # ---- F1 faithfulness (precision) ----
    gradable = [u for u in units]
    faithful = [u for u in gradable if u[2] >= EXACT_MIN]
    f1 = len(faithful) / len(gradable) if gradable else None

    # ---- F2 categorization (placement) : among grounded units whose expected
    #      section actually has content, is the unit grounded there too? Examples
    #      are EXCLUDED: code legitimately lives outside an "Examples" heading in
    #      the source (often inside additional_information), so source location
    #      does not indicate a structuring error. ----
    secs_norm = {k: _canonicalize(v) for k, v in secs.items()}
    PLACEABLE = ("top", "parameters", "returns")
    placement_judgeable = [u for u in faithful
                           if u[5] in PLACEABLE
                           and len(secs_norm.get(u[5], "")) >= CFG.fidelity_min_unit_chars]
    well_placed = [u for u in placement_judgeable if u[3] >= PLACE_MIN]
    misplaced = [u for u in placement_judgeable if u[3] < PLACE_MIN]

    # code-in-prose: usage-example code that landed in a description/notes field.
    code_in_prose = [fid for fid, txt in iter_struct_prose(view) if has_repl_code(txt)]

    # Example categorization, collapsed to ONE judged unit per member (the pipeline is
    # prompted to split examples into individual `example` fields, but for analysis we
    # treat the whole `examples` SEGMENT as the target). When the source carries example
    # code: OK if any of it reached the examples segment; DEFECT if it only reached prose
    # fields; if it reached neither, that is a recall miss (handled in F3), not an F2
    # categorization defect.
    src_has_examples = has_repl_code(block_text)
    example_ok = src_has_examples and len(view.get_examples()) > 0
    example_defect = src_has_examples and not example_ok and bool(code_in_prose)
    example_judged = example_ok or example_defect

    f2_judged = len(placement_judgeable) + (1 if example_judged else 0)
    f2_ok = len(well_placed) + (1 if example_ok else 0)
    f2 = (f2_ok / f2_judged) if f2_judged else None

    # ---- F3 completeness (recall) over source trace units ----
    recall_items = []   # (unit_id, covered_bool)

    # signature: the source always documents a signature line -> represented?
    if src_sig_line:
        recall_items.append(("src_signature", is_present(view.get_signature())))
    struct_param_names = {_canonicalize(p.get("name") or "") for p in view.get_parameters()}
    # token set from NAME fields only -> tolerant of grouped params documented under
    # a single field, e.g. name == "*args, **kwargs" yields tokens {args, kwargs}.
    struct_name_tokens = set()
    for p in view.get_parameters():
        struct_name_tokens.update(re.findall(r"[A-Za-z_]\w*", _canonicalize(p.get("name") or "")))
    for pn in src_params:
        cn = _canonicalize(pn)
        covered = bool(cn) and (cn in struct_param_names or cn in struct_name_tokens)
        recall_items.append((f"src_param[{pn}]", covered))

    # returns documented in source -> represented in structured?
    if member_type != "class" and len(secs_norm.get("returns", "")) >= CFG.fidelity_min_unit_chars:
        covered = is_present(ret.get("type")) or is_present(ret.get("description"))
        recall_items.append(("src_returns", bool(covered)))

    # purpose/summary documented in source top -> represented?
    if len(secs_norm.get("top", "")) >= CFG.fidelity_min_unit_chars:
        covered = is_present(view.get_purpose())
        recall_items.append(("src_purpose", bool(covered)))

    # examples documented in source -> represented? Detection requires real REPL/code
    # markers (not just an "Examples" heading or a stray fence). "Covered" counts the
    # code appearing ANYWHERE in the structured doc (examples field OR a prose field),
    # so recall is separated from the categorization (code-in-prose) question.
    if has_repl_code(block_text):
        covered = (any(has_repl_code(ex.get("example") or "") for ex in view.get_examples())
                   or bool(code_in_prose)
                   or len(view.get_examples()) > 0)
        recall_items.append(("src_examples", bool(covered)))

    # Headline completeness covers signature + parameters + purpose + returns. Examples
    # are EXCLUDED from recall (unreliable: PDF pages concatenate inherited/sibling
    # members, so example code on a page often belongs to a different member) and are
    # reported via example categorization instead.
    core = [(u, c) for u, c in recall_items if not u.startswith("src_examples")]
    f3 = (sum(1 for _, c in core if c) / len(core)) if core else None
    example_recall = next((c for u, c in recall_items if u == "src_examples"), None)

    return {
        "block_scoped": block_scoped,
        "f1_faithfulness": f1,
        "f2_categorization": f2,
        "f3_completeness": f3,
        "n_units": len(gradable),
        "n_faithful": len(faithful),
        "n_placement_judgeable": len(placement_judgeable),
        "n_misplaced": len(misplaced),
        "n_code_in_prose": len(code_in_prose),
        "code_in_prose": code_in_prose,
        "example_ok": example_ok,
        "example_defect": example_defect,
        "example_judged": example_judged,
        "n_recall_items": len(core),
        "n_recall_covered": sum(1 for _, c in core if c),
        "example_recall": example_recall,
        "src_param_count": len(src_params),
        "units": units,
        "recall_items": recall_items,
        "misplaced_units": misplaced,
        "secs_present": {k: bool(v) for k, v in secs.items()},
    }


def _segments_present_join(segs):
    segs = [s for s in segs if is_present(s)]
    return " ".join(s.strip() for s in segs) if segs else ""


# --------------------------------------------------------------------------- #
# Driving / IO
# --------------------------------------------------------------------------- #
def load_review_labels():
    labels = {}
    import glob
    for f in glob.glob(os.path.join(TESTS, "*_fidelity_review.csv")):
        base = os.path.basename(f)[:-len("_fidelity_review.csv")]
        lib, src = base.rsplit("_", 1)
        for r in csv.DictReader(open(f, encoding="utf-8")):
            labels[(lib, src, r["api_name"])] = r
    return labels


def iter_members(lib, src):
    ver = LIB_VERSIONS[lib]
    sdir = os.path.join(ARTIFACTS, "structured_doc", lib, f"v_{ver}_{src}")
    pdir = os.path.join(ARTIFACTS, "preprocessed_doc", lib, f"v_{ver}_{src}", "doc")
    if not os.path.isdir(sdir):
        return
    # Case-only-differing API names (e.g. `sqlalchemy.orm.Composite` class vs
    # `sqlalchemy.orm.composite` function; `torch.xpu.Stream` vs `torch.xpu.stream`)
    # collide on case-insensitive filesystems, so the extractor disambiguates their
    # artifacts with a `-class`/`-function`/`-method` filename suffix. The census
    # `api_name` carries no such suffix, so we key on the de-suffixed name. When both a
    # stale plain-name file and a type-suffixed file resolve to the same canonical name,
    # prefer the (intentionally disambiguated) suffixed artifact and drop the duplicate.
    seen = {}   # canonical api_name -> (has_suffix, sp, tp)
    for fn in sorted(os.listdir(sdir)):
        if not fn.endswith(".json") or fn.endswith(".raw.json"):
            continue
        stem = fn[:-5]
        canonical = re.split(r"-(?:class|function|method)$", stem, maxsplit=1)[0]
        has_suffix = canonical != stem
        sp = os.path.join(sdir, fn)
        tp = os.path.join(pdir, stem + ".txt")
        if not os.path.exists(tp):
            continue
        if canonical in seen and (seen[canonical][0] or not has_suffix):
            # keep the already-seen entry when it is suffixed, or when this one is a
            # plain-name duplicate of an already-seen name.
            continue
        seen[canonical] = (has_suffix, sp, tp)
    for canonical, (_has_suffix, sp, tp) in seen.items():
        yield canonical, sp, tp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", nargs=3, metavar=("LIB", "SRC", "API"))
    args = ap.parse_args()

    labels = load_review_labels()

    if args.debug:
        lib, src, api = args.debug
        for stem, sp, tp in iter_members(lib, src):
            if stem != api:
                continue
            lab = labels.get((lib, src, stem), {})
            mt = lab.get("member_type", "")
            res = evaluate_member(stem, mt, open(tp, encoding="utf-8").read(),
                                  json.load(open(sp, encoding="utf-8")))
            print(f"== {lib}/{src}/{stem}  type={mt}  correct_doc={lab.get('correct_doc')}")
            print(f"   block_scoped={res['block_scoped']}  sections={res['secs_present']}")
            print(f"   F1 faithfulness={res['f1_faithfulness']}  "
                  f"F2 categorization={res['f2_categorization']}  "
                  f"F3 completeness={res['f3_completeness']}")
            print("   --- units (uid, kind, global, section, expected_sec) ---")
            for uid, kind, g, s, isc, esec in res["units"]:
                print(f"      {uid:32s} {kind:14s} g={g:.3f} sec={s:.3f} -> {esec}")
            print("   --- recall items ---")
            for uid, c in res["recall_items"]:
                print(f"      {uid:32s} covered={c}")
            if res["code_in_prose"]:
                print("   --- code-in-prose (example misplaced into prose field) ---")
                for fid in res["code_in_prose"]:
                    print(f"      {fid}")
            return
        print("member not found")
        return

    metrics_rows, unit_rows, recall_rows = [], [], []
    for lib in LIB_VERSIONS:
        for src in SOURCES:
            for stem, sp, tp in iter_members(lib, src):
                lab = labels.get((lib, src, stem), {})
                mt = lab.get("member_type", "")
                if not mt:
                    continue
                try:
                    api_ref = json.load(open(sp, encoding="utf-8"))
                    source_text = open(tp, encoding="utf-8").read()
                except Exception:
                    continue
                res = evaluate_member(stem, mt, source_text, api_ref)
                metrics_rows.append({
                    "library": lib, "source": src, "api_name": stem, "member_type": mt,
                    "correct_doc": lab.get("correct_doc", ""),
                    "block_scoped": res["block_scoped"],
                    "f1_faithfulness": res["f1_faithfulness"],
                    "f2_categorization": res["f2_categorization"],
                    "f3_completeness": res["f3_completeness"],
                    "n_units": res["n_units"], "n_faithful": res["n_faithful"],
                    "n_placement_judgeable": res["n_placement_judgeable"],
                    "n_misplaced": res["n_misplaced"],
                    "n_code_in_prose": res["n_code_in_prose"],
                    "n_recall_items": res["n_recall_items"],
                    "n_recall_covered": res["n_recall_covered"],
                    "example_recall": res["example_recall"],
                    "src_param_count": res["src_param_count"],
                })
                for uid, kind, g, s, isc, esec in res["units"]:
                    unit_rows.append({
                        "library": lib, "source": src, "api_name": stem,
                        "member_type": mt,
                        "correct_doc": lab.get("correct_doc", ""),
                        "unit_id": uid, "kind": kind, "expected_section": esec,
                        "global_score": round(g, 4), "section_score": round(s, 4),
                        "grounded": g >= EXACT_MIN,
                        "misplaced": (g >= EXACT_MIN and esec in ("top", "parameters", "returns")
                                      and s < PLACE_MIN),
                        "note": "",
                    })
                for uid, cov in res["recall_items"]:
                    recall_rows.append({
                        "library": lib, "source": src, "api_name": stem,
                        "member_type": mt, "correct_doc": lab.get("correct_doc", ""),
                        "recall_type": uid.split("[")[0], "covered": bool(cov),
                    })
                # collapsed example-categorization unit (one per member with source examples)
                if res["example_judged"]:
                    unit_rows.append({
                        "library": lib, "source": src, "api_name": stem,
                        "member_type": mt,
                        "correct_doc": lab.get("correct_doc", ""),
                        "unit_id": "examples_segment", "kind": "example_placement",
                        "expected_section": "examples",
                        "global_score": "", "section_score": "",
                        "grounded": res["example_ok"], "misplaced": res["example_defect"],
                        "note": ("; ".join(res["code_in_prose"]) if res["example_defect"] else ""),
                    })

    mpath = os.path.join(TESTS, "trace_fidelity_metrics.csv")
    upath = os.path.join(TESTS, "trace_fidelity_units.csv")
    rpath = os.path.join(TESTS, "trace_fidelity_recall.csv")
    with open(mpath, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(metrics_rows[0].keys()))
        w.writeheader(); w.writerows(metrics_rows)
    with open(upath, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(unit_rows[0].keys()))
        w.writeheader(); w.writerows(unit_rows)
    with open(rpath, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recall_rows[0].keys()))
        w.writeheader(); w.writerows(recall_rows)

    _report(metrics_rows, unit_rows)
    print(f"\nwrote {mpath}\nwrote {upath}\nwrote {rpath}")


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def _report(metrics_rows, unit_rows):
    print("=" * 80)
    print("TRACE-UNIT FIDELITY  (conditioned on correct trace-link unless noted)")
    print(f"thresholds: grounded>={EXACT_MIN}, placement>={PLACE_MIN}, recall>={RECALL_MIN}")
    print("=" * 80)
    for src in SOURCES:
        rows = [r for r in metrics_rows if r["source"] == src and r["correct_doc"] == "yes"]
        print(f"\n[{src}] trace-link-correct members: {len(rows)}")
        for key, lbl in [("f1_faithfulness", "F1 faithfulness"),
                         ("f2_categorization", "F2 categorization"),
                         ("f3_completeness", "F3 completeness")]:
            vals = [r[key] for r in rows if r[key] is not None]
            perfect = sum(1 for v in vals if v >= 0.9999)
            print(f"   {lbl:18s} n={len(vals):4d}  mean={_mean(vals):.3f}  "
                  f"=1.0: {perfect} ({100*perfect/len(vals):.1f}%)" if vals else f"   {lbl}: n=0")

    print("\n--- per-unit grounding x placement (trace-link-correct) ---")
    for src in SOURCES:
        urows = [u for u in unit_rows if u["source"] == src and u["correct_doc"] == "yes"]
        by_kind = defaultdict(lambda: [0, 0, 0])  # grounded, total, misplaced
        for u in urows:
            k = u["kind"]
            by_kind[k][1] += 1
            if u["grounded"]:
                by_kind[k][0] += 1
            if u["misplaced"]:
                by_kind[k][2] += 1
        print(f"\n[{src}]  kind: grounded/total  misplaced")
        for k in sorted(by_kind):
            g, t, m = by_kind[k]
            print(f"   {k:16s} {g:4d}/{t:<4d} ({100*g/t:5.1f}%)  misplaced={m}")


if __name__ == "__main__":
    main()
