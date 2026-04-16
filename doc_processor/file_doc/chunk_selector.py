from __future__ import annotations
import re
import logging
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional
import numpy as np

from doc_processor.file_doc.pdf_localizer import Section

logger = logging.getLogger(__name__)



def build_section_tree(sections: List[Section]) -> List[Section]:
    """
    Organizes a flat list of sections into a hierarchical tree based on their 'level' attribute.

    This function uses a stack-based approach to determine parent-child relationships.
    A section becomes a child of the last item on the stack with a lower level value.

    Args:
        sections: A flat list of `Section` objects, assumed to be in document order.

    Returns:
        A list of the root-level `Section` objects. Each of these objects may contain
        a populated `children` list, forming a tree (or forest).
    """
    if not sections:
        return []

    root_nodes: List[Section] = []
    # This stack tracks the current path of parent sections in the hierarchy.
    parent_stack: List[Section] = []

    for sec in sections:
        sec.children = []  # Reset children for the new tree build.

        # Find the correct parent by popping from the stack until the top item has a level strictly less than the current section's level
        while parent_stack and parent_stack[-1].level >= sec.level:
            parent_stack.pop()

        if not parent_stack:
            # If the stack is empty, this is a root-level node.
            root_nodes.append(sec)
        else:
            # Otherwise, it's a child of the section at the top of the stack.
            parent_stack[-1].children.append(sec)

        # The current section is now a potential parent for subsequent sections.
        parent_stack.append(sec)

    return root_nodes


def get_contextual_text_map(root_nodes: List[Section]) -> Dict[str, str]:
    """
    Traverses the section tree to create a map of section IDs to their contextualized text.

    Contextualized text is formatted as: "Parent Title > Child Title > ... \n Section Content".
    This provides hierarchical context to the embedding model.

    Args:
        root_nodes: The list of root `Section` objects forming the tree.

    Returns:
        A dictionary mapping each `section.id` to its fully contextualized text string.
    """
    text_map: Dict[str, str] = {}

    def traverse(node: Section, path: List[str]):
        current_path = path + [node.title]
        title_context = "\n".join(p for p in current_path if p)

        # The text for embedding includes the title hierarchy and the section's content.
        contextual_text = f"{title_context}\n\n{node.text_norm[:5000]}"
        text_map[node.id] = contextual_text

        for child in node.children:
            traverse(child, current_path)

    for root in root_nodes:
        traverse(root, [])

    return text_map



class APIReferenceLocator:
    """
    Locate candidate sections that likely contain API reference documentation.

    The locator examines both TOC-derived sections (ids starting with "toc-") and regular sections whose titles contain reference-related keywords. 
    It returns a contiguous slice between the earliest and latest match (with one extra section of padding on each side for context) 
    to create a focused candidate set. If no matches are found, the entire list of sections is returned.
    """

    API_TITLES = [
        "api reference",
        "reference api",
        "reference",
        "python api",
        "python api reference",
        "python library reference",
        "python package reference",
        "python module reference",
        "api documentation",
        "reference guide",
        "reference manual"
    ]

    @classmethod
    def _title_matches(cls, title: Optional[str]) -> bool:
        """
        Check if the title contains any of the API reference keywords.
        
        Args:
            title: The title to check.
        
        Returns:
            True if the title contains any of the API reference keywords, False otherwise.
        """
        if not title:
            return False
        lowered = title.lower()
        return any(term in lowered for term in cls.API_TITLES)

    
    @classmethod
    def collect_candidates(cls, sections: List[Section], max_depth: int = 2, toc_end_page: int = 0) -> List[Section]:
        """
        Stage 1: Finds API sections via title keywords, starting search after the TOC region (if one exists).
        
        Strategy:
            1. Traverse the entire tree to find all nodes with matching titles.
            2. Rank matches by title quality (exact phrase match beats partial match).
            3. Among the best-quality matches, select those at the highest structural level.
            4. returns their subtrees up to a maximum depth. This prevents over-collection in very detailed TOCs.
        
        Args:
            sections: All sections from the document.
            max_depth: Maximum depth to traverse below each matched root (default: 3 levels).
            toc_end_page: Page index where TOC ends; search starts after this page.
        """
        if not sections:
            return []

        section_tree = build_section_tree(sections)
        
        # Find all matching nodes with quality scoring, but only consider sections starting after TOC
        all_matches: List[Tuple[Section, int]] = []
        q: List[Section] = list(section_tree)
        
        while q:
            curr = q.pop(0)
            # Skip sections whose content is entirely within the TOC region
            if curr.page_end <= toc_end_page:
                logger.debug(f"DEBUG: Skipping {curr.id} ('{curr.title}', page {curr.page_start}) - before TOC end {toc_end_page}")
                q.extend(curr.children)
                continue
            if cls._title_matches(curr.title):
                title_lower = curr.title.lower()
                quality = 0
                for term in cls.API_TITLES:
                    if term in title_lower:
                        if term == title_lower.strip():
                            quality += 10
                        elif title_lower.strip().startswith(term) or title_lower.strip().endswith(term):
                            quality += 5
                        else:
                            quality += 1
                all_matches.append((curr, quality))
            q.extend(curr.children)
            
        # Fallback to all sections for downstream scoring
        if not all_matches:
            return sections
        
        # Title-based path: select best-quality, highest-level matches
        max_quality = max(quality for _, quality in all_matches)
        best_matches = [sec for sec, quality in all_matches if quality == max_quality]
        min_level = min(sec.level for sec in best_matches)
        api_root_nodes = [sec for sec in best_matches if sec.level == min_level]
        
        # Collect roots and descendants up to max_depth
        candidates: List[Section] = []
        visited = set()
        for root in api_root_nodes:
            q: List[Tuple[Section, int]] = [(root, 0)]  # (section, depth)
            while q:
                curr, depth = q.pop(0)
                if curr.id in visited: continue
                visited.add(curr.id)
                candidates.append(curr)
                # Only traverse children if we haven't hit the depth limit
                if depth < max_depth:
                    q.extend((child, depth + 1) for child in curr.children)
                    
        # If the matched root(s) are at level 1 and there are other level-1 sections after them,
        # include those siblings too (common in flat or semi-flat structures like Pygame)
        if api_root_nodes and all(root.level == 1 for root in api_root_nodes):
            # Get all level-1 sections that appear after the first matched root
            first_root_page = min(root.page_start for root in api_root_nodes)
            level_1_siblings = [s for s in sections 
                               if s.level == 1 
                               and s.page_start >= first_root_page
                               and s not in candidates]
            candidates.extend(level_1_siblings)
            
        # # Prune redundancies (after collecting)
        # candidates = prune_redundant_sections(candidates)
        
        return candidates
    
 

def prune_redundant_sections(sections: List[Section]) -> List[Section]:
    """
    Remove parent sections whose page range is fully covered by their children.
    
    A section is considered redundant if every page in its range is also covered
    by at least one section at a deeper level (higher level number). This prefers
    keeping more granular subsections over their parent containers.
    
    Args:
        sections: List of candidate sections (potentially with parent-child overlaps).
    
    Returns:
        Pruned list with redundant parent sections removed.
        
    Example:
        Input:
            - Chapter (level 1): pages 10-30
            - Section A (level 2): pages 10-20
            - Section B (level 2): pages 20-30
        
        Output: [Section A, Section B]
        (Chapter is fully covered by its children, so it's removed)
        
        Input:
            - Chapter (level 1): pages 10-30
            - Section A (level 2): pages 10-15
            - Section B (level 2): pages 20-30
        
        Output: [Chapter, Section A, Section B]
        (Chapter provides unique coverage for pages 15-20, so it's kept)
    """
    if not sections:
        return []
    
    # First, propagate hierarchical paths so children keep parent context
    propagate_hierarchical_paths(sections)
    
    # Build page -> [(section_id, level)] mapping
    # This tells us which sections cover each page and at what level
    page_coverage: Dict[int, List[Tuple[str, int]]] = {}
    for sec in sections:
        for page in range(sec.page_start, sec.page_end):
            if page not in page_coverage:
                page_coverage[page] = []
            page_coverage[page].append((sec.id, sec.level))
    
    # Determine which sections are redundant
    pruned = []
    for sec in sections:
        is_redundant = True
        
        # Check each page in this section's range
        for page in range(sec.page_start, sec.page_end):
            # Is this page covered by ANY section at a DEEPER level?
            has_deeper_coverage = any(
                lvl > sec.level and sid != sec.id
                for sid, lvl in page_coverage.get(page, [])
            )
            
            if not has_deeper_coverage:
                # This page is only covered by this section or shallower ones
                # Therefore this section is NOT fully redundant
                is_redundant = False
                break
        
        if not is_redundant:
            pruned.append(sec)
    
    return pruned

def propagate_hierarchical_paths(sections: List[Section]) -> None:
    """
    Propagate full hierarchical path to all sections before pruning.
    
    This ensures children maintain context like "API Reference > Core Classes > DataFrame"
    even after their parent sections are pruned.
    
    Modifies sections in-place.
    """
    # Build tree first
    root_nodes = build_section_tree(sections)
    
    def traverse(node: Section, parent_path: List[str]):
        # Build full path: parent titles + this title
        full_path = parent_path + [node.title] if node.title else parent_path
        node.path = full_path
        
        for child in node.children:
            traverse(child, full_path)
    
    for root in root_nodes:
        traverse(root, [])