"""
Extraction Test Script
======================
Tests the per-member documentation extraction pipeline (Steps 1–3 only) using
already-crawled URLs or a local PDF.  No LLM structuring or DB writes occur.

Usage (web — using existing scraped_urls.txt):
    python tests/test_extraction.py \\
        --db-path mapcodoc_output/xgboost.db \\
        --library-name xgboost \\
        --version 3.2.0-dev \\
        --url-file doc_processor/doc_artifacts/crawled_URLs/xgboost/v_3.2.0-dev/scraped_urls.txt

Usage (PDF):
    python tests/test_extraction.py \\
        --db-path mapcodoc_output/numpy.db \\
        --library-name numpy \\
        --version 2 \\
        --pdf-path doc_processor/doc_artifacts/local_doc/numpy/v_2/numpy-ref.pdf \\
        --names-file /api_names/numpy.txt

Optional flags:
    --target-module xgboost          Filter members by API name prefix
    --semantic-mode never            Lexical only (fast); choices: auto/never/always/only
    --overwrite                      Re-extract files that already exist in per_member/
    --report-file results.json       Save per-member results to JSON (default: print only)
"""

import argparse
import asyncio
import json
import logging
import sys
import shutil
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Set

# ── make project root importable ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doc_processor.doc_runner import DocProcessingRunner
from doc_processor.web_doc.doc_scraper import scrape_doc
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig
from doc_processor.file_doc.signature import MemberInput
from doc_processor.file_doc.embeddings import EmbeddingModel
from doc_processor.filter_doc import WebMemberExtractor, WebMemberInfo
from doc_processor.file_doc.pipeline_pdf import extract_api_docs_from_pdf, _sanitize_filename
from mapcodoc_db.query import MemberDetails, InheritedMemberDetails


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_extraction")


# ==============================================================================
# Instrumented extractor — logs match_type for every found anchor
# ==============================================================================

class _InstrumentedExtractor(WebMemberExtractor):
    """
    Thin `WebMemberExtractor` subclass that records the match_type and score
    returned by every successful `extract_batch` / `find_anchor_position` call
    into a shared metadata dict.  Used only by `ExtractionTestRunner`.
    """

    def __init__(self, cfg, embedder, metadata_log: dict):
        super().__init__(cfg, embedder)
        self._log = metadata_log

    def extract_batch(self, text, members, model_name=""):
        results = super().extract_batch(text, members, model_name)
        for api_name, (pos, score, match_type) in results.items():
            if pos >= 0 and match_type != "none":
                self._log[api_name] = {
                    "match_type": match_type,
                    "score": round(float(score), 2),
                }
        return results

    def find_anchor_position(self, text, member, model_name=""):
        result = super().find_anchor_position(text, member, model_name)
        pos, score, match_type = result
        if pos >= 0 and match_type != "none":
            self._log[member.api_name] = {
                "match_type": match_type,
                "score": round(float(score), 2),
            }
        return result


# ==============================================================================
# ExtractionTestRunner
# ==============================================================================

class ExtractionTestRunner(DocProcessingRunner):
    """
    Thin subclass of DocProcessingRunner that:
      - Skips URL crawling (Step 1) by accepting an existing URL file directly.
      - Stops after Step 3 (per-member extraction) — no preprocessing, LLM, or DB writes.
      - Generates a structured report of extraction results.
    """

    def __init__(
        self,
        db_path: str,
        library_name: str,
        version: str,
        semantic_mode: str = "auto",
        overwrite: bool = False,
        api_section_titles: Optional[List[str]] = None,
        sample_names: Optional[List[str]] = None
    ):
        super().__init__(db_path, library_name, version)
        self.semantic_mode = semantic_mode
        self.overwrite = overwrite 
        self.api_section_titles = api_section_titles
        self.sample_names = sample_names   # None => all members; else restrict to these
        self._match_metadata: dict = {} # Populated during extraction; maps api_name -> {match_type, score}

    # ── Override: web pipeline that skips crawling ────────────────────────────

    def _run_web_pipeline_from_file(
        self,
        url_file: str,
        stat_info: Dict,
        members: List[MemberInput],
    ) -> None:
        """
        Web pipeline starting from Step 2 (scraping), using an existing URL file.

        Args:
            url_file:  Path to an existing scraped_urls.txt.
            stat_info: Dict with at least {'base_url': ..., 'sub_path': ...}.
                       Loaded from the statistics.json that sits alongside the URL file.
            members:   MemberInput objects from the DB.
        """
        logger.info("=== Step 2: Scraping HTML pages ===")
        self._ensure_dirs(self.scraped_doc_dir, self.per_member_dir)

        # Delete existing per_member files if overwrite requested
        if self.overwrite and self.per_member_dir.exists():
            for f in self.per_member_dir.glob("*.txt"):
                f.unlink()
            logger.info(f"Overwrite mode: cleared {self.per_member_dir}")

        asyncio.run(scrape_doc(self.library_name, self.version, url_file, stat_info))

        logger.info("=== Step 3: Extracting per-member documentation ===")
        self._run_step3(members)

    # ── Override: PDF pipeline wrapper (already skips crawl) ─────────────────

    def _run_pdf_pipeline_test(self, pdf_path: str, members: List[MemberInput]) -> None:
        """Delegates to the existing PDF pipeline (Steps 1–2 only)."""
        if self.overwrite and self.per_member_dir.exists():
            for f in self.per_member_dir.glob("*.txt"):
                f.unlink()
            logger.info(f"Overwrite mode: cleared {self.per_member_dir}")
            
        self._ensure_dirs(self.local_doc_dir, self.per_member_dir)
        
        # Copy PDF to local_doc if not already there
        pdf_filename = Path(pdf_path).name
        local_pdf_path = self.local_doc_dir / pdf_filename
        if Path(pdf_path) != local_pdf_path:
            shutil.copy2(pdf_path, local_pdf_path)
            logger.info(f"Copied PDF to: {local_pdf_path}")
            
        # Build peer signatures (same as _run_pdf_pipeline)
        peer_signatures = self._build_peer_signatures(members)
        
        # Use self.semantic_mode — NOT the hardcoded "auto" from _run_pdf_pipeline
        member_cfg = MemberExtractorConfig(semantic_mode=self.semantic_mode)
        
        out_json_path = self.scraped_doc_dir / "extracted_docs.json"
        
        extract_api_docs_from_pdf(
            pdf_path=str(local_pdf_path),
            members=members,
            out_json_path=str(out_json_path),
            per_api_txt_dir=str(self.per_member_dir),
            model_name="intfloat/e5-base-v2",
            member_cfg=member_cfg,
            cache_dir=str(self.ARTIFACTS_BASE / ".cache"),
            peer_signatures=peer_signatures,
            api_section_titles=self.api_section_titles
        )
        
        logger.info(f"PDF extraction complete. Results in: {self.scraped_doc_dir}")


    # ── Main step-3 logic (extracted from _run_web_pipeline) ─────────────────

    def _run_step3(self, members: List[MemberInput]) -> None:
        """
        Runs the per-member extraction step (Step 3 a–f) with the configured
        semantic_mode.  This is identical to what DocProcessingRunner does inside
        _run_web_pipeline after scraping, just parameterised by self.semantic_mode.
        """

        model_name = "intfloat/e5-base-v2"
        cfg = MemberExtractorConfig(
            semantic_mode=self.semantic_mode,
            window_chars=3000,
            window_stride=2000,
        )

        member_map, _ = self._build_member_map(members)
        embedder = None
        extractor = None
        extracted_apis = set()
        combined_doc_files = set()

        # Track files already extracted from a previous run
        if self.per_member_dir.exists():
            for txt in self.per_member_dir.glob("*.txt"):
                extracted_apis.add(txt.stem)

        containers_to_filter = []

        # ── Step 3b: per_module pages ─────────────────────────────────────
        members_json = self.per_module_dir / "members.json"
        if self.per_module_dir.exists() and members_json.exists():
            with open(members_json) as f:
                module_members_map = json.load(f)

            for container_name, nested_api_names in module_members_map.items():
                module_txt = self.per_member_dir / f"{container_name}.txt"
                if not module_txt.exists():
                    module_txt = self.per_module_dir / f"{container_name}.txt"
                if not module_txt.exists():
                    continue

                combined_doc_files.add(module_txt)
                combined_text = module_txt.read_text(encoding="utf-8")

                if embedder is None and cfg.semantic_mode != "never":
                    embedder = EmbeddingModel(model_name, cache_dir=str(self.ARTIFACTS_BASE / ".cache"))
                    extractor = _InstrumentedExtractor(cfg, embedder, self._match_metadata)
                elif extractor is None:
                    extractor = _InstrumentedExtractor(cfg, None, self._match_metadata)

                members_to_extract = self._build_extraction_list(nested_api_names, member_map, extracted_apis)
                container_info = self._get_or_create_member_info(container_name, member_map, extracted_apis)
                all_to_extract = ([container_info] if container_info else []) + members_to_extract

                self._extract_and_save_members(combined_text, all_to_extract, self.per_member_dir, extractor, model_name, extracted_apis)
                containers_to_filter.append((module_txt, container_name, nested_api_names))

        # ── Step 3c: per_page (all APIs on one page) ──────────────────────
        per_page_json = self.per_page_dir / "members.json"
        if self.per_page_dir.exists() and per_page_json.exists():
            with open(per_page_json) as f:
                page_data = json.load(f)

            apis_txt = self.per_page_dir / "APIs.txt"
            if apis_txt.exists():
                combined_doc_files.add(apis_txt)
                combined_text = apis_txt.read_text(encoding="utf-8")
                api_names = page_data.get("API_names", [])

                if embedder is None and cfg.semantic_mode != "never":
                    embedder = EmbeddingModel(model_name, cache_dir=str(self.ARTIFACTS_BASE / ".cache"))
                    extractor = _InstrumentedExtractor(cfg, embedder, self._match_metadata)
                elif extractor is None:
                    extractor = _InstrumentedExtractor(cfg, None, self._match_metadata)

                members_to_extract = self._build_extraction_list(api_names, member_map, extracted_apis)
                self._extract_per_page_with_class_anchors(
                    combined_text, members_to_extract, self.per_member_dir,
                    extractor, model_name, extracted_apis, cfg
                )

        # ── Step 3d: fallback — missing methods from class docs ───────────
        if extractor is None:
            if cfg.semantic_mode != "never":
                if embedder is None:
                    embedder = EmbeddingModel(model_name, cache_dir=str(self.ARTIFACTS_BASE / ".cache"))
                extractor = _InstrumentedExtractor(cfg, embedder, self._match_metadata)
            else:
                extractor = _InstrumentedExtractor(cfg, None, self._match_metadata)

        self._extract_missing_methods_from_class_docs(containers_to_filter, extracted_apis, extractor, model_name)

        # ── Steps 3e–3f: filter + relocate ───────────────────────────────
        filtered_outputs: Set[Path] = set()
        for module_txt, container_name, nested_api_names in containers_to_filter:
            out_path = self._filter_container_doc(module_txt, container_name, nested_api_names)
            if out_path:
                filtered_outputs.add(out_path)
        
        self._relocate_combined_docs(combined_doc_files - filtered_outputs, extracted_apis)

    # ── Public test entry point ───────────────────────────────────────────────

    def _resolve_sample_members(self, sample_names: List[str]):
        """
        Resolve each sample API name to EITHER a standalone (direct) member or an inherited member, by exact identity, so a method listed both under its
        defining class (standalone) and under an inheriting class (inherited) resolves to two distinct entries, each extracted separately.
        
        Resolution order (avoids all_api_names aliases swallowing inherited paths):
            1. exact DBMember.primary_api_name        -> standalone (direct)
            2. exact DBInheritedMember.inherited_api_name -> inherited
            3. alias fallback (all_api_names)         -> direct
            
        Returns (direct_members, inherited_pairs, unresolved).
        """
        direct, inherited_pairs, unresolved = [], [], []
        seen_direct, seen_inherited = set(), set()
        
        for name in sample_names:
            # 1. Standalone member, matched on its canonical (primary) API name.
            member = self.qm.get_member_by_api_name(name)
            
            if member is None:
                # 2. Inherited member, matched on its derived path under the inheriting class (distinct name from the standalone original).
                inh = self.qm.get_inherited_member_by_api_name(name)
                if inh is not None:
                    if inh.inherited_api_name not in seen_inherited:
                        seen_inherited.add(inh.inherited_api_name)
                        original = self.qm.get_original_member_for_inherited(name)
                        inherited_pairs.append((inh, original))
                    continue
                # 3. Last resort: a secondary alias of a direct member.
                member = self.qm.get_member_by_any_api_name(name)
                
            if member is not None:
                key = member.fqn or name
                if key not in seen_direct:
                    seen_direct.add(key)
                    direct.append(member)
                continue
            
            unresolved.append(name)
            
        return direct, inherited_pairs, unresolved
    
    
    def _inject_class_anchors(self, pipeline_inputs: List[MemberInput]) -> Set[str]:
        """
        Ensure every method's parent class is present in `pipeline_inputs` as a 'class' member, so the PDF pipeline can anchor (scope) the method to it.

        The PDF extractor scopes each method to its parent class's text region and returns 'not_found' when the parent class isn't anchored. In sample mode the
        inheriting class is often absent, so its methods/inherited members silently drop. These added classes serve ONLY as extraction anchors: they are appended
        to pipeline_inputs in place but are NOT added to the report's member sets.

        Returns the set of api_names added purely as anchors (for later pruning).
        """
        present_classes = {mi.api_name for mi in pipeline_inputs if mi.member_type == "class"}
        parents_needed = {
            mi.api_name.rsplit(".", 1)[0]
            for mi in pipeline_inputs
            if mi.member_type == "method" and "." in (mi.api_name or "")
        }

        anchor_names: Set[str] = set()
        for parent in sorted(parents_needed - present_classes):
            cls = self.qm.get_member_by_api_name(parent) or self.qm.get_member_by_any_api_name(parent)
            # If the parent resolves to a non-class (e.g. a module), it can't anchor.
            if cls is not None and cls.type != "class":
                continue
            pipeline_inputs.append(MemberInput(
                api_name=parent,                                  # must equal method.rsplit('.',1)[0]
                signature_variants=(cls.signatures if cls else {}) or {},
                member_type="class",
                docstring="",
            ))
            anchor_names.add(parent)

        if anchor_names:
            logger.info(f"Added {len(anchor_names)} class anchor(s) for method scoping: , ".join(sorted(anchor_names)))
        return anchor_names

    def _prune_anchor_artifacts(self, anchor_names: Set[str]) -> None:
        """
        Remove per-member .txt files and extracted_docs.json entries for classes added solely as method-scoping anchors, so they don't appear in the result.
        """
        if not anchor_names:
            return

        for name in anchor_names:
            f = self.per_member_dir / f"{_sanitize_filename(name)}.txt"
            if f.exists():
                f.unlink()

        json_path = self.scraped_doc_dir / "extracted_docs.json"
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            if any(name in data for name in anchor_names):
                for name in anchor_names:
                    data.pop(name, None)
                json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


    def run_extraction_test(
        self,
        url_file: Optional[str] = None,
        pdf_path: Optional[str] = None,
        target_module: Optional[str] = None,
        report_file: Optional[str] = None,
    ) -> Dict:
        """
        Run extraction (Steps 1–3 only) and return a results dict.

        Exactly one of url_file or pdf_path must be provided.

        Args:
            url_file: Path to existing scraped_urls.txt (web mode).
            pdf_path: Path to local PDF file (PDF mode).
            target_module: Optional API name prefix filter.
            report_file: Optional path to save JSON report.

        Returns:
            Dict with keys 'summary' and 'members'.
        """
        if not url_file and not pdf_path:
            raise ValueError("Provide either --url-file (web) or --pdf-path (PDF).")
        if url_file and pdf_path:
            raise ValueError("Provide either --url-file or --pdf-path, not both.")

        # ── Load members from DB ──────────────────────────────────────────
        logger.info("Loading members from database...")
        
        if self.sample_names:
            members_db, inherited_members, unresolved = self._resolve_sample_members(self.sample_names)
            logger.info(
                f"Sample: resolved {len(members_db)} direct + {len(inherited_members)} inherited "
                f"from {len(self.sample_names)} names ({len(unresolved)} unresolved)"
            )
            if unresolved:
                logger.warning("Unresolved sample names: " + ", ".join(unresolved))
            if not members_db and not inherited_members:
                logger.warning("No sample members resolved in the database. Aborting.")
                return {}
        else:
            members_db = self._get_target_members(target_module)
            if not members_db:
                logger.warning("No members found in the database. Aborting.")
                return {}
            class_members = [m for m in members_db if m.type == "class"]
            inherited_members = self._get_inherited_members_for_pipeline(class_members)
        
        pipeline_inputs = [
            MemberInput(
                api_name=m.api_name or m.fqn,
                signature_variants=m.signatures or {},
                member_type=m.type,
                docstring=""
            )
            for m in members_db
        ]
        for inherited, original_member in inherited_members:
            pipeline_inputs.append(self._inherited_to_member_input(inherited, original_member))
            
        logger.info(f"Members: {len(members_db)} direct + {len(inherited_members)} inherited = {len(pipeline_inputs)} total")

        # ── Run extraction ────────────────────────────────────────────────
        if url_file:
            stat_info = _load_stat_info(Path(url_file))
            self._run_web_pipeline_from_file(url_file, stat_info, pipeline_inputs)
        else:
            # Add inheriting/parent classes as anchors only (PDF pipeline scopes methods to their parent class; an unanchored parent => not_found).
            anchor_names = self._inject_class_anchors(pipeline_inputs)
            self._run_pdf_pipeline_test(pdf_path, pipeline_inputs)
            self._prune_anchor_artifacts(anchor_names)
            # Read match metadata from PDF pipeline saved extracted_docs.json
            _collect_pdf_match_metadata(self.scraped_doc_dir, self._match_metadata)

        # ── Generate report ───────────────────────────────────────────────
        report = _build_report(members_db, inherited_members, self.per_member_dir, self._match_metadata)
        _print_report(report)

        if report_file:
            Path(report_file).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"Report saved to: {report_file}")

        return report


# ==============================================================================
# Helpers
# ==============================================================================

def _load_stat_info(url_file: Path) -> Dict:
    """
    Load base_url and sub_path from a statistics.json sitting next to the URL file.
    Falls back to inferring base_url from the first line of the URL file itself.
    """
    stat_file = url_file.parent / "statistics.json"

    # Also check for library-prefixed names (e.g. pytorch_statistics.json)
    if not stat_file.exists():
        candidates = list(url_file.parent.glob("*statistics*.json"))
        if candidates:
            stat_file = candidates[0]

    if stat_file.exists():
        with open(stat_file) as f:
            data = json.load(f)
        logger.info(f"Loaded stat_info from {stat_file.name}: base_url={data.get('base_url')}")
        return data

    # Fallback: infer base_url from the first URL in the file
    logger.warning("statistics.json not found — inferring base_url from URL file")
    with open(url_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                from urllib.parse import urlparse
                parsed = urlparse(line)
                base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rsplit('/', 1)[0]}/"
                return {"base_url": base_url, "sub_path": ""}
    raise RuntimeError(f"Could not determine base_url from {url_file}")


def _load_sample_names(path: str) -> List[str]:
    """Read sample API names (one per line), preserving order, dropping blanks/dupes."""
    seen, names = set(), []
    with open(path, encoding="utf-8") as f:
        for line in f:
            n = line.strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    return names


def _collect_pdf_match_metadata(scraped_doc_dir: Path, metadata_log: dict) -> None:
    """
    Read match_type and scores from extracted_docs.json saved by the PDF pipeline.

    Args:
        scraped_doc_dir: The scraped_doc/{lib}/{version}/ directory.
        metadata_log:    Shared dict to populate with {api_name: {match_type, score}}.
    """
    json_path = scraped_doc_dir / "extracted_docs.json"
    if not json_path.exists():
        return

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    for api_name, result in data.items():
        scores = result.get("base_scores") or result.get("scores") or {}
        mt = scores.get("match_type", "unknown")
        final = scores.get("final", scores.get("lexical", 0.0))
        if mt and mt not in ("none", "not_found"):
            metadata_log[api_name] = {
                "match_type": mt,
                "score": round(float(final), 2),
            }
            

def _classify_match_type(match_type: str) -> str:
    """
    Coarse category for a match_type string.

    Returns one of: 'lexical', 'semantic', 'hybrid', 'fallback'
    """
    mt = (match_type or "").lower()
    if "+semantic" in mt:
        return "hybrid"
    if mt in ("semantic", "semantic_window", "semantic+"):
        return "semantic"
    if mt in ("exact", "prefix", "anchor", "regex", "raw_search"):
        return "lexical"
    return "fallback"


def _build_report(members_db, inherited_members, per_member_dir: Path, match_metadata: dict = None) -> Dict:
    """Build a structured extraction report."""
    if match_metadata is None:
        match_metadata = {}

    extracted = {}
    if per_member_dir.exists():
        for txt in per_member_dir.glob("*.txt"):
            lines = txt.read_text(encoding="utf-8").splitlines()
            extracted[txt.stem] = {
                "line_count": len(lines),
                "first_line": lines[0].strip() if lines else "",
                "file": str(txt),
            }

    members_result = []
    found_names = set()

    for m in members_db:
        all_names = set(m.api_names or [])
        if m.api_name:
            all_names.add(m.api_name)
        all_names.add(m.fqn)

        match = next((n for n in all_names if n in extracted), None)
        status = "extracted" if match else "missing"
        if match:
            found_names.add(match)

        # Look up match metadata (try all API name variants)
        meta = None
        for name in ([match] if match else list(all_names)):
            if name in match_metadata:
                meta = match_metadata[name]
                break

        members_result.append({
            "api_name": m.api_name or m.fqn,
            "type": m.type,
            "status": status,
            "file_info": extracted.get(match) if match else None,
            "match_type": meta.get("match_type") if meta else None,
            "match_score": meta.get("score") if meta else None,
            "match_category": _classify_match_type(meta["match_type"]) if meta else None,
        })

    for inherited, _ in inherited_members:
        iname = inherited.inherited_api_name
        match = iname if iname in extracted else None
        if match:
            found_names.add(match)
        meta = match_metadata.get(iname)
        members_result.append({
            "api_name": iname,
            "type": f"inherited_{inherited.member_type or 'method'}",
            "status": "extracted" if match else "missing",
            "file_info": extracted.get(match) if match else None,
            "match_type": meta.get("match_type") if meta else None,
            "match_score": meta.get("score") if meta else None,
            "match_category": _classify_match_type(meta["match_type"]) if meta else None,
        })

    total = len(members_result)
    n_extracted = sum(1 for m in members_result if m["status"] == "extracted")
    n_missing = total - n_extracted
    missing_names = [m["api_name"] for m in members_result if m["status"] == "missing"]

    # ── Match-type breakdown ──────────────────────────────────────────────────
    categories = {"lexical": 0, "semantic": 0, "hybrid": 0, "fallback": 0, "unknown": 0}
    for m in members_result:
        if m["status"] == "extracted":
            cat = m.get("match_category") or "unknown"
            categories[cat] = categories.get(cat, 0) + 1

    return {
        "summary": {
            "total_members": total,
            "extracted": n_extracted,
            "missing": n_missing,
            "coverage_pct": round(100 * n_extracted / total, 1) if total else 0,
            "match_breakdown": {
                "lexical":  categories["lexical"],
                "semantic": categories["semantic"],
                "hybrid":   categories["hybrid"],
                "fallback": categories["fallback"],
                "unknown":  categories["unknown"],
            },
        },
        "members": members_result,
        "missing_names": missing_names,
    }


def _print_report(report: Dict) -> None:
    """Print a human-readable extraction report to stdout."""
    s = report["summary"]
    mb = s.get("match_breakdown", {})

    print("\n" + "=" * 70)
    print("  EXTRACTION TEST REPORT")
    print("=" * 70)
    print(f"  Total members  : {s['total_members']}")
    print(f"  Extracted      : {s['extracted']}  ({s['coverage_pct']}%)")
    print(f"  Missing        : {s['missing']}")

    if any(mb.values()):
        print()
        print("  MATCH TYPE BREAKDOWN (extracted members):")
        print(f"    Lexical   : {mb.get('lexical',  0):>4}  "
              f"({100*mb.get('lexical',0)/max(s['extracted'],1):.1f}%)")
        print(f"    Semantic  : {mb.get('semantic', 0):>4}  "
              f"({100*mb.get('semantic',0)/max(s['extracted'],1):.1f}%)")
        print(f"    Hybrid    : {mb.get('hybrid',   0):>4}  "
              f"({100*mb.get('hybrid',0)/max(s['extracted'],1):.1f}%)")
        print(f"    Fallback  : {mb.get('fallback', 0):>4}  "
              f"({100*mb.get('fallback',0)/max(s['extracted'],1):.1f}%)")
        if mb.get("unknown", 0):
            print(f"    Unknown   : {mb.get('unknown',  0):>4}")

    print("=" * 70)

    if report["missing_names"]:
        print("\n  MISSING MEMBERS:")
        for name in sorted(report["missing_names"]):
            print(f"    - {name}")

    print("\n  EXTRACTED MEMBERS (sample — first 20):")
    extracted_members = [m for m in report["members"] if m["status"] == "extracted"]
    for m in extracted_members[:20]:
        fi = m["file_info"]
        lines = fi["line_count"] if fi else 0
        first = (fi["first_line"] or "")[:70] if fi else ""
        mt = m.get("match_type") or "?"
        score = m.get("match_score")
        score_str = f"  score={score:.1f}" if score is not None else ""
        print(f"    [{m['type']:10s}] {m['api_name']}")
        print(f"               {lines} lines | [{mt}{score_str}] {first}")

    if len(extracted_members) > 20:
        print(f"    ... and {len(extracted_members) - 20} more")
    print()


# ==============================================================================
# CLI entry point
# ==============================================================================

def _parse_args():
    p = argparse.ArgumentParser(description="Test extraction pipeline (Steps 1–3 only, no LLM or DB writes).")
    p.add_argument("--db-path", required=True, help="Path to MapCoDoc SQLite database")
    p.add_argument("--library-name", required=True, help="Library name (e.g. xgboost)")
    p.add_argument("--version", required=True, help="Library version (e.g. 3.2.0-dev)")

    # Source: exactly one of these
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url-file", help="Path to existing scraped_urls.txt (web mode)")
    src.add_argument("--pdf-path", help="Path to local PDF documentation file (PDF mode)")

    p.add_argument("--target-module", default=None, help="Filter members by API name prefix")
    p.add_argument("--names-file", default=None, help="File of sample API names (one per line). When given, only these members are extracted.")
    p.add_argument("--semantic-mode", default="auto", choices=["auto", "never", "always", "only"], help="Semantic search strategy (default: auto)")
    p.add_argument("--overwrite", action="store_true", help="Clear and re-extract per_member/ files even if they already exist")
    p.add_argument("--report-file", default=None, help="Optional path to save JSON report (e.g. results.json)")
    p.add_argument("--api-section-titles",
                   nargs="+",
                   default=None,
                   help=(
                    "Optional list of PDF section titles that mark API-reference chapters "
                    "(case-insensitive, whitespace-tolerant). When provided, replaces the "
                    "default keyword-based section detection. Example: "
                    '--api-section-titles "SQLAlchemy ORM" "SQLAlchemy Core" "SQLAlchemy Events"')
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    runner = ExtractionTestRunner(
        db_path=args.db_path,
        library_name=args.library_name,
        version=args.version,
        semantic_mode=args.semantic_mode,
        overwrite=args.overwrite,
        api_section_titles=args.api_section_titles,
        sample_names=_load_sample_names(args.names_file) if args.names_file else None
    )

    runner.run_extraction_test(
        url_file=args.url_file,
        pdf_path=args.pdf_path,
        target_module=args.target_module,
        report_file=args.report_file
    )