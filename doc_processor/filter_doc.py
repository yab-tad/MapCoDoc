import re
from typing import List, Tuple, Set, Optional, Dict
from dataclasses import dataclass, field
import numpy as np

from doc_processor.file_doc.embeddings import EmbeddingModel
from doc_processor.file_doc.hybrid_search import section_match_score
from doc_processor.file_doc.extraction_utils import MemberExtractorConfig, _windows, _should_use_semantic_member
from doc_processor.file_doc.signature import (
    MemberInput, 
    build_lexical_needles, 
    build_signature_query,
    build_signature_patterns, 
    build_semantic_query,
    build_passage_text
)


class StopSignalMatcher:
    """
    Type-aware stop signal detection with fallback strategy.
    
    Two-phase matching:
        Phase 1 (Primary): Type-specific patterns
            - CLASS: Stop at other classes/functions
            - METHOD: Stop at sibling methods of same class
            - FUNCTION: Stop at other functions/classes
        
        Phase 2 (Fallback): Broader patterns if primary fails
            - CLASS: Look for method signatures to ensure all methods included
            - METHOD: Stop at any class/function
            - FUNCTION: Stop at any signature-like pattern
    
    Search space: Only operates within the section where target member is found.
    """
    
    # Heuristics for name classification
    METHOD_NAME_PATTERN = re.compile(r'^[a-z_][a-z0-9_]*$')
    CLASS_NAME_PATTERN = re.compile(r'^[A-Z][a-zA-Z0-9]*$')
    
    # Generic signature pattern for any callable (used in pre-scan)
    GENERIC_SIGNATURE_PATTERN = re.compile(
        r'^\s*(?:class\s+)?[\w\.]+\s*\(',
        re.MULTILINE
    )
    
    def __init__(
        self, 
        peer_signatures: List[str], 
        target_member_type: str = "function",
        target_api_name: str = ""
    ):
        """
        Build type-aware stop patterns with fallback support.
        
        Args:
            peer_signatures: List of peer signatures
            target_member_type: "class", "method", "function", "variable"
            target_api_name: FQN of target member
        """
        self.target_type = target_member_type
        self.target_name = target_api_name
        
        # Extract parent class for methods
        self.parent_class = None
        self.target_short_name = target_api_name.split('.')[-1] if target_api_name else ""
        if target_member_type == "method" and '.' in target_api_name:
            parts = target_api_name.split('.')
            if len(parts) >= 2:
                self.parent_class = parts[-2]
        
        # Separate patterns into primary and fallback
        self.primary_patterns = []
        self.primary_short_names = set()
        self.fallback_patterns = []
        self.fallback_short_names = set()
        
        # Classify and build patterns for each peer signature
        for sig in peer_signatures:
            if not sig or len(sig) < 1:
                continue
            
            name_part = sig.split('(')[0].strip()
            if not name_part:
                continue
            
            short_name = name_part.split('.')[-1]
            
            # Skip if this is the target itself
            if short_name.lower() == self.target_short_name.lower():
                continue
            
            peer_looks_like_class = self.CLASS_NAME_PATTERN.match(short_name) is not None
            peer_looks_like_method = self.METHOD_NAME_PATTERN.match(short_name) is not None
            
            # Classify as primary or fallback based on target type
            is_primary = self._classify_peer(short_name, peer_looks_like_class, peer_looks_like_method, name_part)
            
            # Build patterns for this peer
            patterns = self._build_patterns_for_peer(short_name, name_part, sig)
            
            if is_primary:
                self.primary_patterns.extend(patterns)
                self.primary_short_names.add(short_name.lower())
            else:
                self.fallback_patterns.extend(patterns)
                self.fallback_short_names.add(short_name.lower())
        
        # State: which pattern set to use (can be switched after pre-scan)
        self.use_fallback = False
        
    def _classify_peer(
        self, 
        short_name: str, 
        peer_looks_like_class: bool,
        peer_looks_like_method: bool,
        full_name: str
    ) -> bool:
        """
        Classify a peer as primary or fallback stop signal.
        
        Returns:
            True if this peer is a PRIMARY stop signal, False for FALLBACK
        """
        # --- CLASS EXTRACTION ---
        if self.target_type == "class":
            # Primary: other classes and functions (not methods)
            if peer_looks_like_class:
                return True  # Primary: other class
            if '.' in full_name and peer_looks_like_method:
                return True  # Primary: qualified function
            # Fallback: methods (to find end of class's method list)
            return False
        
        # --- METHOD EXTRACTION ---
        elif self.target_type == "method":
            # Primary: sibling methods (same parent class)
            if peer_looks_like_method:
                # If we know parent class, check if this could be a sibling
                if self.parent_class:
                    # Methods of same class are primary
                    return True
                return True  # Assume sibling method
            # Fallback: classes and functions
            return False
        
        # --- FUNCTION EXTRACTION ---
        elif self.target_type == "function":
            # Primary: other functions and classes
            return True
        
        return True  # Default: primary
    
    
    def _build_patterns_for_peer(self, short_name: str, full_name: str, full_signature: str = "") -> List[re.Pattern]:
        """
        Build regex patterns for a peer signature.
        
        Args:
            short_name: Just the name (e.g., "apply_over_axes")
            full_name: FQN without params (e.g., "numpy.apply_over_axes")
            full_signature: Complete signature (e.g., "numpy.apply_over_axes(func, a, axes)")
        
        Returns:
            List of compiled regex patterns.
        """
        patterns = []
        short_escaped = re.escape(short_name)
        
        # =========================================================================
        # Pattern 0a (Highest Priority): Exact signature with flexible whitespace
        # Must be at line start or preceded by whitespace (not part of longer FQN)
        # =========================================================================
        if full_signature and short_name != full_name:
            sig_escaped = re.escape(full_signature)
            sig_pattern = sig_escaped
            sig_pattern = sig_pattern.replace(r'\ ', r'\s*')
            sig_pattern = sig_pattern.replace(r'\.', r'\s*\.\s*')
            
            # Only add paren flexibility if signature HAS parens
            if '(' in full_signature:
                sig_pattern = sig_pattern.replace(r'\(', r'\s*\(\s*')
                sig_pattern = sig_pattern.replace(r'\)', r'\s*\)')
                sig_pattern = sig_pattern.replace(r'\,', r'\s*,\s*')
                sig_pattern = sig_pattern.replace(r'\=', r'\s*=\s*')
            
            # Boundary check - must be at line start or preceded by whitespace
            pat0_exact = re.compile(
                rf'(?:^|\s){sig_pattern}',  # ← Added (?:^|\s) prefix
                re.IGNORECASE | re.MULTILINE
            )
            patterns.append(pat0_exact)
            return patterns
        
        
        # =========================================================================
        # Pattern 0b: Full signature with optional class/FQN prefix
        # For short-name signatures (e.g., "Conv2d(...)"), allow doc to have:
        #   - "class Conv2d(...)"
        #   - "torch.nn.Conv2d(...)"
        #   - "class torch.nn.Conv2d(...)"
        # Matches FULL signature to avoid false positives like "apply_over_axes(#page=1284)"
        # =========================================================================
        if full_signature and '(' in full_signature and short_name == full_name:
            sig_escaped = re.escape(full_signature)
            sig_pattern = sig_escaped
            sig_pattern = sig_pattern.replace(r'\ ', r'\s*')
            sig_pattern = sig_pattern.replace(r'\.', r'\s*\.\s*')
            sig_pattern = sig_pattern.replace(r'\(', r'\s*\(\s*')
            sig_pattern = sig_pattern.replace(r'\)', r'\s*\)')
            sig_pattern = sig_pattern.replace(r'\,', r'\s*,\s*')
            sig_pattern = sig_pattern.replace(r'\=', r'\s*=\s*')
            
            # Allow optional "class " prefix and optional FQN prefix (e.g., "torch.nn.")
            pat0b_full_flexible = re.compile(
                rf'(?:^|\s)(?:class\s+)?(?:[\w\.]+\s*\.\s*)?{sig_pattern}',
                re.IGNORECASE | re.MULTILINE
            )
            patterns.append(pat0b_full_flexible)
            return patterns
        
        # =========================================================================
        # Pattern 1: Short name with optional "class" prefix
        # Matches: "Conv2d(" or "class Conv2d("
        # ONLY create this if full_name has NO dots (not an FQN)
        # Otherwise we'd match "apply_over_axes(" when looking for "numpy.apply_over_axes("
        # =========================================================================
        if '.' not in full_name:
            pat1 = re.compile(
                rf'^\s*(?:class\s+)?{short_escaped}\s*\(',
                re.IGNORECASE
            )
            patterns.append(pat1)
        
        # =========================================================================
        # Pattern 2a: Optional "class" + optional FQN + short name
        # Matches: "class torch.nn.Conv2d(" or "class Conv2d("
        # This handles the case where FQN appears optionally between "class" and short name
        # =========================================================================
        pat2_class_fqn = re.compile(
            rf'^\s*(?:class\s+)?[\w\.]*{short_escaped}\s*\(',
            re.IGNORECASE | re.MULTILINE
        )
        patterns.append(pat2_class_fqn)
        
        # =========================================================================
        # Pattern 2b: FQN.short_name anywhere in line
        # Matches "torch.nn.Conv2d(" even when short_name = "Conv2d" has no dots
        # Must be preceded by whitespace or line start to avoid partial matches
        # =========================================================================
        pat2b_fqn_short = re.compile(
            rf'(?:^|\s)[\w\.]+\.{short_escaped}\s*\(',  # Note: [\w\.]+ requires at least one char before the dot
            re.IGNORECASE | re.MULTILINE
        )
        patterns.append(pat2b_fqn_short)
        
        # =========================================================================
        # Pattern 3: Full FQN format (if full_name has dots)
        # Matches: "torch.nn.Conv2d(" or "class torch.nn.Conv2d("
        # =========================================================================
        if '.' in full_name:
            fqn_escaped = re.escape(full_name).replace(r'\.', r'\s*\.\s*')
            pat3_fqn = re.compile(
                rf'^\s*(?:class\s+)?{fqn_escaped}\s*\(',
                re.IGNORECASE
            )
            patterns.append(pat3_fqn)
        
        # =========================================================================
        # Pattern 4: ClassName.method_name format (for methods)
        # Matches: "DataFrame.to_csv("
        # =========================================================================
        if self.parent_class and self.METHOD_NAME_PATTERN.match(short_name):
            pat4_method = re.compile(
                rf'^\s*{re.escape(self.parent_class)}\s*\.\s*{short_escaped}\s*\(',
                re.IGNORECASE
            )
            patterns.append(pat4_method)
        
        # =========================================================================
        # Pattern 5: FQN anywhere in line (requires preceding whitespace or line start)
        # Matches: "Sound.get_num_channels()" or "     Sound.get_num_channels()"
        # Does NOT match: "pandas.DataFrame.at" when looking for "DataFrame.at"
        # =========================================================================
        if '.' in full_name:
            fqn_escaped = re.escape(full_name).replace(r'\.', r'\s*\.\s*')
            # (?:^|\s) = either start of string/line OR preceded by whitespace
            pat5_fqn_anywhere = re.compile(
                rf'(?:^|\s){fqn_escaped}\s*\(',
                re.IGNORECASE | re.MULTILINE
            )
            patterns.append(pat5_fqn_anywhere)
            
            # =========================================================================
            # Pattern 6: FQN for properties/attributes (no parentheses required)
            # Same rule: must be at line start or preceded by whitespace
            # =========================================================================
            pat6_fqn_no_paren = re.compile(
                rf'(?:^|\s){fqn_escaped}(?!\s*\()',
                re.IGNORECASE | re.MULTILINE
            )
            patterns.append(pat6_fqn_no_paren)
        
        return patterns
    
    def pre_scan_section(self, section_text: str, start_pos: int = 0) -> None:
        """
        Pre-scan the section to determine if primary patterns will match.
        
        If no primary pattern matches anywhere in the section text (after start_pos),
        switch to fallback patterns.
        
        This ensures:
            - For methods: if no sibling method found, fallback to classes/functions
            - For classes: if no other class found, methods act as end markers
        
        Args:
            section_text: The full section text (text_raw or text_norm)
            start_pos: Position where extraction starts (skip content before anchor)
        """
        search_region = section_text[start_pos:]
        
        # Check if any primary pattern matches in the search region
        primary_match_found = False
        for name in self.primary_short_names:
            if name in search_region.lower():
                # Quick check passed, do full pattern check
                for pat in self.primary_patterns:
                    if pat.search(search_region):
                        primary_match_found = True
                        break
            if primary_match_found:
                break
        
        # If no primary match found, enable fallback
        if not primary_match_found and self.fallback_patterns:
            self.use_fallback = True
            # For CLASS extraction: if using fallback (methods), we want to find
            # the LAST method to ensure all are included. This is handled by
            # letting extraction continue until we hit a method AFTER all our
            # class's methods have been documented.
    
    def checks_stop(self, line: str) -> Tuple[bool, bool]:
        """
        Check if a line matches a stop pattern, with code example awareness.
        
        Uses content-based detection instead of fence tracking:
            - Lines with REPL prompts (>>>, ...) = low priority (code example)
            - Lines with assignments (m = Conv2d(...)) = low priority (code example)
            - Lines with class/function definitions = high priority (real definition)
        
        Args:
            line: A single line of text to check.
            
        Returns:
            Tuple of (matched, is_high_priority) where:
                - matched: True if a stop pattern was matched
                - is_high_priority: True if this looks like a real definition (stop immediately)
                                   False if this looks like a code example (record as fallback)
        """
        if not line or len(line) < 3:
            return (False, False)
        
        line_stripped = line.strip()
        
        # Select which pattern set to use based on pre-scan result
        if self.use_fallback:
            patterns = self.fallback_patterns
            short_names = self.fallback_short_names
        else:
            patterns = self.primary_patterns
            short_names = self.primary_short_names
        
        # Quick check: does line contain any relevant short name?
        line_lower = line_stripped.lower()
        if not any(name in line_lower for name in short_names):
            return (False, False)
        
        # Full pattern check
        for pat in patterns:
            # if pat.match(line_stripped):
            #     # Match found - determine priority based on line content
            #     # Code examples (>>>, assignments) = low priority (fallback only)
            #     # Real definitions (class X, def x) = high priority (stop immediately)
            #     is_code_example = self._looks_like_code_example(line_stripped)
            #     is_high_priority = not is_code_example
            #     return (True, is_high_priority)
            
            # Also try search for patterns that use \b instead of ^
            # This catches "short_name   FQN.method()" format
            if pat.search(line_stripped):
                is_code_example = self._looks_like_code_example(line_stripped)
                is_high_priority = not is_code_example
                return (True, is_high_priority)
        
        return (False, False)
    
    def _looks_like_code_example(self, line: str) -> bool:
        """
        Check if a line looks like it's from a code example rather than a definition.
        
        Code examples typically have:
            - REPL prompts: >>>, ...
            - Assignment patterns BEFORE the signature: m = Conv2d(...), model = Module(...)
        
        Real definitions look like:
            - class ClassName(...)  at line start
            - function_name(...) at line start
            - ClassName(params) at line start with no assignment
        
        Args:
            line: The line to check (already stripped).
            
        Returns:
            True if this looks like a code example, False if it looks like a definition.
        """
        # REPL prompts are definitely code examples
        if line.startswith('>>>') or line.startswith('...'):
            return True
        
        eq_pos = line.find('=')
        paren_pos = line.find('(')
        
        if eq_pos >= 0 and paren_pos >= 0:
            # '=' appears BEFORE '(' -> this is an assignment (code example)
            if eq_pos < paren_pos:
                return True
        elif eq_pos >= 0 and paren_pos < 0:
            # Has '=' but no '(' at all -> likely code example (e.g., "x = value")
            return True
        
        return False
    
    def get_active_strategy(self) -> str:
        """Return which strategy is currently active (for debugging)."""
        return "fallback" if self.use_fallback else "primary"
    

# =============================================================================
# Web Member Extraction
# =============================================================================

@dataclass
class WebMemberInfo:
    """Extended member info for web extraction with all API name variants."""
    api_name: str  # Primary API name
    all_api_names: Set[str] = field(default_factory=set)  # All known API names
    member_input: MemberInput = None
    member_type: str = "function"
    
    def matches_name(self, name: str) -> bool:
        """Check if any API name variant matches the given name."""
        name_lower = name.lower().strip()
        if name_lower == self.api_name.lower():
            return True
        return any(n.lower() == name_lower for n in self.all_api_names)


class WebMemberExtractor:
    """
    Extract member documentation from web pages using lexical + semantic search.
    
    Implements two-stage semantic search like pipeline_pdf:
        Stage 1: Coarse window search with max-pooling
        Stage 2: Fine-grained line anchor with lookback
    """
    
    def __init__(self, cfg: MemberExtractorConfig, embedder: Optional[EmbeddingModel] = None):
        self.cfg = cfg
        self.embedder = embedder
        
    def _effective_window_params(self) -> Tuple[int, int]:
        """Get window size and stride, respecting embedder context limit if available."""
        if self.embedder is None:
            return self.cfg.window_chars, self.cfg.window_stride
        
        model = getattr(self.embedder, "model", None)
        max_tokens = getattr(model, "max_seq_length", None)
        
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            return self.cfg.window_chars, self.cfg.window_stride
        
        chars_per_token = 4
        max_chars = max_tokens * chars_per_token
        window_chars = min(self.cfg.window_chars, max_chars)
        stride = min(self.cfg.window_stride, window_chars)
        
        return window_chars, stride
    
    
    def _compute_length_penalty(self, text_len: int) -> float:
        """
        Compute adaptive length penalty for semantic gating.
        
        Penalty scales with document length to account for:
            - Short docs: Lexical is reliable, low/no penalty
            - Long docs: More noise, higher penalty (capped)
        
        Args:
            text_len: Length of text in characters.
        
        Returns:
            Penalty value to subtract from lexical score.
        """
        MIN_DOC_LEN = 500      # Below this, no penalty (lexical reliable)
        MAX_DOC_LEN = 15000    # Above this, cap penalty
        MAX_PENALTY = 0.15     # Maximum penalty cap
        
        if text_len <= MIN_DOC_LEN:
            return 0.0
        
        # Linear scale between MIN and MAX, capped
        effective_len = min(text_len, MAX_DOC_LEN) - MIN_DOC_LEN
        max_range = MAX_DOC_LEN - MIN_DOC_LEN
        return MAX_PENALTY * (effective_len / max_range)
    
    
    def find_anchor_position(
        self,
        text: str,
        member: WebMemberInfo,
        model_name: str = ""
    ) -> Tuple[int, float, str]:
        """
        Find the anchor position for a member using lexical and/or semantic search.
        
        Strategy (based on semantic_mode):
            - "never": Pure lexical (skip semantic)
            - "auto": Lexical first, semantic fallback if statistically low confidence
            - "always": Lexical + semantic combined
            - "only": Pure semantic (skip lexical)
        
        Returns:
            Tuple of (char_position, score, match_type)
        """
        mi = member.member_input
        
        # --- GATE: Skip lexical entirely for "only" mode ---
        if self.cfg.semantic_mode == "only":
            if self.embedder is None:
                # Can't do semantic without embedder - return failure
                return (0, 0.0, "none")
            sem_pos, sem_score = self._two_stage_semantic_search(text, mi, model_name, member.api_name)
            return (sem_pos, sem_score * 100, "semantic")
        
        # --- Lexical Search (for "never", "auto", "always") ---
        needles = build_lexical_needles(mi)
        lex_score, line_idx, char_offset, match_type = section_match_score(text, needles, section_title="")
        
        # Apply length penalty (consistent with PDF pipeline)
        length_penalty = self._compute_length_penalty(len(text))
        lex_score_penalized = lex_score - length_penalty
        
        # Convert line index to character position
        lex_pos = -1
        if line_idx >= 0:
            lines = text.splitlines(keepends=True)
            lex_pos = sum(len(lines[i]) for i in range(min(line_idx, len(lines))))
            lex_pos += char_offset
        
        # --- Regex Fallback if lexical fails ---
        if lex_pos < 0:
            patterns = build_signature_patterns(mi)
            for pat in patterns:
                m = pat.search(text)
                if m:
                    lex_pos = m.start()
                    lex_score = 50  # Moderate confidence for regex match
                    lex_score_penalized = lex_score - 0.05 * length_penalty
                    match_type = "regex"
                    break
        
        # --- GATE: Return immediately for "never" mode ---
        if self.cfg.semantic_mode == "never":
            return (max(0, lex_pos), lex_score, match_type)
        
        # --- Decide if semantic needed ---
        use_semantic = False
        if self.cfg.semantic_mode == "only":
            use_semantic = True
        elif self.cfg.semantic_mode == "always":
            use_semantic = True
        elif self.cfg.semantic_mode == "auto":
            # For single-text case, use lexical score array (simulate multi-section)
            use_semantic = _should_use_semantic_member(
                np.array([lex_score_penalized]),
                self.cfg.lexical_sigma_k,
                self.cfg.lexical_margin_min
            )
        
        # Skip semantic if not needed or no embedder
        if not use_semantic or self.embedder is None:
            return (max(0, lex_pos), lex_score, match_type)
        
        # --- Two-Stage Semantic Search ---
        sem_pos, sem_score = self._two_stage_semantic_search(text, mi, model_name, member.api_name)
        
        # Choose best result
        if self.cfg.semantic_mode == "only":
            return (sem_pos, sem_score, "semantic")
        
        # For "auto" and "always", combine based on confidence
        if lex_pos >= 0:
            # Normalize scores for combination (semantic is ~0-1, lexical is 0-100+)
            lex_normalized = lex_score_penalized / 100.0
            
            if self.cfg.semantic_mode == "always":
                # Weighted combination
                combined_score = lex_score + sem_score * 100
                return (lex_pos, combined_score, f"{match_type}+semantic")
            
            # "auto" mode: prefer lexical if confident, else semantic
            # Confident = lexical score is reasonably high
            if lex_normalized >= 0.3:
                return (lex_pos, lex_score, match_type)
        
        # Fallback to semantic
        return (sem_pos, sem_score * 100, "semantic")
    
    def _two_stage_semantic_search(
        self,
        text: str,
        mi: MemberInput,
        model_name: str,
        api_name: str
    ) -> Tuple[int, float]:
        """
        Two-stage semantic search:
            Stage 1: Coarse window search with paragraph max-pooling
            Stage 2: Fine-grained line anchor with lookback
        """
        # --- Stage 1: Coarse Window Search ---
        win_chars, win_stride = self._effective_window_params()
        spans = _windows(text, win_chars, win_stride)
        
        if not spans:
            return (0, 0.0)
        
        # Build semantic query (with proper query prefix for E5)
        semantic_query = build_semantic_query(mi, model_name)
        q_vec = self.embedder.encode([semantic_query])[0]
        
        # Extract name variants for lexical bonus
        api_lower = api_name.lower()
        parts = api_name.split('.')
        parent_name = f"{parts[-2]}.{parts[-1]}".lower() if len(parts) >= 2 else ""
        short_name = parts[-1].lower() if parts else ""
        
        # Score each window with paragraph max-pooling
        MIN_CHUNK_LEN = 50
        MAX_CHUNK_LEN = 800
        LENGTH_PENALTY = 0.00005
        NAME_BONUS = 0.15
        
        window_scores = []
        for (a, b) in spans:
            window_text = text[a:b]
            window_lower = window_text.lower()
            
            # Split into paragraphs
            paragraphs = re.split(r'\n\s*\n', window_text)
            chunks = []
            for para in paragraphs:
                para = para.strip()
                if len(para) < MIN_CHUNK_LEN:
                    continue
                if len(para) > MAX_CHUNK_LEN:
                    for i in range(0, len(para), MAX_CHUNK_LEN // 2):
                        sub = para[i:i + MAX_CHUNK_LEN]
                        if len(sub) > MIN_CHUNK_LEN:
                            chunks.append(sub)
                else:
                    chunks.append(para)
            
            if chunks:
                # Wrap chunks with passage prefix for E5 models
                wrapped_chunks = [build_passage_text(c, model_name) for c in chunks]
                C = self.embedder.encode(wrapped_chunks)
                chunk_sims = C @ q_vec
                max_score = float(np.max(chunk_sims))
                length_penalty = LENGTH_PENALTY * len(window_text)
                
                # Name bonus
                name_bonus = 0.0
                if api_lower in window_lower:
                    name_bonus = NAME_BONUS
                elif parent_name and parent_name in window_lower:
                    name_bonus = NAME_BONUS * 0.7
                
                window_scores.append(max_score - length_penalty + name_bonus)
            else:
                window_scores.append(0.0)
        
        sims = np.array(window_scores)
        
        # Get top 3 candidate windows
        num_candidates = min(3, len(spans))
        top_indices = np.argsort(sims)[::-1][:num_candidates]
        
        # --- Stage 2: Fine-grained Anchor Search with Lookback ---
        sig_query = build_signature_query(mi, model_name)
        sig_vec = self.embedder.encode([sig_query])[0]
        
        candidate_results = []
        
        for window_idx in top_indices:
            window_start, window_end = spans[window_idx]
            
            # Calculate lookback from previous window (last 10 non-empty lines)
            lookback_start = window_start
            if window_idx > 0:
                prev_start, prev_end = spans[window_idx - 1]
                prev_text = text[prev_start:prev_end]
                prev_lines = prev_text.splitlines(keepends=True)
                
                non_empty_count = 0
                for i in range(len(prev_lines) - 1, -1, -1):
                    if prev_lines[i].strip():
                        non_empty_count += 1
                        if non_empty_count >= 10:
                            char_offset = sum(len(prev_lines[j]) for j in range(i))
                            lookback_start = prev_start + char_offset
                            break
            
            # Search region = lookback + current window
            region_end = min(len(text), window_end + 500)
            region_text = text[lookback_start:region_end]
            
            local_pos, fine_score = self._anchor_in_region(region_text, sig_vec, api_name, model_name)
            
            global_pos = lookback_start + local_pos
            candidate_results.append((global_pos, fine_score))
        
        if not candidate_results:
            return (0, 0.0)
        
        # Return best candidate
        best = max(candidate_results, key=lambda x: x[1])
        return best
    
    def _anchor_in_region(
        self,
        text: str,
        sig_vec: np.ndarray,
        api_name: str,
        model_name: str = "",
        max_lines: int = 100
    ) -> Tuple[int, float]:
        """Fine-grained line-level anchor search with passage wrapping."""
        lines = text.splitlines(keepends=True)
        search_lines = lines[:max_lines]
        
        SIGNATURE_PATTERN = re.compile(r'(?:class\s+)?[\w\.]+\s*\([^)]*(?:\)|$)')
        
        # Name variants
        api_lower = api_name.lower()
        parts = api_name.split('.')
        parent_name = f"{parts[-2]}.{parts[-1]}".lower() if len(parts) >= 2 else ""
        short_name = parts[-1].lower() if parts else ""
        
        line_data = []
        char_offset = 0
        
        for i, line in enumerate(search_lines):
            stripped = line.strip()
            if stripped and len(stripped) > 2:
                looks_like_sig = bool(SIGNATURE_PATTERN.search(stripped))
                if stripped.startswith('class ') or 'property ' in stripped:
                    looks_like_sig = True
                
                # Name match score
                line_lower = stripped.lower()
                name_score = 0.0
                if api_lower in line_lower:
                    name_score = 1.0
                elif parent_name and parent_name in line_lower:
                    name_score = 0.7
                elif short_name and short_name in line_lower:
                    name_score = 0.3
                
                line_data.append((i, char_offset, stripped, looks_like_sig, name_score))
            char_offset += len(line)
        
        if not line_data:
            return (0, 0.0)
        
        # Embed lines with passage wrapping for E5 consistency
        line_texts = [ld[2] for ld in line_data]
        wrapped_lines = [build_passage_text(lt, model_name) for lt in line_texts]
        L = self.embedder.encode(wrapped_lines)
        sims = L @ sig_vec
        
        # Apply bonuses
        SIGNATURE_BONUS = 0.08
        NAME_BONUS = 0.30
        
        boosted = sims.copy()
        for i, ld in enumerate(line_data):
            if ld[3]:  # looks_like_signature
                boosted[i] += SIGNATURE_BONUS
            if ld[4] > 0:  # name match
                boosted[i] += NAME_BONUS * ld[4]
        
        best_idx = int(np.argmax(boosted))
        best_score = float(boosted[best_idx])
        _, char_pos, _, _, _ = line_data[best_idx]
        
        return (char_pos, best_score)
    
    # =========================================================================
    # Batch Processing for Multiple Members (Efficiency)
    # =========================================================================
    
    def extract_batch(
        self,
        text: str,
        members: List[WebMemberInfo],
        model_name: str = ""
    ) -> Dict[str, Tuple[int, float, str]]:
        """
        Batch extract anchor positions for multiple members efficiently.
        
        Pre-computes:
            - All lexical scores
            - All semantic query embeddings
            - Window embeddings (reused across members)
        
        Returns:
            Dict mapping api_name -> (char_position, score, match_type)
        """
        if not members:
            return {}
        
        results = {}
        
        # --- Pre-compute lexical scores for all members ---
        length_penalty = self._compute_length_penalty(len(text))
        lex_results = []  # (lex_pos, lex_score, match_type, mi, member)
        
        for member in members:
            mi = member.member_input
            needles = build_lexical_needles(mi)
            
            lex_score, line_idx, char_offset, match_type = section_match_score(
                text, needles, section_title=""
            )
            lex_score_penalized = lex_score - length_penalty
            
            # Convert to char position
            lex_pos = -1
            if line_idx >= 0:
                lines = text.splitlines(keepends=True)
                lex_pos = sum(len(lines[i]) for i in range(min(line_idx, len(lines))))
                lex_pos += char_offset
            
            # Regex fallback
            if lex_pos < 0:
                patterns = build_signature_patterns(mi)
                for pat in patterns:
                    m = pat.search(text)
                    if m:
                        lex_pos = m.start()
                        lex_score = 50
                        lex_score_penalized = lex_score - length_penalty
                        match_type = "regex"
                        break
            
            lex_results.append({
                "member": member,
                "mi": mi,
                "lex_pos": lex_pos,
                "lex_score": lex_score,
                "lex_score_penalized": lex_score_penalized,
                "match_type": match_type
            })
        
        # --- Determine which members need semantic (batch decision) ---
        if self.cfg.semantic_mode == "never" or self.embedder is None:
            # Pure lexical - return immediately
            for lr in lex_results:
                if lr["lex_score"] >= self.cfg.min_lexical_score and lr["lex_pos"] >= 0:
                    results[lr["member"].api_name] = (
                        lr["lex_pos"],
                        lr["lex_score"],
                        lr["match_type"]
                    )
                else:
                    # Below threshold or not found
                    results[lr["member"].api_name] = (
                        -1,  # Use -1 to signal "not found"
                        lr["lex_score"],
                        "none"
                    )
            return results
        
        # Collect scores for statistical gating
        lex_scores_array = np.array([lr["lex_score_penalized"] for lr in lex_results])
        
        use_semantic_per_member = []
        for j, lr in enumerate(lex_results):
            if self.cfg.semantic_mode == "only":
                use_semantic_per_member.append(True)
            elif self.cfg.semantic_mode == "always":
                use_semantic_per_member.append(True)
            else:  # auto
                # Use statistical method on ALL member scores
                needs_sem = _should_use_semantic_member(
                    lex_scores_array,
                    self.cfg.lexical_sigma_k,
                    self.cfg.lexical_margin_min
                )
                # Also check individual confidence
                if not needs_sem and lr["lex_pos"] < 0:
                    needs_sem = True
                use_semantic_per_member.append(needs_sem)
        
        # --- Pre-compute window embeddings if any member needs semantic ---
        W = None
        spans = []
        window_to_chunks = []  # Track chunk indices per window
        
        if any(use_semantic_per_member):
            win_chars, win_stride = self._effective_window_params()
            spans = _windows(text, win_chars, win_stride)
            
            if spans:
                all_chunks = []
                for (a, b) in spans:
                    window_text = text[a:b]
                    paragraphs = re.split(r'\n\s*\n', window_text)
                    
                    chunk_indices = []
                    for para in paragraphs:
                        para = para.strip()
                        if len(para) < 50:
                            continue
                        if len(para) > 800:
                            for i in range(0, len(para), 400):
                                sub = para[i:i + 800]
                                if len(sub) > 50:
                                    chunk_indices.append(len(all_chunks))
                                    all_chunks.append(build_passage_text(sub, model_name))
                        else:
                            chunk_indices.append(len(all_chunks))
                            all_chunks.append(build_passage_text(para, model_name))
                    
                    window_to_chunks.append(chunk_indices)
                
                if all_chunks:
                    W = self.embedder.encode(all_chunks)
        
        # --- Batch encode all semantic queries ---
        members_needing_semantic = [
            (j, lex_results[j]) 
            for j in range(len(members)) 
            if use_semantic_per_member[j]
        ]
        
        Q = None
        Q_sig = None
        if members_needing_semantic:
            semantic_queries = [build_semantic_query(lr["mi"], model_name) for _, lr in members_needing_semantic]
            sig_queries = [build_signature_query(lr["mi"], model_name) for _, lr in members_needing_semantic]
            Q = self.embedder.encode(semantic_queries)
            Q_sig = self.embedder.encode(sig_queries)
        
        # --- Process each member ---
        sem_idx = 0
        for j, lr in enumerate(lex_results):
            member = lr["member"]
            
            if not use_semantic_per_member[j]:
                # Pure lexical result
                if lr["lex_score"] >= self.cfg.min_lexical_score and lr["lex_pos"] >= 0:
                    results[member.api_name] = (
                        lr["lex_pos"],
                        lr["lex_score"],
                        lr["match_type"]
                    )
                else:
                    results[member.api_name] = (-1, lr["lex_score"], "none")
                continue
            
            # Semantic search using pre-computed embeddings
            q_vec = Q[sem_idx]
            sig_vec = Q_sig[sem_idx]
            sem_idx += 1
            
            # Score windows using pre-computed chunk embeddings
            api_lower = member.api_name.lower()
            parts = member.api_name.split('.')
            parent_name = f"{parts[-2]}.{parts[-1]}".lower() if len(parts) >= 2 else ""
            
            window_scores = []
            for w_idx, (a, b) in enumerate(spans):
                chunk_indices = window_to_chunks[w_idx] if w_idx < len(window_to_chunks) else []
                
                if chunk_indices and W is not None:
                    chunk_vecs = W[chunk_indices]
                    chunk_sims = chunk_vecs @ q_vec
                    max_score = float(np.max(chunk_sims))
                    
                    # Length penalty
                    max_score -= 0.00005 * (b - a)
                    
                    # Name bonus
                    window_lower = text[a:b].lower()
                    if api_lower in window_lower:
                        max_score += 0.15
                    elif parent_name and parent_name in window_lower:
                        max_score += 0.105
                    
                    window_scores.append(max_score)
                else:
                    window_scores.append(0.0)
            
            # Get top windows and do fine-grained search
            if window_scores:
                sims = np.array(window_scores)
                top_indices = np.argsort(sims)[::-1][:3]
                
                best_pos, best_score = 0, 0.0
                for window_idx in top_indices:
                    if window_idx >= len(spans):
                        continue
                    window_start, window_end = spans[window_idx]
                    
                    # Lookback
                    lookback_start = window_start
                    if window_idx > 0:
                        prev_start, prev_end = spans[window_idx - 1]
                        lookback_chars = min(500, prev_end - prev_start)
                        lookback_start = max(prev_start, prev_end - lookback_chars)
                    
                    region_end = min(len(text), window_end + 500)
                    region_text = text[lookback_start:region_end]
                    
                    local_pos, fine_score = self._anchor_in_region(
                        region_text, sig_vec, member.api_name, model_name
                    )
                    
                    global_pos = lookback_start + local_pos
                    if fine_score > best_score:
                        best_pos, best_score = global_pos, fine_score
                
                sem_pos, sem_score = best_pos, best_score
            else:
                sem_pos, sem_score = 0, 0.0
            
            # Combine results
            if self.cfg.semantic_mode == "only":
                if sem_score * 100 >= self.cfg.min_semantic_score:
                    results[member.api_name] = (sem_pos, sem_score * 100, "semantic")
                else:
                    # Semantic-only but too low - mark as not found
                    results[member.api_name] = (-1, sem_score * 100, "none")
            elif lr["lex_pos"] >= 0 and lr["lex_score_penalized"] / 100.0 >= 0.3:
                if self.cfg.semantic_mode == "always":
                    combined = lr["lex_score"] + sem_score * 100
                    results[member.api_name] = (lr["lex_pos"], combined, f"{lr['match_type']}+semantic")
                else:
                    results[member.api_name] = (lr["lex_pos"], lr["lex_score"], lr["match_type"])
            elif sem_score * 100 >= self.cfg.min_semantic_score:
                results[member.api_name] = (sem_pos, sem_score * 100, "semantic")
            else:
                # Both lexical and semantic failed to meet thresholds
                results[member.api_name] = (-1, max(lr["lex_score"], sem_score * 100), "none")
        
        return results