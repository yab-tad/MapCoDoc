"""
Documentation Processing Runner.

This module orchestrates the end-to-end documentation extraction workflow.
It serves as the bridge between the Database (targets) and the Extractors (PDF/Web).
"""

import os
import json
import logging
import asyncio
import requests
import shutil
from pathlib import Path
from sqlalchemy import text
from urllib.parse import urlparse
from collections import defaultdict
from typing import Set, List, Dict, Optional, Tuple

from mapcodoc_db.db_manager import MapCoDocDB
from mapcodoc_db.db_models import DBMember, DBInheritedMember
from mapcodoc_db.query import QueryManager, MemberDetails, InheritedMemberDetails
from doc_processor.process_crossRef import preprocess_crossRef, postprocess_crossRef
from doc_processor.filter_doc import StopSignalMatcher, WebMemberExtractor, WebMemberInfo
from doc_processor.structured_doc_extracter import DocumentationExtractor, ConcurrentDocExtractor
from doc_processor.web_doc.doc_scraper import scrape_doc
from doc_processor.web_doc.url_crawler import save_urls_to_file
from doc_processor.file_doc.embeddings import EmbeddingModel
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig
from doc_processor.file_doc.signature import MemberInput, build_lexical_needles
from doc_processor.file_doc.pipeline_pdf import extract_api_docs_from_pdf


logger = logging.getLogger(__name__)


class DocProcessingRunner:
    """
    Orchestrates the documentation extraction workflow.
    
    Directory Structure:
        doc_processor/doc_artifacts/
        ├── crawled_URLs/{lib_name}/{version}/       # Web: crawled URLs
        ├── local_doc/{lib_name}/{version}/          # PDF: local/downloaded PDFs
        ├── scraped_doc/{lib_name}/{version}/        # Raw extracted text
        │   ├── per_member/                          # One file per API
        │   ├── per_module/                          # One file per module/class
        │   └── per_page/                            # Single page with all APIs
        ├── preprocessed_doc/{lib_name}/{version}/   # URL placeholders applied
        │   ├── doc/                                 # Preprocessed text files
        │   └── url_context/                         # URL mapping JSONs
        ├── structured_doc/{lib_name}/{version}/     # LLM-structured JSONs
        └── postprocessed_doc/{lib_name}/{version}/  # Final JSONs with URLs restored
    """
    
    # Base artifacts directory
    ARTIFACTS_BASE = Path("doc_processor/doc_artifacts")
    
    def __init__(self, db_path: str, library_name: str, version: str):
        
        self.library_name = library_name
        self.version = version
        
        self.db_path = db_path
        self.db = MapCoDocDB(db_path)
        self.session = self.db.get_session()
        self.qm = QueryManager(self.session)
        
        # Initialize all path directories
        self._init_paths()
        
    def _init_paths(self):
        """Initialize all artifact paths for this library/version."""
        lib_version = f"{self.library_name}/v_{self.version}"
        
        # Web pipeline paths
        self.crawled_urls_dir = self.ARTIFACTS_BASE / "crawled_URLs" / lib_version
        
        # PDF pipeline paths  
        self.local_doc_dir = self.ARTIFACTS_BASE / "local_doc" / lib_version
        
        # Common paths (both pipelines)
        self.scraped_doc_dir = self.ARTIFACTS_BASE / "scraped_doc" / lib_version
        self.per_member_dir = self.scraped_doc_dir / "per_member"
        self.per_module_dir = self.scraped_doc_dir / "per_module"
        self.per_page_dir = self.scraped_doc_dir / "per_page"
        
        self.preprocessed_doc_dir = self.ARTIFACTS_BASE / "preprocessed_doc" / lib_version / "doc"
        self.url_context_dir = self.ARTIFACTS_BASE / "preprocessed_doc" / lib_version / "url_context"
        
        self.structured_doc_dir = self.ARTIFACTS_BASE / "structured_doc" / lib_version
        
        self.postprocessed_doc_dir = self.ARTIFACTS_BASE / "postprocessed_doc" / lib_version

    def _ensure_dirs(self, *dirs: Path):
        """Create directories if they don't exist."""
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
    
    def __del__(self):
        if hasattr(self, 'session'):
            self.session.close()
            
    @staticmethod
    def _first_sig(mi: MemberInput) -> str:
        """Return the most informative signature string for a member, or api_name as fallback."""
        
        for key in ('full', 'defaults_only', 'no_types', 'no_special', 'no_slash', 'no_asterisk', 'no_types_no_slash', 'no_types_no_asterisk'):
            if key in mi.signature_variants:
                return mi.signature_variants[key]
        if mi.signature_variants:
            return next(iter(mi.signature_variants.values()))
        
        return mi.api_name
    

    def run(self, doc_source: str, target_module: Optional[str] = None, skip_llm: bool = False, api_section_titles: Optional[List[str]] = None):
        """
        Run documentation extraction for a specific module against a source (PDF/URL).
        
        Args:
            doc_source: Path to PDF file OR URL to web documentation.
            target_module: Optional: Module prefix filter (default: use library_name).
            skip_llm: If True, skip LLM structuring and use raw scraped docs.
            api_section_titles: Optional list of section titles to treat as the
                authoritative API-reference chapter roots in the PDF (forwarded to APIReferenceLocator).
        """
        logger.info(f"Starting doc processing from source: {doc_source}")
        
        # 1. Retrieve Target Members
        members_db = self._get_target_members(target_module)
        
        if not members_db:
            logger.warning("No members found for documentation. Aborting.")
            return

        logger.info(f"Found {len(members_db)} members to document.")
        
        # Get inherited members for all classes
        class_members = [m for m in members_db if m.type == 'class']
        inherited_members = self._get_inherited_members_for_pipeline(class_members)
        
        if inherited_members:
            logger.info(f"Found {len(inherited_members)} inherited members to include.")
        
        # Convert DB objects to Pipeline Input objects
        pipeline_inputs = [
            MemberInput(
                api_name=m.api_name or m.fqn,  # Prefer traced API name, fallback to FQN
                signature_variants=m.signatures or {},
                member_type=m.type,
                docstring='' # docstring is optionally used for semantic query context
            )
            for m in members_db
        ]
        
        # Add inherited members to pipeline_inputs
        # Each inherited member uses its derived API name (via inheriting class)
        for inherited, original_member in inherited_members:
            inherited_input = self._inherited_to_member_input(inherited, original_member)
            pipeline_inputs.append(inherited_input)
        
        logger.info(f"Total pipeline inputs: {len(pipeline_inputs)} ({len(members_db)} direct + {len(inherited_members)} inherited)")

        # 2. Dispatch to Extractor (Steps 1-3)
        if self._is_pdf(doc_source):
            # Local PDF file
            self._run_pdf_pipeline(doc_source, pipeline_inputs, api_section_titles)
        elif self._is_pdf_url(doc_source):
            # Remote PDF - download first
            local_pdf = self._download_pdf(doc_source)
            try:
                self._run_pdf_pipeline(local_pdf, pipeline_inputs, api_section_titles)
            finally:
                os.unlink(local_pdf)  # Clean up temp file
        elif self._is_url(doc_source):
            # HTML documentation
            self._run_web_pipeline(doc_source, pipeline_inputs)
        else:
            raise ValueError(f"Unknown doc_source format: {doc_source}")

        # =================================================================
        # Filter pipeline_inputs to only members with extracted docs
        # =================================================================
        extracted_doc_names = set()
        if self.per_member_dir.exists():
            extracted_doc_names = {f.stem for f in self.per_member_dir.glob("*.txt")}

        if extracted_doc_names:
            # Filter to only members with matching extracted docs
            filtered_inputs = []
            for m, mi in zip(members_db, pipeline_inputs):
                # Collect all possible names for this member
                all_names = set(m.api_names or [])
                if m.api_name:
                    all_names.add(m.api_name)
                all_names.add(m.fqn)
                
                # Check if any name matches an extracted doc filename
                if all_names & extracted_doc_names:
                    filtered_inputs.append(mi)
            
            logger.info(f"Filtered from {len(pipeline_inputs)} to {len(filtered_inputs)} members with extracted docs")
            pipeline_inputs = filtered_inputs

        if not pipeline_inputs:
            logger.warning("No members with extracted documentation. Skipping Steps 4-7.")
            return
        
        # Determine if LLM should be skipped (explicit flag or missing API key)
        openai_key = os.environ.get("OPENAI_API_KEY")
        effective_skip_llm = skip_llm or not openai_key
        
        if effective_skip_llm:
            # Skip steps 4-6, update DB directly from scraped per_member docs
            logger.info("LLM processing skipped. Updating database with raw scraped docs.")
            self._update_database_from_raw(pipeline_inputs)
        else:
            # Full pipeline: preprocess -> LLM -> postprocess -> DB update
            # 3. Preprocess all per_member docs (Step 4)
            self._preprocess_all_members()
            
            # 4. Extract structured docs via LLM (Step 5)
            self._extract_structured_docs(pipeline_inputs)
            
            # 5. Postprocess - restore URLs (Step 6)
            self._postprocess_all_members()
            
            # 6. Update database (Step 7)
            self._update_database()
        
        logger.info("Documentation processing complete.")

    
    def _get_target_members(self, target_module: Optional[str] = None) -> List:
        """
        Get members to document using a prioritized strategy.
        
        Priority:
            1. If target_module specified, use it as prefix filter
            2. Try library_name as API name prefix (e.g., 'torch.%')
            3. Fallback to all public members (non-null API names)
        """
        if target_module:
            # User explicitly specified a module filter
            logger.info(f"Using explicit target module filter: {target_module}")
            return self.qm.get_members_for_doc_processing(target_module)
        
        # Try library_name as API prefix first (e.g., 'torch.Conv2d', 'numpy.array')
        logger.info(f"Querying members with API name prefix: {self.library_name}")
        members_db = self.qm.get_members_by_api_name_prefix(self.library_name)
        
        if members_db:
            logger.info(f"Found {len(members_db)} members with API prefix '{self.library_name}'")
            return members_db
        
        # Fallback: get ALL public members
        logger.info(f"No members found with prefix '{self.library_name}'. Fetching all public members.")
        members_db = self.qm.get_all_public_members()
        
        return members_db
    
    def _get_inherited_members_for_pipeline(self, class_members: List[MemberDetails]) -> List[Tuple[InheritedMemberDetails, MemberDetails]]:
        """
        Get all inherited members for classes in the member list.
        
        Returns a list of tuples: (inherited_member_info, original_member_details).
        Each inherited member is returned for EACH class that inherits it,
        allowing documentation to be linked under each inheriting class's API path.
        
        Args:
            class_members: List of MemberDetails (classes only)
            
        Returns:
            List of (InheritedMemberDetails, original_MemberDetails or None) tuples.
            The original member may be None if it's external.
        """
        inherited_results = []
        
        for class_member in class_members:
            if class_member.type != 'class':
                continue
            
            # Query inherited members for this class
            inherited_list = self.qm.get_inherited_members_for_class(class_member.fqn)
            
            for inherited in inherited_list:
                # Get original member details if internal
                original_member = None
                if inherited.original_member_id:
                    original_member = self.qm.get_original_member_for_inherited(
                        inherited.inherited_api_name
                    )
                
                inherited_results.append((inherited, original_member))
                
                logger.debug(
                    f"Including inherited member: {inherited.inherited_api_name} "
                    f"(from {inherited.source_class_fqn})"
                )
        
        return inherited_results


    def _inherited_to_member_input(
        self, 
        inherited: InheritedMemberDetails,
        original_member: Optional[MemberDetails] = None
    ) -> MemberInput:
        """
        Convert an InheritedMemberDetails to a MemberInput for pipeline processing.
        
        The key difference from regular members:
        - api_name uses the INHERITED path (e.g., 'xgboost.XGBRFClassifier.evals_result')
        - signature_variants come from the original definition
        - member_type is always 'method' (inherited members are typically methods)
        
        Args:
            inherited: InheritedMemberDetails from database
            original_member: Original member details (for signature variants)
            
        Returns:
            MemberInput ready for pipeline processing
        """
        # Get signature variants from original member or inherited signature dict
        signature_variants: Dict[str, str] = {}
        
        if original_member and original_member.signatures:
            signature_variants = original_member.signatures
        elif inherited.signature:
            # Extract signature strings from the signature dict
            if isinstance(inherited.signature, dict):
                signature_variants = {str(k): str(v) for k, v in inherited.signature.items()}
            elif isinstance(inherited.signature, str):
                signature_variants = {'full': inherited.signature}
        
        # Fallback: construct a minimal signature from the method name
        if not signature_variants:
            signature_variants = {'full': f"{inherited.member_name}("}
        
        return MemberInput(
            api_name=inherited.inherited_api_name,  # Use inherited path
            signature_variants=signature_variants,
            member_type=inherited.member_type or 'method',
            docstring=''  # Inherited members use original's docstring via lookup
        )
    
    # =========================================================================
    # Path/Type Helpers
    # =========================================================================
    
    def _is_pdf(self, source: str) -> bool:
        return source.lower().endswith('.pdf') and os.path.exists(source)

    def _is_url(self, source: str) -> bool:
        try:
            result = urlparse(source)
            return all([result.scheme, result.netloc])
        except:
            return False
        
    def _is_pdf_url(self, source: str) -> bool:
        """Check if URL points to a PDF file."""
        if not self._is_url(source):
            return False
        return source.lower().endswith('.pdf') or 'pdf' in source.lower()

    def _download_pdf(self, url: str) -> str:
        """Download PDF from URL to local_doc directory."""
        logger.info(f"Downloading PDF from: {url}")
        self._ensure_dirs(self.local_doc_dir)
        
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        
        # Extract filename from URL or use default
        filename = url.split('/')[-1]
        if not filename.endswith('.pdf'):
            filename = f"{self.library_name}_docs.pdf"
        
        local_path = self.local_doc_dir / filename
        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        logger.info(f"Saved PDF to: {local_path}")
        return str(local_path)
    
    def _sanitize_name(self, name: str) -> str:
        """Make a name filesystem-safe."""
        return name.replace(".", "_").replace("/", "_").replace("\\", "_").replace(":", "_")
    
    # =========================================================================
    # PDF Pipeline (Steps 1-3)
    # =========================================================================
    
    def _run_pdf_pipeline(self, pdf_path: str, members: List[MemberInput], api_section_titles: Optional[List[str]] = None):
        """
        PDF extraction pipeline.
        
        Step 1: Copy/store PDF to local_doc/
        Step 2: Extract via pipeline_pdf -> scraped_doc/per_member/
        
        Args:
            pdf_path: Path to local PDF file.
            members: MemberInput objects describing API members to locate.
            api_section_titles: Optional list of section titles to treat as the authoritative API-reference chapter roots in the PDF (forwarded to APIReferenceLocator).
        """
        logger.info("Invoking PDF extraction pipeline...")
        
        # Step 1: Ensure PDF is in local_doc directory
        self._ensure_dirs(self.local_doc_dir, self.per_member_dir)
        
        pdf_filename = Path(pdf_path).name
        local_pdf_path = self.local_doc_dir / pdf_filename
        
        if Path(pdf_path) != local_pdf_path:
            shutil.copy2(pdf_path, local_pdf_path)
            logger.info(f"Copied PDF to: {local_pdf_path}")
        
        # Build peer signatures for stop signal detection
        peer_signatures = self._build_peer_signatures(members)
        
        # Step 2: Run PDF extraction pipeline
        member_cfg = MemberExtractorConfig(semantic_mode="auto")
        
        # Output JSON and per-member txt files go to scraped_doc
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
            api_section_titles=api_section_titles
        )
        
        logger.info(f"PDF extraction complete. Results in: {self.scraped_doc_dir}")
    
    
    def _build_peer_signatures(self, members: List[MemberInput]) -> Dict[str, List[str]]:
        """
        Build peer signature map for stop signal detection.

        Peers are scoped structurally so that each member only receives signatures
        of members that would plausibly appear near it in the PDF/web docs.

        Four caches / indices:
        1. pipeline_needles     : {api_name -> List[str]}  exact needles per member
                                    (built once, reused by every scoped lookup)
        2. module_peers_cache   : {top_module -> List[str]} public exports per unique
                                    top-level module (same as before — supplements the
                                    structural indices with publicly re-exported names)
        3. class_members_cache  : {class_api_name -> List[str]} method/inherited
                                    needles for class-extraction fallback, sorted
                                    alphabetically (unchanged)
        4. siblings_by_class    : {class_fqn -> List[str]}  method/property/variable
                                    needles grouped by parent class
            top_level_by_module  : {module_fqn -> List[str]} class/function/variable
                                    needles grouped by parent module

        Peers assembled per target type:
        CLASS    : top_level_by_module[parent_module] + module_peers_cache[top_module]
                    + class_members_cache[api_name]  (own methods as alphabetical fallback)
        METHOD   : siblings_by_class[parent_class]                     (primary)
                + top_level_by_module[parent_module_of_class]         (fallback)
                + module_peers_cache[top_module]                      (fallback)
        FUNCTION : top_level_by_module[parent_module] + module_peers_cache[top_module]

        StopSignalMatcher filters self by short-name in its own __init__, so the
        shared-list assignments below are safe; the final dedup pass still removes
        the member's own signature variants from its peer list.

        Args:
            members: List of MemberInput objects (all members being processed).

        Returns:
            Dict mapping api_name -> list of peer signature strings.
        """

        # ── Cache 1: per-member exact needles ─────────────────────────────────────
        pipeline_needles: Dict[str, List[str]] = {}
        for mi in members:
            needles = build_lexical_needles(mi)
            pipeline_needles[mi.api_name] = needles.get("exact", [])

        # ── Structural indices (new): bucket pipeline members by parent scope ────
        # Methods / properties / variables with a dotted parent go into
        # siblings_by_class keyed by their parent class FQN.
        # Classes / functions / top-level variables go into top_level_by_module keyed
        # by their parent module FQN.
        siblings_by_class: Dict[str, List[str]] = defaultdict(list)
        top_level_by_module: Dict[str, List[str]] = defaultdict(list)

        for mi in members:
            exacts = pipeline_needles.get(mi.api_name, [])
            if not exacts:
                continue
            parent_fqn = mi.api_name.rsplit('.', 1)[0] if '.' in mi.api_name else ""
            if mi.member_type in ("method", "property"):
                siblings_by_class[parent_fqn].extend(exacts)
            elif mi.member_type == "variable":
                # Variables attached to a class act as siblings of methods; variables
                # at module level act as module-level peers. Heuristic: treat the
                # parent as a class if its last component starts with an upper-case
                # letter (CapWords), module otherwise.
                last = parent_fqn.rsplit('.', 1)[-1] if parent_fqn else ""
                if last and last[:1].isupper():
                    siblings_by_class[parent_fqn].extend(exacts)
                else:
                    top_level_by_module[parent_fqn].extend(exacts)
            else:  # class, function, or unknown → module-level
                top_level_by_module[parent_fqn].extend(exacts)

        # ── Cache 2: module-level public peer signatures (unchanged) ─────────────
        module_peers_cache: Dict[str, List[str]] = {}
        member_to_top_module: Dict[str, str] = {}

        for mi in members:
            exporting_modules = self.qm.get_exporting_modules_for_member(mi.api_name)
            if not exporting_modules:
                continue
            top_module = exporting_modules[0]
            member_to_top_module[mi.api_name] = top_module

            if top_module not in module_peers_cache:
                module_sigs: List[str] = []
                for p in self.qm.get_public_peers(top_module):
                    peer_api_name = p.target_api_name or f"{p.exporter_module}.{p.exported_name}"
                    peer_member = MemberInput(
                        api_name=peer_api_name,
                        signature_variants=p.signatures,
                        member_type=p.target_type or "function",
                    )
                    peer_needles = build_lexical_needles(peer_member)
                    module_sigs.extend(peer_needles.get("exact", []))
                module_peers_cache[top_module] = module_sigs

        # ── Cache 3: class method/inherited signatures (unchanged) ───────────────
        class_members_cache: Dict[str, List[str]] = {}

        # ── Assemble per-member peer lists ───────────────────────────────────────
        peer_signatures: Dict[str, List[str]] = {}

        for mi in members:
            peer_sigs: List[str] = []
            parent_fqn = mi.api_name.rsplit('.', 1)[0] if '.' in mi.api_name else ""

            # --- Module-level public exports (from Cache 2) ---
            top_module = member_to_top_module.get(mi.api_name)
            if top_module:
                peer_sigs.extend(module_peers_cache[top_module])

            # --- Structural peers based on target member type ---
            if mi.member_type == "method" or mi.member_type == "property":
                # Primary: sibling methods/properties of the same parent class
                peer_sigs.extend(siblings_by_class.get(parent_fqn, []))
                # Fallback: module-level peers of the class's parent module
                grandparent = parent_fqn.rsplit('.', 1)[0] if '.' in parent_fqn else ""
                if grandparent:
                    peer_sigs.extend(top_level_by_module.get(grandparent, []))

            elif mi.member_type == "class":
                # Primary: other classes/functions in the same module
                peer_sigs.extend(top_level_by_module.get(parent_fqn, []))
                # Fallback: own methods / inherited members (Cache 3, alphabetical)
                if mi.api_name not in class_members_cache:
                    class_members_cache[mi.api_name] = self._get_class_member_signatures(
                        mi.api_name
                    )
                peer_sigs.extend(class_members_cache[mi.api_name])

            else:  # function, variable, or unknown
                # Primary: other top-level members in the same module
                peer_sigs.extend(top_level_by_module.get(parent_fqn, []))

            # --- Filter own signatures and deduplicate ---
            own_sigs = set(mi.signature_variants.values())
            seen: set = set()
            deduped: List[str] = []
            for sig in peer_sigs:
                if sig not in own_sigs and sig not in seen:
                    seen.add(sig)
                    deduped.append(sig)

            peer_signatures[mi.api_name] = deduped

        return peer_signatures
    
    
    # def _build_peer_signatures(self, members: List[MemberInput]) -> Dict[str, List[str]]:
    #     """
    #     Build peer signature map for stop signal detection.

    #     Optimised with three caches to avoid redundant DB queries and needle builds:
    #     1. pipeline_needles:    {api_name -> List[str]}  exact needles for every
    #                             pipeline member, built once and reused.
    #     2. module_peers_cache:  {module_fqn -> List[str]} public export needles,
    #                             queried and built once per unique top-level module.
    #     3. class_members_cache: {class_api_name -> List[str]} method/inherited
    #                             needles for class fallback stops, built once per class.
        
    #     The StopSignalMatcher automatically classifies these into primary/fallback based on
    #     naming conventions (uppercase = class, lowercase = method).
        
    #     Args:
    #         members: List of MemberInput objects (all members being processed)
            
    #     Returns:
    #         Dict mapping api_name -> list of peer signature strings
    #     """

    #     # ── Cache 1: pipeline member exact needles (build once, reuse N times) ──
    #     pipeline_needles: Dict[str, List[str]] = {}
    #     for mi in members:
    #         needles = build_lexical_needles(mi)
    #         pipeline_needles[mi.api_name] = needles.get("exact", [])

    #     # ── Cache 2: module-level public peer signatures ─────────────────────────
    #     module_peers_cache: Dict[str, List[str]] = {}
    #     member_to_top_module: Dict[str, str] = {}

    #     for mi in members:
    #         exporting_modules = self.qm.get_exporting_modules_for_member(mi.api_name)
    #         if not exporting_modules:
    #             continue
    #         top_module = exporting_modules[0]
    #         member_to_top_module[mi.api_name] = top_module

    #         if top_module not in module_peers_cache:
    #             module_sigs: List[str] = []
    #             for p in self.qm.get_public_peers(top_module):
    #                 peer_api_name = p.target_api_name or f"{p.exporter_module}.{p.exported_name}"
    #                 peer_member = MemberInput(
    #                     api_name=peer_api_name,
    #                     signature_variants=p.signatures,
    #                     member_type=p.target_type or "function",
    #                 )
    #                 peer_needles = build_lexical_needles(peer_member)
    #                 module_sigs.extend(peer_needles.get("exact", []))
    #             module_peers_cache[top_module] = module_sigs

    #     # ── Cache 3: class method/inherited signatures (built once per class) ────
    #     class_members_cache: Dict[str, List[str]] = {}

    #     # ── Assemble per-member peer lists ───────────────────────────────────────
    #     peer_signatures: Dict[str, List[str]] = {}

    #     for mi in members:
    #         peer_sigs: List[str] = []

    #         # --- Module-level public exports (from cache) ---
    #         top_module = member_to_top_module.get(mi.api_name)
    #         if top_module:
    #             peer_sigs.extend(module_peers_cache[top_module])

    #         # --- Class methods/inherited as fallback stops (classes only) ---
    #         if mi.member_type == "class":
    #             if mi.api_name not in class_members_cache:
    #                 class_members_cache[mi.api_name] = self._get_class_member_signatures(
    #                     mi.api_name
    #                 )
    #             peer_sigs.extend(class_members_cache[mi.api_name])

    #         # --- Other pipeline members ---
    #         # Use pre-built cache; exclude self by api_name (matching original logic)
    #         for peer_api_name, peer_exact_needles in pipeline_needles.items():
    #             if peer_api_name == mi.api_name:
    #                 continue
    #             peer_sigs.extend(peer_exact_needles)

    #         # --- Filter own signatures and deduplicate ---
    #         own_sigs = set(mi.signature_variants.values())
    #         seen: set = set()
    #         deduped: List[str] = []
    #         for sig in peer_sigs:
    #             if sig not in own_sigs and sig not in seen:
    #                 seen.add(sig)
    #                 deduped.append(sig)

    #         peer_signatures[mi.api_name] = deduped

    #     return peer_signatures


    def _get_class_member_signatures(self, class_api_name: str) -> List[str]:
        """
        Get all method and inherited member signatures for a class.
        
        These are used as fallback stop signals for class extraction - when no
        other class/function is found within max_chars, the extractor falls back
        to using the class's own methods as boundaries.
        
        Args:
            class_api_name: API name of the class
            
        Returns:
            List of signature strings for methods and inherited members
        """
        signatures = []
        
        # Get class from database
        db_class = self.qm.get_member_by_any_api_name(class_api_name)
        if not db_class or db_class.type != 'class':
            return signatures
        
        # --- Add direct method signatures ---
        class_methods = self.qm.get_class_methods(db_class.fqn)
        for method in class_methods:
            method_input = MemberInput(
                api_name=method.api_name or method.fqn,
                signature_variants=method.signatures or {},
                member_type='method'
            )
            needles = build_lexical_needles(method_input)
            signatures.extend(needles.get("exact", []))
        
        # --- Add inherited member signatures ---
        inherited_members = self.qm.get_inherited_members_for_class(db_class.fqn)
        for inherited in inherited_members:
            # Get signatures from original member or inherited data
            sig_variants = {}
            
            if inherited.original_member_id:
                original = self.qm.get_original_member_for_inherited(inherited.inherited_api_name)
                if original:
                    sig_variants = original.signatures or {}
            
            if not sig_variants and inherited.signature:
                if isinstance(inherited.signature, dict):
                    sig_variants = {str(k): str(v) for k, v in inherited.signature.items()}
                elif isinstance(inherited.signature, str):
                    sig_variants = {'full': inherited.signature}
            
            if not sig_variants:
                sig_variants = {'full': f"{inherited.member_name}("}
            
            inherited_input = MemberInput(
                api_name=inherited.inherited_api_name,
                signature_variants=sig_variants,
                member_type=inherited.member_type or 'method'
            )
            needles = build_lexical_needles(inherited_input)
            signatures.extend(needles.get("exact", []))
        
        # Sort signatures alphabetically by their short name. API docs typically list methods in alphabetical order
        def _short_name_key(sig: str) -> str:
            return sig.split('(')[0].strip().split('.')[-1].lower()
        signatures.sort(key=_short_name_key)
        
        return signatures
    
    # =========================================================================
    # Web Pipeline (Steps 1-3)
    # =========================================================================
    
    def _run_web_pipeline(self, url: str, members: List[MemberInput]):
        """
        Web documentation extraction pipeline.
        
        Step 1: Crawl URLs -> crawled_URLs/{lib}/{version}/
        Step 2: Scrape HTML -> scraped_doc/{lib}/{version}/
        Step 3: Extract per-member -> scraped_doc/{lib}/{version}/per_member/
        
        Args:
            url: URL of the documentation site
            members: List of MemberInput objects
        """
        logger.info("Invoking Web extraction pipeline...")
        
        # =================================================================
        # Step 1: Crawl URLs from the documentation site
        # =================================================================
        self._ensure_dirs(self.crawled_urls_dir)
        
        url_file, stat_info = asyncio.run(
            save_urls_to_file(url, self.library_name, self.version)
        )
        
        if not url_file:
            logger.warning("No URLs found during crawl. Aborting web pipeline.")
            return
        
        # =================================================================
        # Step 2: Scrape HTML pages to raw text
        # =================================================================
        self._ensure_dirs(self.scraped_doc_dir, self.per_member_dir)
        
        asyncio.run(scrape_doc(self.library_name, self.version, url_file, stat_info))
        
        # =================================================================
        # Step 3: Extract individual member docs from combined pages
        # =================================================================
        member_map, primary_to_info = self._build_member_map(members)
        
        model_name = "intfloat/e5-base-v2"
        cfg = MemberExtractorConfig(
            semantic_mode="auto",
            window_chars=3000,
            window_stride=2000
        )
        embedder = None
        extractor = None
        extracted_apis: Set[str] = set()
        
        # Track combined doc files for later cleanup
        combined_doc_files: Set[Path] = set()
        
        # Step 3a. Track existing per_member files
        if self.per_member_dir.exists():
            for txt_file in self.per_member_dir.glob("*.txt"):
                extracted_apis.add(txt_file.stem)
        
        # Step 3b. Handle per_module (module OR class pages)
        members_json_path = self.per_module_dir / "members.json"
        
        # Track containers for later filtering
        containers_to_filter: List[Tuple[Path, str, List[str]]] = []
        
        if self.per_module_dir.exists() and members_json_path.exists():
            with open(members_json_path, 'r') as f:
                module_members_map = json.load(f)
            
            for container_name, nested_api_names in module_members_map.items():
                # Look for combined doc in BOTH per_member/ and per_module/
                module_txt = self.per_member_dir / f"{container_name}.txt"
                if not module_txt.exists():
                    # Fallback: check per_module/ (scraper may have put it there)
                    module_txt = self.per_module_dir / f"{container_name}.txt"
                
                if not module_txt.exists():
                    logger.debug(f"No combined doc found for container '{container_name}'")
                    continue
                
                # Track this as a combined doc file
                combined_doc_files.add(module_txt)
            
                combined_text = module_txt.read_text(encoding='utf-8')
                
                # Initialize embedder/extractor lazily
                if embedder is None and cfg.semantic_mode != "never":
                    embedder = EmbeddingModel(model_name, cache_dir=str(self.ARTIFACTS_BASE / ".cache"))
                    extractor = WebMemberExtractor(cfg, embedder)
                elif extractor is None:
                    extractor = WebMemberExtractor(cfg, None)
                
                # Build extraction list
                members_to_extract = self._build_extraction_list(nested_api_names, member_map, extracted_apis)
                
                # Also extract the container itself
                container_info = self._get_or_create_member_info(container_name, member_map, extracted_apis)
                
                all_to_extract = []
                if container_info:
                    all_to_extract.append(container_info)
                all_to_extract.extend(members_to_extract)
                
                # Extract and save
                self._extract_and_save_members(
                    combined_text=combined_text,
                    members_to_extract=all_to_extract,
                    output_dir=self.per_member_dir,
                    extractor=extractor,
                    model_name=model_name,
                    extracted_apis=extracted_apis
                )
                
                # Record for later filtering (only if container is an API member)
                containers_to_filter.append((module_txt, container_name, nested_api_names))
        
        
        # Step 3c. Handle per_page (all APIs on single page)
        per_page_json = self.per_page_dir / "members.json"
        
        if self.per_page_dir.exists() and per_page_json.exists():
            with open(per_page_json, 'r') as f:
                page_data = json.load(f)
            
            apis_txt = self.per_page_dir / "APIs.txt"
            if apis_txt.exists():
                combined_doc_files.add(apis_txt)  # Track as combined doc
                
                combined_text = apis_txt.read_text(encoding='utf-8')
                api_names = page_data.get("API_names", [])
                
                # Initialize extractor
                if embedder is None and cfg.semantic_mode != "never":
                    embedder = EmbeddingModel(model_name, cache_dir=str(self.ARTIFACTS_BASE / ".cache"))
                    extractor = WebMemberExtractor(cfg, embedder)
                elif extractor is None:
                    extractor = WebMemberExtractor(cfg, None)
                
                members_to_extract = self._build_extraction_list(api_names, member_map, extracted_apis)
                
                # --- SINGLE-PASS EXTRACTION WITH INCREMENTAL ANCHOR BUILDING ---
                # Process members in members.json order (classes naturally come before methods)
                # Build non-method anchors incrementally, use them to scope method extraction
                self._extract_per_page_with_class_anchors(
                    combined_text=combined_text,
                    members_to_extract=members_to_extract,
                    output_dir=self.per_member_dir,
                    extractor=extractor,
                    model_name=model_name,
                    extracted_apis=extracted_apis,
                    cfg=cfg
                )
        
        # Step 3d: Fallback - Extract missing methods from class docs
        # Ensure extractor is initialized (may be None if per_module/per_page blocks were skipped)
        if extractor is None:
            if cfg.semantic_mode != "never":
                if embedder is None:
                    embedder = EmbeddingModel(model_name, cache_dir=str(self.ARTIFACTS_BASE / ".cache"))
                extractor = WebMemberExtractor(cfg, embedder)
            else:
                extractor = WebMemberExtractor(cfg, None)
        
        self._extract_missing_methods_from_class_docs(
            containers_to_filter=containers_to_filter,
            extracted_apis=extracted_apis,
            extractor=extractor,
            model_name=model_name
        )
        
        # Step 3e: NOW filter container docs (after all extractions complete)
        for module_txt, container_name, nested_api_names in containers_to_filter:
            self._filter_container_doc(module_txt, container_name, nested_api_names)
        
        # Step 3f: Move combined docs out of per_member/ to combined/
        self._relocate_combined_docs(combined_doc_files, extracted_apis)
        
        logger.info(f"Web extraction complete. Per-member docs in: {self.per_member_dir}")
    
    
    def _extract_per_page_with_class_anchors(
        self,
        combined_text: str,
        members_to_extract: List[WebMemberInfo],
        output_dir: Path,
        extractor: WebMemberExtractor,
        model_name: str,
        extracted_apis: Set[str],
        cfg: MemberExtractorConfig
    ) -> None:
        """
        Extract members from per_page docs with incremental class anchor propagation.
        
        This method processes members in their natural members.json order, where
        classes appear before their methods. It builds a map of non-method anchor
        positions incrementally, then uses these anchors to scope method searches.
        
        Approach:
            1. Leverages natural ordering from members.json
            2. Builds anchor map incrementally (no separate phase)
            3. Uses actual anchor positions instead of pattern-based detection
            4. Includes functions as scope boundaries, not just classes
        
        Args:
            combined_text: Full text of the combined per_page document
            members_to_extract: List of WebMemberInfo objects in members.json order
            output_dir: Directory to save extracted {API_name}.txt files
            extractor: WebMemberExtractor instance for anchor finding
            model_name: Embedding model name for semantic search
            extracted_apis: Set tracking already extracted APIs (modified in place)
            cfg: Extractor configuration with thresholds
            
        NOTE: This method relies on member_type being correctly set in each WebMemberInfo. 
        The member_type is determined in _build_extraction_list():
            - Primary: Database lookup via get_member_by_any_api_name()
            - Fallback: search_members() by short name
            - Last resort: Heuristic based on naming convention (may be inaccurate)
        For best results, ensure the database is populated before doc extraction.
        """
        if not members_to_extract:
            return
        
        logger.info(f"Extracting {len(members_to_extract)} members from per_page doc with class anchoring...")
        
        # =====================================================================
        # PHASE 1: Find anchor positions for ALL members in batch
        # This is efficient as it reuses embeddings across members
        # =====================================================================
        batch_results = extractor.extract_batch(combined_text, members_to_extract, model_name)
        
        # =====================================================================
        # PHASE 2: Build non-method anchor map from batch results
        # Non-methods (classes, functions) serve as scope boundaries for methods
        # =====================================================================
        non_method_anchors: Dict[str, int] = {}  # {api_name: position}
        anchor_positions_sorted: List[Tuple[str, int]] = []  # [(api_name, pos)] sorted by position
        
        for info in members_to_extract:
            if info.member_type == 'method':
                continue  # Methods don't create boundaries
            
            result = batch_results.get(info.api_name, (-1, 0.0, "none"))
            pos, score, match_type = result
            
            # Only record if found with sufficient confidence
            if pos >= 0 and match_type != "none" and score >= cfg.min_lexical_score:
                non_method_anchors[info.api_name] = pos
                anchor_positions_sorted.append((info.api_name, pos))
        
        # Sort anchors by position for efficient scope_end calculation
        anchor_positions_sorted.sort(key=lambda x: x[1])
        
        logger.debug(f"Built anchor map with {len(non_method_anchors)} non-method members")
        
        # =====================================================================
        # PHASE 3: Process each member with appropriate scoping
        # =====================================================================
        
        # Build list of successfully anchored members with their positions
        member_positions: List[Dict] = []
        
        for info in members_to_extract:
            if info.api_name in extracted_apis:
                continue
            
            result = batch_results.get(info.api_name, (-1, 0.0, "none"))
            pos, score, match_type = result
            
            if info.member_type == 'method':
                # --- METHOD: Use parent class anchor for scoping ---
                scoped_result = self._get_scoped_method_position(
                    combined_text=combined_text,
                    method_info=info,
                    batch_result=result,
                    non_method_anchors=non_method_anchors,
                    anchor_positions_sorted=anchor_positions_sorted,
                    extractor=extractor,
                    model_name=model_name,
                    cfg=cfg
                )
                
                if scoped_result:
                    pos, score, match_type = scoped_result
            
            # Apply threshold check
            if pos < 0 or match_type == "none":
                logger.debug(f"Skipping {info.api_name}: not found (match_type={match_type})")
                continue
            
            if score < cfg.min_lexical_score:
                logger.debug(f"Skipping {info.api_name}: score {score:.1f} below threshold")
                continue
            
            member_positions.append({
                "info": info,
                "position": pos,
                "score": score,
                "match_type": match_type
            })
        
        if not member_positions:
            logger.debug("No members met extraction threshold")
            return
        
        # =====================================================================
        # PHASE 4: Sort by position and extract with stop signals
        # =====================================================================
        member_positions.sort(key=lambda x: x["position"])
        
        for i, mp in enumerate(member_positions):
            info = mp["info"]
            start_pos = mp["position"]
            
            # Build stop signals from subsequent members in position order
            peer_sigs = []
            for peer_mp in member_positions[i+1:]:
                peer_info = peer_mp["info"]
                peer_needles = build_lexical_needles(peer_info.member_input)
                peer_sigs.extend(peer_needles.get("exact", []))
            
            stop_matcher = StopSignalMatcher(
                peer_signatures=peer_sigs,
                target_member_type=info.member_type,
                target_api_name=info.api_name
            )
            
            # Extract text from anchor position
            extracted = self._extract_until_stop(
                combined_text[start_pos:],
                stop_matcher,
                max_chars=25000
            )
            
            # Save to file
            if extracted and len(extracted.strip()) > 50:
                # Ensure output directory exists (defensive check)
                output_dir.mkdir(parents=True, exist_ok=True)
                
                output_file = output_dir / f"{info.api_name}.txt"
                output_file.write_text(extracted, encoding='utf-8')
                extracted_apis.add(info.api_name)
                logger.debug(f"Saved: {info.api_name}")
        
        logger.info(f"Extracted {len([m for m in member_positions if m['info'].api_name in extracted_apis])} members from per_page doc")
    
    
    def _get_scoped_method_position(
        self,
        combined_text: str,
        method_info: WebMemberInfo,
        batch_result: Tuple[int, float, str],
        non_method_anchors: Dict[str, int],
        anchor_positions_sorted: List[Tuple[str, int]],
        extractor: WebMemberExtractor,
        model_name: str,
        cfg: MemberExtractorConfig
    ) -> Optional[Tuple[int, float, str]]:
        """
        Get method position scoped to its parent class region.
        
        This prevents methods like 'fit()' from matching the wrong class's
        documentation when multiple classes have methods with the same name.
        
        Scoping logic:
            - Start: Parent class anchor position
            - End: Next non-method (class/function) anchor position, or text end
        
        If the method is found within scope with better confidence than the
        unscoped batch result, return the scoped position. Otherwise, return
        the original batch result (may be from wrong class but better than nothing).
        
        Args:
            combined_text: Full document text
            method_info: The method to extract
            batch_result: (pos, score, match_type) from initial batch extraction
            non_method_anchors: Map of {api_name: position} for classes/functions
            anchor_positions_sorted: List of (api_name, pos) sorted by position
            extractor: WebMemberExtractor for scoped search
            model_name: Embedding model name
            cfg: Configuration with thresholds
        
        Returns:
            (position, score, match_type) tuple, or None if not found
        """
        api_name = method_info.api_name
        
        # Parse parent class from method name (e.g., "xgboost.Booster.predict" -> "xgboost.Booster")
        parts = api_name.rsplit('.', 1)
        if len(parts) < 2:
            # Not a qualified method name - return batch result
            return batch_result if batch_result[0] >= 0 else None
        
        parent_class_name = parts[0]
        
        # --- Look up parent class in anchor map ---
        parent_anchor_pos: Optional[int] = None
        matched_parent: Optional[str] = None
        
        # Try exact match first
        if parent_class_name in non_method_anchors:
            parent_anchor_pos = non_method_anchors[parent_class_name]
            matched_parent = parent_class_name
        else:
            # Try partial match (handles re-exports like "torch.nn.Conv2d" vs "Conv2d")
            parent_short = parent_class_name.split('.')[-1]
            for anchor_name, pos in non_method_anchors.items():
                if anchor_name.endswith(f'.{parent_short}') or anchor_name == parent_short:
                    parent_anchor_pos = pos
                    matched_parent = anchor_name
                    break
        
            # ------------------------------------------------------------------------
            # For inherited members, also try matching with any class that might document this inherited method 
            # (useful when docs group inherited methods under the original class instead of inheriting class)
            # ------------------------------------------------------------------------
            if parent_anchor_pos is None:
                # Try to find any class anchor that could contain this method
                # by checking if the inheriting class is related to any anchored class
                method_short_name = method_info.api_name.split('.')[-1]
                
                # Check if this is an inherited member by seeing if any other class
                # anchor contains a method with the same short name
                for anchor_name, pos in sorted(non_method_anchors.items(), key=lambda x: x[1]):
                    # Look for a class that might document this method
                    if anchor_name.split('.')[-1][0].isupper():  # Is a class
                        parent_anchor_pos = pos
                        matched_parent = anchor_name
                        logger.debug(f"Inherited member {method_info.api_name}: using nearest class anchor '{anchor_name}' as scope")
                        break
        
        if parent_anchor_pos is None:
            # Parent class not found in anchors - return batch result
            logger.debug(f"Parent class '{parent_class_name}' not in anchor map for method {api_name}")
            return batch_result if batch_result[0] >= 0 else None
        
        # --- Calculate scope boundaries ---
        scope_start = parent_anchor_pos
        scope_end = len(combined_text)  # Default to end of document
        
        # Find next non-method anchor after parent class
        for anchor_name, anchor_pos in anchor_positions_sorted:
            if anchor_pos > parent_anchor_pos:
                # Found next non-method - this is our scope end
                scope_end = anchor_pos
                break
        
        logger.debug(f"Method {api_name} scoped to parent '{matched_parent}': chars {scope_start}-{scope_end}")
        
        # --- Search for method within scoped region ---
        scoped_text = combined_text[scope_start:scope_end]
        
        if not scoped_text.strip():
            return batch_result if batch_result[0] >= 0 else None
        
        # Use extractor to find method in scoped text
        scoped_result = extractor.find_anchor_position(scoped_text, method_info, model_name)
        scoped_pos, scoped_score, scoped_match_type = scoped_result
        
        if scoped_pos < 0 or scoped_match_type == "none":
            # Not found in scope - fall back to batch result
            # (batch result might be from wrong class, but better than nothing)
            logger.debug(f"Method {api_name} not found in parent scope, using batch result")
            return batch_result if batch_result[0] >= 0 else None
        
        # Convert scoped position to absolute position
        absolute_pos = scope_start + scoped_pos
        
        # --- Decide: use scoped result or batch result? ---
        batch_pos, batch_score, batch_match_type = batch_result
        
        # Prefer scoped result if:
        # 1. It has reasonable confidence, AND
        # 2. It's within the expected scope
        if scoped_score >= cfg.min_lexical_score:
            logger.debug(f"Method {api_name} found at pos {absolute_pos} (scoped, score={scoped_score:.1f})")
            return (absolute_pos, scoped_score, scoped_match_type)
        
        # If scoped result is weak but batch was good, check if batch position is in scope
        if batch_pos >= 0 and batch_score >= cfg.min_lexical_score:
            if scope_start <= batch_pos < scope_end:
                # Batch result is within scope - use it
                logger.debug(f"Method {api_name} using batch result at pos {batch_pos} (in scope)")
                return batch_result
            else:
                # Batch result is OUTSIDE scope - likely wrong class, reject
                logger.debug(f"Method {api_name} batch result at {batch_pos} is outside scope {scope_start}-{scope_end}, rejecting")
                return None
        
        # Neither result is good enough
        return None
    
    
    def _extract_missing_methods_from_class_docs(
        self,
        containers_to_filter: List[Tuple[Path, str, List[str]]],
        extracted_apis: Set[str],
        extractor: WebMemberExtractor,
        model_name: str
    ) -> None:
        """
        Extract missing methods (including inherited) from parent class's combined doc.
        
        Uses get_class_methods AND get_inherited_members_for_class to properly
        query all methods belonging to each class (direct + inherited).
        Only extracts public/special methods that haven't been extracted yet.
        """
        total_extracted = 0
        
        for module_txt, container_name, nested_api_names in containers_to_filter:
            # --- Determine if container is a class ---
            db_class = self.qm.get_member_by_any_api_name(container_name)
            
            if not db_class:
                # Try fallback search by short name
                short_name = container_name.split('.')[-1] if '.' in container_name else container_name
                search_results = self.qm.search_members(short_name, limit=5)
                for result in search_results:
                    if result.type == 'class' and result.fqn.split('.')[-1] == short_name:
                        db_class = result
                        break
            
            if not db_class or db_class.type != 'class':
                continue
            
            # --- Get all directly-defined methods for this class ---
            class_methods = self.qm.get_class_methods(db_class.fqn)
            
            # --- Also get inherited methods for this class ---
            inherited_methods = self.qm.get_inherited_members_for_class(db_class.fqn)
            
            if not class_methods and not inherited_methods:
                logger.debug(f"No methods (direct or inherited) found for class {db_class.fqn}")
                continue
            
            # --- Build list of missing methods (direct) ---
            missing_members = []
            
            # Check direct methods
            for method in class_methods:
                # Check if already extracted (by any name variant)
                all_names = set(method.api_names or [])
                all_names.add(method.api_name or '')
                all_names.add(method.fqn)
                all_names.discard('')
                
                if all_names & extracted_apis:
                    continue  # Already extracted
                
                # Only include public or special methods
                if method.access_modifier not in ('public', 'special', None):
                    continue  # Skip private/protected
                
                # Create WebMemberInfo for direct method
                missing_members.append(WebMemberInfo(
                    api_name=method.api_name or method.fqn,
                    all_api_names=all_names,
                    member_input=MemberInput(
                        api_name=method.api_name or method.fqn,
                        signature_variants=method.signatures or {},
                        member_type='method'
                    ),
                    member_type='method'
                ))
            
            # Check inherited methods
            for inherited in inherited_methods:
                all_names = set(inherited.inherited_api_names or [])
                all_names.add(inherited.inherited_api_name or '')
                if inherited.original_api_names:
                    all_names.update(inherited.original_api_names)
                all_names.discard('')
                
                if all_names & extracted_apis:
                    continue
                
                # Get signature variants
                sig_variants = {}
                if inherited.original_member_id:
                    original = self.qm.get_original_member_for_inherited(inherited.inherited_api_name)
                    if original:
                        sig_variants = original.signatures or {}
                
                if not sig_variants and inherited.signature:
                    if isinstance(inherited.signature, dict):
                        sig_variants = {str(k): str(v) for k, v in inherited.signature.items()}
                    elif isinstance(inherited.signature, str):
                        sig_variants = {'full': inherited.signature}
                
                if not sig_variants:
                    sig_variants = {'full': f"{inherited.member_name}("}
                
                missing_members.append(WebMemberInfo(
                    api_name=inherited.inherited_api_name,
                    all_api_names=all_names,
                    member_input=MemberInput(
                        api_name=inherited.inherited_api_name,
                        signature_variants=sig_variants,
                        member_type=inherited.member_type or 'method'
                    ),
                    member_type=inherited.member_type or 'method'
                ))
            
            if not missing_members:
                logger.debug(f"No missing methods for class {db_class.fqn}")
                continue
            
            # --- Read class doc ---
            original_module_txt = self.per_module_dir / f"{container_name}.txt"
            if original_module_txt.exists():
                source_txt = original_module_txt
            elif module_txt.exists():
                source_txt = module_txt
            else:
                logger.debug(f"Class doc not found for fallback extraction: {module_txt}")
                continue
            class_doc_text = source_txt.read_text(encoding='utf-8')
            
            # if not module_txt.exists():
            #     logger.debug(f"Class doc not found: {module_txt}")
            #     continue
            
            # class_doc_text = module_txt.read_text(encoding='utf-8')
            
            logger.info(f"Extracting {len(missing_members)} missing methods (direct + inherited) from class {db_class.fqn}")
            
            # Extract and save
            self._extract_and_save_members(
                combined_text=class_doc_text,
                members_to_extract=missing_members,
                output_dir=self.per_member_dir,
                extractor=extractor,
                model_name=model_name,
                extracted_apis=extracted_apis
            )
            
            total_extracted += len(missing_members)
        
        if total_extracted > 0:
            logger.info(f"Extracted {total_extracted} missing methods from class docs")
    
    
    def _build_member_map(self, members: List[MemberInput]) -> tuple:
        """Build flexible member map using ALL API names."""
        member_map: Dict[str, WebMemberInfo] = {}
        primary_to_info: Dict[str, WebMemberInfo] = {}
        
        for m in members:
            all_names = set(getattr(m, 'api_names', []) or [])
            all_names.add(m.api_name)
            
            info = WebMemberInfo(
                api_name=m.api_name,
                all_api_names=all_names,
                member_input=m,
                member_type=m.member_type
            )
            
            primary_to_info[m.api_name] = info
            
            for name in all_names:
                member_map[name.lower()] = info
        
        return member_map, primary_to_info

    def _build_extraction_list(
        self, 
        api_names: List[str], 
        member_map: Dict[str, WebMemberInfo],
        extracted_apis: Set[str]
    ) -> List[WebMemberInfo]:
        """
        Build extraction list from API names, resolving each via database.

        For each API name:
            1. Try direct member lookup
            2. Try inherited member lookup
            3. Fall back to local member_map
            4. Search by short name
            5. Create dummy as last resort
        
        Args:
            api_names: List of API names from members.json
            member_map: Pre-built map from _build_member_map
            extracted_apis: Set of already extracted API names
        
        Returns:
            List of WebMemberInfo objects ready for extraction
        """
        members_to_extract = []
        
        for api_name in api_names:
            # Skip already extracted
            if api_name in extracted_apis:
                continue
            
            # --- Filter invalid entries ---
            # Skip module references like "module-pygame.cdrom"
            if api_name.startswith("module-"):
                logger.debug(f"Skipping module reference: {api_name}")
                continue
            
            # Skip entries without '.' - not valid FQNs (underscores are okay)
            if '.' not in api_name:
                logger.debug(f"Skipping invalid FQN (no '.'): {api_name}")
                continue
            
            info = None
            
            # --- Strategy 1: Try direct member lookup via ANY API name ---
            db_member = self.qm.get_member_by_any_api_name(api_name)
            if db_member:
                
                # Determine which api_name to use for output
                # If the members.json api_name is in all_api_names, use it.
                member_all_api_names = set(db_member.api_names or [])
                member_all_api_names.add(db_member.api_name or '')
                member_all_api_names.add(db_member.fqn)
                member_all_api_names.discard('')
                
                # Use members.json api_name if it's a known alias, else use primary
                if api_name in member_all_api_names:
                    output_api_name = api_name  # Preserve original
                else:
                    output_api_name = db_member.api_name or db_member.fqn
                
                info = WebMemberInfo(
                    api_name=output_api_name,  # Use determined name
                    all_api_names=member_all_api_names | {api_name},
                    member_input=MemberInput(
                        api_name=output_api_name,
                        signature_variants=db_member.signatures,
                        member_type=db_member.type
                    ),
                    member_type=db_member.type
                )
                logger.debug(f"Matched '{api_name}' via DB -> using output name '{output_api_name}'")
            
            # --- Strategy 2: Try inherited member lookup ---
            if info is None:
                inherited = self.qm.get_inherited_member_by_api_name(api_name)
                if inherited:
                    # Get signature variants from original member or inherited data
                    sig_variants = {}
                    if inherited.original_member_id:
                        original = self.qm.get_original_member_for_inherited(api_name)
                        if original:
                            sig_variants = original.signatures
                    
                    if not sig_variants and inherited.signature:
                        if isinstance(inherited.signature, dict):
                            sig_variants = {str(k): str(v) for k, v in inherited.signature.items()}
                        elif isinstance(inherited.signature, str):
                            sig_variants = {'full': inherited.signature}
                    
                    if not sig_variants:
                        sig_variants = {'full': f"{inherited.member_name}("}
                    
                    # Collect all API names for this inherited member
                    all_names = set(inherited.inherited_api_names or [])
                    all_names.add(inherited.inherited_api_name)
                    all_names.add(api_name)
                    if inherited.original_api_names:
                        all_names.update(inherited.original_api_names)
                    
                    info = WebMemberInfo(
                        api_name=api_name,
                        all_api_names=all_names,
                        member_input=MemberInput(
                            api_name=api_name,
                            signature_variants=sig_variants,
                            member_type=inherited.member_type or 'method'
                        ),
                        member_type=inherited.member_type or 'method'
                    )
                    logger.debug(f"Matched '{api_name}' as inherited member (from {inherited.source_class_fqn})")
            
            # --- Strategy 3: Try local member_map (case-insensitive) ---
            if info is None:
                info = member_map.get(api_name.lower())
                if info:
                    logger.debug(f"Matched '{api_name}' via local member_map")
            
            # --- Strategy 4: Search by short name in database ---
            if info is None:
                short_name = api_name.split('.')[-1]
                search_results = self.qm.search_members(short_name, limit=5)
                
                # Find best match - prefer exact short name match
                for result in search_results:
                    if result.name == short_name:
                        # check if api_name is in result's all_api_names
                        result_all_api_names = set(result.api_names or [])
                        result_all_api_names.add(result.api_name or '')
                        result_all_api_names.add(result.fqn)
                        result_all_api_names.discard('')
                        
                        if api_name in result_all_api_names:
                            output_api_name = api_name
                        else:
                            output_api_name = result.api_name or result.fqn
                        
                        info = WebMemberInfo(
                            api_name=output_api_name,
                            all_api_names=result_all_api_names | {api_name},
                            member_input=MemberInput(
                                api_name=output_api_name,
                                signature_variants=result.signatures,
                                member_type=result.type
                            ),
                            member_type=result.type
                        )
                        logger.debug(f"Matched '{api_name}' via short name search -> '{output_api_name}'")
                        break
            
            # --- Strategy 5: Create dummy (last resort) ---
            if info is None:
                logger.warning(f"No DB member found for '{api_name}', creating dummy")
                
                short_name = api_name.split('.')[-1]
                parts = api_name.rsplit('.', 1)
                
                # Infer member type from naming convention and parent structure
                if short_name[0].isupper():
                    inferred_type = "class"
                elif len(parts) == 2:
                    # Has a parent - check if parent looks like a class
                    parent_name = parts[0].split('.')[-1]  # e.g., "XGBRFClassifier" from "xgboost.XGBRFClassifier"
                    if parent_name and parent_name[0].isupper():
                        # Parent is likely a class, so this is likely a method
                        inferred_type = "method"
                    else:
                        inferred_type = "function"
                else:
                    inferred_type = "function"
                
                info = WebMemberInfo(
                    api_name=api_name,
                    all_api_names={api_name},
                    member_input=MemberInput(
                        api_name=api_name,
                        signature_variants={'full': f"{short_name}("},
                        member_type=inferred_type
                    ),
                    member_type=inferred_type
                )
            
            members_to_extract.append(info)
        
        return members_to_extract

    def _get_or_create_member_info(
        self,
        container_name: str,
        member_map: Dict[str, WebMemberInfo],
        extracted_apis: Set[str]
    ) -> Optional[WebMemberInfo]:
        """Get or create WebMemberInfo for a container."""
        if container_name in extracted_apis:
            return None
        
        container_info = member_map.get(container_name.lower())
        if container_info is None:
            short_name = container_name.split('.')[-1]
            is_class = short_name[0].isupper() if short_name else False
            
            container_info = WebMemberInfo(
                api_name=container_name,
                all_api_names={container_name},
                member_input=MemberInput(
                    api_name=container_name,
                    signature_variants={'full': container_name},
                    member_type="class" if is_class else "function"
                ),
                member_type="class" if is_class else "function"
            )
        return container_info
    
    def _extract_and_save_members(
        self,
        combined_text: str,
        members_to_extract: List[WebMemberInfo],
        output_dir: Path,
        extractor: WebMemberExtractor,
        model_name: str,
        extracted_apis: Set[str]
    ) -> None:
        """
        Extract members using two-stage semantic search and save to files.
        
        Args:
            combined_text: Full text of combined doc page
            members_to_extract: List of WebMemberInfo objects
            output_dir: Where to save {API_name}.txt files
            extractor: WebMemberExtractor instance
            model_name: Embedding model name
            extracted_apis: Set to track extracted APIs (modified in place)
        """
        if not members_to_extract:
            return
        
        logger.info(f"Extracting {len(members_to_extract)} members...")
        
        # --- Use batch extraction ---
        batch_results = extractor.extract_batch(combined_text, members_to_extract, model_name)
        
        # Build position list from batch results
        member_positions = []
        for info in members_to_extract:
            result = batch_results.get(info.api_name, (-1, 0.0, "none"))
            pos, score, match_type = result
            
            # --- THRESHOLD CHECK ---
            # Skip members that weren't found or have very low confidence
            if pos < 0 or match_type == "none":
                logger.debug(f"Skipping {info.api_name}: not found in document (match_type={match_type})")
                continue
            
            if score < extractor.cfg.min_lexical_score:
                logger.debug(f"Skipping {info.api_name}: score {score:.1f} below threshold")
                continue
            # ---
            
            member_positions.append({
                "info": info,
                "position": pos,
                "score": score,
                "match_type": match_type
            })
            
            if not member_positions:
                logger.debug("No members met extraction threshold")
                return
        
        # --- Sort by position ---
        member_positions.sort(key=lambda x: x["position"])
        
        # --- Extract each member with stop signals ---
        for i, mp in enumerate(member_positions):
            info = mp["info"]
            start_pos = mp["position"]
            
            # Build stop signals from subsequent members
            peer_sigs = []
            for peer_mp in member_positions[i+1:]:
                peer_info = peer_mp["info"]
                peer_needles = build_lexical_needles(peer_info.member_input)
                peer_sigs.extend(peer_needles.get("exact", []))
            
            stop_matcher = StopSignalMatcher(
                peer_signatures=peer_sigs,
                target_member_type=info.member_type,
                target_api_name=info.api_name
            )
            
            # Extract text
            extracted = self._extract_until_stop(
                combined_text[start_pos:],
                stop_matcher,
                max_chars=25000
            )
            
            # Save to file using API name
            output_file = output_dir / f"{info.api_name}.txt"
            output_file.write_text(extracted, encoding='utf-8')
            extracted_apis.add(info.api_name)
            logger.debug(f"Saved: {info.api_name}")

    
    def _extract_until_stop(self, text: str, stop_matcher: StopSignalMatcher, max_chars: int = 25000) -> str:
        """
        Extract text until a high-priority stop signal is found or max_chars is reached.
        
        Mirrors the fence-aware logic of pipeline_pdf.PDFExtractor._extract_lines_until_stop:
            - Lines inside a code fence (``` ... ```) are never high-priority stops.
            - Stop signals that fire inside a fence are recorded as a fallback
              truncation point and applied only if no definitive stop is found.
            - The first line (index 0) is skipped for stop-signal checking because
              it is the anchor line for the current member.
        
        Fallback behavior:
            - For CLASS: If max_chars reached without finding another class/function,
            retry with fallback patterns (methods/inherited members as boundaries)
            - For METHOD: If max_chars reached without finding a sibling method,
            retry with fallback patterns (classes/functions as boundaries)
        
        Args:
            text: Text to extract from
            stop_matcher: StopSignalMatcher configured with peer signatures
            max_chars: Maximum characters to extract
            
        Returns:
            Extracted text string
        """
        lines = text.split('\n')
        
        # --- Pre-scan to determine if fallback should be used upfront ---
        stop_matcher.pre_scan_section(text, start_pos=0)
        
        result_lines = []
        char_count = 0
        found_stop = False
        fallback_stop_line_idx = None   # index into result_lines for soft truncation
        fence_count_total = 0   # total ``` markers seen so far
        
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            
            # Determine fence state BEFORE processing this line
            is_inside_fence = (fence_count_total % 2) == 1
            fences_on_line  = line.count('```')
            is_fence_line   = (
                line_stripped == '```'
                or (line_stripped.startswith('```') and len(line_stripped) < 20)
            )
            # Update fence counter for the NEXT iteration
            fence_count_total += fences_on_line
            
            # --- Safety limit ---
            if char_count >= max_chars:
                if fallback_stop_line_idx is not None:
                    result_lines = result_lines[:fallback_stop_line_idx]
                    found_stop = True
                break
            
            # --- Stop-signal check (skip first line — it is the anchor itself) ---
            if i > 0 and stop_matcher:
                matched, is_high_priority = stop_matcher.checks_stop(line)
                
                if matched:
                    if is_inside_fence or is_fence_line:
                        # Inside a code block → record as soft fallback only
                        if fallback_stop_line_idx is None:
                            fallback_stop_line_idx = len(result_lines)
                    elif is_high_priority:
                        # Definitive stop outside a fence
                        found_stop = True
                        break
                    else:
                        # Low-priority match outside fence → soft fallback
                        if fallback_stop_line_idx is None:
                            fallback_stop_line_idx = len(result_lines)
            
            result_lines.append(line)
            char_count += len(line) + 1
            
        # --- Fallback retry ---
        if not found_stop and char_count >= max_chars:
            if not stop_matcher.use_fallback and stop_matcher.fallback_patterns:
                target_type = stop_matcher.target_type
                target_name = stop_matcher.target_name
                if target_type == "class":
                    logger.debug(f"CLASS {target_name}: hit max_chars without stop, retrying with method/inherited member fallback patterns")
                elif target_type == "method":
                    logger.debug(f"METHOD {target_name}: hit max_chars without sibling method stop, retrying with class/function fallback patterns")
                else:
                    logger.debug(f"{target_type.upper()} {target_name}: hit max_chars, retrying with fallback patterns")
                    
                stop_matcher.use_fallback = True
                result_lines = []
                char_count = 0
                fence_count_total = 0
                fallback_stop_line_idx = None
                
                for i, line in enumerate(lines):
                    line_stripped = line.strip()
                    is_inside_fence = (fence_count_total % 2) == 1
                    fences_on_line  = line.count('```')
                    is_fence_line   = (
                        line_stripped == '```'
                        or (line_stripped.startswith('```') and len(line_stripped) < 20)
                    )
                    fence_count_total += fences_on_line
                    
                    if char_count >= max_chars:
                        if fallback_stop_line_idx is not None:
                            result_lines = result_lines[:fallback_stop_line_idx]
                        break
                    if i > 0 and stop_matcher:
                        matched, is_high_priority = stop_matcher.checks_stop(line)
                        if matched:
                            if is_inside_fence or is_fence_line:
                                if fallback_stop_line_idx is None:
                                    fallback_stop_line_idx = len(result_lines)
                            elif is_high_priority:
                                break
                            else:
                                if fallback_stop_line_idx is None:
                                    fallback_stop_line_idx = len(result_lines)
                    
                    result_lines.append(line)
                    char_count += len(line) + 1
        
        return '\n'.join(result_lines)
    
    
    def _filter_container_doc(self, module_txt: Path, container_name: str, nested_api_names: List[str]) -> bool:
        """
        Filter container doc to remove nested member content.
        
        Returns:
            True if filtering was performed (container is an API member),
            False if skipped (container is a module, not an API member).
        """
        if not module_txt.exists():
            return False
        
        # --- Determine if container is an API member and get canonical name ---
        output_api_name = None # The name to use for the output file
        db_member = None
        
        # Strategy 1: Direct API name lookup
        db_member = self.qm.get_member_by_api_name(container_name)
        if db_member:
            # container_name IS the API name - use it directly
            output_api_name = container_name
        else:
            # Strategy 2: Search by short name
            short_name = container_name.split('.')[-1] if '.' in container_name else container_name
            search_results = self.qm.search_members(short_name, limit=5)
            
            for result in search_results:
                if result.fqn.split('.')[-1] == short_name and result.type in ('class', 'function'):
                    db_member = result
                    # Found via search - use the canonical FQN from database
                    output_api_name = db_member.fqn
                    break
        
        if db_member is None:
            # Container is NOT an API member (it's a module name)
            logger.debug(f"Container '{container_name}' is not an API member, skipping filter")
            return False
        
        # Container IS an API member - proceed with filtering
        logger.debug(f"Filtering container '{container_name}' -> output: {output_api_name}")
        
        combined_text = module_txt.read_text(encoding='utf-8')
        
        # --- Build stop signals from ACTUAL database signatures ---
        stop_sigs = []
        for api_name in nested_api_names:
            if api_name == container_name or api_name == output_api_name:
                continue
            
            # Query database for this nested member's signatures
            nested_member = self.qm.get_member_by_any_api_name(api_name)
            if nested_member:
                # Build proper lexical needles from actual signatures
                member_input = MemberInput(
                    api_name= nested_member.api_name or nested_member.fqn,
                    signature_variants=nested_member.signatures or {},
                    member_type=nested_member.type or "function"
                )
                needles = build_lexical_needles(member_input)
                
                # Add exact needle tier as stop signals
                stop_sigs.extend(needles.get("exact", []))
            else:
                # Fallback: basic patterns if not in database
                short_name = api_name.split('.')[-1]
                stop_sigs.append(api_name)
                stop_sigs.append(short_name)
                stop_sigs.append(f"{short_name}(")
        
        # ------------------------------------------------------------------------
        # For class containers, add inherited member signatures as fallback
        # These ensure proper trimming even when no nested methods were extracted
        # ------------------------------------------------------------------------
        if db_member.type == 'class':
            inherited_members = self.qm.get_inherited_members_for_class(db_member.fqn)
            for inherited in inherited_members:
                # Skip if already covered by nested_api_names
                if inherited.inherited_api_name in nested_api_names:
                    continue
                
                # Get signatures
                sig_variants = {}
                if inherited.original_member_id:
                    original = self.qm.get_original_member_for_inherited(inherited.inherited_api_name)
                    if original:
                        sig_variants = original.signatures or {}
                
                if not sig_variants and inherited.signature:
                    if isinstance(inherited.signature, dict):
                        sig_variants = {str(k): str(v) for k, v in inherited.signature.items()}
                    elif isinstance(inherited.signature, str):
                        sig_variants = {'full': inherited.signature}
                
                if not sig_variants:
                    sig_variants = {'full': f"{inherited.member_name}("}
                
                inherited_input = MemberInput(
                    api_name=inherited.inherited_api_name,
                    signature_variants=sig_variants,
                    member_type=inherited.member_type or 'method'
                )
                needles = build_lexical_needles(inherited_input)
                stop_sigs.extend(needles.get("exact", []))
            
            logger.debug(f"Added {len(inherited_members)} inherited member signatures as stop signals for class {output_api_name}")
        
        if not stop_sigs:
            filtered_text = combined_text
        else:
            stop_matcher = StopSignalMatcher(
                peer_signatures=stop_sigs,
                target_member_type=db_member.type or "class",
                target_api_name=output_api_name
            )
            filtered_text = self._extract_until_stop(combined_text, stop_matcher, max_chars=50000)
        
        container_output = self.per_member_dir / f"{output_api_name}.txt"
        container_output.write_text(filtered_text, encoding='utf-8')
        
        logger.debug(f"Saved filtered container doc: {container_output.name}")
        return True
    
    
    def _relocate_combined_docs(
        self,
        combined_doc_files: Set[Path],
        extracted_apis: Set[str]
    ) -> None:
        """
        Move combined/module docs out of per_member/ to a separate combined/ folder.
        
        This ensures per_member/ only contains individual API member docs for
        downstream processing (preprocessing, LLM structuring, etc.)
        
        Args:
            combined_doc_files: Set of paths to known combined doc files
            extracted_apis: Set of extracted API names (file stems)
        """
        combined_dir = self.scraped_doc_dir / "combined"
        combined_dir.mkdir(parents=True, exist_ok=True)
        
        moved_count = 0
        
        # Move explicitly tracked combined docs
        for combined_path in combined_doc_files:
            if combined_path.exists() and combined_path.parent == self.per_member_dir:
                dest = combined_dir / combined_path.name
                shutil.move(str(combined_path), str(dest))
                logger.debug(f"Moved combined doc: {combined_path.name} -> combined/")
                moved_count += 1
        
        # Also detect and move any remaining module-named files (no '.' in stem)
        # These are likely scraped module pages, not individual API members
        if self.per_member_dir.exists():
            for txt_file in list(self.per_member_dir.glob("*.txt")):
                stem = txt_file.stem
                
                # Skip if it looks like an API name (has dots)
                if '.' in stem:
                    continue
                
                # Skip if this stem was explicitly extracted as an API (sanity check)
                if stem in extracted_apis and self.qm.get_member_by_api_name(stem):
                    continue
                
                # Check if it's a known module/container (not an API member)
                db_member = self.qm.get_member_by_api_name(stem)
                if db_member is None:
                    # Not an API member - likely a module doc
                    dest = combined_dir / txt_file.name
                    shutil.move(str(txt_file), str(dest))
                    logger.debug(f"Moved module doc: {txt_file.name} -> combined/")
                    moved_count += 1
        
        if moved_count > 0:
            logger.info(f"Relocated {moved_count} combined/module docs to: {combined_dir}")
    
    
    # =========================================================================
    # Step 4: Preprocess (URL placeholder replacement)
    # =========================================================================
    
    def _preprocess_all_members(self):
        """Preprocess all per_member docs: replace URLs with placeholders."""
        logger.info("Step 4: Preprocessing documents (URL placeholder replacement)...")
        
        self._ensure_dirs(self.preprocessed_doc_dir, self.url_context_dir)
        
        if not self.per_member_dir.exists():
            logger.warning(f"No per_member directory found at {self.per_member_dir}")
            return
        
        count = 0
        for txt_file in self.per_member_dir.glob("*.txt"):
            api_name = txt_file.stem
            
            # Output paths
            preprocessed_doc_path = self.preprocessed_doc_dir / f"{api_name}.txt"
            url_context_path = self.url_context_dir / f"{api_name}.json"
            
            try:
                preprocess_crossRef(
                    str(txt_file),
                    str(preprocessed_doc_path),
                    str(url_context_path)
                )
                count += 1
            except Exception as e:
                logger.warning(f"Preprocessing failed for {api_name}: {e}")
        
        logger.info(f"Preprocessed {count} documents. Output in: {self.preprocessed_doc_dir}")

    # =========================================================================
    # Step 5: Structured Doc Extraction (LLM)
    # =========================================================================
    
    def _extract_structured_docs(self, members: List[MemberInput]):
        """Extract structured documentation using LLM (concurrent)."""
        logger.info("Step 5: Extracting structured documentation via LLM...")
        
        self._ensure_dirs(self.structured_doc_dir)
        
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set. Skipping structured extraction.")
            return
        
        # Build lookup for member info
        member_info = {m.api_name: m for m in members}
        
        # Collect extraction requests
        extraction_requests = []
        already_done = 0
        
        for preprocessed_file in self.preprocessed_doc_dir.glob("*.txt"):
            api_name = preprocessed_file.stem
            mi = member_info.get(api_name)
            if not mi:
                logger.debug(f"No member info for {api_name}, skipping")
                continue
            
            # Skip if already processed
            structured_doc_path = self.structured_doc_dir / f"{api_name}.json"
            if structured_doc_path.exists():
                already_done += 1
                continue
            
            # Build prompts using DocumentationExtractor
            signature = self._first_sig(mi) #mi.signature_variants[0] if mi.signature_variants else api_name
            
            temp_extractor = DocumentationExtractor(
                MM_type=mi.member_type,
                MM_signature=signature,
                MM_code_body="",
                MM_methods_and_attributes_signature="",
                scraped_doc_path=str(preprocessed_file),
                api_key="",  # Not needed for prompt generation
                input_choice='module_member_signature'
            )
            temp_extractor._generate_prompts()
            
            extraction_requests.append({
                'api_name': api_name,
                'member_type': mi.member_type,
                'signature': signature,
                'system_prompt': temp_extractor.system_prompt,
                'user_prompt': temp_extractor.user_prompt,
                'json_schema': temp_extractor.json_schema
            })
        
        if not extraction_requests:
            logger.info(f"All {already_done} docs already processed. Nothing to do.")
            return
        
        logger.info(f"Processing {len(extraction_requests)} docs concurrently ({already_done} already done)...")
        
        # Use ConcurrentDocExtractor
        extractor = ConcurrentDocExtractor(api_key, max_concurrent=10)
        
        def progress_callback(completed, total):
            if completed % 10 == 0 or completed == total:
                logger.info(f"Progress: {completed}/{total}")
        
        results = asyncio.run(extractor.extract_all(extraction_requests, progress_callback))
        
        # Save results
        success_count = 0
        for api_name, doc_json in results.items():
            if doc_json:
                output_path = self.structured_doc_dir / f"{api_name}.json"
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(json.loads(doc_json), f, indent=4, ensure_ascii=False)
                success_count += 1
        
        total_done = already_done + success_count
        failed = len(results) - success_count
        
        logger.info(f"Structured doc extraction complete: {total_done} done, {failed} failed")
        logger.info(f"Output in: {self.structured_doc_dir}")
    
    
    def _extract_structured_docs_iterative(self, members: List[MemberInput]):
        """Extract structured documentation using LLM."""
        logger.info("Step 5: Extracting structured documentation via LLM...")
        
        self._ensure_dirs(self.structured_doc_dir)
        
        # Build lookup for member info
        member_info = {m.api_name: m for m in members}
        
        # Get API key (you may want to make this configurable)
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set. Skipping structured extraction.")
            return
        
        count = 0
        for preprocessed_file in self.preprocessed_doc_dir.glob("*.txt"):
            api_name = preprocessed_file.stem
            
            # Get member details
            mi = member_info.get(api_name)
            if not mi:
                logger.debug(f"No member info for {api_name}, skipping structured extraction")
                continue
            
            # Output path
            structured_doc_path = self.structured_doc_dir / f"{api_name}.json"
            
            # Skip if already processed
            if structured_doc_path.exists():
                count += 1
                continue
            
            try:
                # Determine signature to use
                signature = self._first_sig(mi) #mi.signature_variants[0] if mi.signature_variants else api_name
                
                extractor = DocumentationExtractor(
                    MM_type=mi.member_type,
                    MM_signature=signature,
                    MM_code_body="",  # Could be populated from DB
                    MM_methods_and_attributes_signature="",
                    scraped_doc_path=str(preprocessed_file),
                    api_key=api_key,
                    input_choice='module_member_signature'
                )
                
                # Generate and call LLM
                extractor._generate_prompts()
                extractor._call_openai_api()
                
                if extractor.extracted_doc:
                    with open(structured_doc_path, 'w', encoding='utf-8') as f:
                        json.dump(json.loads(extractor.extracted_doc), f, indent=4, ensure_ascii=False)
                    count += 1
                    logger.debug(f"Structured: {api_name}")
                    
            except Exception as e:
                logger.warning(f"Structured extraction failed for {api_name}: {e}")
        
        logger.info(f"Extracted structured docs for {count} members. Output in: {self.structured_doc_dir}")

    # =========================================================================
    # Step 6: Postprocess (URL restoration)
    # =========================================================================
    
    def _postprocess_all_members(self):
        """Postprocess all structured docs: restore URLs from placeholders."""
        logger.info("Step 6: Postprocessing documents (URL restoration)...")
        
        self._ensure_dirs(self.postprocessed_doc_dir)
        
        if not self.structured_doc_dir.exists():
            logger.warning(f"No structured_doc directory found at {self.structured_doc_dir}")
            return
        
        count = 0
        for structured_file in self.structured_doc_dir.glob("*.json"):
            api_name = structured_file.stem
            
            # Find corresponding URL context
            url_context_path = self.url_context_dir / f"{api_name}.json"
            if not url_context_path.exists():
                logger.debug(f"No URL context for {api_name}, copying as-is")
                shutil.copy2(structured_file, self.postprocessed_doc_dir / f"{api_name}.json")
                count += 1
                continue
            
            # Output path
            postprocessed_path = self.postprocessed_doc_dir / f"{api_name}.json"
            
            try:
                postprocess_crossRef(
                    str(url_context_path),
                    str(structured_file),
                    str(postprocessed_path)
                )
                count += 1
            except Exception as e:
                logger.warning(f"Postprocessing failed for {api_name}: {e}")
        
        logger.info(f"Postprocessed {count} documents. Output in: {self.postprocessed_doc_dir}")

    # =========================================================================
    # Step 7: Database Update
    # =========================================================================
    
    def _update_database(self):
        """Update DB members with extracted documentation from postprocessed_doc."""
        logger.info("Step 7: Updating database with structured documentation...")
        
        if not self.postprocessed_doc_dir.exists():
            logger.warning(f"No postprocessed_doc directory found at {self.postprocessed_doc_dir}")
            return
        
        member_count = 0
        inherited_count = 0
        
        for doc_file in self.postprocessed_doc_dir.glob("*.json"):
            api_name = doc_file.stem
            
            try:
                with open(doc_file, 'r', encoding='utf-8') as f:
                    doc_data = json.load(f)
                
                # --- Find direct member (check FQN, primary_api_name, and all_api_names) ---
                member = self.session.query(DBMember).filter_by(fully_qualified_name=api_name).first()
                if not member:
                    member = self.session.query(DBMember).filter_by(primary_api_name=api_name).first()
                
                # Fallback to searching all_api_names JSON array
                # This enables finding members even when the filename uses a secondary API name from members.json
                if not member:
                    from sqlalchemy import text
                    member = (
                        self.session.query(DBMember)
                        .filter(
                            text("EXISTS (SELECT 1 FROM json_each(all_api_names) WHERE json_each.value = :api_name)")
                        )
                        .params(api_name=api_name)
                        .first()
                    )
                
                if member:
                    # Update with STRUCTURED doc fields
                    member.doc_format = "structured"  # Mark as structured JSON
                    member.doc_source_type = "web" if self.crawled_urls_dir.exists() else "pdf"
                    member.doc_source_path = str(doc_file)
                    member.api_reference = doc_data
                    member.api_reference_file = str(doc_file)
                    member.doc_raw_text = None  # Clear raw text since we have structured
                    
                    # Extract quick-access fields from structured JSON
                    member.doc_signature = doc_data.get("module_member_signature", "")
                    desc = doc_data.get("module_member_description", {})
                    member.doc_description = desc.get("purpose", "") if isinstance(desc, dict) else str(desc)
                    member.doc_examples = doc_data.get("examples", [])
                    
                    member_count += 1
                    continue
                
                # --- Try finding as inherited member ---
                inherited = self.session.query(DBInheritedMember).filter_by(inherited_api_name=api_name).first()
                
                # Also check inherited_api_names JSON array
                if not inherited:
                    inherited = (
                        self.session.query(DBInheritedMember)
                        .filter(
                            text("EXISTS (SELECT 1 FROM json_each(inherited_api_names) WHERE json_each.value = :api_name)")
                        )
                        .params(api_name=api_name)
                        .first()
                    )
                
                if inherited:
                    if inherited.original_member_id:
                        # Internal inherited member - save to original
                        original = self.session.query(DBMember).get(inherited.original_member_id)
                        if original and not original.api_reference:
                            original.doc_format = "structured"
                            original.doc_source_type = "web" if self.crawled_urls_dir.exists() else "pdf"
                            original.doc_source_path = str(doc_file)
                            original.api_reference = doc_data
                            original.api_reference_file = str(doc_file)
                            original.doc_raw_text = None
                            
                            original.doc_signature = doc_data.get("module_member_signature", "")
                            desc = doc_data.get("module_member_description", {})
                            original.doc_description = desc.get("purpose", "") if isinstance(desc, dict) else str(desc)
                            original.doc_examples = doc_data.get("examples", [])
                    else:
                        # External inherited member - save directly to inherited record
                        inherited.doc_format = "structured"
                        inherited.doc_source_type = "web" if self.crawled_urls_dir.exists() else "pdf"
                        inherited.doc_source_path = str(doc_file)
                        inherited.api_reference = doc_data
                        inherited.doc_raw_text = None
                        
                        inherited.doc_signature = doc_data.get("module_member_signature", "")
                        desc = doc_data.get("module_member_description", {})
                        inherited.doc_description = desc.get("purpose", "") if isinstance(desc, dict) else str(desc)
                    
                    inherited_count += 1
                else:
                    logger.debug(f"No DB member found for: {api_name}")
                    
            except Exception as e:
                logger.warning(f"Database update failed for {api_name}: {e}")
        
        self.session.commit()
        logger.info(f"Updated {member_count} direct + {inherited_count} inherited members with structured docs.")
    
    
    def _update_database_from_raw(self, members: List[MemberInput]):
        """
        Update DB members with raw scraped documentation (no LLM structuring).
        
        Stores raw text in dedicated field and marks doc_format='raw' to
        distinguish from LLM-structured JSON documentation.
        """
        logger.info("Step 7 (raw): Updating database with raw scraped documentation...")
        
        if not self.per_member_dir.exists():
            logger.warning(f"No per_member directory found at {self.per_member_dir}")
            return
        
        # Build lookup for member info
        member_info = {m.api_name: m for m in members}
        
        member_count = 0
        inherited_count = 0
        
        for doc_file in self.per_member_dir.glob("*.txt"):
            api_name = doc_file.stem
            
            try:
                raw_text = doc_file.read_text(encoding='utf-8')
                
                # --- Find direct member (FQN, primary, then all_api_names) ---
                member = self.session.query(DBMember).filter_by(fully_qualified_name=api_name).first()
                if not member:
                    member = self.session.query(DBMember).filter_by(primary_api_name=api_name).first()
                
                # Fallback to searching all_api_names JSON array
                if not member:
                    member = (
                        self.session.query(DBMember)
                        .filter(
                            text("EXISTS (SELECT 1 FROM json_each(all_api_names) WHERE json_each.value = :api_name)")
                        )
                        .params(api_name=api_name)
                        .first()
                    )
                
                if member:
                    # Update with RAW doc fields
                    member.doc_format = "raw"  # Mark as raw text
                    member.doc_source_type = "web" if self.crawled_urls_dir.exists() else "pdf"
                    member.doc_source_path = str(doc_file)
                    member.api_reference_file = str(doc_file)
                    
                    # Store full raw text in dedicated field
                    member.doc_raw_text = raw_text
                    
                    # api_reference stays None for raw docs
                    member.api_reference = None
                    member.doc_examples = None
                    
                    # Try to extract basic info from raw text
                    lines = raw_text.strip().split('\n')
                    if lines:
                        first_line = lines[0].strip()
                        mi = member_info.get(api_name)
                        
                        # Extract signature (first line often has signature)
                        if mi and '(' in first_line and mi.api_name.split('.')[-1] in first_line:
                            member.doc_signature = first_line
                            # Description is everything after signature
                            member.doc_description = '\n'.join(lines[1:]).strip()[:2000]  # Limit size
                        else:
                            # No clear signature, use full text as description
                            member.doc_signature = None
                            member.doc_description = raw_text[:2000]  # Limit size
                    
                    member_count += 1
                    continue
                
                # --- Try finding as inherited member ---
                inherited = self.session.query(DBInheritedMember).filter_by(inherited_api_name=api_name).first()
                
                # Also check inherited_api_names JSON array
                if not inherited:
                    inherited = (
                        self.session.query(DBInheritedMember)
                        .filter(
                            text("EXISTS (SELECT 1 FROM json_each(inherited_api_names) WHERE json_each.value = :api_name)")
                        )
                        .params(api_name=api_name)
                        .first()
                    )
                
                if inherited:
                    if inherited.original_member_id:
                        # Internal inherited member - save to original
                        original = self.session.query(DBMember).get(inherited.original_member_id)
                        if original and not original.doc_raw_text and not original.api_reference:
                            original.doc_format = "raw"
                            original.doc_source_type = "web" if self.crawled_urls_dir.exists() else "pdf"
                            original.doc_source_path = str(doc_file)
                            original.api_reference_file = str(doc_file)
                            original.doc_raw_text = raw_text
                            original.api_reference = None
                            original.doc_description = raw_text[:2000]
                    else:
                        # External inherited member - save directly to inherited record
                        inherited.doc_format = "raw"
                        inherited.doc_source_type = "web" if self.crawled_urls_dir.exists() else "pdf"
                        inherited.doc_source_path = str(doc_file)
                        inherited.doc_raw_text = raw_text
                        inherited.api_reference = None
                        inherited.doc_description = raw_text[:2000] if raw_text else None
                    
                    inherited_count += 1
                else:
                    logger.debug(f"No DB member found for: {api_name}")
                    
            except Exception as e:
                logger.warning(f"Database update (raw) failed for {api_name}: {e}")
        
        self.session.commit()
        logger.info(f"Updated {member_count} direct + {inherited_count} inherited members with raw docs.")
