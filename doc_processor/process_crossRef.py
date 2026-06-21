import os
import re
import json
import logging
import unicodedata
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


__all__ = ['preprocess_crossRef', 'postprocess_crossRef']



# Sphinx "permalink to this definition / headline" markers that leak into scraped text. The pilcrow (¶) is the default; some themes use other glyphs.
_ANCHOR_MARKERS = (
    '\u00b6',  # ¶ PILCROW SIGN (Sphinx headerlink)
    '\uf0c1'   # FontAwesome link glyph (Private Use Area; some themes)
)

# ASCII/Unicode control characters EXCEPT tab (\x09), newline (\x0a), CR (\x0d).
# These show up as \u0000, \u0006, etc. in LLM output and corrupt the docs.
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# Matches a parenthesized http(s) URL, e.g. "(https://docs.example.com/page#frag)".
# Used to replace the URL inside the parentheses with a placeholder, leaving the surrounding reference text untouched
_URL_PAREN_RE = re.compile(r'\((https?://[^)]+)\)')

# PDF internal cross-refs emitted by pipeline_pdf, e.g. "Pipeline(#page=2000)".
_PAGE_PAREN_RE = re.compile(r'\(#page=(\d+)\)')

# PDF column-alignment padding (large space runs): collapse so it stops looking like repeatable low-information content that pushes the LLM into a loop.
_BIG_SPACE_RE = re.compile(r' {6,}')


# A PDF endnote/footnote superscript number wedged between a token's own hyperlink and its (duplicate) endnote hyperlink: "int(https://…#int)5839(https://…#int)".
# torch's PDF builder numbers every cross-reference and links the number too, so it emits "(URL_A)<digits>(URL_B)". Collapse to the single leading link, dropping both
# the stray number and the redundant endnote URL. The no-whitespace gluing makes this specific to the artifact (web docs never produce adjacent parenthesized links).
_FOOTNOTE_BETWEEN_LINKS_RE = re.compile(r'(\((?:https?://|#page=)[^)]*\))\d+\((?:https?://|#page=)[^)]*\)')

# A PDF endnote/bibliography entry printed at the page bottom: "6455 https://…".
# Not parenthesized, so the URL replacer never touches it; drop the whole line.
_ENDNOTE_LINE_RE = re.compile(r'^\s*\d{1,6}\s+https?://\S+\s*$')

# Strong (non-ASCII) math/Greek/bracket glyphs that survive NFKC and appear in 2-D PDF equation layouts (inside OR outside ``` fences). Supersedes the curated symbol sets
# in pdf_localizer.py and, crucially, also covers the glyphs that actually broke torch:
# arrows (←), combining marks (γ̃), sub/superscripts, and the big/lenticular brackets (︂) where none of which are in that file's MATH_SYMBOLS/GREEK_LETTERS sets.
_MATH_CHARS_RE = re.compile(
    '['
    '\u0300-\u036F'   # combining diacritical marks (over/under bars on symbols)
    '\u0370-\u03FF'   # Greek and Coptic (π, μ, γ, θ, λ, ε)
    '\u1F00-\u1FFF'   # Greek Extended
    '\u2070-\u209F'   # super/subscripts
    '\u2100-\u214F'   # letterlike symbols (ℝ, ℓ)
    '\u2190-\u21FF'   # arrows (←, →)
    '\u2200-\u22FF'   # mathematical operators (∑, ∇, ∈, ≤, √)
    '\u2300-\u23FF'   # misc technical (⌊ ⌋ ⎛)
    '\u25A0-\u25FF'   # geometric shapes
    '\u27C0-\u27EF'   # misc math symbols-A
    '\u2980-\u29FF'   # misc math symbols-B
    '\u2A00-\u2AFF'   # supplemental math operators
    '\u3000-\u303F'   # CJK symbols/punct (NFKC-folded lenticular brackets)
    '\uFE30-\uFE4F'   # CJK presentation-form brackets (︂)
    ']'
)

# ASCII operators that corroborate an equation ONLY when a strong glyph is already present (mirrors pdf_localizer's has_greek_equation = has_greek and has_operators).
# Never a standalone signal — '=', '<', '>' are ubiquitous in signatures/prose. '-' is deliberately excluded (hyphenation / dashes).
_ASCII_MATH_OPS = frozenset({'=', '+', '<', '>', '/', '^', '|'})


# Typographic Unicode punctuation that some LLMs mangle into malformed \uXXXX escapes (then degenerate into repetition loops, e.g. a '•' becoming "\u0002022\u0000b\u0000b..."). 
# Map to ASCII equivalents. Deliberately limited to punctuation/whitespace (never math symbols or letters) so meaningful content (e.g. '<=', Greek, '×') is preserved
_TYPOGRAPHIC_MAP = {
    '\u2022': '-', '\u2023': '-', '\u25e6': '-', '\u2043': '-',   # bullets
    '\u2219': '-', '\u00b7': '-',                                 # bullet/middle dot
    '\u2013': '-', '\u2014': '-', '\u2212': '-',                  # en/em dash and MINUS SIGN (math)
    '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',   # smart quotes
    '\u2026': '...',                                              # ellipsis
    '\u00a0': ' ', '\u202f': ' ', '\u2009': ' '                   # nbsp/thin spaces
}
_TYPO_TABLE = str.maketrans(_TYPOGRAPHIC_MAP)


def _normalize_typography(text: str) -> str:
    """Map LLM-hostile typographic punctuation to ASCII before structuring."""
    return text.translate(_TYPO_TABLE)


def _sanitize_anchor_noise(text: str) -> str:
    """
    Remove Sphinx section-anchor markers, normalize LLM-hostile typographic punctuation, and strip stray control characters from scraped text BEFORE URL
    placeholders are applied so that none of it reaches the LLM, which otherwise echoes it back as corrupt control-char escapes 
    (e.g. '\\u0006(url_placeholder_1)', '\\u0000a', or a '•' degenerating into '\\u0002022\\u0000b\\u0000b...').
    """
    for mark in _ANCHOR_MARKERS:
        text = text.replace(mark, '')
    
    # NFKC folds Mathematical Alphanumeric Symbols (𝐿->L, 𝑊->W), superscripts (²->2), fullwidth forms and ligatures to ASCII-compatible forms, which neutralizes garbled PDF math blocks (a frequent repetition-loop trigger).
    text = unicodedata.normalize('NFKC', text)
    
    text = _normalize_typography(text)
    text = _BIG_SPACE_RE.sub('  ', text)
    return _CONTROL_CHARS_RE.sub('', text)


def _strip_control_chars(text: str) -> str:
    """Defensive cleanup of control characters in LLM output during postprocessing."""
    return _CONTROL_CHARS_RE.sub('', text)


# Collapse any whitespace run (including newlines), for signatures, which PDFs wrap across lines with alignment padding.
_SIG_WS_RE = re.compile(r'\s+')

# A word split across a line break by a soft (wrap) hyphen:
#   "approxi-\n      mates" and "approxi-\nmates"  ->  "approximates"
# group(1) = token before the hyphen, group(2) = first char of the continuation.
_HYPHEN_WRAP_RE = re.compile(r'(\w+)-\n[ \t]*(\w)')

# Short compound prefixes where the hyphen is semantic, so it's kept when a real compound wraps at the hyphen (e.g. "non-\nnegative" -> "non-negative")
_KEEP_HYPHEN_PREFIXES = frozenset({
    'non', 'self', 'multi', 'inter', 'intra', 'anti', 'pre', 'post', 'sub',
    're', 'co', 'un', 'well', 'high', 'low', 'cross', 'meta', 'semi', 'pseudo'
})

# Keys whose string content is code/verbatim and must not be de-hyphenated.
_VERBATIM_KEYS = frozenset({'example', 'name', 'type', 'identifier'})


def _normalize_signature(sig: str) -> str:
    """Collapse newlines and padding runs in a member signature to single spaces."""
    return _SIG_WS_RE.sub(' ', sig).strip()


def _dehyphenate(text: str) -> str:
    """Re-join words broken across line breaks by a wrap hyphen, preserving genuine compound hyphens for known prefixes."""
    def _join(m: 're.Match') -> str:
        before, nxt = m.group(1), m.group(2)
        if before.lower() in _KEEP_HYPHEN_PREFIXES:
            return f"{before}-{nxt}"   # keep hyphen, drop the newline and  padding
        return f"{before}{nxt}"        # soft wrap hyphen -> remove entirely
    return _HYPHEN_WRAP_RE.sub(_join, text)


def _normalize_structured_fields(value, key=None):
    """
    Recursively normalize a structured-doc value:
      - 'module_member_signature'  -> collapse whitespace
      - 'example' (code)           -> left untouched
      - every other string         -> de-hyphenate wrap hyphens
    Covers module_member_description (str or {purpose, additional_information}), parameters[].description/additional_information, returns.*, 
    additional_notes.*, attributes[].*, methods[].* uniformly, by key, at any depth.
    """
    if isinstance(value, dict):
        return {k: _normalize_structured_fields(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_structured_fields(item, key) for item in value]
    if isinstance(value, str):
        if key in ['module_member_signature', 'signature']:
            return _normalize_signature(value)
        if key in _VERBATIM_KEYS:
            return value
        return _dehyphenate(value)
    return value




class URLReplacer:
    """
    Preprocess and postporcess documentation
    """
    
    def __init__(self, scrapedDocPath: str):
        self.scrapedDocPath = scrapedDocPath    
        
    def extract_urlDict_newDoc(self):
        """
        Extracts URLs from the scraped documentation file, replaces them with unique placeholders, and 
        creates a dictionary URLs and their surrounding context in the document with their corresponding placeholders as keys. 
        
        Args:
            scrapedDocPath (str): file path to the scraped documentation.
            
        Returns:
            new_doc_lines: scraped documentation lines with url placeholders
            url_dict: a dictionary mapping URLs with their surrounding context and placeholders
        """
        
        with open(self.scrapedDocPath, 'r', encoding='utf-8') as f:
            scraped_doc_lines = f.readlines()
        
        self.url_count = 0
        self.url_dict = dict()
        self.new_doc_lines = list()
        
        blank_run = 0
        prev_equation = False
        for line in scraped_doc_lines:
            line = _sanitize_anchor_noise(line)
            
            # Drop PDF endnote/bibliography lines ("6455 https://…").
            if _ENDNOTE_LINE_RE.match(line):
                continue
            
            if line.strip() == '':
                blank_run += 1
                if blank_run > 2:          # keep at most 2 consecutive blank lines
                    continue
            else:
                blank_run = 0
                
            # Drop superscript endnote numbers wedged between duplicate links (must run on raw parens, before placeholder substitution).
            line = _FOOTNOTE_BETWEEN_LINKS_RE.sub(r'\1', line)
            
            self.new_doc_lines.append(self._replace_line_urls(line))
            
        return self.new_doc_lines, self.url_dict
        


    def _replace_line_urls(self, line: str) -> str:
        """Replace parenthesized http(s) URLs and PDF (#page=N) refs with unique url_placeholder tokens, recording each mapping in self.url_dict."""
        line = self._replace_http_urls(line)
        line = self._replace_page_refs(line)
        return line
    
    
    def _replace_http_urls(self, line: str) -> str:
        """Replace every parenthesized http(s) URL in a line with a unique placeholder token, recording {placeholder: {'url': url}}."""
        def _sub(match) -> str:
            url = match.group(1)
            placeholder = f"url_placeholder_{self.url_count}"
            self.url_dict[placeholder] = {'url': url}
            self.url_count += 1
            return f"({placeholder})"
        
        return _URL_PAREN_RE.sub(_sub, line)
    
    
    def _replace_page_refs(self, line: str) -> str:
        """Replace every PDF internal cross-reference in a line with a unique placeholder token, recording {placeholder: {'url': page_ref}}."""
        def _sub(match) -> str:
            page_ref = f"#page={match.group(1)}"
            placeholder = f"url_placeholder_{self.url_count}"
            self.url_dict[placeholder] = {'url': page_ref}
            self.url_count += 1
            return f"({placeholder})"
        
        return _PAGE_PAREN_RE.sub(_sub, line)


def preprocess_crossRef(scraped_doc_path: str, doc_file_path: str, url_file_path: str):
    """
    Replace the cross-reference URLs in the scraped documentation with placeholders and 
    save the preprocessed documentation and URL context information dictionary.
    
    Args:
        scraped_doc_path: Path to the scraped documentation.
        doc_file_path: Path to the preprocessed documentation.
        url_file_path: Path to the URL dictionary.
    """
    
    new_doc_lines, url_dict = URLReplacer(scraped_doc_path).extract_urlDict_newDoc()
    
    # Create directories if they don't exist
    os.makedirs(os.path.dirname(doc_file_path), exist_ok=True)
    os.makedirs(os.path.dirname(url_file_path), exist_ok=True)
    
    with open(doc_file_path, 'w', encoding='utf-8', errors='ignore') as file:
        file.writelines(new_doc_lines)
    
    with open(url_file_path, 'w', encoding='utf-8') as url_f:
        json.dump(url_dict, url_f, indent=4, ensure_ascii=False)
            


class URLPlaceholderReplacer:
    """
    A class to handle replacement of URL placeholders in documentation with actual URLs.
    
    This class processes documentation by replacing URL placeholders with their corresponding URLs,
    handling reference chains between placeholders and ensuring correct URL reference patterns.
    
    Attributes:
        url_mapping (Dict): Dictionary containing URL placeholder mappings with their references and actual URLs
        documentation (Dict): Structured documentation containing URL placeholders to be replaced
        processed_placeholders (set): Set to track processed URL placeholders to avoid duplicates
    """
    
    def __init__(self, url_mapping: Dict, documentation: Dict):
        """
        Initialize URLPlaceholderReplacer with URL mappings and documentation.
        
        Args:
            url_mapping: Dictionary containing URL placeholder mappings
            documentation: Structured documentation containing URL placeholders
        """
        self.url_mapping = url_mapping
        self.documentation = documentation
        self.processed_placeholders = set()

    def _get_url_reference_variations(self, placeholder: str, mapping_data: Dict) -> List[str]:
        """
        Generate variations of URL reference patterns for matching.
        
        Creates two possible variations of URL reference patterns:
        1. Concatenation of all reference elements with placeholder
        2. Concatenation of first reference element with placeholder
        
        Args:
            placeholder: URL placeholder key
            mapping_data: Dictionary containing URL reference data
            
        Returns:
            List of possible URL reference variations for matching
        """
        
        variations = []
        url_references = mapping_data.get('url_reference', [])
        
        if len(url_references) == 2:
            # Full concatenation of both elements
            variations.append(f"{url_references[0]}{url_references[1]}({placeholder})")
            # If the first character is not alphanumeric, it could be excluded in the LLM generated documentation, so add a variation without it.
            if not (url_references[0][0]).isalnum():
                variations.append(f"{url_references[0][1:]}{url_references[1]}({placeholder})") 
            # First element concatenation
            variations.append(f"{url_references[0]}({placeholder})")
            if not (url_references[0][0]).isalnum():
               variations.append(f"{url_references[0][1:]}({placeholder})") 
        elif len(url_references) == 1:
            variations.append(f"{url_references[0]}({placeholder})")
            # If the first character is not alphanumeric, it could be excluded in the LLM generated documentation, so add a variation without it.
            if not (url_references[0][0]).isalnum():
               variations.append(f"{url_references[0][1:]}({placeholder})") 
            
        return variations

    def _validate_reference_chain(self, current_placeholder: str, mapping_data: Dict) -> bool:
        """
        Validate reference chain between placeholders.
        
        Checks if the current placeholder's URL reference starts with its predecessor's
        URL reference when a reference chain exists.
        
        Args:
            current_placeholder: Current URL placeholder being processed
            mapping_data: Dictionary containing reference chain information
            
        Returns:
            Boolean indicating whether reference chain is valid
        """
        
        reference_to = mapping_data['reference_to'] #.get('reference_to')
        if not reference_to or reference_to not in self.url_mapping:
            return True

        referenced_data = self.url_mapping[reference_to]
        # current_url_reference = mapping_data.get('url_reference', [])[0]
        current_url_ref = ''.join(ref for ref in mapping_data['url_reference'])
        referenced_url_ref = referenced_data.get('url_reference', [])[0]

        return referenced_url_ref.startswith(current_url_ref)

    def _replace_placeholder(self, content: str, placeholder: str) -> Tuple[str, bool]:
        """
        Replace a single placeholder with its corresponding URL.
        
        Attempts to replace the placeholder using different URL reference variations
        and validates reference chains before replacement.
        
        Args:
            content: Text content containing URL placeholders
            placeholder: URL placeholder to be replaced
            
        Returns:
            Tuple containing:
            - Updated content with placeholder replaced (if successful)
            - Boolean indicating whether replacement was successful
        """
        
        if placeholder not in self.url_mapping:
            logger.warning(f"Missing URL mapping for placeholder: {placeholder}")
            return content, False

        mapping_data = self.url_mapping[placeholder]
        
        if not self._validate_reference_chain(placeholder, mapping_data):
            logger.warning(f"Invalid reference chain for placeholder: {placeholder}")
            return content, False

        variations = self._get_url_reference_variations(placeholder, mapping_data)
        url = mapping_data['url']
        
        for variation in variations:
            if variation in content:
                url_str = f"{variation[:variation.index(placeholder)]}{url})"
                content = content.replace(variation, url_str)
                self.processed_placeholders.add(placeholder)
                return content, True

        logger.warning(f"No matching pattern found for placeholder: {placeholder}")
        logger.warning(f"Attempted variations: {variations}")
        return content, False

    def process_content(self, content: str) -> str:
        """
        Process content by replacing URL placeholders in reverse order.
        
        Processes placeholders from highest to lowest index to handle nested references correctly.
        
        Args:
            content: Text content containing URL placeholders
            
        Returns:
            Processed content with URL placeholders replaced with actual URLs
        """
        
        # # Track processed placeholders PER content string, not across the whole document. The same placeholder can legitimately appear in multiple
        # # schema fields (e.g. a return type in both `module_member_signature` and `returns.type`); a document-global set would skip every occurrence after the first, leaving those placeholders unrestored.
        # self.processed_placeholders = set()
        
        sorted_placeholders = sorted(self.url_mapping.keys(), key=len, reverse=True)
        
        for placeholder in sorted_placeholders:
            # if placeholder in self.processed_placeholders:
            #     continue
            
            url = self.url_mapping[placeholder].get('url')
            if not url:
                continue
            
            if placeholder in content:
                content = content.replace(placeholder, url)
            else:
                logger.debug(f"Placeholder not present in this field: {placeholder}")
            
            # content, success = self._replace_placeholder(content, placeholder)
            # if not success:
            #     logger.warning(f"Failed to process placeholder: {placeholder}")
        
        return _strip_control_chars(content)


    def process_documentation(self) -> Dict:
        """
        Process the entire documentation structure.
        
        Recursively processes all string values in the documentation structure,
        replacing URL placeholders with actual URLs.
        
        Returns:
            Processed documentation with all URL placeholders replaced
        """
        
        def process_value(value: any) -> any:
            if isinstance(value, str):
                return self.process_content(value)
            elif isinstance(value, dict):
                return {k: process_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [process_value(item) for item in value]
            return value

        return process_value(self.documentation)


def postprocess_crossRef(url_mapping_path: str, structured_doc_path: str, processed_doc_path: str) -> Dict:
    """
    Function to process documentation with URL replacements.
    
    Loads URL mapping and documentation files, initializes URLPlaceholderReplacer, and 
    processes the documentation to replace URL placeholders with actual URLs.
    
    Args:
        url_mapping_path: Path to JSON file containing URL mappings
        documentation: Structured documentation extracted by the LLM
        processed_doc_path: Path to write the processed documentation.
        
    Returns:
        Processed documentation with URL placeholders replaced with actual URLs
        
    Raises:
        Exception: If there's an error reading files or processing documentation
    """
    
    try:
        with open(url_mapping_path, 'r', encoding='utf-8') as f:
            url_mapping = json.load(f)
        
        with open(structured_doc_path, 'r', encoding='utf-8') as f:
            structured_doc = json.load(f)
        
        replacer = URLPlaceholderReplacer(url_mapping, structured_doc)
        processed_documentation = replacer.process_documentation()
        processed_documentation = _normalize_structured_fields(processed_documentation)
        
        # Create directories if they don't exist
        os.makedirs(os.path.dirname(processed_doc_path), exist_ok=True)
        
        with open(processed_doc_path, 'w', encoding='utf-8') as doc_f:
            json.dump(processed_documentation, doc_f, indent=4, ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"Error processing documentation: {str(e)}")
        raise

