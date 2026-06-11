"""
Structuring Test Script
=======================
Tests the post-extraction documentation pipeline (Steps 4-6 only):

    Step 4  URL preprocessing       (preprocess_crossRef)
    Step 5  Structured extraction   (LLM / ConcurrentDocExtractor)  -- needs OPENAI_API_KEY
    Step 6  URL postprocessing      (postprocess_crossRef)

It is the complement of tests/test_extraction.py (which covers Steps 1-3).
Instead of crawling/scraping, the already-retrieved per-member text is provided
as input. No DB writes occur (Step 7 is skipped).

Inputs:
  --db-path / --library-name / --version : same as test_extraction.py
  --input-file : JSON mapping  {api_name: extracted_text}
                 or a list of  [{"api_name": ..., "text": ...}, ...]
        OR
  --input-dir  : directory of {api_name}.txt files (one per member)

Usage:
    python tests/test_structuring.py \\
        --db-path mapcodoc_output/xgboost.db \\
        --library-name xgboost --version 3.2.0-dev \\
        --input-file retrieved_texts.json \\
        --report-file structuring_results.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doc_processor.doc_runner import DocProcessingRunner
from doc_processor.file_doc.signature import MemberInput


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_structuring")


# DocumentationExtractor only accepts these member types.
_LLM_TYPES = {"class", "function", "method"}


def _normalize_member_type(member_type: Optional[str]) -> str:
    """Coerce DB/inherited member types into the 3 the LLM extractor accepts."""
    mt = (member_type or "").lower()
    if mt in _LLM_TYPES:
        return mt
    if mt == "function":
        return "function"
    if mt == "class":
        return "class"
    # property / attribute / variable / inherited_* -> treat as method
    return "method"


class StructuringTestRunner(DocProcessingRunner):
    """
    Subclass of DocProcessingRunner that:
      - Skips Steps 1-3 by seeding per_member/ with provided extracted text.
      - Runs Steps 4-6 (preprocess -> LLM structure -> postprocess).
      - Skips Step 7 (no DB writes); generates a structured report instead.
    """

    def __init__(self, db_path, library_name, version, overwrite=False):
        super().__init__(db_path, library_name, version)
        self.overwrite = overwrite

    # ── Seed per_member/ from provided texts ─────────────────────────────────
    def _seed_per_member(self, texts: Dict[str, str]) -> List[str]:
        """Write each provided extracted text to per_member/{api_name}.txt."""
        
        self._ensure_dirs(self.per_member_dir)

        if self.overwrite:
            for sub in (self.per_member_dir, self.preprocessed_doc_dir,
                        self.url_context_dir, self.structured_doc_dir,
                        self.postprocessed_doc_dir):
                if sub.exists():
                    for f in sub.glob("*"):
                        if f.is_file():
                            f.unlink()
            logger.info("Overwrite mode: cleared Step 4-6 artifact dirs")

        seeded = []
        for api_name, text in texts.items():
            out = self.per_member_dir / f"{api_name}.txt"
            out.write_text(text, encoding="utf-8")
            seeded.append(api_name)
        logger.info(f"Seeded {len(seeded)} per-member files into {self.per_member_dir}")
        return seeded

    # ── Build MemberInput list (type + signature from DB) ────────────────────
    def _build_inputs_for(self, api_names: List[str]) -> List[MemberInput]:
        inputs = []
        for api_name in api_names:
            member_type, sig_variants = self._lookup_type_and_signatures(api_name)
            inputs.append(MemberInput(
                api_name=api_name,
                signature_variants=sig_variants or {},
                member_type=_normalize_member_type(member_type),
                docstring="",
            ))
        return inputs

    def _lookup_type_and_signatures(self, api_name: str):
        """Resolve (member_type, signature_variants) from the DB for an API name."""
        m = self.qm.get_member_by_any_api_name(api_name)
        if m:
            return m.type, (m.signatures or {})

        inh = self.qm.get_inherited_member_by_api_name(api_name)
        if inh:
            member_type = getattr(inh, "member_type", "method")
            sigs = getattr(inh, "signatures", None) or {}
            # last resort: bare name needle for prompt context
            if not sigs:
                short = api_name.split(".")[-1]
                sigs = {"full": f"{short}("}
            return member_type, sigs

        logger.warning(f"No DB member found for '{api_name}'; defaulting type=function")
        return "function", {}

    # ── Public entry point ───────────────────────────────────────────────────
    def run_structuring_test(self, texts: Dict[str, str], report_file: Optional[str] = None) -> Dict:
        if not texts:
            raise ValueError("No input texts provided.")

        api_names = self._seed_per_member(texts)
        pipeline_inputs = self._build_inputs_for(api_names)

        # Step 4: URL preprocessing
        logger.info("=== Step 4: URL preprocessing ===")
        self._preprocess_all_members()

        # Step 5: LLM structured extraction
        logger.info("=== Step 5: Structured extraction (LLM) ===")
        self._extract_structured_docs(pipeline_inputs)

        # Step 6: URL postprocessing
        logger.info("=== Step 6: URL postprocessing ===")
        self._postprocess_all_members()

        report = self._build_report(api_names, pipeline_inputs)
        _print_report(report)
        if report_file:
            Path(report_file).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"Report saved to: {report_file}")
        return report

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
                "type": type_by_name.get(api_name),
                "preprocessed": pre.exists(),
                "url_placeholders": n_placeholders,
                "structured": struct_ok,
                "postprocessed": post_ok,
                "residual_placeholders": residual,
                "status": "ok" if (pre.exists() and struct_ok and post_ok and not residual) else "incomplete"
            })

        total = len(members)
        return {
            "summary": {
                "total_members": total,
                "preprocessed": sum(m["preprocessed"] for m in members),
                "structured": sum(m["structured"] for m in members),
                "postprocessed": sum(m["postprocessed"] for m in members),
                "fully_ok": sum(m["status"] == "ok" for m in members),
                "with_residual_placeholders": sum(1 for m in members if m["residual_placeholders"])
            },
            "members": members
        }


# ── helpers ───────────────────────────────────────────────────────────────────
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


def _load_texts(input_file: Optional[str], input_dir: Optional[str]) -> Dict[str, str]:
    if input_file:
        data = json.loads(Path(input_file).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        if isinstance(data, list):
            return {d["api_name"]: d.get("text", d.get("extracted_text", "")) for d in data}
        
        raise ValueError("input-file must be a JSON object or list")
    
    texts = {}
    for txt in Path(input_dir).glob("*.txt"):
        texts[txt.stem] = txt.read_text(encoding="utf-8")
    return texts


def _print_report(report: Dict) -> None:
    s = report["summary"]
    print("\n" + "=" * 70)
    print("  STRUCTURING TEST REPORT (Steps 4-6)")
    print("=" * 70)
    print(f"  Total members        : {s['total_members']}")
    print(f"  Step 4 preprocessed  : {s['preprocessed']}")
    print(f"  Step 5 structured    : {s['structured']}")
    print(f"  Step 6 postprocessed : {s['postprocessed']}")
    print(f"  Fully OK             : {s['fully_ok']}")
    print(f"  Residual placeholders: {s['with_residual_placeholders']}")
    print("=" * 70)
    for m in report["members"]:
        flags = (f"pre={int(m['preprocessed'])} "
                 f"struct={int(m['structured'])} post={int(m['postprocessed'])} "
                 f"urls={m['url_placeholders']} residual={m['residual_placeholders']}")
        print(f"  [{m['status']:10s}] {m['api_name']}  ({m['type']})  {flags}")
    print()


def _parse_args():
    p = argparse.ArgumentParser(description="Test structuring pipeline (Steps 4-6: preprocess, LLM, postprocess).")
    p.add_argument("--db-path", required=True)
    p.add_argument("--library-name", required=True)
    p.add_argument("--version", required=True)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input-file", help="JSON {api_name: text} or list of objects")
    src.add_argument("--input-dir", help="Directory of {api_name}.txt files")
    p.add_argument("--overwrite", action="store_true", help="Clear Step 4-6 artifact dirs before running")
    p.add_argument("--report-file", default=None)
    return p.parse_args()


if __name__ == "__main__":
    import os
    args = _parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        logger.warning("OPENAI_API_KEY not set -- Step 5 (LLM) will be skipped, so structured/postprocessed outputs will be empty.")

    texts = _load_texts(args.input_file, args.input_dir)
    runner = StructuringTestRunner(
        db_path=args.db_path,
        library_name=args.library_name,
        version=args.version,
        overwrite=args.overwrite
    )
    runner.run_structuring_test(texts=texts, report_file=args.report_file)
