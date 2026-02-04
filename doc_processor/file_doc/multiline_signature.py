import re
from dataclasses import dataclass


@dataclass
class SignatureLineInfo:
    """
    Metadata about a line for API signature detection and joining.
    
    This dataclass captures all relevant properties of a text line
    to determine if it's part of a multi-line API signature.
    """
    text: str                           # Original line text (with indentation)
    stripped: str                       # Stripped text (no leading/trailing whitespace)
    indent: int                         # Number of leading spaces
    fonts: set                          # Set of font names in this line
    font_sizes: list                    # List of font sizes in this line
    avg_font_size: float                # Average font size
    ends_with: str                      # Last non-whitespace character
    paren_balance: int                  # Net open parentheses: count('(') - count(')')
    bracket_balance: int                # Net open brackets: count('[') - count(']')
    has_equals: bool                    # Contains '=' (default values)
    has_comma: bool                     # Contains ',' (parameter separator)
    has_colon: bool                     # Contains ':' (type hints)
    

class SignatureJoiner:
    """
    Joins multi-line API signatures into single lines while preserving other content.
    
    This class identifies API signatures that span multiple lines in PDF documentation
    and joins them into single lines for easier parsing. It uses a combination of:
    
    1. Pattern matching for signature starts (qualified names followed by '(')
    2. Indentation analysis for continuation detection
    3. Character-based heuristics for line endings
    4. Font consistency checking
    5. Prose exclusion criteria
    
    Content that is preserved as-is:
        - Code blocks (between ``` fences)
        - List items (bullets, numbered)
        - REPL examples (>>> lines)
        - Regular prose paragraphs
        - Equations and other formatted content
    """
    
    # Characters that indicate a line continues to the next
    CONTINUATION_ENDINGS = {',', ':', '(', '[', '{', '=', '\\'}
    
    # Characters that can appear alone as positional/keyword markers
    PARAM_MARKERS = {'*', '/'}
    
    # Common prose starter words (lowercase)
    PROSE_STARTERS = {
        'the', 'a', 'an', 'this', 'that', 'these', 'those',
        'if', 'when', 'where', 'while', 'for', 'is', 'are',
        'it', 'they', 'we', 'you', 'can', 'will', 'should',
        'note', 'warning', 'example', 'see', 'returns', 'raises'
    }
    
    # Pattern for API signature start
    # Matches: name(, Name(, module.name(, class Name(, module.path.Name(
    SIGNATURE_PATTERN = re.compile(
        r'^(class\s+)?'                    # Optional 'class ' prefix
        r'([\w]+(?:\.[\w]+)*)'             # Qualified name: word or word.word.word
        r'\s*\('                           # Opening parenthesis
    )
    
    def __init__(self, spans_info: list):
        """
        Initialize the SignatureJoiner.
        
        Args:
            spans_info: List of (font_size, font_name, text) tuples from PDF extraction.
                        Used for font consistency analysis.
        """
        self.spans_info = spans_info
        self.font_map = self._build_font_map()
    
    def _build_font_map(self) -> dict:
        """
        Build a mapping from text snippets to their font information.
        
        Returns:
            Dict mapping stripped text -> {'sizes': [float], 'fonts': set}
        """
        font_map = {}
        for item in self.spans_info:
            if len(item) >= 3:
                size, font, text = item[0], item[1], item[2]
            else:
                # Fallback for old format (size, text)
                size, text = item[0], item[1]
                font = ""
            
            key = text.strip()
            if key:
                if key not in font_map:
                    font_map[key] = {'sizes': [], 'fonts': set()}
                font_map[key]['sizes'].append(size)
                if font:
                    font_map[key]['fonts'].add(font)
        
        return font_map
    
    def _get_line_fonts(self, line: str) -> tuple:
        """
        Get font information for a line by matching against spans_info.
        
        Args:
            line: The line text to look up.
            
        Returns:
            Tuple of (set of font names, list of font sizes, average font size)
        """
        stripped = line.strip()
        fonts = set()
        sizes = []
        
        # Try exact match first
        if stripped in self.font_map:
            info = self.font_map[stripped]
            return info['fonts'], info['sizes'], sum(info['sizes']) / len(info['sizes']) if info['sizes'] else 0.0
        
        # Try matching substrings (words/tokens in the line)
        for word in stripped.split():
            word_clean = word.strip('(),[]{}:;')
            if word_clean in self.font_map:
                info = self.font_map[word_clean]
                fonts.update(info['fonts'])
                sizes.extend(info['sizes'])
        
        avg_size = sum(sizes) / len(sizes) if sizes else 0.0
        return fonts, sizes, avg_size
    
    def _extract_line_info(self, line: str) -> SignatureLineInfo:
        """
        Extract all relevant metadata from a line.
        
        Args:
            line: The line text (may include leading whitespace).
            
        Returns:
            SignatureLineInfo with all properties populated.
        """
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        
        # Get font info
        fonts, font_sizes, avg_font_size = self._get_line_fonts(line)
        
        # Get ending character
        ends_with = stripped[-1] if stripped else ''
        
        # Calculate balances
        paren_balance = stripped.count('(') - stripped.count(')')
        bracket_balance = stripped.count('[') - stripped.count(']')
        
        return SignatureLineInfo(
            text=line,
            stripped=stripped,
            indent=indent,
            fonts=fonts,
            font_sizes=font_sizes,
            avg_font_size=avg_font_size,
            ends_with=ends_with,
            paren_balance=paren_balance,
            bracket_balance=bracket_balance,
            has_equals='=' in stripped,
            has_comma=',' in stripped,
            has_colon=':' in stripped,
        )
    
    def _is_blank(self, line: str) -> bool:
        """Check if line is blank."""
        return not line.strip()
    
    def _is_code_fence(self, line: str) -> bool:
        """Check if line is a code fence."""
        return line.strip().startswith('```')
    
    def _is_list_item(self, line: str) -> bool:
        """
        Check if line starts a list item.
        
        Matches:
            - Bullet points: -, *, •, ►, ▪
            - Numbered lists: 1., 1), (1)
            - Field lists: :param:, :returns:
        """
        stripped = line.strip()
        
        # Bullet points
        if re.match(r'^[-*•►▪]\s', stripped):
            return True
        
        # Numbered lists
        if re.match(r'^\d+[.\)]\s', stripped):
            return True
        if re.match(r'^\(\d+\)\s', stripped):
            return True
        
        # Field lists (Sphinx-style)
        if re.match(r'^:[a-z_]+:', stripped):
            return True
        
        return False
    
    def _is_repl_line(self, line: str) -> bool:
        """Check if line is a REPL prompt or continuation."""
        stripped = line.strip()
        return stripped.startswith('>>>') or (
            stripped.startswith('...') and not stripped.startswith('....')
        )
    
    def _is_signature_start(self, info: SignatureLineInfo) -> bool:
        """
        Check if a line looks like the start of an API signature.
        
        Criteria:
            1. Matches pattern: [class] qualified.name( or name(
            2. Does NOT look like prose (balanced parens + ends with '.')
        
        Args:
            info: SignatureLineInfo for the line.
            
        Returns:
            True if this looks like a signature start.
        """
        if not info.stripped:
            return False
        
        # Must contain '('
        if '(' not in info.stripped:
            return False
        
        # Must match signature pattern
        if not self.SIGNATURE_PATTERN.match(info.stripped):
            return False
        
        # Exclusion: Check if this is prose with a function call
        # Pattern: balanced parens AND ends with '.' after a word
        if self._looks_like_prose_with_call(info):
            return False
        
        return True
    
    def _looks_like_prose_with_call(self, info: SignatureLineInfo) -> bool:
        """
        Check if a line is prose that happens to contain a function call.
        
        Example: "Use torch.func(x) to process data." is prose, not a signature.
        
        Args:
            info: SignatureLineInfo for the line.
            
        Returns:
            True if this looks like prose containing a function call.
        """
        # If parens are not balanced, it's likely a multi-line signature
        if info.paren_balance != 0:
            return False
        
        # If doesn't end with '.', not prose
        if info.ends_with != '.':
            return False
        
        # Get the character before the final '.'
        before_dot = info.stripped[:-1].rstrip()
        if not before_dot:
            return False
        
        last_char = before_dot[-1]
        
        # If ends with digit before '.', could be version/float - not necessarily prose
        # But check context: "version 1.0." is prose, "atol=1e-05." might not be
        if last_char.isdigit():
            # If there's '=' before, likely a default value, not prose
            if '=' in info.stripped:
                return False
            # Otherwise, could be prose
            return True
        
        # If ends with ')' before '.', check if there's '=' (default value)
        if last_char == ')':
            if '=' in info.stripped:
                return False  # default=something.method()
            return True  # Likely prose: "Call func(x)."
        
        # If ends with a letter before '.', check if it's a qualified name continuation
        # "sklearn.utils." could continue on next line
        if last_char.isalpha():
            # Check if the text before '.' looks like a qualified name
            # Pattern: word.word.word.
            if re.search(r'[\w]+\.[\w]+\.$', info.stripped):
                return False  # Qualified name, might continue
            return True  # Likely prose ending with a word
        
        return False
    
    def _is_valid_continuation(self, 
                               info: SignatureLineInfo,
                               first_info: SignatureLineInfo,
                               accumulated_paren_balance: int
                               ) -> bool:
        """
        Check if a line is a valid continuation of a multi-line signature.
        
        Criteria:
            1. Must be indented more than the first line
            2. Must end with a continuation character OR close the signature
            3. Must not look like prose
            4. Font should be consistent (optional, lenient)
        
        Args:
            info: SignatureLineInfo for the potential continuation line.
            first_info: SignatureLineInfo for the signature's first line.
            accumulated_paren_balance: Running total of unclosed parens.
            
        Returns:
            True if this is a valid continuation.
        """
        # Must be indented more than first line
        if info.indent <= first_info.indent:
            return False
        
        # Empty line is not a continuation
        if not info.stripped:
            return False
        
        # Check for return arrow at start (→ Tensor or -> Tensor)
        if info.stripped.startswith('→') or info.stripped.startswith('->'):
            return True
        
        # Check ending character
        ends_with = info.ends_with
        
        # Standard continuation endings
        if ends_with in self.CONTINUATION_ENDINGS:
            if not self._is_prose_like(info):
                return True
        
        # Parameter markers (* or /) at end of line or followed by comma
        if ends_with in self.PARAM_MARKERS:
            return True
        if info.stripped in self.PARAM_MARKERS:
            return True
        if re.match(r'^[*/]\s*,', info.stripped):
            return True
        
        # Closes the signature with ')'
        if ends_with == ')':
            return True
        
        # Ends with a letter/digit - check if it's part of a qualified name
        # Example: "default=sklearn.utils." continues with "metadata_routing.UNCHANGED)"
        if ends_with == '.' and accumulated_paren_balance > 0:
            # Check if it looks like a qualified name continuation
            if re.search(r'[\w]+\.$', info.stripped):
                return True
        
        # Has parameter-like content and is indented (fallback)
        if (info.has_equals or info.has_comma) and info.indent > first_info.indent:
            if not self._is_prose_like(info):
                return True
        
        return False
    
    def _is_prose_like(self, info: SignatureLineInfo) -> bool:
        """
        Check if a line looks like prose rather than signature content.
        
        Criteria:
            1. Ends with '.' after a word (with exceptions)
            2. Multiple words without parameter indicators
            3. Starts with common prose words
        
        Args:
            info: SignatureLineInfo for the line.
            
        Returns:
            True if this looks like prose.
        """
        # Check ending with '.'
        if info.ends_with == '.':
            before_dot = info.stripped[:-1].rstrip()
            if before_dot:
                last_char = before_dot[-1]
                
                # Digit before '.' - could be number, check context
                if last_char.isdigit():
                    # If '=' present, likely default value
                    if info.has_equals:
                        return False
                
                # Letter before '.' - check if qualified name
                elif last_char.isalpha():
                    # Qualified name pattern: word.word.
                    if re.search(r'[\w]+\.[\w]+\.$', info.stripped):
                        return False  # Qualified name continuation
                    # Otherwise, likely prose
                    return True
                
                # ')' before '.' - function call ending
                elif last_char == ')':
                    if info.has_equals:
                        return False  # default=func().
                    return True  # Prose with function call
        
        # Multiple words without parameter indicators suggests prose
        words = info.stripped.split()
        if len(words) > 4 and not info.has_equals and not info.has_comma:
            # Check for prose starter words
            if words and words[0].lower() in self.PROSE_STARTERS:
                return True
        
        return False
    
    def _check_font_consistency(self, 
                                info: SignatureLineInfo, 
                                first_info: SignatureLineInfo,
                                signature_fonts: set
                                ) -> bool:
        """
        Check if a continuation line has consistent fonts with the signature.
        
        This is a lenient check - we allow continuation if:
            1. No font info available (can't verify)
            2. At least one common font with signature
            3. Font size is similar (within tolerance)
        
        Args:
            info: SignatureLineInfo for the continuation line.
            first_info: SignatureLineInfo for the first line.
            signature_fonts: Set of fonts seen in signature so far.
            
        Returns:
            True if fonts are consistent (or can't be verified).
        """
        # If no font info, allow (can't verify)
        if not info.fonts or not signature_fonts:
            return True
        
        # Check for common fonts
        common = info.fonts & signature_fonts
        if common:
            return True
        
        # Check font size similarity (allow 20% variation for default values)
        if info.avg_font_size > 0 and first_info.avg_font_size > 0:
            ratio = info.avg_font_size / first_info.avg_font_size
            if 0.8 <= ratio <= 1.2:
                return True
        
        # Be lenient - allow if we can't definitively reject
        return True
    
    def _should_skip_line(self, line: str, in_code_block: bool) -> tuple:
        """
        Check if a line should be skipped (not processed for signature joining).
        
        Args:
            line: The line text.
            in_code_block: Whether we're currently inside a code block.
            
        Returns:
            Tuple of (should_skip: bool, new_in_code_block: bool)
        """
        # Code fence toggles code block state
        if self._is_code_fence(line):
            return True, not in_code_block
        
        # Inside code block - skip
        if in_code_block:
            return True, in_code_block
        
        # Blank line
        if self._is_blank(line):
            return True, in_code_block
        
        # REPL line
        if self._is_repl_line(line):
            return True, in_code_block
        
        # List item
        if self._is_list_item(line):
            return True, in_code_block
        
        return False, in_code_block
    
    def _is_stop_condition(self, line: str, in_code_block: bool, first_info: SignatureLineInfo) -> bool:
        """
        Check if we should stop collecting continuation lines.
        
        Args:
            line: The potential continuation line.
            in_code_block: Whether we're in a code block.
            first_info: SignatureLineInfo for the signature's first line.
            
        Returns:
            True if we should stop.
        """
        # Code fence
        if self._is_code_fence(line):
            return True
        
        # Blank line
        if self._is_blank(line):
            return True
        
        # List item
        if self._is_list_item(line):
            return True
        
        # REPL line
        if self._is_repl_line(line):
            return True
        
        # New signature at same or lesser indent
        info = self._extract_line_info(line)
        if self._is_signature_start(info) and info.indent <= first_info.indent:
            return True
        
        return False
    
    def join(self, text: str) -> str:
        """
        Join multi-line API signatures in the text.
        
        This is the main entry point. It processes the text line by line, 
        identifies signature starts, collects valid continuations, 
        and joins them into single lines.
        
        Args:
            text: The input text with potential multi-line signatures.
            
        Returns:
            Text with multi-line signatures joined.
        """
        lines = text.split('\n')
        result = []
        i = 0
        in_code_block = False
        
        while i < len(lines):
            line = lines[i]
            
            # Check if we should skip this line
            should_skip, in_code_block = self._should_skip_line(line, in_code_block)
            if should_skip:
                result.append(line)
                i += 1
                continue
            
            # Extract line info
            line_info = self._extract_line_info(line)
            
            # Check if this is a signature start
            if self._is_signature_start(line_info):
                # Check if already complete on one line
                if line_info.paren_balance == 0 and line_info.bracket_balance == 0:
                    # Check for return arrow on next line
                    if i + 1 < len(lines):
                        next_stripped = lines[i + 1].strip()
                        if next_stripped.startswith('→') or next_stripped.startswith('->'):
                            # Include return type
                            joined = line_info.stripped + ' ' + next_stripped
                            result.append(joined)
                            i += 2
                            continue
                    
                    # Complete signature
                    result.append(line)
                    i += 1
                    continue
                
                # Collect continuation lines
                para_lines = [line]
                accumulated_paren = line_info.paren_balance
                accumulated_bracket = line_info.bracket_balance
                signature_fonts = line_info.fonts.copy()
                j = i + 1
                
                while j < len(lines):
                    next_line = lines[j]
                    
                    # Check stop conditions
                    if self._is_stop_condition(next_line, in_code_block, line_info):
                        break
                    
                    next_info = self._extract_line_info(next_line)
                    
                    # Check if valid continuation
                    if self._is_valid_continuation(next_info, line_info, accumulated_paren):
                        # Check font consistency
                        if self._check_font_consistency(next_info, line_info, signature_fonts):
                            para_lines.append(next_line)
                            accumulated_paren += next_info.paren_balance
                            accumulated_bracket += next_info.bracket_balance
                            signature_fonts.update(next_info.fonts)
                            j += 1
                            
                            # Check if signature is complete
                            if accumulated_paren == 0 and accumulated_bracket == 0:
                                # Look for return arrow on next line
                                if j < len(lines):
                                    peek = lines[j].strip()
                                    if peek.startswith('→') or peek.startswith('->'):
                                        para_lines.append(lines[j])
                                        j += 1
                                break
                            continue
                    
                    # Not a valid continuation
                    break
                
                # Join if multiple lines collected
                if len(para_lines) > 1:
                    joined = ' '.join(l.strip() for l in para_lines)
                    # Normalize internal whitespace
                    joined = re.sub(r'\s+', ' ', joined)
                    result.append(joined)
                    i = j
                else:
                    result.append(line)
                    i += 1
            else:
                # Not a signature start - preserve as-is
                result.append(line)
                i += 1
        
        return '\n'.join(result)
