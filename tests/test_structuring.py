"""
Structuring Test Script
=======================
Runs the post-extraction documentation pipeline (Steps 4-6 only):

    Step 4  URL preprocessing       (preprocess_crossRef)
    Step 5  Structured extraction   (LLM / ConcurrentDocExtractor)  -- needs OPENAI_API_KEY
    Step 6  URL postprocessing      (postprocess_crossRef)

Input is the output of tests/collect_member_docs.py:

    <collected-dir>/
        <sample_api_name>/
            web_doc/<file>.txt      (file may be named with an alias API path)
            pdf_doc/<file>.txt

Each member folder's NAME is the authoritative API name (it came from the
sample list and was verified against the DB by the collector). Web and PDF
docs are processed as separate batches with separate artifact trees:

    preprocessed_doc/{lib}/v_{version}_web/...   structured_doc/{lib}/v_{version}_web/...
    preprocessed_doc/{lib}/v_{version}_pdf/...   structured_doc/{lib}/v_{version}_pdf/...

No DB writes occur (Step 7 is skipped).

Usage:
    python tests/test_structuring.py \\
        --db-path mapcodoc_output/doc_test/sklearn_1.8.0.db \\
        --library-name sklearn --version 1.8.0 \\
        --collected-dir "C:/.../samples/docs/sklearn" \\
        --sources web pdf \\
        --report-file tests/sklearn_structuring.json
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doc_processor.doc_runner import DocProcessingRunner
from doc_processor.file_doc.signature import MemberInput
from doc_processor.file_doc.extraction_utils import parse_artifact_stem


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_structuring")


# DocumentationExtractor only accepts these member types.
_LLM_TYPES = {"class", "function", "method"}

# Collector subfolder names per doc source.
_SOURCE_DIRS = {"web": "web_doc", "pdf": "pdf_doc"}


def _normalize_member_type(member_type: Optional[str]) -> str:
    """Coerce DB/inherited member types into the 3 the LLM extractor accepts."""
    mt = (member_type or "").lower()
    if mt in _LLM_TYPES:
        return mt
    # property / attribute / variable / inherited_* -> treat as method
    return "method"


# ==============================================================================
# Input loading (collector output layout)
# ==============================================================================

def load_collected_texts(collected_dir: str, source: str) -> Tuple[Dict[str, str], Dict]:
    """
    Walk <collected-dir>/<member>/<web_doc|pdf_doc>/ and return
    {sample_api_name: doc_text} for the given source.

    The member FOLDER name is the authoritative API name; inner files may be
    named with alias paths and are read for content only. If a member has
    multiple files for one source (alias copies), the largest is used and the
    case is recorded for the report.
    """
    sub = _SOURCE_DIRS[source]
    root = Path(collected_dir)
    if not root.exists():
        raise FileNotFoundError(f"Collected docs directory not found: {root}")

    texts: Dict[str, str] = {}
    notes = {"multi_file": {}, "empty": []}

    for member_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        src_dir = member_dir / sub
        if not src_dir.exists():
            continue
        files = sorted(src_dir.glob("*.txt"))
        if not files:
            continue

        chosen = max(files, key=lambda f: f.stat().st_size)
        text = chosen.read_text(encoding="utf-8")
        if not text.strip():
            notes["empty"].append(member_dir.name)
            continue

        texts[member_dir.name] = text
        if len(files) > 1:
            notes["multi_file"][member_dir.name] = {
                "used": chosen.name,
                "all": [f.name for f in files],
            }

    return texts, notes


# ==============================================================================
# Runner
# ==============================================================================

class StructuringTestRunner(DocProcessingRunner):
    """
    Subclass of DocProcessingRunner that:
      - Skips Steps 1-3 by seeding per_member/ with collected doc text.
      - Uses a source-suffixed version label so web and pdf batches get
        separate artifact trees (per_member, preprocessed, structured, post).
      - Runs Steps 4-6; skips Step 7 (no DB writes).
    """

    def __init__(self, db_path, library_name, version, source: str, overwrite=False):
        # Suffix the version so all artifact dirs are per-source.
        super().__init__(db_path, library_name, f"{version}_{source}")
        self.source = source
        self.overwrite = overwrite

    # ── Seed per_member/ ──────────────────────────────────────────────────────
    def _seed_per_member(self, texts: Dict[str, str]) -> List[str]:
        self._ensure_dirs(self.per_member_dir)

        if self.overwrite:
            for sub in (self.per_member_dir, self.preprocessed_doc_dir, self.url_context_dir, self.structured_doc_dir, self.postprocessed_doc_dir):
                if sub.exists():
                    for f in sub.glob("*"):
                        if f.is_file():
                            f.unlink()
            logger.info("Overwrite mode: cleared Step 4-6 artifact dirs")

        seeded = []
        for api_name, text in texts.items():
            (self.per_member_dir / f"{api_name}.txt").write_text(text, encoding="utf-8")
            seeded.append(api_name)
        logger.info(f"[{self.source}] Seeded {len(seeded)} per-member files into {self.per_member_dir}")
        return seeded

    # ── MemberInput construction (type and signatures from DB) ─────────────────
    def _build_inputs_for(self, stems: List[str]) -> List[MemberInput]:
        inputs = []
        for stem in stems:
            true_api, _ = parse_artifact_stem(stem)
            member_type, sig_variants = self._lookup_type_and_signatures(true_api)
            inputs.append(MemberInput(
                api_name=true_api,
                signature_variants=sig_variants or {},
                member_type=_normalize_member_type(member_type),
                docstring=""
            ))
        return inputs

    def _lookup_type_and_signatures(self, api_name: str):
        """Resolve (member_type, signature_variants) from the DB for an API name."""
        m = self.qm.get_member_by_any_api_name(api_name)
        if m:
            return m.type, (m.signatures or {})

        inh = self.qm.get_inherited_member_by_api_name(api_name)
        if inh:
            # Prefer the original member's signature variants when linked.
            if inh.original_member_id:
                original = self.qm.get_original_member_for_inherited(api_name)
                if original and original.signatures:
                    return inh.member_type or "method", original.signatures

            # InheritedMemberDetails carries `signature` (a dict), not `signatures`.
            sig = inh.signature
            if isinstance(sig, dict) and sig:
                return inh.member_type or "method", {str(k): str(v) for k, v in sig.items()}
            if isinstance(sig, str) and sig:
                return inh.member_type or "method", {"full": sig}

            short = api_name.split(".")[-1]
            return inh.member_type or "method", {"full": f"{short}("}

        logger.warning(f"No DB member found for '{api_name}'; defaulting type=function")
        return "function", {}

    # ── Steps 4-6 ─────────────────────────────────────────────────────────────
    def run_structuring(self, texts: Dict[str, str]) -> Dict:
        if not texts:
            logger.warning(f"[{self.source}] No input texts; skipping batch.")
            return {"summary": {}, "members": []}

        api_names = self._seed_per_member(texts)
        pipeline_inputs = self._build_inputs_for(api_names)

        logger.info(f"=== [{self.source}] Step 4: URL preprocessing ===")
        self._preprocess_all_members()

        logger.info(f"=== [{self.source}] Step 5: Structured extraction (LLM) ===")
        self._extract_structured_docs(pipeline_inputs)

        logger.info(f"=== [{self.source}] Step 6: URL postprocessing ===")
        self._postprocess_all_members()

        return self._build_report(api_names, pipeline_inputs)

    # ── Report ────────────────────────────────────────────────────────────────
    def _build_report(self, api_names, pipeline_inputs) -> Dict:
        type_by_name = {mi.api_name: mi.member_type for mi in pipeline_inputs}
        members = []
        for api_name in api_names:
            pre = self.preprocessed_doc_dir / f"{api_name}.txt"
            ctx = self.url_context_dir / f"{api_name}.json"
            struct = self.structured_doc_dir / f"{api_name}.json"
            post = self.postprocessed_doc_dir / f"{api_name}.json"

            n_placeholders = 0
            if ctx.exists():
                try:
                    n_placeholders = len(json.loads(ctx.read_text(encoding="utf-8")))
                except Exception:
                    pass

            struct_ok = struct.exists() and _is_valid_json(struct)
            post_ok = post.exists() and _is_valid_json(post)
            residual = _count_residual_placeholders(post) if post.exists() else None

            members.append({
                "api_name": api_name,
                "source": self.source,
                "type": type_by_name.get(parse_artifact_stem(api_name)[0]),
                "preprocessed": pre.exists(),
                "url_placeholders": n_placeholders,
                "structured": struct_ok,
                "structured_raw_only": (self.structured_doc_dir / f"{api_name}.raw.json").exists(),
                "postprocessed": post_ok,
                "postprocessed_file": str(post) if post_ok else None,
                "residual_placeholders": residual,
                "status": "ok" if (pre.exists() and struct_ok and post_ok and not residual) else "incomplete"
            })

        total = len(members)
        return {
            "summary": {
                "source": self.source,
                "total_members": total,
                "preprocessed": sum(m["preprocessed"] for m in members),
                "structured": sum(m["structured"] for m in members),
                "postprocessed": sum(m["postprocessed"] for m in members),
                "structured_raw_only": sum(m["structured_raw_only"] for m in members),
                "fully_ok": sum(m["status"] == "ok" for m in members),
                "with_residual_placeholders": sum(1 for m in members if m["residual_placeholders"])
            },
            "members": members,
        }


# ==============================================================================
# Helpers
# ==============================================================================

def _is_valid_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def _count_residual_placeholders(path: Path) -> int:
    try:
        return path.read_text(encoding="utf-8").count("url_placeholder_")
    except Exception:
        return 0


def _print_report(report: Dict) -> None:
    for src_report in report["sources"].values():
        s = src_report.get("summary") or {}
        if not s:
            continue
        print("\n" + "=" * 70)
        print(f"  STRUCTURING REPORT — {s['source'].upper()} DOCS (Steps 4-6)")
        print("=" * 70)
        print(f"  Total members        : {s['total_members']}")
        print(f"  Step 4 preprocessed  : {s['preprocessed']}")
        print(f"  Step 5 structured    : {s['structured']}")
        print(f"  Step 6 postprocessed : {s['postprocessed']}")
        print(f"  Fully OK             : {s['fully_ok']}")
        print(f"  Residual placeholders: {s['with_residual_placeholders']}")
        print("=" * 70)
        for m in src_report["members"]:
            if m["status"] != "ok":
                flags = (f"pre={int(m['preprocessed'])} struct={int(m['structured'])} "
                         f"post={int(m['postprocessed'])} urls={m['url_placeholders']} "
                         f"residual={m['residual_placeholders']}")
                print(f"  [incomplete] {m['api_name']}  ({m['type']})  {flags}")
    print()


def _parse_args():
    p = argparse.ArgumentParser(description="Run Steps 4-6 (preprocess, LLM structure, postprocess) on docs collected by collect_member_docs.py.")
    p.add_argument("--db-path", required=True)
    p.add_argument("--library-name", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--collected-dir", required=True, help="Output dir of collect_member_docs.py (<dir>/<api_name>/web_doc|pdf_doc/)")
    p.add_argument("--sources", nargs="+", choices=["web", "pdf"], default=["web", "pdf"], help="Which doc sources to process (default: both)")
    p.add_argument("--overwrite", action="store_true", help="Clear Step 4-6 artifact dirs before running")
    p.add_argument("--report-file", default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        logger.warning("OPENAI_API_KEY not set -- Step 5 (LLM) will be skipped, so structured/postprocessed outputs will be empty.")

    full_report = {"collected_dir": args.collected_dir, "sources": {}, "input_notes": {}}

    for source in args.sources:
        texts, notes = load_collected_texts(args.collected_dir, source)
        logger.info(f"[{source}] Loaded {len(texts)} member docs from {args.collected_dir}")
        full_report["input_notes"][source] = notes

        runner = StructuringTestRunner(
            db_path=args.db_path,
            library_name=args.library_name,
            version=args.version,
            source=source,
            overwrite=args.overwrite
        )
        full_report["sources"][source] = runner.run_structuring(texts)

    _print_report(full_report)
    if args.report_file:
        Path(args.report_file).write_text(json.dumps(full_report, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Report saved to: {args.report_file}")
