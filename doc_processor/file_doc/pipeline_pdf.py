"""
PDF API Documentation Extraction Pipeline

This module orchestrates the coarse-to-fine retrieval of API reference documentation
from a single PDF. It implements a two-stage process:
1. Chunk Selection: Identifying the broad section containing API docs.
2. Member Extraction: Precisely locating and extracting the documentation block
    for specific members using "Anchor and Expand" logic with structural stop signals.
"""

from __future__ import annotations

import os, json, re, logging
from typing import List, Dict, Any, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

from doc_processor.file_doc.extraction_utils import MemberExtractorConfig, _windows, _dynamic_threshold, _should_use_semantic_member
from doc_processor.filter_doc import StopSignalMatcher
from doc_processor.file_doc.pdf_localizer import PDFSectionizer, Section
from doc_processor.file_doc.embeddings import EmbeddingModel
from doc_processor.file_doc.chunk_selector import APIReferenceLocator
from doc_processor.file_doc.hybrid_search import section_match_score, cosine_similarity
from doc_processor.file_doc.signature import (
    MemberInput, 
    build_signature_patterns, 
    build_lexical_needles, 
    build_semantic_query, 
    build_passage_text, 
    build_signature_query
) 

logger = logging.getLogger(__name__)



class NumpyEncoder(json.JSONEncoder):
    """Custom encoder to handle numpy types."""
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

class PDFExtractor:
    """
    Extracts structure-aware snippets from a `Section`.
    
    Strategy: "Anchor and Expand"
        - Anchor: Find the start line using regex/semantic search.
        - Expand: Read forward line-by-line until a Stop Signal is hit.
      
    Stop Signals:
        1. Signature of a peer member (sibling/parent).
        2. Major structural heading (e.g., "Module Index").
        3. Visual formatting reset (e.g., outdentation - though harder on raw text).
        4. Safety limits (max lines).
    """
    def __init__(self, max_chars: int = 25000, try_subhead_trim: bool = True):
        """
        Initialize extractor windowing behavior.

        Args:
            max_chars: Maximum snippet length (characters) beyond the anchor.
            try_subhead_trim: If True, try to trim the snippet at common subheaders to avoid excessive content; otherwise return the full window.
        """
        self.max_chars = max_chars
        self.try_subhead_trim = try_subhead_trim
        
        # Subheaders that do NOT stop extraction (they are part of the doc)
        self.valid_subheads = {
            "parameters", "args", "arguments", "returns", "yields", "raises",
            "examples", "notes", "see also", "references", "attributes"
        }

    
    def extract_by_line_expansion(
        self, 
        section: Section, 
        start_char_idx: int, 
        stop_matcher: Optional[StopSignalMatcher] = None
    ) -> Tuple[str, List[int]]:
        """
        Extract text starting from an anchor, expanding until a stop signal is found.
        
        Uses content-based stop signal detection with code fence awareness:
            - Stop signals that look like real definitions trigger immediate stop
            - Stop signals that look like code examples are recorded as fallbacks
            - If we reach max_chars with only code-example stops, truncate at fallback
        
        Note: For CLASS extraction, if max_chars is reached without finding a stop,
        retries with fallback patterns (methods/inherited members as boundaries).
        
        Args:
            section: The source section containing the documentation.
            start_char_idx: Character index in text_raw where the member starts.
            stop_matcher: Object to check lines for stop signals (peer members).
                        If None, extraction continues until max_chars.
            
        Returns:
            Tuple of (extracted_text, page_range)
        """
        full_text = section.text_raw
        
        # Pre-scan to determine stop strategy (primary vs fallback patterns)
        if stop_matcher:
            stop_matcher.pre_scan_section(full_text, start_char_idx)
        
        # Align character index to the nearest line start
        start_line_idx = full_text.rfind('\n', 0, start_char_idx) + 1
        
        # Split into lines from the anchor position
        lines = full_text[start_line_idx:].splitlines(keepends=True)
        
        # --- First pass: Try with current patterns (primary or fallback from pre-scan) ---
        extracted_lines, found_stop, hit_max_chars = self._extract_lines_until_stop(
            lines, stop_matcher
        )
        
        # --- Fallback retry for CLASS extraction ---
        # If we hit max_chars without finding a stop, and this is CLASS extraction,
        # retry with fallback patterns (methods/inherited members as boundaries)
        if (hit_max_chars and not found_stop and 
            stop_matcher and stop_matcher.target_type == "class" and 
            not stop_matcher.use_fallback and stop_matcher.fallback_patterns):
            
            logger.debug(
                f"CLASS {stop_matcher.target_name}: hit max_chars without stop, "
                "retrying with method/inherited member fallback patterns"
            )
            
            # Force fallback mode and re-extract
            stop_matcher.use_fallback = True
            extracted_lines, found_stop, _ = self._extract_lines_until_stop(
                lines, stop_matcher
            )
        
        return "".join(extracted_lines), list(range(section.page_start, section.page_end))


    def _extract_lines_until_stop(
        self, 
        lines: List[str], 
        stop_matcher: Optional[StopSignalMatcher]
    ) -> Tuple[List[str], bool, bool]:
        """
        Core line-by-line extraction logic with stop signal detection.
        
        Args:
            lines: List of lines to process
            stop_matcher: StopSignalMatcher instance or None
            
        Returns:
            Tuple of (extracted_lines, found_stop, hit_max_chars)
        """
        extracted_lines = []
        char_count = 0
        found_stop = False
        hit_max_chars = False
        
        # Track first low-priority stop signal (inside code block) as fallback
        fallback_stop_line_idx: Optional[int] = None
        
        # Code fence tracking
        fence_count_total = 0
        
        for i, line in enumerate(lines):
            # --- Determine fence state for THIS line ---
            is_inside_fence = (fence_count_total % 2) == 1
            
            # Count fences ON this line for state update
            fences_on_line = line.count('```')
            
            # Special case: line that IS a fence marker
            line_stripped = line.strip()
            is_fence_line = (
                line_stripped == '```' or
                line_stripped.startswith('```') and len(line_stripped) < 20
            )
            
            # Update total fence count for NEXT iteration
            fence_count_total += fences_on_line
            
            # --- Stop Condition 1: Safety Limit ---
            if char_count > self.max_chars:
                hit_max_chars = True
                if fallback_stop_line_idx is not None and fallback_stop_line_idx < len(extracted_lines):
                    extracted_lines = extracted_lines[:fallback_stop_line_idx]
                    found_stop = True  # We did find a fallback stop
                else:
                    extracted_lines.append("\n... [truncated] ...")
                break
            
            # --- Stop Condition 2: Peer Signature ---
            if i > 0 and stop_matcher:
                matched, is_high_priority_content = stop_matcher.checks_stop(line)
                
                if matched:
                    # Inside fence OR fence line itself -> low priority
                    if is_inside_fence or is_fence_line:
                        if fallback_stop_line_idx is None:
                            fallback_stop_line_idx = len(extracted_lines)
                    # Outside fence AND high priority content -> stop now
                    elif is_high_priority_content:
                        found_stop = True
                        break
                    # Outside fence but low priority content -> fallback
                    else:
                        if fallback_stop_line_idx is None:
                            fallback_stop_line_idx = len(extracted_lines)
            
            # Add line to extracted content
            extracted_lines.append(line)
            char_count += len(line)
        
        return extracted_lines, found_stop, hit_max_chars


def _sanitize_filename(api_name: str) -> str:
    """
    Produce a filesystem-safe filename from an API FQN.

    Replaces path separators, whitespace, and special characters with underscores.

    Args:
        api_name: API fully-qualified name.

    Returns:
        A normalized filename (without extension).
    """
    return api_name.replace(" ", "_").replace("/", "_").replace("\\", "_").replace(":", "_")



class MemberExtractor:
    """
    Stage 2: Extract member documentation from selected sections.
    
    Uses 'Anchor and Expand':
    1.  Anchor: Find the start position of the member's definition via Regex or Semantic Search.
    2.  Expand: Read forward from the anchor until a 'Stop Signal' (start of next member) is found.
    """
    def __init__(self, cfg: MemberExtractorConfig, extractor: Optional[PDFExtractor] = None):
        """
        Initialize the member extractor.

        Args:
            cfg: Member extraction configuration (gating, shortlist/window sizes, parallelism).
            extractor: Optional `PDFExtractor` for snippet generation; a default is created if None.
        """
        self.cfg = cfg
        self.extractor = extractor or PDFExtractor()

    def _effective_window_params(self, embedder: EmbeddingModel) -> Tuple[int, int]:
        """
        Compute window size and stride aligned with the embedding model's context window.

        The goal is to ensure that every character in a long section is fully visible
        within at least one embedded window, while respecting the model's max_seq_length.

        Args:
            embedder: Embedding model wrapper (must expose `.model.max_seq_length` if available).

        Returns:
            (window_chars, window_stride) to use for semantic windowing.
        """
        model = getattr(embedder, "model", None)
        max_tokens = getattr(model, "max_seq_length", None)

        # If we can't introspect, fall back to configured values.
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            return self.cfg.window_chars, self.cfg.window_stride

        # Conservative chars-per-token estimate.
        chars_per_token = 4
        max_chars = max_tokens * chars_per_token

        # Do not exceed either the model context or the configured window.
        window_chars = min(self.cfg.window_chars, max_chars)
        # Ensure stride does not exceed window size.
        stride = min(self.cfg.window_stride, window_chars)
        if stride <= 0:
            stride = window_chars

        return window_chars, stride
    
    
    def _regex_refine(self, sec_obj: Section, patterns: List[re.Pattern]) -> int:
        """
        Find an anchor position for a member within a section using tolerant regex on normalized text.

        Args:
            sec_obj: Candidate section.
            patterns: Precompiled tolerant regex patterns for the member.

        Returns:
            Anchor index relative to the section's raw text if possible; otherwise a best-effort index.
            Returns -1 if no match is found.
        """
        for pat in patterns:
            m = pat.search(sec_obj.text_norm)
            if m:
                # Map normalized index back to raw index via local context matching
                # Grab a small chunk around the match from normalized text
                anchor = sec_obj.text_norm[max(0, m.start()-60): m.start()+60]
                
                # Find that chunk in the raw text (ignoring newlines/spaces differences)
                # This is a heuristic map; exact mapping requires character-level map from Sectionizer
                # Simplest robust way: fuzzy find the anchor string in raw text
                # For now, use direct find with whitespace relaxation
                raw_anchor_pos = sec_obj.text_raw.find(anchor.replace('\n', ' ').strip())
                
                # Fallback: if simple map fails, trust the ratio (roughly)
                if raw_anchor_pos < 0:
                    ratio = len(sec_obj.text_raw) / max(1, len(sec_obj.text_norm))
                    raw_anchor_pos = int(m.start() * ratio)
                return raw_anchor_pos
        return -1
    
    def _semantic_window_search(
        self, 
        embedder: EmbeddingModel, 
        sec_obj: Section, 
        q_vec: np.ndarray,
        sig_query_vec: Optional[np.ndarray] = None,
        api_name: str = ""
    ) -> Optional[Tuple[int, float]]:
        """
        Find anchor position using semantic search, returning both position AND score.
        This allows cross-section comparison to select the best anchor.
        
        Two-stage process:
            1. Coarse: Rank windows using chunk-level max pooling (prevents content accumulation bias)
            2. Fine: Run fine-grained anchor search on each top candidate (with lookback)
        
        Args:
            embedder: Embedding model for encoding windows/lines.
            sec_obj: Candidate section to search within.
            q_vec: Query embedding for coarse search (full context, L2-normalized).
            sig_query_vec: Optional signature-focused query embedding for fine search.
                        If None, returns the coarse window start position.
            api_name: API name to search for.
        
        Returns:
            Tuple of (anchor_position, fine_score) or None if no windows available.
        """
        #--- Stage 1: Coarse region finding with line-aligned windows ---
        win_chars, win_stride = self._effective_window_params(embedder)
        spans = _windows(sec_obj.text_norm, win_chars, win_stride)
        if not spans:
            return None
        
        # Extract target name variants for window-level verification
        target_fqn = api_name.lower() if api_name else ""
        parts = api_name.split('.') if api_name else []
        target_parent_name = f"{parts[-2]}.{parts[-1]}".lower() if len(parts) >= 2 else ""
        target_short = parts[-1].lower() if parts else ""
        
        # Paragraph-Based Max Pooling with Length Penalty
        # Split by paragraphs (double newlines or blank lines) for semantic coherence
        MIN_CHUNK_LEN = 50    # Skip very short paragraphs
        MAX_CHUNK_LEN = 800   # Sub-chunk very long paragraphs
        LENGTH_PENALTY_FACTOR = 0.00005
        NAME_PRESENT_BONUS = 0.15  # Bonus if window contains target name

        window_scores = []
        for (a, b) in spans:
            window_text = sec_obj.text_norm[a:b]
            window_lower = window_text.lower()
            
            # Split into paragraphs (blank lines or double newlines)
            raw_paragraphs = re.split(r'\n\s*\n', window_text)
            
            # Process paragraphs: filter small, sub-chunk large
            chunks = []
            for para in raw_paragraphs:
                para = para.strip()
                if len(para) < MIN_CHUNK_LEN:
                    continue  # Skip tiny paragraphs
                
                if len(para) > MAX_CHUNK_LEN:
                    # Sub-chunk long paragraphs by sentences or fixed size
                    for i in range(0, len(para), MAX_CHUNK_LEN // 2):
                        sub = para[i:i + MAX_CHUNK_LEN]
                        if len(sub) > MIN_CHUNK_LEN:
                            chunks.append(sub)
                else:
                    chunks.append(para)
            
            if chunks:
                C = embedder.encode(chunks)
                chunk_sims = C @ q_vec
                
                # Max pooling: best paragraph match
                max_score = float(np.max(chunk_sims))
                length_penalty = LENGTH_PENALTY_FACTOR * len(window_text)
                
                # --- Window-Level Lexical Name Verification ---
                # Bonus if window contains the target API name
                name_bonus = 0.0
                if target_fqn and target_fqn in window_lower:
                    name_bonus = NAME_PRESENT_BONUS  # Full FQN found
                elif target_parent_name and target_parent_name in window_lower:
                    name_bonus = NAME_PRESENT_BONUS * 0.7  # Parent.Name found
                # Don't give bonus for just short name (too ambiguous)
                
                window_scores.append(max_score - length_penalty + name_bonus)
            else:
                window_scores.append(0.0)

        sims = np.array(window_scores)
        
        # Get top 3 windows by normalized score
        num_candidates = min(3, len(spans))
        top_indices = np.argsort(sims)[::-1][:num_candidates]
        
        # Map normalized text positions to raw text positions
        ratio = len(sec_obj.text_raw) / max(1, len(sec_obj.text_norm))
        
        # If no signature query vector, return start of best window with coarse score
        if sig_query_vec is None:
            best_idx = int(top_indices[0])
            best_start, _ = spans[best_idx]
            return (int(best_start * ratio), float(sims[best_idx]))
        
        # --- Stage 2: Fine-grained search on each top candidate ---
        # For each candidate, include lookback from previous window
        
        candidate_results = []  # List of (anchor_pos, fine_score)
        
        for window_idx in top_indices:
            window_start, window_end = spans[window_idx]
            raw_start = int(window_start * ratio)
            raw_end = min(len(sec_obj.text_raw), int(window_end * ratio) + 500)
            
            # Calculate lookback: last 10 non-empty lines from previous window
            lookback_start = raw_start  # Default: no lookback
            
            if window_idx > 0:
                prev_start, prev_end = spans[window_idx - 1]
                prev_raw_start = int(prev_start * ratio)
                prev_raw_end = int(prev_end * ratio)
                prev_window_text = sec_obj.text_raw[prev_raw_start:prev_raw_end]
                
                # Find last 10 non-empty lines from previous window
                prev_lines = prev_window_text.splitlines(keepends=True)
                non_empty_count = 0
                target_count = 10
                
                for i in range(len(prev_lines) - 1, -1, -1):
                    if prev_lines[i].strip():
                        non_empty_count += 1
                        if non_empty_count >= target_count:
                            # Calculate char position of this line
                            char_offset = sum(len(prev_lines[j]) for j in range(i))
                            lookback_start = prev_raw_start + char_offset
                            break
            
            # Run fine-grained search on (lookback + window) region
            region_text = sec_obj.text_raw[lookback_start:raw_end]
            local_pos, fine_score = self._semantic_anchor_in_region_with_score(embedder, region_text, sig_query_vec, target_api_name=api_name)
            
            global_pos = lookback_start + local_pos
            candidate_results.append((global_pos, fine_score))
        
        # Select candidate with best fine-grained score
        if not candidate_results:
            return None
        
        best_result = max(candidate_results, key=lambda x: x[1])
        return best_result
    
    def _semantic_anchor_in_region_with_score(
        self, 
        embedder: EmbeddingModel, 
        text: str, 
        sig_query_vec: np.ndarray,
        max_lines: int = 100,
        target_api_name: str = ""
    ) -> Tuple[int, float]:
        """
        Find the exact anchor line within a text region using semantic similarity and lexical name verification.
        
        Args:
            embedder: Embedding model for encoding lines.
            text: Text region to search within.
            sig_query_vec: Precomputed embedding of signature-focused query (L2-normalized).
            max_lines: Maximum lines to search (for efficiency).
            target_api_name: API name to search for.
        
        Returns:
            Tuple of (char_position, best_score) where:
                - char_position: Character index of the best matching line's start
                - best_score: The similarity score (for comparing across candidates)
        """
        lines = text.splitlines(keepends=True)
        search_lines = lines[:max_lines]
        
        # Signature pattern: name followed by parentheses
        SIGNATURE_PATTERN = re.compile(r'(?:class\s+)?[\w\.]+\s*\([^)]*(?:\)|$)')
        
        # --- Extract target name variants for lexical matching ---
        # Priority: Full FQN > Parent.Name > Module.Name
        target_fqn = target_api_name.lower() if target_api_name else ""
        
        # Get parent.name (e.g., "nn.Conv1d" from "torch.nn.Conv1d")
        parts = target_api_name.split('.') if target_api_name else []
        target_parent_name = ""
        if len(parts) >= 2:
            target_parent_name = f"{parts[-2]}.{parts[-1]}".lower()  # e.g., "nn.conv1d"
        
        target_short = parts[-1].lower() if parts else ""  # e.g., "conv1d"
        
        line_data = []  # (line_idx, char_offset, line_text, looks_like_signature, name_match_score)
        char_offset = 0
        
        for i, line in enumerate(search_lines):
            stripped = line.strip()
            if stripped and len(stripped) > 2:
                looks_like_signature = bool(SIGNATURE_PATTERN.search(stripped))
                if stripped.startswith('class ') or 'property ' in stripped:
                    looks_like_signature = True
                
                # Lexical Name Matching with Priority
                line_lower = stripped.lower()
                
                # Check in order of specificity (most specific first)
                name_match_score = 0.0
                
                if target_fqn and target_fqn in line_lower:
                    # Full FQN match: "torch.nn.conv1d" in line
                    name_match_score = 1.0  # Best
                elif target_parent_name and target_parent_name in line_lower:
                    # Parent.Name match: "nn.conv1d" in line (but not full FQN)
                    name_match_score = 0.7  # Good
                elif target_short and target_short in line_lower:
                    # Short name only: "conv1d" in line (could be any conv1d)
                    name_match_score = 0.3  # Okay (might be wrong API)
                
                line_data.append((i, char_offset, stripped, looks_like_signature, name_match_score))
            char_offset += len(line)
        
        if not line_data:
            return (0, 0.0)
        
        # Embed all candidate lines
        line_texts = [ld[2] for ld in line_data]
        L = embedder.encode(line_texts)  # (N, D)
        
        # Compute base semantic similarities
        sims = L @ sig_query_vec  # (N,)
        
        # Apply signature structure bonus
        SIGNATURE_BONUS = 0.08
        NAME_MATCH_BONUS = 0.30  # Base bonus, scaled by match quality
        
        boosted_sims = sims.copy()
        for i, ld in enumerate(line_data):
            if ld[3]:  # looks_like_signature
                boosted_sims[i] += SIGNATURE_BONUS
            if ld[4] > 0:  # contains_target (both module AND short name)
                boosted_sims[i] += NAME_MATCH_BONUS * ld[4]
        
        # Find best matching line
        best_idx = int(np.argmax(boosted_sims))
        best_score = float(boosted_sims[best_idx])
        _, char_pos, _, _, _ = line_data[best_idx]
        
        return (char_pos, best_score)        

    
    def extract(
        self, 
        sections: List[Section], 
        members: List[MemberInput], 
        embedder: EmbeddingModel, 
        peer_signatures: Optional[Dict[str, List[str]]] = None,
        model_name: str = ""
    ) -> Dict[str, Any]:
        """
        Extract documentation snippets for multiple members using two-phase extraction.
        
        Phase 1: Extract classes and functions (no parent context needed)
        Phase 2: Extract methods with class anchor context (prevents mismatches)
        
        This ensures methods like 'fit()' are correctly associated with their
        parent class even when multiple classes have methods with the same name.
        
        Args:
            sections: Candidate sections (typically from Stage 1).
            members: List of module members (API FQNs, signature variants, optional docstrings).
            embedder: Embedding model for semantic scoring and snippet-level boosts.
            peer_signatures: Optional dictionary mapping API FQNs to their peer signatures.
            model_name: Name of the embedding model to use for semantic scoring.
        
        Returns:
            A dict mapping API FQNs to extraction results with text, pages, scores, etc.
        """
        if not sections or not members:
            return {}
        
        # =================================================================
        # PHASE 1: Separate members by type
        # =================================================================
        classes: List[MemberInput] = []
        methods: List[MemberInput] = []
        others: List[MemberInput] = []  # functions, variables, etc. (these set boundaries too)
        
        for m in members:
            if m.member_type == 'class':
                classes.append(m)
            elif m.member_type == 'method':
                methods.append(m)
            else:
                others.append(m)
        
        logger.debug(f"Two-phase extraction: {len(classes)} classes, {len(methods)} methods, {len(others)} others")
        
        # =================================================================
        # PHASE 2: Extract non-methods first (classes, functions)
        # =================================================================
        phase1_members = classes + others
        phase1_results = self._extract_members_batch(
            sections=sections,
            members=phase1_members,
            embedder=embedder,
            peer_signatures=peer_signatures,
            model_name=model_name,
            class_anchors=None  # No class context needed for phase 1
        )
        
        # =================================================================
        # PHASE 3: Build class anchor map from phase 1 results
        # =================================================================
        class_anchors = self._build_class_anchor_map(classes, phase1_results, sections)
        
        if class_anchors:
            logger.debug(f"Built class anchor map with {len(class_anchors)} classes")
        
        # =================================================================
        # PHASE 4: Extract methods with class context
        # =================================================================
        phase2_results = self._extract_members_batch(
            sections=sections,
            members=methods,
            embedder=embedder,
            peer_signatures=peer_signatures,
            model_name=model_name,
            class_anchors=class_anchors  # Provide class context for methods
        )
        
        # =================================================================
        # PHASE 5: Combine and reorder results to match original input
        # =================================================================
        all_results: Dict[str, Any] = {}
        
        # Add phase 1 results
        for m, r in zip(phase1_members, phase1_results):
            if r:
                all_results[m.api_name] = r
        
        # Add phase 2 results
        for m, r in zip(methods, phase2_results):
            if r:
                all_results[m.api_name] = r
        
        # Build final output in original member order
        output: Dict[str, Any] = {}
        for m in members:
            if m.api_name in all_results:
                output[m.api_name] = all_results[m.api_name]
            else:
                # Member not found - add placeholder
                output[m.api_name] = {
                    "text": "",
                    "pages": [],
                    "section_path": [],
                    "scores": {"lexical": 0.0, "semantic": 0.0, "final": 0.0, "match_type": "not_found"},
                    "warning": "Member documentation not found in PDF."
                }
        
        return output
    
    
    def _build_class_anchor_map(
        self,
        phase1_members: List[MemberInput],
        phase1_results: List[Dict[str, Any]],
        sections: List[Section]
    ) -> Dict[str, Tuple[int, int, int]]:
        """
        Build anchor map for all non-method members (classes and functions).
        
        Scope boundaries for method extraction:
            - Start: Parent class's anchor position
            - End: Next non-method (class/function) anchor position OR section end
        
        This ensures methods are only searched within the text region between
        their parent class and the next top-level member, preventing false
        matches to methods of other classes with the same name.
        
        Args:
            phase1_members: Classes and functions extracted in phase 1
            phase1_results: Extraction results from phase 1
            sections: List of sections for determining section boundaries
        
        Returns:
            Dict mapping member API names to (section_idx, anchor_pos, scope_end)
        """
        class_anchors: Dict[str, Tuple[int, int, int]] = {}
        
        # Collect all non-method anchors (classes AND functions)
        anchor_data = []  # List of (api_name, section_idx, anchor_pos, member_type)
        
        for m, result in zip(phase1_members, phase1_results):
            if result is None:
                continue
            
            section_idx = result.get("idx", -1)
            anchor_pos = result.get("anchor_pos", -1)
            
            if section_idx >= 0 and anchor_pos >= 0:
                anchor_data.append((m.api_name, section_idx, anchor_pos, m.member_type))
        
        if not anchor_data:
            return class_anchors
        
        # Sort by section index, then by position within section
        anchor_data.sort(key=lambda x: (x[1], x[2]))
        
        # Determine scope_end for each anchor
        for i, (api_name, sec_idx, anchor_pos, member_type) in enumerate(anchor_data):
            # Default: end of section
            section_end = len(sections[sec_idx].text_raw) if sec_idx < len(sections) else 0
            scope_end = section_end
            
            # Find next non-method member in the SAME section
            for j in range(i + 1, len(anchor_data)):
                next_api_name, next_sec_idx, next_anchor_pos, next_type = anchor_data[j]
                
                if next_sec_idx != sec_idx:
                    # Different section - stop looking
                    break
                
                # Next class or function in same section = our scope end
                # (next_type is already non-method since phase1 only has classes/functions)
                scope_end = next_anchor_pos
                break
            
            class_anchors[api_name] = (sec_idx, anchor_pos, scope_end)
            
            logger.debug(
                f"Anchor [{member_type}]: {api_name} -> section {sec_idx}, "
                f"pos {anchor_pos}-{scope_end}"
            )
        
        return class_anchors
    
    
    def _extract_members_batch(
        self,
        sections: List[Section],
        members: List[MemberInput],
        embedder: EmbeddingModel,
        peer_signatures: Optional[Dict[str, List[str]]],
        model_name: str,
        class_anchors: Optional[Dict[str, Tuple[int, int, int]]] = None
    ) -> List[Dict[str, Any]]:
        """
        Extract documentation for a batch of members.
        
        This is the core extraction logic, factored out to support two-phase extraction.
        When class_anchors is provided (phase 2), method extraction is scoped to the
        parent class's section and position range.
        
        Args:
            sections: Candidate sections for extraction
            members: List of members to extract
            embedder: Embedding model for semantic search
            peer_signatures: Stop signal signatures for each member
            model_name: Name of embedding model
            class_anchors: Optional dict of class positions for scoping method extraction
        
        Returns:
            List of extraction result dicts (same order as members input)
        """
        if not members:
            return []
        
        output_results: List[Optional[Dict[str, Any]]] = [None] * len(members)
        
        # Precompute common data
        lengths = np.array([max(1.0, len(s.text_norm) / 5000.0) for s in sections], dtype=float)
        
        patterns_per = [build_signature_patterns(mi) for mi in members]
        needles_per = [build_lexical_needles(mi) for mi in members]
        semantic_queries = [build_semantic_query(mi, model_name) for mi in members]
        
        # Pre-compute lexical scores for all members × sections
        scores_per: List[np.ndarray] = []
        matches_per: List[List[Tuple[int, int, str]]] = []
        
        if self.cfg.semantic_mode != "only":
            for j, mi in enumerate(members):
                needles = needles_per[j]
                member_scores = []
                member_matches = []
                
                for sec in sections:
                    score, line_idx, char_offset, match_type = section_match_score(
                        sec.text_norm, needles, section_title=sec.title
                    )
                    member_scores.append(score)
                    member_matches.append((line_idx, char_offset, match_type))
                
                scores_per.append(np.array(member_scores, dtype=float))
                matches_per.append(member_matches)
        else:
            for _ in range(len(members)):
                scores_per.append(np.zeros(len(sections), dtype=float))
                matches_per.append([(-1, -1, "none")] * len(sections))
        
        # Decide semantic usage per member
        use_semantic_member = []
        for j in range(len(members)):
            if self.cfg.semantic_mode == "always":
                use_semantic_member.append(True)
            elif self.cfg.semantic_mode == "only":
                use_semantic_member.append(True)
            elif self.cfg.semantic_mode == "never":
                use_semantic_member.append(False)
            else:  # auto
                lex_scores = scores_per[j] - 0.05 * lengths
                needs_semantic = _should_use_semantic_member(
                    lex_scores, self.cfg.lexical_sigma_k, self.cfg.lexical_margin_min
                )
                use_semantic_member.append(needs_semantic)
        
        # Precompute embeddings if needed
        W: Optional[np.ndarray] = None
        Q: Optional[np.ndarray] = None
        section_to_win_indices: List[List[int]] = [[] for _ in range(len(sections))]
        
        if any(use_semantic_member) and sections:
            window_texts: List[str] = []
            win_chars, win_stride = self._effective_window_params(embedder)
            
            for s_idx, sec in enumerate(sections):
                context_prefix = "\n".join(sec.path or [])
                if context_prefix:
                    context_prefix += "\n"
                context_prefix += sec.title or ""
                if context_prefix:
                    context_prefix += "\n\n"
                
                spans = _windows(sec.text_norm, win_chars, win_stride)
                for a, b in spans:
                    section_to_win_indices[s_idx].append(len(window_texts))
                    window_content = context_prefix + sec.text_norm[a:b]
                    window_texts.append(build_passage_text(window_content, model_name))
            
            if window_texts:
                W = embedder.encode(window_texts)
            Q = embedder.encode(semantic_queries)
        
        # Define the refinement function (closure over precomputed data)
        def refine_one(j: int) -> Tuple[int, Optional[Dict[str, Any]]]:
            """Extract single member with optional class scoping."""
            mi = members[j]
            needles = needles_per[j]
            
            # --- Determine if this method should be scoped to a parent class ---
            scoped_section_idx: Optional[int] = None
            scoped_start: Optional[int] = None
            scoped_end: Optional[int] = None
            
            if class_anchors and mi.member_type == 'method':
                # Parse parent class from method API name
                parts = mi.api_name.rsplit('.', 1)
                if len(parts) >= 2:
                    parent_class_name = parts[0]
                    
                    # Look up parent class anchor
                    if parent_class_name in class_anchors:
                        scoped_section_idx, scoped_start, scoped_end = class_anchors[parent_class_name]
                        logger.debug(f"Method {mi.api_name} scoped to class {parent_class_name}: "
                                   f"section {scoped_section_idx}, chars {scoped_start}-{scoped_end}")
                    else:
                        # Try partial match (handle re-exports)
                        parent_short = parent_class_name.split('.')[-1]
                        for class_fqn, anchor_info in class_anchors.items():
                            if class_fqn.endswith(f'.{parent_short}') or class_fqn == parent_short:
                                scoped_section_idx, scoped_start, scoped_end = anchor_info
                                logger.debug(f"Method {mi.api_name} scoped to class {class_fqn} (partial match)")
                                break
            
            # --- Compute section scores (possibly scoped) ---
            section_scores = scores_per[j].tolist()
            section_matches = matches_per[j]
            
            # Apply length penalty
            for s_idx in range(len(sections)):
                length_factor = len(sections[s_idx].text_norm) / 5000.0
                section_scores[s_idx] -= 0.05 * length_factor
            
            # Add semantic scores if enabled
            sem_scores = np.zeros(len(sections), dtype=float)
            if self.cfg.semantic_mode != "never" and use_semantic_member[j] and W is not None and Q is not None:
                q_vec = Q[j]
                sims = W @ q_vec
                for s_idx, win_indices in enumerate(section_to_win_indices):
                    if win_indices:
                        sem_scores[s_idx] = float(np.max(sims[win_indices]))
            
            # Combine scores
            scores_arr = np.array(section_scores, dtype=float)
            mode = self.cfg.semantic_mode
            
            if mode == "only":
                finals = sem_scores if np.any(sem_scores != 0.0) else scores_arr
            elif mode == "never":
                finals = scores_arr
            else:
                finals = scores_arr + sem_scores
            
            # --- Determine which sections to search ---
            if scoped_section_idx is not None:
                # METHOD WITH CLASS SCOPE: Only search the parent class's section
                ranked_indices = np.array([scoped_section_idx])
            else:
                # NORMAL: Rank all sections
                K = min(self.cfg.topK_sections, len(sections))
                ranked_indices = finals.argsort()[::-1][:K]
                
                thr = _dynamic_threshold(finals[ranked_indices]) if len(ranked_indices) > 0 else -1e9
                ranked_indices = np.array([i for i in ranked_indices if finals[i] >= thr])
            
            # Initialize stop matcher
            stop_matcher = None
            if peer_signatures and mi.api_name in peer_signatures:
                stop_matcher = StopSignalMatcher(
                    peer_signatures=peer_signatures[mi.api_name],
                    target_member_type=mi.member_type,
                    target_api_name=mi.api_name
                )
            
            # Helper: Find anchor in raw text (possibly scoped)
            def find_anchor_in_raw(sec_obj: Section) -> int:
                """Find needle position, optionally scoped to class region."""
                raw_text = sec_obj.text_raw
                
                # If scoped, only search within class region
                if scoped_start is not None and scoped_end is not None:
                    search_text = raw_text[scoped_start:scoped_end]
                    search_lower = search_text.lower()
                    offset = scoped_start
                else:
                    search_text = raw_text
                    search_lower = raw_text.lower()
                    offset = 0
                
                # Priority 1: Exact signatures
                for needle in needles.get("exact", []):
                    needle_norm = ' '.join(needle.lower().split())
                    pos = search_lower.find(needle_norm)
                    if pos >= 0:
                        return offset + pos
                
                # Priority 2: Prefix patterns
                for needle in needles.get("prefix", []):
                    pos = search_lower.find(needle.lower())
                    if pos >= 0:
                        return offset + pos
                
                # Priority 3: Anchor patterns with word boundary + paren
                for needle in needles.get("anchor", []):
                    pattern = r'\b' + re.escape(needle) + r'\s*\('
                    match = re.search(pattern, search_text, re.IGNORECASE)
                    if match:
                        return offset + match.start()
                
                # Priority 4: Just anchor name
                for needle in needles.get("anchor", []):
                    pattern = r'\b' + re.escape(needle) + r'\b'
                    match = re.search(pattern, search_text, re.IGNORECASE)
                    if match:
                        return offset + match.start()
                
                return -1
            
            # --- Extraction Strategies ---
            best = None
            best_final_score = -1e9
            
            min_lexical = self.cfg.min_lexical_score
            min_semantic = self.cfg.min_semantic_score
            min_fallback = self.cfg.min_fallback_score
            
            skip_lexical = (self.cfg.semantic_mode == "only")
            
            if not skip_lexical:
                # Strategy 1: Direct anchor search
                for idx in ranked_indices:
                    sec_obj = sections[idx]
                    line_idx, char_offset, match_type = section_matches[idx]
                    
                    if line_idx >= 0 and match_type != "none" and section_scores[idx] >= min_lexical:
                        raw_pos = find_anchor_in_raw(sec_obj)
                        if raw_pos >= 0:
                            snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, raw_pos, stop_matcher)
                            if snippet.strip():
                                score = finals[idx]
                                if score > best_final_score:
                                    best_final_score = score
                                    best = {
                                        "api_name": mi.api_name,
                                        "snippet": snippet,
                                        "pages": pages,
                                        "section_path": sec_obj.path or [sec_obj.title],
                                        "idx": idx,
                                        "anchor_pos": raw_pos,
                                        "base_scores": {
                                            "lexical": float(section_scores[idx]),
                                            "semantic": float(sem_scores[idx]),
                                            "final": float(score),
                                            "match_type": match_type
                                        }
                                    }
                
                # Strategy 2: Try all ranked sections
                if not best:
                    for idx in ranked_indices:
                        if section_scores[idx] < min_lexical:
                            continue
                        
                        sec_obj = sections[idx]
                        raw_pos = find_anchor_in_raw(sec_obj)
                        
                        if raw_pos >= 0:
                            snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, raw_pos, stop_matcher)
                            if snippet.strip():
                                best = {
                                    "api_name": mi.api_name,
                                    "snippet": snippet,
                                    "pages": pages,
                                    "section_path": sec_obj.path or [sec_obj.title],
                                    "idx": idx,
                                    "anchor_pos": raw_pos,
                                    "base_scores": {
                                        "lexical": float(section_scores[idx]),
                                        "semantic": float(sem_scores[idx]),
                                        "final": float(finals[idx]),
                                        "match_type": "raw_search"
                                    }
                                }
                                break
                
                # Strategy 3: Regex fallback
                if not best:
                    for idx in ranked_indices:
                        if section_scores[idx] < min_lexical:
                            continue
                        
                        sec_obj = sections[idx]
                        match_pos = self._regex_refine(sec_obj, patterns_per[j])
                        
                        # Apply scoping if needed
                        if scoped_start is not None and match_pos >= 0:
                            if match_pos < scoped_start or match_pos >= scoped_end:
                                continue  # Outside class scope
                        
                        if match_pos >= 0:
                            snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, match_pos, stop_matcher)
                            if snippet.strip():
                                best = {
                                    "api_name": mi.api_name,
                                    "snippet": snippet,
                                    "pages": pages,
                                    "section_path": sec_obj.path or [sec_obj.title],
                                    "idx": idx,
                                    "anchor_pos": match_pos,
                                    "base_scores": {
                                        "lexical": float(section_scores[idx]),
                                        "semantic": float(sem_scores[idx]),
                                        "final": float(finals[idx]),
                                        "match_type": "regex"
                                    }
                                }
                                break
            
            # Strategy 4: Semantic window search (respects scoping)
            if not best and use_semantic_member[j] and Q is not None:
                q_vec = Q[j]
                sig_query = build_signature_query(mi, model_name)
                sig_q_vec = embedder.encode([sig_query])[0] if embedder else None
                
                num_sections_to_try = min(3, len(ranked_indices))
                section_candidates = []
                
                for rank, idx in enumerate(ranked_indices[:num_sections_to_try]):
                    sec_obj = sections[idx]
                    
                    sw_result = self._semantic_window_search(
                        embedder, sec_obj, q_vec, sig_query_vec=sig_q_vec, api_name=mi.api_name
                    )
                    
                    if sw_result is not None:
                        anchor_pos, fine_score = sw_result
                        
                        # Apply class scoping check
                        if scoped_start is not None:
                            if anchor_pos < scoped_start or anchor_pos >= scoped_end:
                                continue  # Outside class scope
                        
                        if anchor_pos >= 0 and fine_score >= (min_semantic / 100.0):
                            snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, anchor_pos, stop_matcher)
                            if snippet.strip():
                                section_candidates.append({
                                    "anchor_pos": anchor_pos,
                                    "fine_score": fine_score,
                                    "section_idx": idx,
                                    "section_obj": sec_obj,
                                    "snippet": snippet,
                                    "pages": pages,
                                    "rank": rank
                                })
                
                if section_candidates:
                    best_candidate = max(section_candidates, key=lambda c: (c["fine_score"], -c["rank"]))
                    sec_idx = best_candidate["section_idx"]
                    best = {
                        "api_name": mi.api_name,
                        "snippet": best_candidate["snippet"],
                        "pages": best_candidate["pages"],
                        "section_path": best_candidate["section_obj"].path or [best_candidate["section_obj"].title],
                        "idx": sec_idx,
                        "anchor_pos": best_candidate["anchor_pos"],
                        "base_scores": {
                            "lexical": float(section_scores[sec_idx]),
                            "semantic": float(sem_scores[sec_idx]),
                            "final": float(finals[sec_idx]),
                            "match_type": "semantic_window"
                        },
                        "warning": None if skip_lexical else "No direct anchor found; semantic window fallback used."
                    }
            
            # Strategy 5: Final fallback
            if not best:
                top_idx = int(ranked_indices[0]) if len(ranked_indices) > 0 else 0
                final_score = float(finals[top_idx]) if len(finals) > top_idx else 0.0
                
                if final_score >= min_fallback and top_idx < len(sections):
                    sec_obj = sections[top_idx]
                    
                    # Determine extraction start (scoped or section start)
                    extract_start = scoped_start if scoped_start is not None else 0
                    
                    snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, extract_start, stop_matcher=None)
                    if snippet.strip():
                        best = {
                            "api_name": mi.api_name,
                            "snippet": snippet,
                            "pages": pages,
                            "section_path": sec_obj.path or [sec_obj.title],
                            "idx": top_idx,
                            "anchor_pos": extract_start,
                            "base_scores": {
                                "lexical": float(section_scores[top_idx]) if section_scores else 0.0,
                                "semantic": float(sem_scores[top_idx]) if len(sem_scores) > top_idx else 0.0,
                                "final": final_score,
                                "match_type": "fallback"
                            },
                            "warning": "Low-confidence match; using top-ranked section start."
                        }
                
                if not best:
                    best = {
                        "api_name": mi.api_name,
                        "snippet": "",
                        "pages": [],
                        "section_path": [],
                        "idx": -1,
                        "anchor_pos": -1,
                        "base_scores": {
                            "lexical": 0.0,
                            "semantic": 0.0,
                            "final": final_score if 'final_score' in dir() else 0.0,
                            "match_type": "not_found"
                        },
                        "warning": f"Member documentation not found in PDF."
                    }
            
            return j, best
        
        # Execute refinement in parallel
        results = [None] * len(members)
        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            futures = [ex.submit(refine_one, j) for j in range(len(members))]
            for fut in as_completed(futures):
                j, best = fut.result()
                results[j] = best
        
        # Apply snippet boost and build final output
        for j, r in enumerate(results):
            if not r:
                continue
            
            boost = 0.0
            if use_semantic_member[j] and Q is not None:
                sec_idx = r.get("idx", -1)
                if sec_idx >= 0 and sec_idx < len(sections):
                    sec = sections[sec_idx]
                    context_prefix = "\n".join(sec.path or [])
                    if context_prefix:
                        context_prefix += "\n"
                    context_prefix += sec.title or ""
                    if context_prefix:
                        context_prefix += "\n\n"
                    contextual_snippet = context_prefix + r["snippet"]
                else:
                    contextual_snippet = r["snippet"]
                
                snip_vec = embedder.encode([build_passage_text(contextual_snippet, model_name)])[0]
                boost = float(cosine_similarity(Q[j], snip_vec.reshape(1, -1))[0])
            
            base = r.get("base_scores", {})
            final_score = base.get("final", 0.0) + self.cfg.snippet_boost_weight * boost
            
            output_results[j] = {
                "text": r["snippet"],
                "pages": r["pages"],
                "section_path": r["section_path"],
                "idx": r.get("idx", -1),
                "anchor_pos": r.get("anchor_pos", -1),
                "scores": {
                    "lexical": base.get("lexical", 0.0),
                    "semantic": base.get("semantic", 0.0),
                    "final": final_score,
                    "match_type": base.get("match_type", "unknown")
                }
            }
            if "warning" in r and r["warning"]:
                output_results[j]["warning"] = r["warning"]
        
        return output_results
    
    
    # def extract(
    #     self, 
    #     sections: List[Section], 
    #     members: List[MemberInput], 
    #     embedder: EmbeddingModel, 
    #     peer_signatures: Optional[Dict[str, List[str]]] = None,
    #     model_name: str = ""
    #     ) -> Dict[str, Any]:
    #     """
    #     Extract documentation snippets for multiple members from the given sections.

    #     For each member:
    #         - Compute lexical-only scores and decide semantic usage (based on config).
    #         - Optionally compute semantic similarity and hybrid scores.
    #         - Refine by regex anchoring; fall back to semantic window search if needed.
    #         - Optionally add a snippet-level semantic boost (batched across members).

    #     Args:
    #         sections: Candidate sections (typically from Stage 1).
    #         members: List of module members (API FQNs, signature variants, optional docstrings).
    #         embedder: Embedding model for semantic scoring and snippet-level boosts.
    #         peer_signatures: Optional dictionary mapping API FQNs to their peer signatures.
    #         model_name: Name of the embedding model to use for semantic scoring.

    #     Returns:
    #         A dict mapping API FQNs to:
    #             {
    #                 "text": verbatim snippet,
    #                 "pages": list of page indices (coarse),
    #                 "section_path": hierarchical titles or a single section title,
    #                 "scores": {"exact": float, "fuzzy": float, "semantic": float, "final": float},
    #                 "warning": optional string if fallbacks were used
    #             }
    #     """
        
    #     output: Dict[str, Any] = {}

    #     # Precompute common lexical signals per section
    #     lengths = np.array([max(1.0, len(s.text_norm) / 5000.0) for s in sections], dtype=float)

    #     # Prepare per-member metadata
    #     patterns_per = [build_signature_patterns(mi) for mi in members]
    #     needles_per = [build_lexical_needles(mi) for mi in members]
    #     semantic_queries = [build_semantic_query(mi, model_name) for mi in members]
        
    #     # --- Pre-compute full lexical scores for all members × sections ---
    #     # This serves two purposes:
    #     # 1. Auto-gate decision (whether to compute semantics per member)
    #     # 2. Reuse in refine_one() to avoid double computation
        
    #     scores_per: List[np.ndarray] = [] # scores_per[j] = array of scores for member j across all sections
    #     matches_per: List[List[Tuple[int, int, str]]] = [] # matches_per[j] = list of (line_idx, char_offset, match_type) tuples
        
    #     if self.cfg.semantic_mode != "only":
    #         for j, mi in enumerate(members):
    #             needles = needles_per[j]
    #             member_scores = []
    #             member_matches = []
                
    #             for sec in sections:
    #                 score, line_idx, char_offset, match_type = section_match_score(
    #                     sec.text_norm,
    #                     needles,
    #                     section_title=sec.title
    #                 )
    #                 member_scores.append(score)
    #                 member_matches.append((line_idx, char_offset, match_type))
                
    #             scores_per.append(np.array(member_scores, dtype=float))
    #             matches_per.append(member_matches)
    #     else:
    #         # Placeholder empty arrays for 'only' mode
    #         for j in range(len(members)):
    #             scores_per.append(np.zeros(len(sections), dtype=float))
    #             matches_per.append([(-1, -1, "none")] * len(sections))
        
    #     #--- Decide which members need semantic (auto gate per member) ---
    #     use_semantic_member = []
    #     for j in range(len(members)):
    #         if self.cfg.semantic_mode == "always":
    #             use_semantic_member.append(True)
    #         elif self.cfg.semantic_mode == "only":
    #             use_semantic_member.append(True)
    #         elif self.cfg.semantic_mode == "never":
    #             use_semantic_member.append(False)
    #         else: # auto mode
    #             # Apply length penalty to scores for the decision
    #             lex_scores = scores_per[j] - 0.05 * lengths
    #             # Use the statistical method to decide
    #             needs_semantic = _should_use_semantic_member(
    #                 lex_scores, 
    #                 self.cfg.lexical_sigma_k, 
    #                 self.cfg.lexical_margin_min
    #             )
    #             use_semantic_member.append(needs_semantic)

    #     # Precompute window-level embeddings and query embeddings if any member needs semantics
    #     W: Optional[np.ndarray] = None
    #     Q: Optional[np.ndarray] = None
    #     section_to_win_indices: List[List[int]] = [[] for _ in range(len(sections))]

    #     if any(use_semantic_member) and sections:
    #         window_texts: List[str] = []
    #         # Use embedding-aligned window parameters globally for all sections.
    #         win_chars, win_stride = self._effective_window_params(embedder)
            
    #         for s_idx, sec in enumerate(sections):
    #             # Build contextual prefix for this section (hierarchical path + title)
    #             context_prefix = "\n".join(sec.path or [])  # Hierarchical path
    #             if context_prefix:
    #                 context_prefix += "\n"
    #             context_prefix += sec.title or ""
    #             if context_prefix:
    #                 context_prefix += "\n\n"  # Separate from body
                
    #             spans = _windows(sec.text_norm, win_chars, win_stride)
    #             for a, b in spans:
    #                 section_to_win_indices[s_idx].append(len(window_texts))
    #                 window_content = context_prefix + sec.text_norm[a:b]
    #                 window_texts.append(build_passage_text(window_content, model_name))

    #         if window_texts:
    #             W = embedder.encode(window_texts)
    #         Q = embedder.encode(semantic_queries) # Encode semantic queries (already have query prefix if e5)
        
        
    #     def refine_one(j: int):
    #         """
    #         Refine extraction for a single member using hybrid matching.
            
    #         Strategy:
    #             1. Use pre-computed section scores (from text_norm) for ranking
    #             2. Optionally add semantic scores if enabled
    #             3. For the best-ranked sections, find anchor directly in text_raw
    #             4. Extract using line expansion with stop signals
                
    #         Note: text_norm is used for SECTION SELECTION (consistent matching), but text_raw is used for ANCHOR FINDING (exact position for extraction). Both contain joined signatures from SignatureJoiner.
    #         """
    #         mi = members[j]
    #         needles = needles_per[j]  # Dict with "exact", "prefix", "anchor" tiers
            
    #         # --- Use pre-computed lexical scores and match positions ---
    #         section_scores = scores_per[j].tolist()  # Already computed from text_norm
    #         section_matches = matches_per[j]          # (line_idx, char_offset, match_type) per section
            
    #         # Apply length penalty (consistent with auto-gate decision)
    #         for s_idx in range(len(sections)):
    #             length_factor = len(sections[s_idx].text_norm) / 5000.0
    #             section_scores[s_idx] -= 0.05 * length_factor
            
    #         # --- Add semantic scores if enabled ---
    #         sem_scores = np.zeros(len(sections), dtype=float)
    #         if (
    #             self.cfg.semantic_mode != "never"
    #             and use_semantic_member[j]
    #             and W is not None
    #             and Q is not None
    #         ):
    #             q_vec = Q[j]  # (D,)
    #             sims = W @ q_vec  # (W,)
    #             for s_idx, win_indices in enumerate(section_to_win_indices):
    #                 if win_indices:
    #                     sem_scores[s_idx] = float(np.max(sims[win_indices]))
            
    #         # --- Combine scores based on mode ---
    #         scores_arr = np.array(section_scores, dtype=float)
    #         mode = self.cfg.semantic_mode
            
    #         if mode == "only":
    #             # Semantic-only ranking
    #             if np.any(sem_scores != 0.0):
    #                 finals = sem_scores
    #             else:
    #                 finals = scores_arr  # Fallback to lexical if no semantics
    #         elif mode == "never":
    #             # Pure lexical
    #             finals = scores_arr
    #         else:
    #             # "auto" or "always": hybrid
    #             # Lexical provides localization, semantic provides relevance
    #             finals = scores_arr + sem_scores
            
    #         # --- Shortlist top-K sections ---
    #         K = min(self.cfg.topK_sections, len(sections))
    #         ranked_indices = finals.argsort()[::-1][:K]
            
    #         # Dynamic threshold to filter weak candidates
    #         thr = _dynamic_threshold(finals[ranked_indices]) if len(ranked_indices) > 0 else -1e9
    #         ranked_indices = np.array([i for i in ranked_indices if finals[i] >= thr])
            
    #         # --- Initialize Stop Matcher for this member (type-aware) ---
    #         stop_matcher = None
    #         if peer_signatures and mi.api_name in peer_signatures:
    #             stop_matcher = StopSignalMatcher(
    #                 peer_signatures=peer_signatures[mi.api_name],
    #                 target_member_type=mi.member_type,
    #                 target_api_name=mi.api_name
    #             )
            
    #         # --- Helper: Find anchor position directly in text_raw ---
    #         def find_anchor_in_raw(sec_obj: Section) -> int:
    #             """
    #             Search for needle directly in text_raw for accurate anchor position.
                
    #             Searches in priority order (most specific first):
    #                 1. Exact signatures
    #                 2. Prefix patterns (name + open paren)
    #                 3. Anchor patterns with word boundaries
                
    #             Returns:
    #                 Character position in text_raw, or -1 if not found.
    #             """
    #             raw_text = sec_obj.text_raw
    #             raw_lower = raw_text.lower()
                
    #             # Priority 1: Exact signature matches
    #             for needle in needles.get("exact", []):
    #                 needle_lower = needle.lower()
    #                 # Normalize whitespace in needle for matching
    #                 needle_norm = ' '.join(needle_lower.split())
    #                 pos = raw_lower.find(needle_norm)
    #                 if pos >= 0:
    #                     return pos
                
    #             # Priority 2: Prefix patterns (name + opening paren)
    #             for needle in needles.get("prefix", []):
    #                 needle_lower = needle.lower()
    #                 pos = raw_lower.find(needle_lower)
    #                 if pos >= 0:
    #                     return pos
                
    #             # Priority 3: Anchor patterns with word boundary + opening paren
    #             # This helps find "ModuleList(" even if preceded by "class "
    #             for needle in needles.get("anchor", []):
    #                 # Pattern: word boundary + name + optional whitespace + (
    #                 pattern = r'\b' + re.escape(needle) + r'\s*\('
    #                 match = re.search(pattern, raw_text, re.IGNORECASE)
    #                 if match:
    #                     return match.start()
                
    #             # Priority 4: Just the anchor name with word boundary (last resort)
    #             for needle in needles.get("anchor", []):
    #                 pattern = r'\b' + re.escape(needle) + r'\b'
    #                 match = re.search(pattern, raw_text, re.IGNORECASE)
    #                 if match:
    #                     return match.start()
                
    #             return -1
            
    #         # --- Anchoring & Extraction ---
    #         best = None
    #         best_final_score = -1e9
            
    #         # Get thresholds from config
    #         min_lexical = self.cfg.min_lexical_score
    #         min_semantic = self.cfg.min_semantic_score
    #         min_fallback = self.cfg.min_fallback_score
            
    #         skip_lexical = (self.cfg.semantic_mode == "only")
    #         if not skip_lexical:
    #             # Strategy 1: Direct anchor search in text_raw for top-ranked sections
    #             # We use text_norm scoring to SELECT sections, but search in text_raw for POSITION
    #             for idx in ranked_indices:
    #                 sec_obj = sections[idx]
    #                 line_idx, char_offset, match_type = section_matches[idx]
                    
    #                 # Only proceed if text_norm scoring found a match (section is relevant)
    #                 if line_idx >= 0 and match_type != "none" and section_scores[idx] >= min_lexical:
    #                     # Find anchor directly in text_raw (not via ratio mapping)
    #                     raw_pos = find_anchor_in_raw(sec_obj)
    #                     if raw_pos >= 0:
    #                         # Extract with line expansion
    #                         snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, raw_pos, stop_matcher)
    #                         if snippet.strip():
    #                             score = finals[idx]
    #                             if score > best_final_score:
    #                                 best_final_score = score
    #                                 best = {
    #                                     "api_name": mi.api_name,
    #                                     "snippet": snippet,
    #                                     "pages": pages,
    #                                     "section_path": sec_obj.path or [sec_obj.title],
    #                                     "idx": idx,
    #                                     "base_scores": {
    #                                         "lexical": float(section_scores[idx]),
    #                                         "semantic": float(sem_scores[idx]),
    #                                         "final": float(score),
    #                                         "match_type": match_type
    #                                     }
    #                                 }
                
    #             # Strategy 2: Try all ranked sections even if text_norm didn't find match
    #             # (Catches cases where normalization differences caused missed matches)
    #             if not best:
    #                 for idx in ranked_indices:
    #                     # Skip if score is too low
    #                     if section_scores[idx] < min_lexical:
    #                         continue
                        
    #                     sec_obj = sections[idx]
    #                     raw_pos = find_anchor_in_raw(sec_obj)
                        
    #                     if raw_pos >= 0:
    #                         snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, raw_pos, stop_matcher)
    #                         if snippet.strip():
    #                             score = finals[idx]
    #                             best = {
    #                                 "api_name": mi.api_name,
    #                                 "snippet": snippet,
    #                                 "pages": pages,
    #                                 "section_path": sec_obj.path or [sec_obj.title],
    #                                 "idx": idx,
    #                                 "base_scores": {
    #                                     "lexical": float(section_scores[idx]),
    #                                     "semantic": float(sem_scores[idx]),
    #                                     "final": float(score),
    #                                     "match_type": "raw_search"
    #                                 }
    #                             }
    #                             break
                
    #             # Strategy 3: Regex anchor fallback (uses existing patterns)
    #             if not best:
    #                 for idx in ranked_indices:
    #                     # Skip if score is too low
    #                     if section_scores[idx] < min_lexical:
    #                         continue
                        
    #                     sec_obj = sections[idx]
    #                     match_pos = self._regex_refine(sec_obj, patterns_per[j])
    #                     if match_pos >= 0:
    #                         snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, match_pos, stop_matcher)
    #                         if snippet.strip():
    #                             score = finals[idx]
    #                             best = {
    #                                 "api_name": mi.api_name,
    #                                 "snippet": snippet,
    #                                 "pages": pages,
    #                                 "section_path": sec_obj.path or [sec_obj.title],
    #                                 "idx": idx,
    #                                 "base_scores": {
    #                                     "lexical": float(section_scores[idx]),
    #                                     "semantic": float(sem_scores[idx]),
    #                                     "final": float(score),
    #                                     "match_type": "regex"
    #                                 }
    #                             }
    #                             break
            
    #         # Strategy 4: Semantic window search fallback - CROSS-SECTION evaluation
    #         if not best and use_semantic_member[j] and Q is not None:
    #             q_vec = Q[j]
    #             # Build signature-focused query for fine-grained anchoring
    #             sig_query = build_signature_query(mi, model_name)
    #             sig_q_vec = embedder.encode([sig_query])[0] if embedder else None
                
    #             # Evaluate top 3 SECTIONS, not just the first one
    #             num_sections_to_try = min(3, len(ranked_indices))
    #             section_candidates = []  # List of (anchor_pos, fine_score, section_idx, snippet, pages)
                
    #             for rank, idx in enumerate(ranked_indices[:num_sections_to_try]):
    #                 sec_obj = sections[idx]
                    
    #                 # Get best anchor position AND score from this section
    #                 sw_result = self._semantic_window_search(embedder, sec_obj, q_vec, sig_query_vec=sig_q_vec, api_name=mi.api_name)
                    
    #                 if sw_result is not None:
    #                     anchor_pos, fine_score = sw_result
    #                     if anchor_pos >= 0 and fine_score >= (min_semantic / 100.0):
    #                         # Extract snippet for this candidate
    #                         snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, anchor_pos, stop_matcher)
    #                         if snippet.strip():
    #                             section_candidates.append({
    #                                 "anchor_pos": anchor_pos,
    #                                 "fine_score": fine_score,
    #                                 "section_idx": idx,
    #                                 "section_obj": sec_obj,
    #                                 "snippet": snippet,
    #                                 "pages": pages,
    #                                 "rank": rank
    #                             })
                
    #             # Select best candidate across all sections
    #             if section_candidates:
    #                 # Primary: best fine-grained semantic score
    #                 # Tie-breaker: prefer earlier-ranked sections (higher lexical/semantic relevance)
    #                 best_candidate = max(
    #                     section_candidates, 
    #                     key=lambda c: (c["fine_score"], -c["rank"])
    #                 )
                    
    #                 sec_idx = best_candidate["section_idx"]
    #                 best = {
    #                     "api_name": mi.api_name,
    #                     "snippet": best_candidate["snippet"],
    #                     "pages": best_candidate["pages"],
    #                     "section_path": best_candidate["section_obj"].path or [best_candidate["section_obj"].title],
    #                     "idx": sec_idx,
    #                     "base_scores": {
    #                         "lexical": float(section_scores[sec_idx]),
    #                         "semantic": float(sem_scores[sec_idx]),
    #                         "final": float(finals[sec_idx]),
    #                         "match_type": "semantic_window"
    #                     },
    #                     "warning": None if skip_lexical else "No direct anchor found; semantic window fallback used."
    #                 }
            
    #         # Strategy 5: Final fallback - CONDITIONAL on reasonable score
    #         if not best:
    #             top_idx = int(ranked_indices[0]) if len(ranked_indices) > 0 else 0
    #             final_score = float(finals[top_idx]) if len(finals) > top_idx else 0.0
                
    #             # Only use fallback if score indicates the member might be in this section
    #             if final_score >= min_fallback and top_idx < len(sections):
    #                 sec_obj = sections[top_idx]
    #                 snippet, pages = self.extractor.extract_by_line_expansion(sec_obj, 0, stop_matcher=None)
    #                 best = {
    #                     "api_name": mi.api_name,
    #                     "snippet": snippet,
    #                     "pages": pages,
    #                     "section_path": sec_obj.path or [sec_obj.title],
    #                     "idx": top_idx,
    #                     "base_scores": {
    #                         "lexical": float(section_scores[top_idx]) if section_scores else 0.0,
    #                         "semantic": float(sem_scores[top_idx]) if len(sem_scores) > top_idx else 0.0,
    #                         "final": final_score,
    #                         "match_type": "fallback"
    #                     },
    #                     "warning": "Low-confidence match; using top-ranked section start."
    #                 }
                
    #             # If still no result, return empty with clear indication
    #             if not best:
    #                 best = {
    #                     "api_name": mi.api_name,
    #                     "snippet": "",  # Empty - member not found
    #                     "pages": [],
    #                     "section_path": [],
    #                     "idx": -1,
    #                     "base_scores": {
    #                         "lexical": 0.0, 
    #                         "semantic": 0.0, 
    #                         "final": final_score if 'final_score' in dir() else 0.0,
    #                         "match_type": "not_found"
    #                     },
    #                     "warning": f"Member documentation not found in PDF."
    #                 }
            
    #         return j, best

    #     # Parallel Execution/Refinement (regex/text-only path; model calls are outside)
    #     results = [None] * len(members)
    #     with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
    #         futures = [ex.submit(refine_one, j) for j in range(len(members))]
    #         for fut in as_completed(futures):
    #             j, best = fut.result()
    #             results[j] = best

    #     # Snippet Boost & Output Construction
    #     for j, r in enumerate(results):
    #         if not r: continue # Should not happen
            
    #         # Add snippet boost if semantic used
    #         boost = 0.0
    #         if use_semantic_member[j] and Q is not None:
                
    #             # Build contextual snippet (similar to windows)
    #             sec_idx = r.get("idx", -1)
    #             if sec_idx >= 0 and sec_idx < len(sections):
    #                 sec = sections[sec_idx]
    #                 context_prefix = "\n".join(sec.path or [])
    #                 if context_prefix:
    #                     context_prefix += "\n"
    #                 context_prefix += sec.title or ""
    #                 if context_prefix:
    #                     context_prefix += "\n\n"
    #                 contextual_snippet = context_prefix + r["snippet"]
    #             else:
    #                 contextual_snippet = r["snippet"]
                
    #             # Encode just the snippet to see if it matches query
    #             # snip_vec = embedder.encode([build_passage_text(r["snippet"], model_name)])[0]
    #             snip_vec = embedder.encode([build_passage_text(contextual_snippet, model_name)])[0]
    #             boost = float(cosine_similarity(Q[j], snip_vec.reshape(1, -1))[0])
            
    #         base = r.get("base_scores", {})
    #         final_score = base.get("final", 0.0) + self.cfg.snippet_boost_weight * boost
            
    #         payload = {
    #             "text": r["snippet"],
    #             "pages": r["pages"],
    #             "section_path": r["section_path"],
    #             "scores": {
    #                 "lexical": base.get("lexical", 0.0),
    #                 "semantic": base.get("semantic", 0.0),
    #                 "final": final_score,
    #                 "match_type": base.get("match_type", "unknown")
    #             }
    #         }
    #         if "warning" in r:
    #             payload["warning"] = r["warning"]
    #         output[members[j].api_name] = payload

    #     return output


def extract_api_docs_from_pdf(
    pdf_path: str,
    members: List[MemberInput],
    out_json_path: str,
    per_api_txt_dir: str,
    model_name: str = "intfloat/e5-base-v2",
    cache_dir: str = None,
    member_cfg: MemberExtractorConfig = MemberExtractorConfig(semantic_mode="auto"),
    peer_signatures: Optional[Dict[str, List[str]]] = None
) -> Dict[str, Any]:
    """
    Orchestrate Stage 1 (chunk selection) and Stage 2 (member extraction) for a single PDF.

    Pipeline:
        1. Sectionize the PDF using `PDFSectionizer`.
        2. Stage 1: Select API-reference sections.
        3. Stage 2: Extract per-member documentation with `MemberExtractor`
            (lexical-first; semantic "auto/never/always/only" per config, with regex refinement and window fallback).
        4. Persist a JSON mapping {api_fqn -> snippet + metadata}, and optionally write per-API .txt mirrors.

    Args:
        pdf_path: Path to the PDF file to analyze.
        members: Member descriptors (API FQN, signature variants, optional docstring).
        out_json_path: Path to write the output JSON mapping.
        per_api_txt_dir: Directory to also write per-API/member .txt mirrors of extracted snippets.
        model_name: Sentence-transformers model identifier (default: "intfloat/e5-base-v2").
        cache_dir: Optional path for on-disk embedding cache.
        member_cfg: Stage-2 configuration (semantic mode, shortlist/window sizes, parallelism).
        peer_signatures: Optional dictionary mapping API FQNs to their peer signatures.

    Returns:
        The in-memory JSON mapping written to `out_json_path`.
    """
    
    # 1. Sectionize PDF
    sec = PDFSectionizer(pdf_path)
    sections, _ = sec.sectionize()
    
    # Detect TOC region to avoid false matches
    page_raw, page_norm, _ = sec._collect_pages()
    toc_start, toc_end = sec._detect_toc_page_range(page_norm)

    # 2. Prepare embedder; pass to chunk selector if needed
    if member_cfg.semantic_mode == "never": embedder = None
    else: embedder = EmbeddingModel(model_name=model_name, cache_dir=cache_dir)

    # 3. Stage-1: chunk selection (API region)
    api_sections = APIReferenceLocator.collect_candidates(sections, max_depth=2, toc_end_page=toc_end)

    # 4. Stage-2: member extraction from selected sections
    output = MemberExtractor(member_cfg).extract(api_sections, members, embedder, peer_signatures, model_name)

    # 5a. Persist results
    os.makedirs(os.path.dirname(out_json_path), exist_ok=True)
    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, cls=NumpyEncoder)

    # 5b. write per-API .txt mirror (post-aggregation)
    if per_api_txt_dir:
        for api_fqn, payload in output.items():
            fname = _sanitize_filename(api_fqn) + ".txt"
            with open(os.path.join(per_api_txt_dir, fname), "w", encoding="utf-8") as ftxt:
                ftxt.write(payload["text"])

    return output
