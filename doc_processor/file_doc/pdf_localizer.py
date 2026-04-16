from __future__ import annotations
import os, re, json, math, hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any
import fitz  # PyMuPDF

from .multiline_signature import SignatureJoiner


# Lazy import for pix2tex (expensive to load)
_latex_ocr_model = None

def _get_latex_ocr_model():
    """Lazy-load the LaTeX OCR model to avoid startup cost."""
    global _latex_ocr_model
    if _latex_ocr_model is None:
        try:
            from pix2tex.cli import LatexOCR
            import torch
            
            # Force CUDA if available
            if torch.cuda.is_available():
                torch.set_default_device('cuda')
                print(f"LaTeX OCR: Using GPU ({torch.cuda.get_device_name(0)})")
            else:
                print("LaTeX OCR: CUDA not available, using CPU")
            
            _latex_ocr_model = LatexOCR()
        except ImportError:
            print("Warning: pix2tex not installed. LaTeX OCR fallback disabled.")
            _latex_ocr_model = False  # Mark as unavailable
        except Exception as e:
            print(f"Warning: Failed to load LaTeX OCR model: {e}")
            _latex_ocr_model = False
    return _latex_ocr_model if _latex_ocr_model else None


@dataclass
class Section:
    id: str
    title: str
    level: int
    page_start: int
    page_end: int           # exclusive
    text_raw: str
    text_norm: str
    path: List[str] = field(default_factory=list)
    children: List["Section"] = field(default_factory=list)


class PDFSectionizer:
    """
    Parse PDF into a heading tree by leveraging:
      - Outline/TOC if present
      - Font size clustering from per-span metadata
      - Reading-order normalization (two-column handling via x0 histogram)
    Produces sections with raw and normalized text.
    """
    def __init__(self, pdf_path: str, use_equation_ocr: bool = False):
        self.pdf_path = pdf_path
        self.doc = fitz.open(pdf_path)
        self._page_text_cache: Dict[int, Dict[str, Any]] = {}
        self._blocks_cache: Dict[int, List[Dict]] = {} # page index -> blocks
        self.use_equation_ocr = use_equation_ocr
    
    def _get_doc_hash(self) -> str:
        stat = os.stat(self.pdf_path)
        h = hashlib.sha256()
        h.update(self.pdf_path.encode())
        h.update(str(stat.st_mtime_ns).encode())
        return h.hexdigest()

    def _dehyphenate(self, s: str) -> str:
        # soft hyphens and line-end hyphenation
        s = s.replace('\u00ad', '')
        s = re.sub(r'(\w)-\n(\w)', r'\1\2', s)
        return s

    def _normalize(self, s: str) -> str:
        # normalize for matching but keep raw for output
        s = self._dehyphenate(s)
        s = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', s)
        s = s.replace('\u2013', '-').replace('\u2014', '--')
        s = s.replace('\u2018', "'").replace('\u2019', "'")
        s = s.replace('\u201c', '"').replace('\u201d', '"')
        # Collapse whitespace OUTSIDE code blocks only
        # Split by code fences, normalize non-code parts, rejoin
        parts = re.split(r'(```[\s\S]*?```)', s)
        normalized_parts = []
        for i, part in enumerate(parts):
            if (part.startswith('```') and part.endswith('```')) or (part.startswith('$$') and part.endswith('$$')):
                # Code block or LaTeX block - preserve as-is
                normalized_parts.append(part)
            else:
                # Non-code - collapse INLINE whitespace only, preserve leading indentation
                # Process line by line to preserve leading spaces
                lines = part.split('\n')
                normalized_lines = []
                for line in lines:
                    # Preserve leading whitespace, collapse internal whitespace
                    leading = len(line) - len(line.lstrip(' \t'))
                    leading_spaces = line[:leading]
                    rest = line[leading:]
                    # Collapse multiple spaces/tabs in the rest of the line
                    rest = re.sub(r'[ \t]+', ' ', rest)
                    normalized_lines.append(leading_spaces + rest)
                part = '\n'.join(normalized_lines)
                normalized_parts.append(part)
        s = ''.join(normalized_parts)
        s = re.sub(r'\n{3,}', '\n\n', s)
        return s
    
    def _page_blocks(self, pidx: int) -> List[Dict[str, Any]]:
        """Returns a list of blocks on the page."""
        if pidx in self._blocks_cache:
            return self._blocks_cache[pidx]
        
        page = self.doc.load_page(pidx)
        data = page.get_text("dict")  # blocks/lines/spans with fonts and sizes
        self._blocks_cache[pidx] = data.get("blocks", [])
        return data.get("blocks", [])

    def _reading_order_blocks(self, pidx: int) -> List[Dict[str, Any]]:
        """Returns a list of blocks in reading order of the page."""
        
        blocks = [b for b in self._page_blocks(pidx) if b.get("type", 0) == 0]
        if not blocks:
            return []
        
        page = self.doc.load_page(pidx)
        page_width = page.rect.width
        
        # Collect left edges of all blocks
        xs = [b["bbox"][0] for b in blocks]
        
        # True two-column detection:
        # 1. Check for a clear gutter (gap) near the middle of the page
        # 2. Both columns must have substantial content
        # 3. Column widths should be roughly equal
        
        mid_zone_start = page_width * 0.4
        mid_zone_end = page_width * 0.6
        
        # Count blocks starting in left third vs right third
        left_third = [b for b in blocks if b["bbox"][0] < page_width * 0.35]
        right_third = [b for b in blocks if b["bbox"][0] > page_width * 0.55]
        
        # Check if blocks in each "column" span significant height
        def column_height_span(block_list):
            if not block_list:
                return 0
            ys = [b["bbox"][1] for b in block_list] + [b["bbox"][3] for b in block_list]
            return max(ys) - min(ys)
        
        left_span = column_height_span(left_third)
        right_span = column_height_span(right_third)
        page_height = page.rect.height
        
        # Require BOTH columns to span at least 30% of page height
        # AND have at least 3 blocks each
        is_two_column = (
            len(left_third) >= 3 and 
            len(right_third) >= 3 and
            left_span > page_height * 0.3 and
            right_span > page_height * 0.3
        )
        
        if is_two_column:
            # True two-column: read left column top-to-bottom, then right column
            left_col = [b for b in blocks if b["bbox"][2] < page_width * 0.52]  # right edge in left half
            right_col = [b for b in blocks if b["bbox"][0] > page_width * 0.48]  # left edge in right half
            
            left_sorted = sorted(left_col, key=lambda b: (b["bbox"][1], b["bbox"][0]))
            right_sorted = sorted(right_col, key=lambda b: (b["bbox"][1], b["bbox"][0]))
            return left_sorted + right_sorted
        
        # Single column (or mixed layout): simple top-to-bottom, left-to-right
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))
    
    
    def _page_likely_has_tables(self, pidx: int) -> bool:
        """
        Fast heuristic to detect if a page likely contains tables.
        
        This is used to gate the expensive find_tables() call.
        
        Detection signals:
            1. Column alignment: Multiple blocks have spans at similar x-coordinates
            2. Grid lines: Page has horizontal/vertical drawing elements
            3. Row uniformity: Multiple lines with same number of text segments
            4. Cell-like content: Short text segments in grid patterns
            5. Numeric density: Higher than average number/symbol content
        
        Args:
            pidx: Zero-based page index.
        
        Returns:
            True if page likely contains tables, False otherwise.
        """
        blocks = self._reading_order_blocks(pidx)
        if not blocks:
            return False
        
        page = self.doc.load_page(pidx)
        
        # =========================================================================
        # Signal 1: Check for drawing elements (lines/rectangles = table borders)
        # EXCLUDE lines in header/footer zones (decorative separators)
        # =========================================================================
        
        page_height = page.rect.height
        header_zone = page_height * 0.12  # Top 12%
        footer_zone = page_height * 0.88  # Bottom 12%
        
        try:
            drawings = page.get_drawings()
            # Count horizontal and vertical lines
            h_lines = 0
            v_lines = 0
            for d in drawings:
                if d.get("type") == "l":  # line
                    # Check if mostly horizontal or vertical
                    items = d.get("items", [])
                    for item in items:
                        if item[0] == "l":  # line item
                            p1, p2 = item[1], item[2]
                            
                            # Skip lines in header/footer zones
                            line_y = (p1.y + p2.y) / 2
                            if line_y < header_zone or line_y > footer_zone:
                                continue
                            
                            dx = abs(p2.x - p1.x)
                            dy = abs(p2.y - p1.y)
                            
                            # Also skip full-width horizontal lines (likely decorative)
                            page_width = page.rect.width
                            if dx > page_width * 0.8 and dy < 5:
                                continue  # Skip page-spanning lines
                            
                            if dx > 50 and dy < 5:  # Horizontal line
                                h_lines += 1
                            elif dy > 20 and dx < 5:  # Vertical line
                                v_lines += 1
                elif d.get("type") == "re":  # rectangle
                    # Check rectangle is in content zone
                    rect = d.get("rect")
                    if rect:
                        rect_y = (rect.y0 + rect.y1) / 2
                        if header_zone < rect_y < footer_zone:
                            h_lines += 2
                            v_lines += 2
            
            # If we have a grid-like pattern, likely a table
            if h_lines >= 3 and v_lines >= 2:
                return True
        except Exception:
            pass  # get_drawings() may not be available
        
        # =========================================================================
        # Signal 2: Column alignment analysis
        # =========================================================================
        # Collect x-coordinates of all span starts
        x_coords_per_line: List[List[float]] = []
        all_x_coords: List[float] = []
        
        for b in blocks:
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                if len(spans) >= 2:  # Multiple spans in a line = potential columns
                    xs = [round(sp["bbox"][0], 0) for sp in spans if sp.get("text", "").strip()]
                    if len(xs) >= 2:
                        x_coords_per_line.append(sorted(xs))
                        all_x_coords.extend(xs)
        
        # Check for repeated x-coordinate patterns (column alignment)
        if len(x_coords_per_line) >= 3:
            # Count how many lines share similar column structure
            from collections import Counter
            # Create a "column signature" for each line (number of columns + rough positions)
            signatures = []
            for xs in x_coords_per_line:
                # Signature: number of columns + binned first few positions
                sig = (len(xs), tuple(x // 50 for x in xs[:4]))  # Bin to 50px
                signatures.append(sig)
            
            sig_counts = Counter(signatures)
            most_common_count = sig_counts.most_common(1)[0][1] if sig_counts else 0
            
            # If 3+ lines share the same column pattern, likely a table
            if most_common_count >= 3:
                return True
        
        # =========================================================================
        # Signal 3: Cell-like content detection
        # =========================================================================
        # Tables often have many short text segments
        short_span_count = 0
        total_span_count = 0
        numeric_span_count = 0
        
        for b in blocks:
            for line in b.get("lines", []):
                for sp in line.get("spans", []):
                    text = sp.get("text", "").strip()
                    if not text:
                        continue
                    total_span_count += 1
                    
                    # Short text (< 30 chars) is table-like
                    if len(text) < 30:
                        short_span_count += 1
                    
                    # Numeric/symbolic content
                    if any(c.isdigit() for c in text) or text in {'-', '–', '—', '×', '✓', '✗', 'N/A', 'Yes', 'No'}:
                        numeric_span_count += 1
        
        if total_span_count > 0:
            short_ratio = short_span_count / total_span_count
            numeric_ratio = numeric_span_count / total_span_count
            
            # High ratio of short spans + some numeric content = likely table
            if short_ratio > 0.7 and numeric_ratio > 0.2 and total_span_count >= 10:
                return True
        
        # =========================================================================
        # Signal 4: Block with many multi-span lines
        # =========================================================================
        for b in blocks:
            lines = b.get("lines", [])
            if len(lines) < 3:
                continue
            
            multi_span_lines = sum(1 for line in lines if len(line.get("spans", [])) >= 2)
            
            # If most lines in a block have multiple spans, likely a table
            if multi_span_lines >= 3 and multi_span_lines / len(lines) > 0.6:
                return True
        
        return False
    
    
    def _is_table_block(self, block: Dict) -> bool:
        """
        Detect if a block looks like a table based on column alignment patterns.
        
        Used as fallback when find_tables() is unavailable or disabled.
        
        Args:
            block: PyMuPDF block dictionary.
        
        Returns:
            True if block appears to be a table.
        """
        lines = block.get("lines", [])
        if len(lines) < 2:
            return False
        
        # Collect x-coordinate patterns for each line
        x_patterns = []
        for line in lines:
            xs = sorted(set(
                round(sp["bbox"][0], 0)
                for sp in line.get("spans", [])
                if sp.get("text", "").strip()
            ))
            if len(xs) >= 2:  # Multiple columns
                x_patterns.append(tuple(xs))
        
        # Check for consistent column structure
        if len(x_patterns) >= 2:
            first_pattern = x_patterns[0]
            matches = sum(1 for p in x_patterns if len(p) == len(first_pattern))
            if matches >= len(x_patterns) * 0.6:
                return True
        return False
    
    # =========================================================================
    # TEXT EXTRACTION HELPER METHODS
    # =========================================================================
    
    def _get_page_links(self, page: fitz.Page) -> List[Tuple[fitz.Rect, str]]:
        """
        Extract all hyperlinks from a page.
        
        Args:
            page: PyMuPDF page object.
        
        Returns:
            List of (rect, uri) tuples where rect is the clickable area.
        """
        link_map: List[Tuple[fitz.Rect, str]] = []
        try:
            for link in page.get_links():
                rect = link.get("from")
                if not rect:
                    continue
                
                uri = None
                if link.get("uri"):
                    uri = link["uri"]
                elif link.get("page") is not None:
                    target_page = link["page"] + 1
                    uri = f"#page={target_page}"
                elif link.get("nameddest"):
                    uri = f"#{link['nameddest']}"
                
                if uri:
                    link_map.append((fitz.Rect(rect), uri))
        except Exception:
            pass
        return link_map
    
    def _get_link_for_span(self, span_bbox: Tuple[float, float, float, float], 
                           link_map: List[Tuple[fitz.Rect, str]]) -> Optional[str]:
        """
        Check if a span overlaps with any hyperlink and return the URI.
        
        Args:
            span_bbox: (x0, y0, x1, y1) of the span.
            link_map: List of (rect, uri) tuples from _get_page_links.
        
        Returns:
            URI string if span is linked, None otherwise.
        """
        span_rect = fitz.Rect(span_bbox)
        for link_rect, uri in link_map:
            intersection = span_rect & link_rect
            if intersection.is_empty:
                continue
            
            span_area = span_rect.width * span_rect.height
            if span_area > 0:
                overlap_ratio = (intersection.width * intersection.height) / span_area
                if overlap_ratio > 0.5:
                    return uri
            
            span_center = fitz.Point(
                (span_bbox[0] + span_bbox[2]) / 2,
                (span_bbox[1] + span_bbox[3]) / 2
            )
            if link_rect.contains(span_center):
                return uri
        return None
    
    def _extract_tables_from_page(self, page: fitz.Page, pidx: int) -> List[Dict[str, Any]]:
        """
        Extract tables from a page using PyMuPDF's find_tables() API.
        
        Args:
            page: PyMuPDF page object.
            pidx: Page index (for heuristic check).
        
        Returns:
            List of table entries with position and formatted text.
        """
        table_entries: List[Dict[str, Any]] = []
        
        if not self._page_likely_has_tables(pidx):
            return table_entries
        
        try:
            tables = page.find_tables()
            for table in tables:
                bbox = table.bbox
                rows = table.extract()
                
                formatted_rows = []
                for row in rows:
                    formatted_row = " | ".join(
                        str(cell).strip() if cell else ""
                        for cell in row
                    )
                    formatted_rows.append(formatted_row)
                formatted_text = "\n".join(formatted_rows)
                
                table_entries.append({
                    'y_top': bbox[1],
                    'y_bottom': bbox[3],
                    'x0': bbox[0],
                    'x1': bbox[2],
                    'text': formatted_text,
                    'inserted': False
                })
        except (AttributeError, Exception):
            pass
        
        table_entries.sort(key=lambda t: t['y_top'])
        return table_entries
    
    def _block_overlaps_table(self, block: Dict, table_entries: List[Dict]) -> bool:
        """
        Check if a text block significantly overlaps any table region.
        
        Args:
            block: PyMuPDF block dictionary with 'bbox' key.
            table_entries: List of extracted table entries.
        
        Returns:
            True if block overlaps a table region.
        """
        if not table_entries:
            return False
        
        bx0, by0, bx1, by1 = block["bbox"]
        for tbl in table_entries:
            tx0, ty0, tx1, ty1 = tbl['x0'], tbl['y_top'], tbl['x1'], tbl['y_bottom']
            
            vert_overlap = min(by1, ty1) - max(by0, ty0)
            block_height = by1 - by0
            
            if block_height > 0 and vert_overlap > block_height * 0.5:
                horiz_overlap = min(bx1, tx1) - max(bx0, tx0)
                if horiz_overlap > 0:
                    return True
        return False
    
    def _detect_monospace_fonts(self, blocks: List[Dict]) -> set:
        """
        Detect monospace fonts used in the page.
        
        Uses ONLY font name keywords - structural detection is unreliable
        because it measures span-level averages, not individual characters.
        
        Args:
            blocks: List of PyMuPDF block dictionaries.
        
        Returns:
            Set of font names identified as monospace.
        """
        font_names = set()
        
        for b in blocks:
            for line in b.get("lines", []):
                for sp in line.get("spans", []):
                    font = sp.get("font", "")
                    if font:
                        font_names.add(font)
        
        # Keyword-based detection ONLY
        monospace_keywords = {
            'mono', 'courier', 'code', 'consolas', 'menlo',
            'source', 'fira', 'dejavu', 'liberation', 'inconsolata',
            'fixed', 'typewriter', 'terminal', 'lucida console',
            'andale', 'monaco', 'pragmata', 'hack', 'iosevka',
            'jetbrains', 'cascadia', 'sf mono', 'roboto mono',
            'ubuntu mono', 'droid sans mono', 'noto mono'
        }
        
        return {
            f for f in font_names
            if any(kw in f.lower() for kw in monospace_keywords)
        }
    
    def _is_bullet_or_list_item(self, text: str) -> bool:
        """
        Check if a line starts with a bullet or list marker.
        
        Args:
            text: Line text content.
        
        Returns:
            True if line starts with a list marker.
        """
        stripped = text.strip()
        if not stripped:
            return False
        
        if stripped[0] in '•◦▪▸►-–—*':
            return True
        
        if re.match(r'^(\d+\.|[a-z]\.|[ivx]+\.|\(\d+\)|\([a-z]\))\s', stripped, re.IGNORECASE):
            return True
        
        return False
    
    def _calculate_char_width(self, spans: List[Dict]) -> float:
        """
        Calculate character width from spans (for monospace alignment).
        
        Args:
            spans: List of span dictionaries.
        
        Returns:
            Estimated character width in pixels.
        """
        char_widths = []
        for sp in spans:
            t = sp.get("text", "")
            if t and len(t) > 0:
                sp_width = sp["bbox"][2] - sp["bbox"][0]
                char_widths.append(sp_width / len(t))
        
        if char_widths:
            return max(sorted(char_widths)[len(char_widths) // 2], 4.0)
        return 7.0
    
    
    def _build_code_line_text(self, spans: List[Dict], code_region_x0: float, link_map: List[Tuple[fitz.Rect, str]]) -> str:
        """
        Build code line text with proper alignment.
        
        For code blocks, we need to preserve exact character positioning.
        This uses a character-grid approach where we place each character at its exact column position based on x-coordinate.
        
        Args:
            spans: List of span dictionaries sorted by x-coordinate.
            code_region_x0: Left boundary of the code region (used as reference for leading indent).
            link_map: List of hyperlink mappings.
        
        Returns:
            Formatted code line string with preserved column alignment.
        """
        if not spans:
            return ""
        
        # Filter and sort spans by x-coordinate
        valid_spans = [sp for sp in spans if sp.get("text")]
        if not valid_spans:
            return ""
        valid_spans.sort(key=lambda sp: sp["bbox"][0])
        
        # Calculate character width - use MINIMUM to get the tightest fit
        char_widths = []
        for sp in valid_spans:
            t = sp.get("text", "")
            if len(t) > 0:
                sp_width = sp["bbox"][2] - sp["bbox"][0]
                cw = sp_width / len(t)
                if 3.0 < cw < 15.0:
                    char_widths.append(cw)
        
        if char_widths:
            char_width = min(char_widths)
        else:
            char_width = 6.0
        
        # Ensure minimum
        char_width = max(char_width, 4.0)
        
        ref_x0 = code_region_x0
        
        # Build a character grid - place each span's text at its column position
        span_placements = []  # List of (start_col, text, orig_len)
        
        for sp in valid_spans:
            t = sp.get("text", "")
            if not t:
                continue
            
            sp_x0 = sp["bbox"][0]
            
            # Calculate column position relative to code region's left edge
            # This preserves the original indentation/margin
            col = int(round((sp_x0 - ref_x0) / char_width))
            col = max(0, col)
            
            # Check for hyperlink
            span_bbox = sp.get("bbox")
            link_uri = self._get_link_for_span(span_bbox, link_map) if span_bbox else None
            
            if link_uri:
                text_to_add = f"{t}({link_uri})"
            else:
                text_to_add = t
            
            span_placements.append((col, text_to_add, len(t)))  # Store original text length
        
        # Build the result string by placing spans at their columns
        result = []
        current_col = 0
        
        for col, text, orig_len in span_placements:
            if col > current_col:
                # Add spaces to reach the target column
                result.append(' ' * (col - current_col))
                current_col = col
            # If col <= current_col, spans are adjacent/overlapping - no extra space
            
            result.append(text)
            # Update current column based on ORIGINAL text length (not including hyperlink)
            current_col = col + orig_len
        
        return ''.join(result)
    
    
    def _detect_code_block_rects(self, page: fitz.Page) -> List[fitz.Rect]:
        """
        Detect rectangles that likely contain code blocks.
        
        Code blocks in PDFs are often rendered with:
            - A shaded/filled background rectangle
            - A bordered rectangle
        
        Args:
            page: PyMuPDF page object.
        
        Returns:
            List of rectangles that likely contain code blocks.
        """
        code_rects = []
        page_width = page.rect.width
        page_height = page.rect.height
        
        try:
            drawings = page.get_drawings()
            
            for path in drawings:
                rect = path.get("rect")
                if not rect:
                    continue
                
                rect = fitz.Rect(rect)
                width = rect.width
                height = rect.height
                
                # Filter criteria for code block rectangles:
                # 1. Reasonable width (at least 40% of page width, not full page)
                # 2. Reasonable height (at least 15px, not too tall like page border)
                # 3. Has fill color (shaded background) OR has stroke (border)
                
                width_ratio = width / page_width
                is_reasonable_width = 0.3 < width_ratio < 0.95
                is_reasonable_height = 15 < height < page_height * 0.8
                
                has_fill = path.get("fill") is not None
                has_stroke = path.get("color") is not None and path.get("width", 0) > 0
                
                if is_reasonable_width and is_reasonable_height and (has_fill or has_stroke):
                    # Additional check: fill color should be light (not black text highlight)
                    if has_fill:
                        fill = path.get("fill")
                        if isinstance(fill, (list, tuple)) and len(fill) >= 3:
                            # Check if it's a light color (gray, light blue, etc.)
                            avg_color = sum(fill[:3]) / 3
                            if avg_color > 0.7:  # Light color
                                code_rects.append(rect)
                        elif isinstance(fill, (int, float)) and fill > 0.7:
                            # Grayscale light color
                            code_rects.append(rect)
                    elif has_stroke:
                        # Bordered rectangle without fill - also likely code block
                        code_rects.append(rect)
        
        except Exception:
            pass
        
        # Merge overlapping rectangles
        merged = []
        for rect in sorted(code_rects, key=lambda r: (r.y0, r.x0)):
            if not merged:
                merged.append(rect)
            else:
                last = merged[-1]
                # Check if rectangles overlap or are very close
                if rect.y0 <= last.y1 + 5 and rect.x0 < last.x1:
                    # Merge
                    merged[-1] = last | rect  # Union
                else:
                    merged.append(rect)
        return merged
    
    def _is_equation_region(self, rows: List[Dict], start_idx: int) -> Tuple[bool, int]:
        """
        Detect if a sequence of rows forms an equation block.
        
        Equations typically have:
            - Centered positioning (x0 significantly indented from margin)
            - Mathematical symbols (∑, ∫, ∏, √, ±, ≤, ≥, etc.)
            - Small vertical gaps between components (subscripts, superscripts)
            - Mixed font sizes in close proximity
            - Short line lengths (not full-width prose)
        
        Args:
            rows: List of visual row metadata
            start_idx: Index to start checking from
            
        Returns:
            (is_equation, end_idx) - whether it's an equation and where it ends
        """
        if start_idx >= len(rows):
            return False, start_idx
        
        # Math symbols that indicate equation content
        MATH_SYMBOLS = {'∑', '∫', '∏', '√', '±', '≤', '≥', '≠', '∈', '∉', '⊂', '⊃', 
                        '∪', '∩', '∀', '∃', '∞', '∂', '∇', '⋆', '×', '÷', '∝', '≈',
                        'Σ', 'Π', '∆', '⌊', '⌋', '⌈', '⌉'} 
        
        # Greek letters (often in equations but also in prose)
        GREEK_LETTERS = {'α', 'β', 'γ', 'δ', 'ε', 'θ', 'λ', 'μ', 'σ', 'φ', 'ω', 'π', 'ρ', 'τ', 'Γ', 'Δ', 'Ε', 'Θ', 'Λ', 'Σ', 'Φ', 'Ω', 'Π'}
        
        # Mathematical operators (for detecting inline equations)
        MATH_OPERATORS = {'+', '-', '×', '÷', '=', '<', '>', '≤', '≥', '≠', '±', '∝', '≈'}
        
        row = rows[start_idx]
        row_text = row.get("text", "")
        row_x0 = row.get("x0", 0)
        
        # Check for strong math indicators (symbols that ONLY appear in equations)
        has_strong_math = any(sym in row_text for sym in MATH_SYMBOLS)
        
        # Check for Greek letters with operators (likely equation)
        has_greek = any(sym in row_text for sym in GREEK_LETTERS)
        has_operators = any(op in row_text for op in MATH_OPERATORS)
        has_greek_equation = has_greek and has_operators
        
        # Check for equation-like patterns
        has_equation_pattern = bool(
            re.search(r'\w+\s*\([^)]*\)\s*=', row_text) or  # func() = 
            re.search(r'[A-Z]\s*[∑Σ]\s*[a-z]', row_text) or  # C∑in
            re.search(r'\^\{?\d+\}?', row_text) # superscript like x^2 or x^{2}
        )
        
        # Check if this is a SHORT line (equations are typically not full-width)
        is_short_line = len(row_text.strip()) < 100
        
        # Check if line is centered (indented from left margin)
        # Typical page margin is ~70px, centered content starts at ~150px+
        is_centered = row_x0 > 120  # Adjust based on your PDFs
        
        # Detect equation if:
        # 1. Has strong math symbols AND is short, OR
        # 2. Has Greek letters with operators AND is short AND centered, OR
        # 3. Has equation patterns AND is centered AND short
        is_equation_start = (
            (has_strong_math and is_short_line) or
            (has_greek_equation and is_short_line and is_centered) or
            (has_equation_pattern and is_centered and is_short_line)
        )
        
        if not is_equation_start:
            return False, start_idx
        
        # =========================================================================
        # Found potential equation start - now find the FULL extent
        # =========================================================================
        
        end_idx = start_idx + 1
        prev_y = row.get("y_center", 0)
        
        # Track the equation's x-bounds to detect when we leave the equation region
        eq_x0_min = row_x0
        eq_x0_max = row_x0
        
        while end_idx < len(rows):
            next_row = rows[end_idx]
            next_text = next_row.get("text", "").strip()
            next_y = next_row.get("y_center", 0)
            next_x0 = next_row.get("x0", 0)
            
            y_gap = next_y - prev_y
            
            # =====================================================================
            # STOP CONDITIONS
            # =====================================================================
            
            # 1. Very large vertical gap (> 50 pixels = definitely new section)
            if y_gap > 50:
                break
            
            # 2. Line starts with bullet/list marker - not part of equation
            if next_text and next_text[0] in '•◦▪▸►-–—*':
                break
            
            # 3. Line starts with "where" or similar prose connector - STOP
            #    These explain the equation but aren't part of it
            if re.match(r'^(where|here|note|with|for|if|when|such that)\s', next_text, re.IGNORECASE):
                break
            
            # 4. Long prose line (> 120 chars without math symbols)
            if len(next_text) > 120 and not any(sym in next_text for sym in MATH_SYMBOLS):
                break
            
            # 5. Line ends with period and is long (prose sentence)
            if next_text.endswith('.') and len(next_text) > 80:
                break
            
            # =====================================================================
            # CONTINUE CONDITIONS
            # =====================================================================
            
            has_math = any(sym in next_text for sym in MATH_SYMBOLS)
            has_greek = any(sym in next_text for sym in GREEK_LETTERS)
            is_short = len(next_text) < 80
            is_very_short = len(next_text) < 20  # Likely subscript/superscript
            
            # Check if x0 is consistent with equation region (within tolerance)
            x0_in_range = abs(next_x0 - eq_x0_min) < 100 or abs(next_x0 - eq_x0_max) < 100
            
            # Include if:
            # - Has math symbols
            # - Is very short (likely sub/superscript)
            # - Has equation patterns (like "k=0")
            # - Is short and within x-bounds
            
            if has_math:
                end_idx += 1
                prev_y = next_y
                eq_x0_min = min(eq_x0_min, next_x0)
                eq_x0_max = max(eq_x0_max, next_x0)
            elif is_very_short and x0_in_range:
                # Very short line in equation region - likely part of equation
                end_idx += 1
                prev_y = next_y
            elif is_short and re.search(r'[a-zA-Z]\s*=\s*\d+', next_text):
                # Pattern like "k=0" or "n=1"
                end_idx += 1
                prev_y = next_y
            elif is_short and y_gap < 15 and x0_in_range:
                # Close vertical proximity and within x-bounds
                end_idx += 1
                prev_y = next_y
            else:
                # Doesn't look like part of the equation
                break
        
        # Only return as equation if we have at least 1 row
        return True, end_idx


    def _extract_equation_from_rect(self, page: fitz.Page, rect: fitz.Rect, link_map: List) -> str:
        """
        Extract equation from a page rectangle using character-level positioning.
        
        Uses PyMuPDF's rawdict to get exact character positions and builds
        a 2D grid to preserve the visual layout.
        
        Args:
            page: PyMuPDF page object
            rect: Rectangle defining the equation region
            link_map: Hyperlink mappings
            
        Returns:
            Formatted equation text wrapped in a code fence
        """
        try:
            data = page.get_text("rawdict", clip=rect)
        except Exception:
            return ""
        
        # Collect all characters with positions
        all_chars = []
        
        for block in data.get("blocks", []):
            if block.get("type") != 0:  # Skip non-text blocks
                continue
            
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font_size = span.get("size", 10)
                    span_bbox = span.get("bbox", [0, 0, 0, 0])
                    
                    # Get character-level data
                    chars = span.get("chars", [])
                    
                    if chars:
                        for char in chars:
                            c = char.get("c", "")
                            if not c or c.isspace():
                                continue
                            
                            bbox = char.get("bbox", [0, 0, 0, 0])
                            origin = char.get("origin", [bbox[0], bbox[3]])
                            
                            all_chars.append({
                                "c": c,
                                "x": bbox[0],
                                "y": origin[1] if len(origin) > 1 else bbox[3],
                                "width": max(bbox[2] - bbox[0], 1),
                                "size": font_size
                            })
                    else:
                        # Fallback: estimate from span
                        text = span.get("text", "")
                        if text:
                            char_width = (span_bbox[2] - span_bbox[0]) / max(len(text), 1)
                            for i, c in enumerate(text):
                                if c.isspace():
                                    continue
                                all_chars.append({
                                    "c": c,
                                    "x": span_bbox[0] + i * char_width,
                                    "y": span_bbox[3],
                                    "width": max(char_width, 1),
                                    "size": font_size
                                })
        
        if not all_chars:
            return ""
        
        # Calculate grid parameters
        min_x = min(c["x"] for c in all_chars)
        max_x = max(c["x"] + c["width"] for c in all_chars)
        min_y = min(c["y"] for c in all_chars)
        max_y = max(c["y"] for c in all_chars)
        
        # Cell width: use median character width
        widths = sorted([c["width"] for c in all_chars if c["width"] > 1])
        cell_width = widths[len(widths) // 2] if widths else 5.0
        cell_width = max(cell_width, 2.5)
        
        # Cell height: based on smallest font (for subscripts)
        sizes = [c["size"] for c in all_chars if c["size"] > 0]
        cell_height = (min(sizes) if sizes else 8.0) * 1.1
        cell_height = max(cell_height, 5.0)
        
        # Grid dimensions
        cols = int((max_x - min_x) / cell_width) + 2
        rows = int((max_y - min_y) / cell_height) + 2
        
        # Safety limits
        cols = min(max(cols, 1), 400)
        rows = min(max(rows, 1), 100)
        
        # Initialize grid
        grid = [[' ' for _ in range(cols)] for _ in range(rows)]
        occupied = [[False for _ in range(cols)] for _ in range(rows)]
        
        # Sort by position for consistent placement
        all_chars.sort(key=lambda c: (round(c["y"] / cell_height), c["x"]))
        
        # Place characters
        for char in all_chars:
            col = int((char["x"] - min_x) / cell_width)
            row = int((char["y"] - min_y) / cell_height)
            
            if not (0 <= row < rows and 0 <= col < cols):
                continue
            
            c = char["c"]
            
            # Try to place in empty cell
            placed = False
            for offset in [0, 1, -1, 2, -2]:
                test_col = col + offset
                if 0 <= test_col < cols and not occupied[row][test_col]:
                    grid[row][test_col] = c
                    occupied[row][test_col] = True
                    placed = True
                    break
            
            if not placed:
                # Overwrite if necessary
                grid[row][col] = c
        
        # Build output lines
        lines = [''.join(row).rstrip() for row in grid]
        
        # Trim empty lines
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        
        # Remove common leading indent
        if lines:
            min_indent = min(
                (len(line) - len(line.lstrip()) for line in lines if line.strip()),
                default=0
            )
            if min_indent > 0:
                lines = [line[min_indent:] if len(line) >= min_indent else line for line in lines]
        
        if not lines:
            return ""
        
        return f"```\n{chr(10).join(lines)}\n```"
    
    
    def _batch_ocr_equations(self, page: fitz.Page, equation_rects: List[fitz.Rect]) -> Dict[int, str]:
        """
        Batch OCR multiple equations from a single page.
                
        Args:
            page: PyMuPDF page object
            equation_rects: List of rectangles defining equation regions
            
        Returns:
            Dict mapping rect index to LaTeX string (or None if OCR failed)
        """
        results = {}
        model = _get_latex_ocr_model()
        
        if model is None:
            return results
        
        try:
            from PIL import Image
            import io
            
            for idx, rect in enumerate(equation_rects):
                try:
                    # Render the equation region to an image
                    clip = fitz.Rect(rect)
                    
                    # Add padding
                    padding = 10
                    clip.x0 = max(0, clip.x0 - padding)
                    clip.y0 = max(0, clip.y0 - padding)
                    clip.x1 = min(page.rect.width, clip.x1 + padding)
                    clip.y1 = min(page.rect.height, clip.y1 + padding)
                    
                    # Render at 300 DPI
                    mat = fitz.Matrix(300 / 72, 300 / 72)
                    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                    
                    # Convert to PIL Image
                    img_data = pix.tobytes("png")
                    img = Image.open(io.BytesIO(img_data))
                    
                    # Run OCR
                    latex = model(img)
                    
                    if latex and latex.strip():
                        results[idx] = latex.strip()
                
                except Exception:
                    # Skip this equation on error
                    pass
        
        except Exception:
            pass
        
        return results
    
    
    def _extract_page_text(self, pidx: int) -> Tuple[str, List[Tuple[float, str]]]:
        """
        Extract page text while preserving document structure.
        
        This method uses a visual-row-based approach to correctly handle:
            - Code blocks with tabular output (DataFrame displays)
            - Tables
            - Bullet lists
            - Regular prose
        
        The key innovation is grouping text elements by their visual row
        (y-coordinate) ACROSS block boundaries, then propagating "code"
        status from REPL prompts (>>>) to subsequent output rows.
        
        Args:
            pidx: Zero-based page index.
        
        Returns:
            A tuple of:
                - raw: Extracted text with structure preserved.
                - spans_info: List of (font_size, text) tuples for heading detection.
        """
        
        blocks = self._reading_order_blocks(pidx)
        page = self.doc.load_page(pidx)
        page_height = page.rect.height
        page_width = page.rect.width
        
        parts: List[str] = []
        spans_info: List[Tuple[float, str]] = []
        
        # =====================================================================
        # SETUP: Extract links, tables, detect fonts
        # =====================================================================
        link_map = self._get_page_links(page)
        table_entries = []
        # table_entries = self._extract_tables_from_page(page, pidx) # commented to save compute
        monospace_fonts = self._detect_monospace_fonts(blocks)
        
        # Detect code block rectangles from PDF drawings
        code_block_rects = self._detect_code_block_rects(page)
        
        # Calculate base margin and line width statistics
        line_widths = []
        left_margins = []
        for b in blocks:
            for line in b.get("lines", []):
                w = line["bbox"][2] - line["bbox"][0]
                x0 = line["bbox"][0]
                if w > 10:
                    line_widths.append(w)
                    left_margins.append(x0)
        
        typical_width = sorted(line_widths)[len(line_widths)//2] if line_widths else page_width * 0.8
        short_line_thresh = typical_width * 0.75
        base_margin = min(left_margins) if left_margins else 0
        
        # =====================================================================
        # PHASE 1: Collect ALL lines from ALL blocks with metadata
        # =====================================================================
        all_lines_meta: List[Dict[str, Any]] = []
        
        for block_idx, b in enumerate(blocks):
            if b.get("type", 0) != 0:  # Skip non-text blocks
                continue
            if self._block_overlaps_table(b, table_entries):
                continue
            
            # Check if this block is a heuristic table (fallback when find_tables unavailable)
            block_is_heuristic_table = False
            if not table_entries:  # Only use heuristic if no PyMuPDF tables found
                block_is_heuristic_table = self._is_table_block(b)
            
            for line in b.get("lines", []):
                y_center = (line["bbox"][1] + line["bbox"][3]) / 2
                x0 = line["bbox"][0]
                x1 = line["bbox"][2]
                
                # Get line text
                line_text = "".join(s.get("text", "") for s in line.get("spans", []))
                has_repl = line_text.strip().startswith(('>>>', '...'))
                
                # Check for monospace font
                line_has_monospace = False
                for sp in line.get("spans", []):
                    font = sp.get("font", "")
                    if font in monospace_fonts:
                        line_has_monospace = True
                        break
                
                all_lines_meta.append({
                    "line": line,
                    "block_idx": block_idx,
                    "y_center": y_center,
                    "x0": x0,
                    "x1": x1,
                    "has_repl": has_repl,
                    "has_monospace": line_has_monospace,
                    "is_code": has_repl or line_has_monospace,  # Initial detection
                    "is_table": block_is_heuristic_table, 
                    "text": line_text,
                })
        
        # =====================================================================
        # PHASE 2: Group lines into visual rows (by y-coordinate)
        # =====================================================================
        # Use STRICT y-center matching to prevent merging adjacent DataFrame rows
        all_lines_meta.sort(key=lambda m: (m["y_center"], m["x0"]))
        
        visual_rows: List[List[Dict]] = []
        current_row: List[Dict] = []
        current_row_y: Optional[float] = None
        
        # Use a SMALL tolerance (2-3 pixels) to prevent row merging
        Y_TOLERANCE = 2.5
        
        for meta in all_lines_meta:
            if current_row_y is None:
                current_row = [meta]
                current_row_y = meta["y_center"]
            else:
                # Check if this line has the SAME y-center (within tolerance)
                y_diff = abs(meta["y_center"] - current_row_y)
                
                if y_diff <= Y_TOLERANCE:
                    # Same visual row - add to current row
                    current_row.append(meta)
                else:
                    # New row - save current and start new
                    if current_row:
                        current_row.sort(key=lambda m: m["x0"])
                        visual_rows.append(current_row)
                    current_row = [meta]
                    current_row_y = meta["y_center"]
        
        # Don't forget the last row
        if current_row:
            current_row.sort(key=lambda m: m["x0"])
            visual_rows.append(current_row)
        
        # =====================================================================
        # PHASE 3: Determine code status using VISUAL RECTANGLES + REPL markers
        # =====================================================================
        # Strategy:
        # 1. PRIMARY: If code block rectangles are detected, use them as boundaries
        # 2. FALLBACK: If no rectangles, use REPL markers (>>>) to start code regions and propagate to subsequent rows within similar x-bounds
        
        # Determine if we should use rectangle-based detection
        use_rect_detection = len(code_block_rects) > 0
        
        def row_in_code_rect(row_y0: float, row_y1: float, row_x0: float, row_x1: float) -> bool:
            """
            Check if a row falls inside any code block rectangle.
            
            Args:
                row_y0: Top y-coordinate of the row.
                row_y1: Bottom y-coordinate of the row.
                row_x0: Left x-coordinate of the row.
                row_x1: Right x-coordinate of the row.
            
            Returns:
                True if the row's center is inside a code block rectangle.
            """
            row_y_center = (row_y0 + row_y1) / 2
            row_x_center = (row_x0 + row_x1) / 2
            
            for rect in code_block_rects:
                # Check if row center is inside the rectangle (with small tolerance)
                if (rect.y0 - 5 <= row_y_center <= rect.y1 + 5 and
                    rect.x0 - 10 <= row_x_center <= rect.x1 + 10):
                    return True
            return False
        
        def get_containing_rect(row_y0: float, row_y1: float, row_x0: float, row_x1: float) -> Optional[fitz.Rect]:
            """
            Get the code block rectangle that contains this row.
            
            Returns:
                The containing rectangle, or None if not in any rectangle.
            """
            row_y_center = (row_y0 + row_y1) / 2
            row_x_center = (row_x0 + row_x1) / 2
            
            for rect in code_block_rects:
                if (rect.y0 - 5 <= row_y_center <= rect.y1 + 5 and
                    rect.x0 - 10 <= row_x_center <= rect.x1 + 10):
                    return rect
            return None
        
        # For fallback mode: track code region state
        in_code_region = False
        code_region_x0_fallback = None
        code_region_x1_fallback = None
        
        for row in visual_rows:
            # Calculate row bounds
            row_x0 = min(m["x0"] for m in row)
            row_x1 = max(m["x1"] for m in row)
            row_width = row_x1 - row_x0
            
            # Get y-bounds from the actual line bboxes
            row_y0 = min(m["line"]["bbox"][1] for m in row)
            row_y1 = max(m["line"]["bbox"][3] for m in row)
            
            # Get row text
            row_text = "".join(m["text"] for m in row).strip()
            
            # Check if this is a REPL line
            has_repl_marker = row_text.startswith('>>>') or row_text.startswith('...')
            
            if use_rect_detection:
                # ===== PRIMARY: Rectangle-based detection =====
                is_in_code_rect = row_in_code_rect(row_y0, row_y1, row_x0, row_x1)
                is_code = has_repl_marker or is_in_code_rect
                
                # Get the containing rectangle for spacing calculation
                containing_rect = get_containing_rect(row_y0, row_y1, row_x0, row_x1)
                code_region_x0 = containing_rect.x0 if containing_rect else row_x0
                
            else:
                # ===== FALLBACK: REPL-based propagation =====
                # REPL markers start a code region, propagate to output rows
                
                if has_repl_marker:
                    # Start/continue code region
                    in_code_region = True
                    is_code = True
                    
                    # Update code region bounds
                    if code_region_x0_fallback is None:
                        code_region_x0_fallback = row_x0
                        code_region_x1_fallback = row_x1
                    else:
                        code_region_x0_fallback = min(code_region_x0_fallback, row_x0)
                        code_region_x1_fallback = max(code_region_x1_fallback, row_x1)
                    
                    code_region_x0 = code_region_x0_fallback
                    
                elif in_code_region:
                    # Check if this row is likely code output (within bounds, not prose)
                    x_tolerance = 20
                    starts_within = row_x0 >= code_region_x0_fallback - x_tolerance
                    
                    # Check if row is much wider than code region (likely prose)
                    code_region_width = code_region_x1_fallback - code_region_x0_fallback
                    width_ratio = row_width / code_region_width if code_region_width > 0 else 1
                    
                    # Prose detection
                    is_prose_like = (
                        width_ratio > 1.3 or
                        len(row_text) > 80 or
                        (row_text.endswith('.') and len(row_text) > 40)
                    )
                    
                    if starts_within and not is_prose_like:
                        # This is code output
                        is_code = True
                        code_region_x1_fallback = max(code_region_x1_fallback, row_x1)
                        code_region_x0 = code_region_x0_fallback  # Use CONSISTENT x0
                    else:
                        # End code region - this is prose
                        is_code = False
                        in_code_region = False
                        code_region_x0_fallback = None
                        code_region_x1_fallback = None
                        code_region_x0 = row_x0
                else:
                    # Not in a code region
                    is_code = False
                    code_region_x0 = row_x0
            
            # Mark all lines in this row
            if is_code:
                for m in row:
                    m["is_code"] = True
                    m["is_repl_input"] = has_repl_marker
                    m["code_region_x0"] = code_region_x0
            else:
                for m in row:
                    m["is_code"] = False
                    m["is_repl_input"] = False
        
        # =====================================================================
        # PHASE 4: Insert tables at correct positions (helper function)
        # =====================================================================
        table_insert_idx = 0
        
        def insert_pending_tables(current_y: float) -> None:
            nonlocal table_insert_idx
            while table_insert_idx < len(table_entries):
                tbl = table_entries[table_insert_idx]
                if not tbl['inserted'] and tbl['y_top'] < current_y:
                    parts.append("\n")
                    parts.append(tbl['text'])
                    parts.append("\n\n")
                    tbl['inserted'] = True
                    table_insert_idx += 1
                else:
                    break
        
        # =====================================================================
        # PHASE 5: Reconstruct text from visual rows
        # =====================================================================
        in_code_block = False
        prev_row_was_code = False
        prev_indent = 0
        prev_row_y = None
        pending_hyphen = False
        
        equation_rows = []
        for row in visual_rows:
            # Aggregate row properties
            row_text = "".join(m.get("text", "") for m in row).strip()
            row_x0 = min(m.get("x0", 0) for m in row)
            row_y_center = row[0].get("y_center", 0)
            equation_rows.append({
                "text": row_text,
                "x0": row_x0,
                "y_center": row_y_center,
                "_original_row": row  # Keep reference to original for extraction
            })
        
        # ---------------------------------------------------------------------
        # PRE-PASS: Detect all equations (for both OCR and heuristic modes)
        # This ensures we know equation boundaries before processing
        # ---------------------------------------------------------------------
        use_ocr = getattr(self, 'use_equation_ocr', False)
        
        # Detect all equation regions (used by both modes)
        detected_equations: List[Tuple[int, int, fitz.Rect]] = []  # (start_idx, end_idx, rect)
        
        temp_idx = 0
        while temp_idx < len(visual_rows):
            temp_row = visual_rows[temp_idx]
            temp_is_code = any(m.get("is_code", False) for m in temp_row)
            temp_is_table = any(m.get("is_table", False) for m in temp_row)
            
            if not temp_is_code and not temp_is_table:
                is_eq, eq_end = self._is_equation_region(equation_rows, temp_idx)
                
                if is_eq:
                    # Calculate bounding box for this equation region
                    eq_y_min = float('inf')
                    eq_y_max = float('-inf')
                    eq_x_min = float('inf')
                    eq_x_max = float('-inf')
                    
                    for eidx in range(temp_idx, eq_end):
                        if eidx < len(visual_rows):
                            for m in visual_rows[eidx]:
                                if "line" in m:
                                    bbox = m["line"].get("bbox", [0, 0, 0, 0])
                                    eq_x_min = min(eq_x_min, bbox[0])
                                    eq_y_min = min(eq_y_min, bbox[1])
                                    eq_x_max = max(eq_x_max, bbox[2])
                                    eq_y_max = max(eq_y_max, bbox[3])
                    
                    eq_rect = fitz.Rect(eq_x_min - 5, eq_y_min - 5, eq_x_max + 5, eq_y_max + 5)
                    detected_equations.append((temp_idx, eq_end, eq_rect))
                    temp_idx = eq_end
                    continue
            
            temp_idx += 1
        
        # Build lookup: start_row_idx -> (end_row_idx, rect)
        equation_lookup: Dict[int, Tuple[int, fitz.Rect]] = {
            start: (end, rect) for start, end, rect in detected_equations
        }
        
        # Set of all row indices that are PART of an equation (for skipping)
        equation_row_set: set = set()
        for start, end, rect in detected_equations:
            for i in range(start, end):
                equation_row_set.add(i)
        
        # OCR: Batch process all equations once
        precomputed_equation_text: Dict[int, str] = {}  # start_idx -> extracted text
        
        if use_ocr and detected_equations:
            all_rects = [eq[2] for eq in detected_equations]
            ocr_results = self._batch_ocr_equations(page, all_rects)
            
            for i, (start_idx, end_idx, rect) in enumerate(detected_equations):
                if i in ocr_results:
                    precomputed_equation_text[start_idx] = f"$$\n{ocr_results[i]}\n$$"
                # else:
                #     # OCR failed - fallback to heuristic
                #     precomputed_equation_text[start_idx] = self._extract_equation_from_rect(page, rect, link_map)
        
        # Heuristic: Pre-compute all equations too (for consistency)
        if not use_ocr:
            for start_idx, end_idx, rect in detected_equations:
                precomputed_equation_text[start_idx] = self._extract_equation_from_rect(page, rect, link_map)
        
        # ---------------------------------------------------------------------
        # MAIN PROCESSING LOOP
        # ---------------------------------------------------------------------
        row_idx = 0
        while row_idx < len(visual_rows):
            row = visual_rows[row_idx]
            eq_row = equation_rows[row_idx]
            
            # Skip rows that are INSIDE an equation (not the start row)
            if row_idx in equation_row_set and row_idx not in equation_lookup:
                row_idx += 1
                continue
            
            # Insert any pending tables
            row_y = row[0]["y_center"]
            insert_pending_tables(row_y)
            
            # Check for large vertical gap (paragraph break indicator)
            if prev_row_y is not None and not prev_row_was_code:
                y_gap = row_y - prev_row_y
                if y_gap > 18 and parts and not parts[-1].endswith('\n\n'):
                    parts.append("\n")
                    pending_hyphen = False
            
            # Determine if this row is code (from Phase 3)
            row_is_code = any(m["is_code"] for m in row)
            row_is_table = any(m.get("is_table") for m in row)
            
            # ===== EQUATION HANDLING (unified for both modes) =====
            if not row_is_code and not row_is_table and row_idx in equation_lookup:
                eq_end_idx, eq_rect = equation_lookup[row_idx]
                
                # Collect all spans from this row
                all_spans = []
                for m in row:
                    all_spans.extend(m["line"].get("spans", []))
                all_spans.sort(key=lambda sp: sp.get("bbox", [0])[0])
                
                # Find spans BEFORE the equation (to the left of eq_rect)
                before_spans = [sp for sp in all_spans if sp["bbox"][2] < eq_rect.x0 - 5]
                
                # Output prose BEFORE equation
                if before_spans:
                    before_text = " ".join(sp.get("text", "") for sp in before_spans).strip()
                    if before_text:
                        parts.append(before_text)
                        parts.append(" ")
                
                # Output the equation
                equation_text = precomputed_equation_text.get(row_idx, "")
                if equation_text.strip():
                    parts.append(equation_text)
                    parts.append("\n\n")
                
                # Check last row of equation for spans AFTER the equation
                if eq_end_idx > 0 and eq_end_idx - 1 < len(visual_rows):
                    last_eq_row = visual_rows[eq_end_idx - 1]
                    last_spans = []
                    for m in last_eq_row:
                        last_spans.extend(m["line"].get("spans", []))
                    
                    # Find spans AFTER the equation (to the right of eq_rect)
                    after_spans = [sp for sp in last_spans if sp["bbox"][0] > eq_rect.x1 + 5]
                    
                    if after_spans:
                        after_spans.sort(key=lambda sp: sp["bbox"][0])
                        after_text = " ".join(sp.get("text", "") for sp in after_spans).strip()
                        if after_text:
                            parts.append(after_text)
                            parts.append(" ")
                
                row_idx = eq_end_idx
                prev_row_y = row_y
                prev_row_was_code = False
                continue
            
            # Get code region x0 for this row (for spacing calculation)
            row_code_region_x0 = None
            for m in row:
                if m.get("code_region_x0") is not None:
                    row_code_region_x0 = m["code_region_x0"]
                    break
            
            # Collect all spans from all lines in this row
            all_spans = []
            for m in row:
                all_spans.extend(m["line"].get("spans", []))
            all_spans.sort(key=lambda sp: sp.get("bbox", [0])[0])
            
            if not all_spans:
                prev_row_y = row_y
                row_idx += 1
                continue
            
            # Get raw text for this row
            raw_text = "".join(sp.get("text", "") for sp in all_spans).strip()
            
            # Skip headers/footers
            y0 = min(sp["bbox"][1] for sp in all_spans)
            y1 = max(sp["bbox"][3] for sp in all_spans)
            
            if self._is_header_footer(y0, y1, raw_text, page_height):
                prev_row_y = row_y
                row_idx += 1
                continue
            
            # Collect span info for font analysis
            for sp in all_spans:
                t = sp.get("text", "")
                if t:
                    spans_info.append((
                        float(sp.get("size", 0)),
                        sp.get("font", ""),
                        t
                    ))
            
            # Calculate indent for this row
            row_x0 = min(sp["bbox"][0] for sp in all_spans)
            
            # Calculate character width from this row's spans for accurate indent
            row_char_widths = []
            for sp in all_spans:
                t = sp.get("text", "")
                if len(t) > 0:
                    sp_width = sp["bbox"][2] - sp["bbox"][0]
                    cw = sp_width / len(t)
                    if 3.0 < cw < 15.0:
                        row_char_widths.append(cw)
            
            if row_char_widths:
                row_char_width = sum(row_char_widths) / len(row_char_widths)
            else:
                row_char_width = 6.0  # Default fallback
            
            # Calculate indent in character units
            row_indent = int(round((row_x0 - base_margin) / row_char_width))
            row_indent = max(0, row_indent)
            
            # ===== Handle pending hyphenation from previous row =====
            first_char = raw_text[0] if raw_text else ''
            is_word_continuation = first_char.islower()
            
            if pending_hyphen and is_word_continuation:
                # Join hyphenated word
                # Step 1: Remove trailing separators (newlines, code fences)
                while parts:
                    last = parts[-1]
                    if last == '\n':
                        parts.pop()
                    elif last.strip() in ('```', '```\n', '```\n\n', '\n```\n'):
                        parts.pop()
                        in_code_block = False
                    else:
                        break
                
                # Step 2: The last part should have the hyphenated word - remove hyphen
                if parts:
                    last_part = parts[-1]
                    # Strip trailing whitespace/newlines, then remove hyphen
                    cleaned = last_part.rstrip()
                    if cleaned.endswith('-'):
                        # Remove the hyphen
                        parts[-1] = cleaned[:-1]
                    else:
                        # Just remove trailing whitespace
                        parts[-1] = cleaned
                
                # Step 3: Handle code block transition if needed
                if prev_row_was_code and not row_is_code:
                    # We were in code, now in prose - but joining hyphenated word
                    # Don't add fence here, the word will join naturally
                    in_code_block = False
                
                # Step 4: Append the continuation text DIRECTLY (no space!)
                # Strip leading whitespace from the continuation
                if row_is_code or row_is_table:
                    ref_x0 = row_code_region_x0 if row_code_region_x0 is not None else base_margin
                    continuation_text = self._build_code_line_text(all_spans, ref_x0, link_map)
                    parts.append(continuation_text.lstrip())  # Remove leading space
                    parts.append("\n")
                else:
                    # For prose, build text and strip leading space
                    text_parts = []
                    prev_span_end_x = None
                    char_widths = [sp["bbox"][2] - sp["bbox"][0] / max(len(sp.get("text", " ")), 1) 
                                   for sp in all_spans if sp.get("text")]
                    char_width = sum(char_widths) / len(char_widths) if char_widths else 6.0
                    
                    for sp in all_spans:
                        t = sp.get("text", "")
                        if not t:
                            continue
                        sp_x0 = sp["bbox"][0]
                        sp_x1 = sp["bbox"][2]
                        if prev_span_end_x is not None:
                            gap = sp_x0 - prev_span_end_x
                            if gap > char_width * 1.5:
                                text_parts.append(' ' * max(1, int(round(gap / char_width))))
                            elif gap > char_width * 0.3:
                                text_parts.append(' ')
                        span_bbox = sp.get("bbox")
                        link_uri = self._get_link_for_span(span_bbox, link_map) if span_bbox else None
                        if link_uri:
                            text_parts.append(f"{t}({link_uri})")
                        else:
                            text_parts.append(t)
                        prev_span_end_x = sp_x1
                    
                    continuation_text = "".join(text_parts).lstrip()  # Remove leading space!
                    parts.append(continuation_text)
                    
                    # Determine if we need newline
                    stripped = continuation_text.strip()
                    if stripped.endswith('.') or stripped.endswith(':'):
                        parts.append("\n")
                    else:
                        parts.append(" ")
                
                pending_hyphen = False
                prev_row_was_code = row_is_code
                prev_row_y = row_y
                row_idx += 1
                continue  # Skip the normal text building below
            
            else:
                # Normal code block fence management (no pending hyphen)
                if row_is_code and not in_code_block:
                    parts.append("\n```\n")
                    in_code_block = True
                elif not row_is_code and in_code_block:
                    parts.append("```\n\n")
                    in_code_block = False
                
                pending_hyphen = False
            
            # ===== Build row text =====
            if row_is_code or row_is_table:
                # Use column-aligned code reconstruction
                ref_x0 = row_code_region_x0 if row_code_region_x0 is not None else base_margin
                code_line = self._build_code_line_text(all_spans, ref_x0, link_map)
                parts.append(code_line)
                parts.append("\n")
                
                # Check if this line ends with hyphen (for word continuation)
                stripped_code = code_line.rstrip().rstrip('\n')
                if stripped_code.endswith('-') and len(stripped_code) > 1:
                    # Only set pending_hyphen for likely word breaks, not operators
                    # Check: last word before hyphen should be alphabetic
                    words = stripped_code[:-1].split()
                    if words:
                        last_word = words[-1]
                        if last_word and last_word[-1].isalpha() and not stripped_code.endswith('--'):
                            pending_hyphen = True
            
            else:
                # Regular text handling with spacing between spans
                text_parts = []
                prev_span_end_x = None
                
                # Calculate character width for spacing
                char_widths = []
                for sp in all_spans:
                    t = sp.get("text", "")
                    if len(t) > 0:
                        sp_width = sp["bbox"][2] - sp["bbox"][0]
                        cw = sp_width / len(t)
                        if 3.0 < cw < 20.0:
                            char_widths.append(cw)
                
                if char_widths:
                    char_width = sum(char_widths) / len(char_widths)
                else:
                    char_width = 6.0
                
                # Calculate leading indent from first span position
                first_span_x0 = all_spans[0]["bbox"][0] if all_spans else base_margin
                leading_indent_pixels = first_span_x0 - base_margin
                leading_indent_chars = int(round(leading_indent_pixels / char_width))
                leading_indent_chars = max(0, min(leading_indent_chars, 40))  # Cap at 40 spaces
                
                # Add leading indentation
                if leading_indent_chars > 0:
                    text_parts.append(' ' * leading_indent_chars)
                
                for sp in all_spans:
                    t = sp.get("text", "")
                    if not t:
                        continue
                    
                    sp_x0 = sp["bbox"][0]
                    sp_x1 = sp["bbox"][2]
                    
                    # Calculate gap from previous span (not from margin for subsequent spans)
                    if prev_span_end_x is not None:
                        gap = sp_x0 - prev_span_end_x
                        
                        if gap > char_width * 1.5:
                            num_spaces = int(round(gap / char_width))
                            num_spaces = max(1, num_spaces)
                            # Detect merged lines: two logically separate elements at the same y-coordinate in the PDF
                            _callable_start = re.match(r'^[A-Za-z_]\w*\((?!https?://)', t.strip())
                            if (not row_is_code
                                    and not row_is_table
                                    and num_spaces >= 5
                                    and _callable_start):
                                # Split: the right-side callable belongs on its own line.
                                # Re-use the leading indent already computed for this row.
                                indent_str = ' ' * leading_indent_chars
                                text_parts.append('\n' + indent_str)
                            else:
                                text_parts.append(' ' * num_spaces)
                        elif gap > char_width * 0.3:
                            text_parts.append(' ')
                    
                    # Check for hyperlink
                    span_bbox = sp.get("bbox")
                    link_uri = self._get_link_for_span(span_bbox, link_map) if span_bbox else None
                    
                    if link_uri:
                        text_parts.append(f"{t}({link_uri})")
                    else:
                        text_parts.append(t)
                    
                    prev_span_end_x = sp_x1
                
                text_str = "".join(text_parts)
                
                # This handles multi-span callables in separate PDF spans) that the per-span check cannot catch
                if not row_is_code and not row_is_table:
                    _MERGED_SIG_PAT = re.compile(
                        r'^([^(\n]+?)\s{5,}([a-z_]\w*\((?!https?://))',
                        re.MULTILINE
                    )
                    def _split_merged_sig(m: re.Match) -> str:
                        return (
                            f"{m.group(1).rstrip()}"
                            f"\n{' ' * leading_indent_chars}{m.group(2)}"
                        )
                    text_str = _MERGED_SIG_PAT.sub(_split_merged_sig, text_str)
                
                stripped = text_str.strip().rstrip('\n')
                
                if not stripped:
                    prev_row_y = row_y
                    prev_indent = row_indent
                    prev_row_was_code = row_is_code
                    row_idx += 1
                    continue
                
                # Determine line characteristics
                row_width = max(sp["bbox"][2] for sp in all_spans) - row_x0
                is_short = row_width < short_line_thresh
                ends_sentence = stripped[-1] in {'.', '!', '?', ':'} if stripped else False
                ends_hyphen = stripped.endswith('-') and len(stripped) > 1
                is_bullet = self._is_bullet_or_list_item(stripped)
                
                # 1. BULLET/LIST ITEMS
                if is_bullet:
                    parts.append(text_str)
                    parts.append("\n")
                
                # 2. SIGNIFICANT INDENTATION CHANGE (new paragraph with indent)
                elif row_indent > prev_indent + 2:
                    parts.append("\n")
                    parts.append(text_str)
                    parts.append("\n")
                
                # 3. SHORT LINE or SENTENCE END
                elif is_short or ends_sentence:
                    parts.append(text_str)
                    parts.append("\n")
                
                # 4. HYPHENATED LINE END
                elif ends_hyphen:
                    # Check if this looks like a real word hyphenation (not an operator)
                    # Real hyphenation: ends with letter + hyphen (e.g., "homoge-")
                    # Not hyphenation: operators like "--", "-=", "self-"
                    word_before_hyphen = stripped[:-1].split()[-1] if stripped[:-1].split() else ""
                    is_real_hyphenation = (
                        len(word_before_hyphen) >= 2 and
                        word_before_hyphen[-1].isalpha() and
                        not stripped.endswith('--') and
                        not stripped.endswith('-=')
                    )
                    
                    if is_real_hyphenation:
                        # Remove hyphen and set flag for joining
                        parts.append(text_str[:-1])
                        pending_hyphen = True
                        # No newline - next line joins directly
                    else:
                        # Keep the hyphen (it's an operator or special case)
                        parts.append(text_str)
                        parts.append("\n")
                
                # 5. REGULAR PROSE (continuation)
                else:
                    # Normal prose flow - join with space
                    parts.append(text_str)
                    parts.append(" ")
            
            prev_row_was_code = row_is_code
            prev_indent = row_indent
            prev_row_y = row_y
            row_idx += 1
        
        # =====================================================================
        # CLEANUP
        # =====================================================================
        
        # Close unclosed code block
        if in_code_block:
            parts.append("```\n")
        
        # Insert remaining tables
        for tbl in table_entries:
            if not tbl['inserted']:
                parts.append("\n")
                parts.append(tbl['text'])
                parts.append("\n\n")
        
        raw = "".join(parts)
        raw = re.sub(r'\n{4,}', '\n\n\n', raw)
        
        # ── Page-level post-process: split merged API signature lines ──────────
        _MERGED_SIG_RE = re.compile(
            r'^([^(\n]+?)\s{5,}([a-z_]\w*\((?!https?://))',
            re.MULTILINE,
        )
        raw = _MERGED_SIG_RE.sub(
            lambda m: f"{m.group(1).rstrip()}\n     {m.group(2)}",
            raw,
        )
        # ─────────────────────────────────
        
        # =====================================================================
        # JOIN MULTI-LINE API SIGNATURES
        # =====================================================================
        signature_joiner = SignatureJoiner(spans_info)
        raw = signature_joiner.join(raw)
        
        return raw, spans_info

    
    def _font_size_levels(self, spans: List[Tuple], max_levels: int = 5) -> List[float]:
        """
        Estimate font size thresholds for heading levels using quantiles over unique sizes.
        Returns sorted unique sizes (descending).
        """
        # Handle both old format (size, text) and new format (size, font, text)
        sizes = set()
        for item in spans:
            size = item[0]
            sizes.add(round(size, 1))
        
        uniq = sorted(sizes, reverse=True)
        # cap to max_levels; PDFs often have few discrete sizes
        return uniq[:max_levels]

    
    def _collect_pages(self) -> Tuple[List[str], List[str], List[List[Tuple[float, str]]]]:
        """Collects page raw text, normalized text, and spans for font clustering."""
        page_raw, page_norm, page_spans = [], [], []
        for i in range(self.doc.page_count):
            raw, spans = self._extract_page_text(i)
            page_raw.append(raw)
            page_norm.append(self._normalize(raw))
            page_spans.append(spans)
        return page_raw, page_norm, page_spans

    def _outline(self) -> List[Tuple[int, str, int]]:
        """Returns a list of (level, title, page) for the outline of the document."""
        try:
            toc = self.doc.get_toc()
            # toc: [[level, title, page], ...] with 1-based page
            return [(lvl, title, max(0, pg - 1)) for (lvl, title, pg, *_) in toc]
        except Exception:
            return []

    def _find_section_by_title(self, titles: List[str], page_norm: List[str]) -> Optional[int]:
        needles = [t.lower() for t in titles]
        for i, txt in enumerate(page_norm):
            head = "\n".join(txt.splitlines()[:60]).lower()
            if any(n in head for n in needles):
                return i
        return None
    
    def _detect_toc_page_range(self, page_norm: List[str]) -> Tuple[int, int]:
        """
        Detect the page range occupied by the Table of Contents (TOC) using structural heuristics.
        
        TOC pages typically have:
          - High density of lines ending with page numbers
          - Low prose ratio (short lines, many dots/dashes)
          - Patterns like "...23" or "  45"
        
        Args:
            page_norm: A list of normalized text content for initial pages of the document.
        
        Returns:
            (start_page, end_page) where end is exclusive. Returns (0, 0) if no TOC detected.
        """
        
        # Patterns for TOC-like content, including varied dot/space leaders and potential Roman numerals
        toc_patterns = [
            re.compile(r'^\s*[\w\s\.]+\s+\d+\s*$', re.MULTILINE), # "Title   10" or "Title. 10" (basic)
            re.compile(r'([\w\s\.]+?)\s*\.{2,}\s*(\d+)\s*$', re.MULTILINE), # "Title ....... 10" (dots, capture group for title and page)
            re.compile(r'([\w\s\.]+?)\s{3,}(\d+)\s*$', re.MULTILINE), # "Title    10" (spaces, capture group for title and page)
            re.compile(r'^\s*([ivxlcdm]+)\s*$', re.MULTILINE | re.IGNORECASE), # Isolated Roman numerals (for detecting actual page numbers)
            # Dot leaders with Arabic or Roman numerals (most common)
            re.compile(r'^\s*([a-zA-Z0-9\s\.\-_]+?)\s*\.{2,}\s*(\d+|[ivxlcdm]+)\s*$', re.MULTILINE | re.IGNORECASE),
            # Space leaders with Arabic or Roman numerals (less common but exists)
            re.compile(r'^\s*([a-zA-Z0-9\s\.\-_]+?)\s{3,}(\d+|[ivxlcdm]+)\s*$', re.MULTILINE | re.IGNORECASE),
            # Simple lines with only a title and a page number at the very end
            re.compile(r'^\s*[\w\s\.\-_]+\s+\d+\s*$', re.MULTILINE),
        ]
        
        
        toc_scores = []
        
        # Score first 20 pages for TOC-like structure
        for i in range(min(20, len(page_norm))):
            text = page_norm[i]
            lines = text.splitlines()
            if not lines:
                toc_scores.append(0.0)
                continue
            
            total_matches = 0
            for pattern in toc_patterns:
                # Count matches for each pattern
                total_matches += len(pattern.findall(text))
            
            # Normalize by line count to get a density score
            score = total_matches / max(1, len(lines))
            toc_scores.append(score)
        
        # Identify potential TOC pages based on high scores
        if not toc_scores or max(toc_scores) < 0.2: # Minimum score to consider as TOC
            return 0, 0

        # Find a contiguous block of high-scoring pages
        toc_start = -1
        toc_end = -1
        
        # Look for the first page with a high score
        for i, score in enumerate(toc_scores):
            if score > 0.3: # Threshold for starting a TOC block
                toc_start = i
                break
        
        if toc_start == -1:
            return 0, 0

        # Extend to include all contiguous high-scoring pages
        for i in range(toc_start, len(toc_scores)):
            if toc_scores[i] > 0.1: # Lower threshold to continue a TOC block
                toc_end = i + 1
            else:
                break
                
        # Smallest TOC must be at least 2 pages
        if toc_end - toc_start < 1:
            return 0, 0

        return toc_start, toc_end
    

    def _parse_visual_toc(self, page_raw: List[str], page_norm: List[str]) -> List[Tuple[int, str, int]]:
        """
        Attempt to parse a visual table of contents from the first few pages.
        
        Looks for patterns like:
          - "Introduction ........ 5"
          - "API Reference      23"
          - "Getting Started - 10"
        
        Returns:
            List of (level, title, page) tuples similar to PDF outline format.
        """
        visual_toc: List[Tuple[int, str, int]] = []
        
        # Patterns for different TOC formats
        toc_patterns = [
            # Standard: "Title .... 23"
            re.compile(r'([A-Z][A-Za-z\s\-]+?)\s*\.{2,}\s*(\d+)', re.MULTILINE),
            # "ClassName ......123" (capitalized, standalone)
            re.compile(r'\b([A-Z][A-Za-z]{2,})\s*\.{2,}\s*(\d+)', re.MULTILINE),
            # Spaced: "Title     23"
            re.compile(r'([A-Z][A-Za-z\s\-]+?)\s{5,}(\d+)', re.MULTILINE),
            # Generic dotted: "some.name.....123"
            re.compile(r'([\w\.]+)\s*\.{2,}\s*(\d+)', re.MULTILINE),
            # Dashed: "Title --- 23"
            re.compile(r'([A-Z][A-Za-z\s\-]+?)\s*-{2,}\s*(\d+)', re.MULTILINE),
        ]
        
        # Scan first 10 pages
        for page_idx in range(min(10, len(page_norm))):
            text = page_norm[page_idx]
            
            # Skip if page doesn't look like a TOC (no "index" or "contents" mention)
            text_lower = text.lower()
            if not any(kw in text_lower for kw in ['index', 'contents', 'table of']):
                continue
            
            for pattern in toc_patterns:
                for match in pattern.finditer(text):
                    title = match.group(1).strip()
                    try:
                        page_num = int(match.group(2))
                    except (ValueError, IndexError):
                        continue
                    
                    # Skip obviously wrong entries (page beyond doc bounds)
                    if page_num >= self.doc.page_count or page_num < 0:
                        continue
                    
                    # Normalize title
                    title_clean = title.strip().rstrip('.')
                    
                    # Level assignment based solely on name hierarchy
                    # A title is a child (higher level number) only if it extends a previous title's name
                    level = 1  # Default: top-level
                    
                    # Check only the immediately preceding entry
                    if visual_toc:  # If there are previous entries
                        prev_lvl, prev_title, prev_pg = visual_toc[-1]
                        prev_clean = prev_title.strip().rstrip('.')
                        
                        # Check if current title extends the previous title
                        if '.' in title_clean and title_clean.startswith(prev_clean + '.'):
                            # This is a child of the previous entry
                            level = prev_lvl + 1
                    
                    visual_toc.append((level, title_clean, page_num))
        
        # Deduplicate by (title, page) and sort by page number
        seen = set()
        deduped = []
        for lvl, title, pg in visual_toc:
            # Normalize title: strip and remove trailing dots
            normalized_title = title.strip().rstrip('.')
            key = (normalized_title, pg)
            if key not in seen:
                seen.add(key)
                # Keep the version with fewer dots (cleaner title)
                clean_title = normalized_title
                deduped.append((lvl, clean_title, pg))
        
        # Sort by page number to ensure correct ordering
        deduped.sort(key=lambda x: x[2])
        
        return deduped
    
    
    def _detect_header_footer_zones(self, start_page: int = 0) -> Tuple[float, float, List[re.Pattern], List[re.Pattern]]:
        """
        Detect header/footer zones using y-coordinates and validate with text frequency.
        Uses normalization to handle dynamic content (e.g., page numbers).
        
        Args:
            start_page: Start sampling after this page.
        
        Returns:
            (header_y_max, footer_y_min, header_patterns, footer_patterns)
        """
        
        # Configuration
        header_ratio = 0.10  # Top 10%
        footer_ratio = 0.88  # Bottom 12% (was 0.90 - expanded to catch more)
        min_frequency = 0.08  # Text must appear on at least 8% of sampled pages
        
        # Sample pages across the document (e.g., every 5th page) to catch global patterns
        # but respect the start_page (skip TOC/preamble if requested)
        step = 1#max(1, self.doc.page_count // 50) # Sample ~50 pages max
        sample_indices = range(start_page, self.doc.page_count, step)
        
        top_candidates = []
        bottom_candidates = []
        
        # Helper to normalize text for frequency counting (mask numbers)
        def normalize_for_freq(text: str) -> str:
            s = text.strip()
            # Normalize whitespace variations (spaces, tabs, multiple spaces)
            s = re.sub(r'\s+', ' ', s)
            # Mask section numbers
            s = re.sub(r'^(\d+\.)+\d*\s+', 'SECTION_NUM ', s)
            # Mask trailing page numbers (with various separators)
            s = re.sub(r'[\s\-–—\.]+\d+\s*$', ' PAGE_NUM', s)
            # Mask standalone page numbers
            if re.match(r'^\d+$', s.strip()): s = 'PAGE_NUM'
            # Section title masking
            if s.startswith('SECTION_NUM '): s = 'SECTION_NUM TEXT'
            # Replace digit sequences
            s = re.sub(r'\d+', '#', s)
            # Collapse whitespace again
            s = re.sub(r'\s+', ' ', s)
            # Mask Roman numerals
            s = re.sub(r'\b[ivxlcdm]+\b', '#', s, flags=re.IGNORECASE)
            return s.strip()

        for pidx in sample_indices:
            try:
                page = self.doc.load_page(pidx)
            except Exception:
                continue
                
            # blocks = page.get_text("blocks") # (x0, y0, x1, y1, text, block_no, block_type)
            blocks = page.get_text("dict")["blocks"]
            page_height = page.rect.height
            
            for b in blocks:
                if "lines" not in b: continue
                
                # Get block-level y coordinates for multi-line footer detection
                block_y0 = b["bbox"][1]
                block_y1 = b["bbox"][3]
                
                # Check if entire block is in footer zone
                block_in_footer = block_y0 > page_height * footer_ratio
                
                for line in b["lines"]:
                    # Use line bbox, not block bbox
                    y0, y1 = line["bbox"][1], line["bbox"][3]
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    
                    if not text: continue
                    
                    # Header detection
                    if y1 < page_height * header_ratio:
                        top_candidates.append((y1, normalize_for_freq(text)))
                    
                    # Footer detection
                    # Check line position OR if block is in footer zone
                    elif y0 > page_height * footer_ratio or block_in_footer:
                        # Multiple footer patterns
                        if re.search(r'[\s\-–—\.]+\d+\s*$', text):  # "Title ... 123" or "Title - 123"
                            norm_text = "DYNAMIC_PAGE_FOOTER"
                        elif re.match(r'^\d+\s*$', text):  # Standalone page number
                            norm_text = "PAGE_NUM_ONLY"
                        elif re.match(r'^[ivxlcdm]+\s*$', text, re.IGNORECASE):  # Roman numeral
                            norm_text = "ROMAN_PAGE_NUM"
                        elif len(text) < 50:  # Short text in footer zone
                            norm_text = normalize_for_freq(text)
                        else:
                            norm_text = normalize_for_freq(text)
                        
                        bottom_candidates.append((y0, norm_text))

        # Analyze frequencies
        num_samples = len(sample_indices)
        if num_samples == 0:
            return 0.0, float('inf'), [], []

        top_counts = Counter(t[1] for t in top_candidates)
        bottom_counts = Counter(t[1] for t in bottom_candidates)
        
        # Filter candidates that appear frequently enough
        valid_top_texts = {txt for txt, count in top_counts.items() if count / num_samples >= min_frequency}
        valid_bottom_texts = {txt for txt, count in bottom_counts.items() if count / num_samples >= min_frequency}
        
        # Add common footer patterns that might not reach frequency threshold
        common_footer_patterns = {'PAGE_NUM_ONLY', 'ROMAN_PAGE_NUM', 'DYNAMIC_PAGE_FOOTER'}
        for pattern in common_footer_patterns:
            if pattern in bottom_counts and bottom_counts[pattern] >= 3:
                valid_bottom_texts.add(pattern)
        
        # Determine cut-off Y coordinates based on valid candidates
        header_y = 0.0
        if valid_top_texts:
            # Max y1 of valid header blocks
            header_y = max((y for y, txt in top_candidates if txt in valid_top_texts), default=0.0)
            
        footer_y = float('inf')
        if valid_bottom_texts:
            # Min y0 of valid footer blocks
            footer_y = min((y for y, txt in bottom_candidates if txt in valid_bottom_texts), default=float('inf'))

        # Build regex patterns for filtering
        # Escape special chars but allow the '#' placeholder to match digits
        def build_patterns(texts):
            patterns = []
            for txt in texts:
                # Escape regex chars, then replace our '#' placeholder with \d+
                escaped = re.escape(txt)
                pattern_str = escaped
                # SECTION_NUM -> matches "4.2.", "10.", "1.1.1"
                pattern_str = pattern_str.replace('SECTION_NUM', r'(\d+\.)+\d*')
                # Handle PAGE_NUM -> matches " ... 181"
                pattern_str = pattern_str.replace('PAGE_NUM', r'\s+\d+')
                # Handle DYNAMIC_PAGE_FOOTER -> match anything ending in digits
                pattern_str = pattern_str.replace('DYNAMIC_PAGE_FOOTER', r'.*?[\s\-–—\.]+\d+')
                # Handle PAGE_NUM_ONLY -> matches standalone page number
                pattern_str = pattern_str.replace('PAGE_NUM_ONLY', r'\d+')
                # Handle ROMAN_PAGE_NUM -> matches Roman numerals
                pattern_str = pattern_str.replace('ROMAN_PAGE_NUM', r'[ivxlcdm]+')
                # TEXT -> matches the variable title content for any sequence of words/spaces/punctuation (greedy match until end or page number)
                pattern_str = pattern_str.replace(r'TEXT', r'.*?')
                # Allow '#' to match digits OR Roman numerals OR short words (like "Page")
                pattern_str = escaped.replace(r'\#', r'(\d+|[ivxlcdm]+)') #r'(\d+|[ivxlcdm]+|\w{1,4})') 
                # Allow some flexibility for whitespace
                pattern_str = pattern_str.replace(r'\ ', r'\s+')
                # Allow arbitrary content at start/end if it's short (e.g. "Page 1" vs "1")
                patterns.append(re.compile(f"^{pattern_str}$", re.IGNORECASE))
            return patterns

        header_patterns = build_patterns(valid_top_texts)
        footer_patterns = build_patterns(valid_bottom_texts)
        
        print(f"Detected Header Y < {header_y:.1f}, Patterns: {valid_top_texts}")
        print(f"Detected Footer Y > {footer_y:.1f}, Patterns: {valid_bottom_texts}")
        
        return header_y, footer_y, header_patterns, footer_patterns

    
    def _is_header_footer(self, y0: float, y1: float, text: str, page_height: float) -> bool:
        """Check if a line is a header or footer based on position and patterns."""
        text_stripped = text.strip()
        if not text_stripped:
            return False
        
        # Zone definitions - EXPANDED to catch more headers/footers
        header_zone = page_height * 0.12      # Top 12%
        footer_zone = page_height * 0.85      # Bottom 15% (was 0.90)
        extreme_header = page_height * 0.06
        extreme_footer = page_height * 0.92   # (was 0.94)
        
        # =========================================================================
        # PATTERN-BASED DETECTION (checked in broader zones)
        # =========================================================================
        
        # Check if in header/footer zones (expanded)
        in_header_zone = y1 < header_zone
        in_footer_zone = y0 > footer_zone
        
        # For strong patterns, check in even broader zone (bottom 20%)
        in_broad_footer_zone = y0 > page_height * 0.80
        
        # Strong footer patterns - check in broad zone
        if in_broad_footer_zone:
            # 1. Page number at START followed by section/content
            #    Pattern: "850 1. Python API" or "1234 Chapter 5"
            if re.match(r'^\d{2,}\s+\d*\.?\s*[A-Za-z]', text_stripped):
                return True
            
            # 2. Section number + title + page number at END
            #    Pattern: "1.5 Methods 234" or "API Reference 850"
            if re.search(r'[A-Za-z]\s+\d{2,}$', text_stripped):
                return True
            
            # 3. Digits concatenated with text (PDF extraction artifact)
            if re.match(r'^\d{2,}[A-Za-z]', text_stripped):  # "1626Chapter"
                return True
            if re.search(r'[A-Za-z]\d{2,}$', text_stripped):  # "DataFrame1625"
                return True
            
            # 4. Standalone page numbers
            if re.match(r'^\d+$', text_stripped):
                return True
            
            # 5. Short text with page number pattern (likely running footer)
            if len(text_stripped) < 60 and re.search(r'\d{2,}', text_stripped):
                # Contains 2+ digit number and is short - likely a footer
                return True
        
        # Check in narrow zones for pattern matches
        if in_header_zone or in_footer_zone:
            # Check against detected repeating patterns
            normalized = re.sub(r'\d+', '#', text_stripped)
            if hasattr(self, '_header_patterns') and self._header_patterns:
                if any(p.match(normalized) for p in self._header_patterns):
                    return True
            if hasattr(self, '_footer_patterns') and self._footer_patterns:
                if any(p.match(normalized) for p in self._footer_patterns):
                    return True
        
        # =========================================================================
        # EXTREME POSITION FALLBACK
        # =========================================================================
        if y1 < extreme_header and len(text_stripped) < 80:
            return True
        if y0 > extreme_footer and len(text_stripped) < 80:
            return True
        
        return False
    

    def sectionize(self) -> Tuple[List[Section], Dict[str, Section]]:
        """
        Build a flat list of sections (preorder) and a map by id.
        A simple, robust approach:
          - derive global font size levels from the whole doc
          - detect candidate headings per page as lines dominated by larger sizes
          - make sections between consecutive headings of same-or-higher level
        """
        
        # Quick first pass: detect TOC range (needs minimal page inspection)
        temp_pages = []
        for i in range(min(20, self.doc.page_count)):
            page = self.doc.load_page(i)
            temp_pages.append(self._normalize(page.get_text("text")))
        _, toc_end = self._detect_toc_page_range(temp_pages)
        
        # Detect header/footer zones (uses cached blocks)
        # self._header_y, self._footer_y, self._header_texts, self._footer_texts = self._detect_header_footer_zones(start_page=toc_end)
        self._header_y, self._footer_y, self._header_patterns, self._footer_patterns = self._detect_header_footer_zones(start_page=toc_end)
        
        # Now collect pages with filtering active
        page_raw, page_norm, page_spans = self._collect_pages()
        all_spans = [s for spans in page_spans for s in spans]
        size_levels = self._font_size_levels(all_spans)

        def line_looks_like_heading(line: str) -> bool:
            # heuristics: short-ish, no trailing punctuation; contains letters
            if len(line.strip()) > 160:
                return False
            if not re.search(r'[A-Za-z]', line):
                return False
            return True
        
        # Font-aware heading detection: scan all pages, validate with font sizes
        headings: List[Tuple[int, int, str, int, float]] = []
        
        for page_idx, spans in enumerate(page_spans):
            # Build a map of text fragments to their font sizes
            text_to_size: Dict[str, float] = {}
            for item in spans:
                # Handle both old format (size, text) and new format (size, font, text)
                size = item[0]
                txt = item[-1]  # text is always last
                key = txt.strip()[:50]  # use first 50 chars as key
                if key:
                    text_to_size[key] = max(text_to_size.get(key, 0.0), size)
            
            # Scan all lines on this page
            lines = page_norm[page_idx].splitlines()
            for line_idx, line in enumerate(lines):
                line_clean = line.strip()
                if not line_looks_like_heading(line_clean):
                    continue
                
                # Find the font size for this line
                key = line_clean[:50]
                line_size = text_to_size.get(key, 0.0)
                
                # Only consider it a heading if it's in the top font sizes
                if line_size == 0.0 or (size_levels and line_size < size_levels[-1]):
                    continue
                
                # Assign level based on font size rank
                lvl = len([s for s in size_levels if line_size < s]) + 1
                lvl = min(max(1, lvl), 5)
                
                headings.append((page_idx, line_idx * 100, line_clean, lvl, line_size))

        # if outline exists, prefer it for top-level anchors
        toc = self._outline()
        
        # If no embedded TOC, try parsing a visual TOC
        if not toc:
            toc = self._parse_visual_toc(page_raw, page_norm)
        
        if toc:
            # build top-level anchors and their spans
            top: List[Tuple[str, int, int, int]] = []  # (title, level, start_page, end_page)
            for idx, (lvl, title, pg) in enumerate(toc):
                # end at next same-or-higher level
                j = idx + 1
                while j < len(toc) and toc[j][0] > lvl:
                    j += 1
                end_pg = toc[j][2] if j < len(toc) else self.doc.page_count
                
                # Validate and fix inverted ranges
                if end_pg < pg:
                    end_pg = pg + 1  # Minimum 1-page section
                
                top.append((title, lvl, pg, end_pg))
                
            # Turn each top region into a section; substructure will be coarse (page-bounded)
            sections: List[Section] = []
            by_id: Dict[str, Section] = {}
            for k, (title, lvl, start, end) in enumerate(top):
                # Validate page range
                if end <= start:
                    end = start + 1
                
                raw = "\n".join(page_raw[start:end])
                norm = "\n".join(page_norm[start:end])
                sid = f"toc-{k}"
                sec = Section(
                    id=sid, title=title.strip(), level=lvl,
                    page_start=start, page_end=end, text_raw=raw, text_norm=norm, path=[title.strip()]
                )
                sections.append(sec)
                by_id[sid] = sec
            return sections, by_id

        # fallback: make coarse sections by detected headings
        headings.sort(key=lambda t: (t[0], t[1]))
        sections: List[Section] = []
        by_id: Dict[str, Section] = {}
        if not headings:
            raw = "\n".join(page_raw)
            norm = "\n".join(page_norm)
            sec = Section(id="doc-0", title="Document", level=1, page_start=0, page_end=self.doc.page_count,
                          text_raw=raw, text_norm=norm, path=["Document"])
            return [sec], {"doc-0": sec}

        # build ranges
        anchors = []
        for idx, heading_data in enumerate(headings):
            pg, off, title, lvl = heading_data[:4]  # Unpack first 4, ignore extras if present
            next_pg = headings[idx + 1][0] if idx + 1 < len(headings) else self.doc.page_count
            anchors.append((title, lvl, pg, next_pg))
        for k, (title, lvl, start, end) in enumerate(anchors):
            raw = "\n".join(page_raw[start:end])
            norm = "\n".join(page_norm[start:end])
            sid = f"h-{k}"
            sec = Section(
                id=sid, title=title.strip(), level=lvl,
                page_start=start, page_end=end,
                text_raw=raw, text_norm=norm, path=[title.strip()]
            )
            sections.append(sec)
            by_id[sid] = sec
        return sections, by_id
