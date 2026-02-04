import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


__all__ = ['preprocess_crossRef', 'postprocess_crossRef']


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
        
        for line in scraped_doc_lines:
            self.line_change = line
            
            if '(http' in self.line_change:
                url_line = self.line_change.split(' ')
                
                for i in url_line:
                    if '(http' in i:
                                                    
                        self._urlDict_newDoc(i)

            self.new_doc_lines.append(self.line_change)
        
        return self.new_doc_lines, self.url_dict


    def _urlDict_newDoc(self, line_segment: str) -> None:
        """
        Extract URL and URL reference (context surrounding the URL)
        
        Args:
            line_segment (str): Space-separated segment of the line currently being processed.
        """
        
        # Check how many URLs are present in each space-separated-word
        parts_with_url = line_segment[line_segment.index('(http'):].split('(http')
                        
        for ref_to, url_part in enumerate(parts_with_url):
            if url_part:
                reference_to = None
                url_part = '(http' + url_part

                url_start = line_segment.index(url_part)
                url = url_part[1: url_part.index(')')]

                if line_segment[:url_start][-1] == ']':
                    url_ref = line_segment[:url_start].split('[')
                    if len(url_ref) > 1:
                        if (url_ref[-1]).lower().endswith('source]'):
                            url_ref[-1] = '[' + url_ref[-1]
                            url_ref[-2] = url_ref[-2].split(' ')[-1]
                            url_ref = url_ref[-2:]
                        elif url_ref[-2] == '':
                            url_ref = ['[' + url_ref[-1]]
                    else:
                        url_ref_idx = self.line_change.index((url_ref[-1]+ '(' + url))
                        url_ref = ['[' + self.line_change[:url_ref_idx].split('[')[-1] + url_ref[-1]]

                elif (line_segment[:url_start][-1]).isalnum():
                    url_ref = [line_segment[:url_start].split(' ')[-1]]
                else:
                    if len(line_segment[:url_start]) > 1:
                        url_ref = line_segment[:url_start].split(' ')
                        
                        if len(url_ref) > 1:
                            if url_ref[-1] == '':
                                url_ref[-1] = ' '
                            url_ref = url_ref[-2:]
                    else:
                        url_ref = ['']
                        
                if len(parts_with_url) > 2 and len(parts_with_url) > ref_to + 1:
                    reference_to = f'url_placeholder_{self.url_count+1}'
                
                # Update URL dictionary with currently processed URL and its reference(s)
                self.url_dict.update({f"url_placeholder_{self.url_count}": {'url_reference': url_ref, 'url': url, 'reference_to': reference_to}}) 
                
                # Construct possible URL reference
                url_reference = ''
                for ref_part in url_ref:
                    url_reference += ref_part
                
                # Replace URLs with placeholders
                replace_str_with = f"{url_reference}(url_placeholder_{self.url_count})"
                replace_str = f"{url_reference}({url})"
                
                self.url_count += 1

                self.line_change = self.line_change.replace(replace_str, replace_str_with)
                line_segment = line_segment.replace(replace_str, replace_str_with)


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
        
        sorted_placeholders = sorted(self.url_mapping.keys(), reverse=True)
        
        for placeholder in sorted_placeholders:
            if placeholder in self.processed_placeholders:
                continue
                
            content, success = self._replace_placeholder(content, placeholder)
            
            if not success:
                logger.warning(f"Failed to process placeholder: {placeholder}")

        return content

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
        
        # Create directories if they don't exist
        os.makedirs(os.path.dirname(processed_doc_path), exist_ok=True)
        
        with open(processed_doc_path, 'w', encoding='utf-8') as doc_f:
            json.dump(processed_documentation, doc_f, indent=4, ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"Error processing documentation: {str(e)}")
        raise

