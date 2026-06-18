import os
from typing import Optional, List, Tuple
import numpy as np



class MemberExtractorConfig:
    """
    Configuration for Stage 2 (member-level extraction).

    Attributes:
        semantic_mode: Controls semantic usage for members:
            - "auto": lexical-first; semantic only if lexical confidence is low.
            - "never": never compute semantic; use lexical signals only.
            - "always": always compute semantic for ranking and fallbacks.
            - "only": skip lexical scoring entirely and use semantic ranking.
        lexical_sigma_k: Strictness for lexical confidence gating ("auto" mode).
        lexical_margin_min: Required top-1 vs top-2 margin for lexical confidence ("auto" mode).
        topK_sections: Number of top-ranked sections considered in the refinement stage.
        window_chars: Size of sliding windows for semantic fallback (no regex anchor case).
        window_stride: Stride for the sliding window generator.
        max_workers: Max threads used in the refinement stage (regex/IO only; no model calls).
        snippet_boost_weight: Weight of the final snippet-level semantic boost added to the score.
        min_lexical_score: Minimum lexical score to accept a match (0-100+ scale).
        min_semantic_score: Minimum semantic score to accept a match (0-100 scale after normalization).
        min_fallback_score: Minimum combined score for fallback extraction (PDF only).
    """
    def __init__(
        self,
        semantic_mode: str = "auto",           # {"auto","never","always","only"}
        lexical_sigma_k: float = 0.25,
        lexical_margin_min: float = 0.20,
        topK_sections: int = 50,
        window_chars: int = 3000,
        window_stride: int = 2000,
        max_workers: Optional[int] = None,
        snippet_boost_weight: float = 0.5,
        min_lexical_score: float = 25.0,
        min_semantic_score: float = 30.0,
        min_fallback_score: float = 15.0,
        use_stop_signals: bool = False 
    ):
        self.semantic_mode = semantic_mode
        self.lexical_sigma_k = lexical_sigma_k
        self.lexical_margin_min = lexical_margin_min
        self.topK_sections = topK_sections
        self.window_chars = window_chars
        self.window_stride = window_stride
        self.max_workers = max_workers or min(8, os.cpu_count() or 4)
        self.snippet_boost_weight = snippet_boost_weight
        self.min_lexical_score = min_lexical_score
        self.min_semantic_score = min_semantic_score
        self.min_fallback_score = min_fallback_score
        self.use_stop_signals = use_stop_signals


def _dynamic_threshold(scores: np.ndarray) -> float:
    """
    Compute a conservative acceptance threshold for a shortlist.

    Heuristic:
        - Returns mean(scores) - 0.25*std(scores); values below this are considered weak.
        - Useful for pruning a top-K list by discarding weak tail candidates.

    Args:
        scores: 1D array of real-valued scores.

    Returns:
        A float threshold for pruning.
    """
    if scores.size == 0: return -1.0
    mu, sigma = float(scores.mean()), float(scores.std() + 1e-6)
    return mu - 0.25 * sigma


def _should_use_semantic_member(lex_scores: np.ndarray, sigma_k: float = 0.25, margin_min: float = 0.20) -> bool:
    """
    Decide whether to invoke semantic scoring for a specific member.

    The decision is based on lexical-only section scores:
        - If top score >= mean + sigma_k*std and (top-1 - top-2) >= margin_min*top-1, lexical is considered confident and semantic is skipped.
        - Otherwise, semantic is used to improve ranking.

    Args:
        lex_scores: 1D array of lexical-only scores (one per section).
        sigma_k: Strictness of the mean+sigma rule (higher -> skip semantic more often).
        margin_min: Minimum relative margin between the top two scores.

    Returns:
        True if semantic should be computed; False otherwise.
    """
    
    if lex_scores.size == 0:
        return True
    top2 = np.sort(lex_scores)[-2:] if lex_scores.size >= 2 else np.array([0.0, lex_scores.max()])
    mu, sigma = float(lex_scores.mean()), float(lex_scores.std() + 1e-6)
    confident_level = lex_scores.max() >= (mu + sigma_k * sigma)
    confident_margin = (top2[-1] - top2[-2]) >= (margin_min * max(1e-6, top2[-1]))
    return not (confident_level and confident_margin)


def _windows(text: str, window_chars: int = 3000, stride: int = 2000) -> List[Tuple[int, int]]:
    """
    Generate overlapping sliding windows aligned to line boundaries.
    
    Line-aligned windows prevent signatures from being split across windows,
    ensuring each API signature is fully contained in at least one window.
    This is critical for semantic search where the model needs to see
    complete signature lines to properly embed them.

    Args:
        text: The text to window.
        window_chars: Target window size (characters). Actual size may vary
                     slightly due to line alignment.
        stride: Target step size between windows (characters). Actual stride
               snaps to line boundaries.

    Returns:
        A list of (start, end) character index tuples covering the text.
        Each window starts and ends at line boundaries.
    """
    if not text:
        return []
    
    lines = text.splitlines(keepends=True)
    if not lines:
        return [(0, len(text))]
    
    # Build cumulative character positions for each line start
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))
    
    spans = []
    line_idx = 0  # Current starting line index
    
    while line_idx < len(lines):
        window_start_char = line_starts[line_idx]
        
        # Find end line: accumulate lines until we reach ~window_chars
        end_line_idx = line_idx
        while end_line_idx < len(lines):
            window_end_char = line_starts[end_line_idx + 1]
            if (window_end_char - window_start_char) >= window_chars:
                # Include this line (don't split mid-signature)
                end_line_idx += 1
                break
            end_line_idx += 1
        
        # Record this window
        window_end_char = line_starts[min(end_line_idx, len(lines))]
        spans.append((window_start_char, window_end_char))
        
        # Check if we've reached the end
        if end_line_idx >= len(lines):
            break
        
        # Advance by stride (in characters), snapping to line boundary
        target_start = window_start_char + stride
        
        # Find first line that starts at or after target_start
        new_line_idx = line_idx + 1  # At minimum, advance by one line
        while new_line_idx < len(lines) and line_starts[new_line_idx] < target_start:
            new_line_idx += 1
        
        # Ensure we don't go backwards or stall
        if new_line_idx <= line_idx:
            new_line_idx = line_idx + 1
        
        line_idx = new_line_idx
    
    return spans


