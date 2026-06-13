import json
import asyncio
import logging
from typing import List, Dict, Optional
from pathlib import Path
import openai
from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


MODEL_NAME = "gpt-4.1-2025-04-14"

class DocumentationExtractor:
    def __init__(self, MM_type : str, MM_signature : str, MM_code_body : str, MM_methods_and_attributes_signature : str, scraped_doc_path : str, api_key : str, input_choice : str = 'module_member_signature'):
        """
        Initialize the DocumentationExtractor with module member details and API key.
        
        Args:
            MM_type: Type of module member (class, function, or method)
            MM_signature: Signature of the module member
            MM_code_body: Code body of the module member
            MM_methods_and_attributes_signature: Signature of the module member's methods and attributes
            scraped_doc_path: Path to the scraped documentation
            api_key: API key for the OpenAI API
            input_choice: Choice of input (code_body, module_member_signature, methods_and_attributes_signature)
        """
        self.MM_type = MM_type.lower()
        self.MM_signature = MM_signature
        self.MM_code_body = MM_code_body
        self.MM_methods_and_attributes_signature = MM_methods_and_attributes_signature
        self.scraped_doc_path = scraped_doc_path
        self.api_key = api_key
        self.input_choice = input_choice  # Determines which source code info to use

        self.system_prompt = ''
        self.user_prompt = ''
        self.extracted_doc = ''
        self.scraped_doc = ''

        # Set the OpenAI API key
        openai.api_key = self.api_key

        # Read the scraped documentation from the provided .txt file
        self._read_scraped_doc()

    def _read_scraped_doc(self):
        """
        Reads the scraped documentation from the .txt file.
        """
        try:
            with open(self.scraped_doc_path, 'r', encoding='utf-8') as f:
                self.scraped_doc = f.read()
        except FileNotFoundError as e:
            print(f"Error: Scraped documentation file not found: {e}")
        except Exception as e:
            print(f"An error occurred while reading the scraped documentation: {e}")

    def _generate_prompts(self):
        """
        Generates the system and user prompts based on the module member type and input choice.
        """
        source_code_info = ''

        if self.input_choice == 'code_body':
            source_code_info = self.MM_code_body
        elif self.input_choice == 'module_member_signature':
            source_code_info = self.MM_signature
        elif self.input_choice == 'methods_and_attributes_signature':
            source_code_info = self.MM_methods_and_attributes_signature
        else:
            # Default to module member signature
            source_code_info = self.MM_signature

        # Now define the prompts based on MM_type

        if self.MM_type == 'class':
            self.system_prompt = """
You are tasked with accurately and **completely** extracting the reference documentation for a Python class from text scraped from its official documentation website. The scraped text has been modified to replace actual URLs with placeholders like `url_placeholder_0`, `url_placeholder_1`, etc.

You will be provided with two inputs:

1. **Source Code of the Module Member (Class)**: This could be:
   - The full code body of the class.
   - The class signature only.
   - A combination of the class signature and the signatures for its methods and attributes.

2. **Scraped Documentation Text**: Text extracted from the official documentation, with URLs replaced by placeholders.

### **Task**:

- **Exact Extraction Only**: Extract information **exactly** as it appears in the scraped text, including all placeholders and the immediate characters surrounding them, without any additions, inferences, or interpretations. **Do not modify, interpret, or remove the URL placeholders**.

- **Preserve Formatting**: Retain all original formatting, including new lines, spaces, indentation, and especially the formatting of code examples and blocks. Ensure that the placeholders and their surrounding characters are preserved in their exact positions.

- **Include Placeholders Appropriately**: Treat URL placeholders as integral parts of the text. **Include them exactly where they appear in the scraped text**, along with any immediate surrounding characters.

- **Module Member Description**:
  - **Purpose**: Extract the main description of the class, outlining its purpose and functionality, and include it in the `purpose` field.
  - **Additional Information**: Extract any additional information related to the class description that doesn't belong to other specific fields, and include it in the `additional_information` array.
    - **Keep Related Content Together**: If multiple sentences or paragraphs form a cohesive explanation or are part of the same topic, include them as a single string in the array element, preserving the original formatting.
    - **Split Content Only When Necessary**: Separate content into different array elements only if there is a clear distinction in topics or if the content addresses completely different aspects of the class.

- **Example Handling**:
  - **Separate Examples**: When multiple examples are present in the scraped text, extract each example as a separate object within the `examples` array.
  - **Maintain Order**: Preserve the order of the examples as they appear in the scraped text.
  - **Include Associated Descriptions**: If an example has a description or notes that directly relate to it, include this information within the same `example` object.
  - **Example Object Structure**:
    - **example**: Should contain the code example, including any descriptions or notes that are directly associated with it.
    - **additional_information**: Use this field for any supplementary information that is separate from the main example content.

- **Correct Use of Fields**:
  - **Signature**: Only include a signature if it is explicitly provided in the scraped text as a signature, and associated URL placeholders. It is important to note that the signature for a class or attribute could consist of the member's API name without parameters within parentheses. If this is present instead of the parenthesized signature, it should be included in the `signature` field. If neither are no explicitly provided, set the `signature` field to "N/A".
  - **Name**: The `name` field should include the module member's name only if it is explicitly provided in the scraped text.
  - **Additional Information**: Use this field only for supplementary content that is explicitly separate from the main description within a specific method, or parameter. This could also be additional content provided that's separate from the main functional description of the parameter or method.
    - **Do not include general supplementary information or sections unrelated to the method or parameter here.**

- **Attributes Section**:
  - **identifier**: Include the attribute name and any associated content (e.g., URL placeholders, parentheses) exactly as it appears in the scraped text.
  - **description**: Provide the attribute's description. If unavailable, set to "N/A".
  - **additional_information**: Include any supplementary information related specifically to the attribute. If none, set to "N/A".
    - **Do not include general supplementary information or sections unrelated to the attribute here.**

- **Additional Notes Structure**:
  - The `additional_notes` section must be an object containing two arrays:
    - **supplementary_information**: An array containing any general supplementary information about the class that is not tied to a specific field. This includes "Notes", "See Also", "References", "Remarks", or descriptions that provide additional context or direct readers to more resources.
    - **edge_cases**: An array containing information about edge cases, limitations, or potential pitfalls related to the class. This includes specific scenarios where the class may not function as expected, requiring special attention.

### **Key Guidelines**:

- **Handling URL Placeholders**:
  - **Retain All Placeholders**: Ensure that all `url_placeholder_X` tokens are included exactly as they appear.
  - **Preserve Surrounding Characters**: Keep any characters immediately before and after the placeholders (e.g., parentheses, brackets, punctuation).

- **Correct Classification of Additional Notes**: 
  - **Supplementary Information**:
    - For any general supplementary information sections found in the scraped text that provides additional context or resources related to the class as a whole that are **not directly associated with specific parameters, attributes, methods, or examples**, include them in the `supplementary_information` array within `additional_notes`.
    - **Do not include general supplementary information in the `additional_information` fields** of other sections unless it is directly and specifically related to that section.
  - **Edge Cases**: Reserve `edge_cases` for specific scenarios that might lead to errors or require special handling.

- **Avoid Inferring Information**: Do not infer or add information not explicitly present in the scraped text.

- **Do Not Restructure Content**:
  - Maintain the original grouping and order of the content as it appears in the scraped text.
  - Do not split or combine content in ways that alter the original meaning or context.
  - Do not split closely related content into separate array elements if they are part of the same explanation or context.
  
- **Preserve Formatting**: Retain all formatting, including new lines, paragraph breaks, indentation, mathematical equations, and any special formatting present in the scraped text.

- **Completeness**:
  - Include all relevant information from the scraped text.
  - If a particular field is absent, confirm its absence before marking it as "N/A".

- **Verification**:
  - Review the extracted information to ensure accuracy, especially regarding the placeholders and their context.
  - Carefully check that all extracted information aligns perfectly with the scraped text, ensuring no omissions or additions.
"""
            self.user_prompt = f"""
### **Inputs**:

1. **Source Code Information for the Module Member (Class)**:
```python
{source_code_info}
```

2. **Scraped Documentation Text**:
{self.scraped_doc}

### **Task**:

1. **Extract All Relevant Documentation**: Extract the complete reference documentation for the specified class from the scraped text, ensuring that no information is omitted or inferred.

2. **Adhere to the JSON Schema**: Format your extraction according to the JSON schema provided in the response format. Ensure each field is correctly populated:
   - Only include information present in the scraped text.
   
   - **Module Member Description**:
     - **Purpose**: Populate this field with the main description or overview of the class.
     - **Additional Information**: Include any additional information **directly related** to the class description in this array, such as additional content on its functionality or operation, general notes, or other pertinent details.
       - **Do not include general supplementary information or sections unrelated to the class description here.**
       - **Keep Related Information Together**: If the content is part of a continuous explanation or discusses the same topic, include it as a single entry in the array, preserving the original formatting.
   
   - **Other Fields**: Ensure all other sections are populated according to the schema, and set any missing data to "N/A" after confirming its absence.

3. **Preserve Original Formatting**: Maintain all original formatting from the scraped text, including new lines, spaces, indentation, and especially code examples and blocks.

4. **Retain URL Placeholders and Surrounding Characters**:
   - **Include all `url_placeholder_X` tokens** exactly as they appear in the scraped text.
   - **Preserve any immediate characters** surrounding the placeholders (e.g., parentheses, brackets, punctuation).
   - **Do not modify or interpret the placeholders**.

5. **Extract Examples Separately**:
    - Identify and extract each example as a distinct entry in the `examples` array.
    - **Include Descriptions**: For each example, include any accompanying description or notes within the `example` field.
    - **Do not include general supplementary information or sections unrelated to the example here.**

6. **Avoid Restructuring or Inferring Content**:
   - Do not split or rearrange content from how it appears in the scraped text.
   - Do not infer additional descriptions or explanations.   

7. **Assign Additional Notes Appropriately**:
   - **Within Sections/Subsections**:
     - Keep any `additional_information` that is specific to a method, parameter, attribute, or example within its respective section.
     - **Do not include general supplementary information or sections unrelated to the specific section here.**
   - **In the `additional_notes` Section**:
     - **Supplementary Information**:
       - Include any **general supplementary information about the class** that is **not specific to any one component**.
       - This includes any additional context, notes, usage guidelines, general information, or references that pertain to the class as a whole.
     - **Edge Cases**:
       - Include any information about limitations, warnings, exceptions, or unusual usage scenarios related to the class as a whole.
       - This includes specific scenarios that require special attention due to potential unexpected behavior.

8. **Careful Verification**: Review your extraction to confirm that it is an exact replica of the scraped text within the structure of the JSON schema, with no additions or omissions.

### **Remember**:

- **Authenticity is Crucial**: The goal is to create a precise and complete extraction that mirrors the scraped documentation, including all placeholders and their context.

- **Accuracy with Placeholders**: Ensure that all URL placeholders and their surrounding characters are retained exactly as they appear.

- **Clarity in Structuring**: Ensure that all content is placed in the correct fields as per the schema definitions.

- **Consistency**: Your extraction should be consistent with the original scraped text in both content and formatting.
"""

            self.json_schema={
                "type": "json_schema",
                "json_schema": {
                "name": "python_class_documentation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                    "module_member_signature": {
                        "type": "string",
                        "description": "Class signature if available, otherwise 'N/A'."
                    },
                    "module_member_description": {
                        "type": "object",
                        "description": "Contains the main description and additional information about the class.",
                        "properties": {
                          "purpose": {
                            "type": "string",
                            "description": "Class description or overview, outlining its purpose and functionality."
                          },
                          "additional_information": {
                            "type": "array",
                            "description": "Array of additional information related to the class.",
                            "items": {
                              "type": "string",
                              "description": "A piece of additional information about the class."
                            }
                          }
                        },
                        "required": ["purpose", "additional_information"],
                        "additionalProperties": False
                    },
                    "parameters": {
                        "type": "array",
                        "description": "List of parameters / arguments for the class.",
                        "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                            "type": "string",
                            "description": "Parameter name, or 'N/A'."
                            },
                            "type": {
                            "type": "string",
                            "description": "Parameter type, or 'N/A'."
                            },
                            "description": {
                            "type": "string",
                            "description": "Parameter description, or 'N/A'."
                            },
                            "additional_information": {
                            "type": "string",
                            "description": "Supplementary information, or 'N/A'."
                            }
                        },
                        "required": [
                            "name",
                            "type",
                            "description",
                            "additional_information"
                        ],
                        "additionalProperties": False
                        }
                    },
                    "attributes": {
                        "type": "array",
                        "description": "List of attributes of the class.",
                        "items": {
                            "type": "object",
                            "properties": {
                            "identifier": {
                                "type": "string",
                                "description": "Attribute name along with any associated content such as URL placeholders."
                            },
                            "description": {
                                "type": "string",
                                "description": "Attribute description, or 'N/A'."
                            },
                            "additional_information": {
                                "type": "string",
                                "description": "Supplementary information related to the attribute, or 'N/A'."
                            }
                            },
                            "required": ["identifier", "description", "additional_information"],
                            "additionalProperties": False
                        }
                    },
                    "methods": {
                        "type": "array",
                        "description": "List of methods within the class.",
                        "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                            "type": "string",
                            "description": "Method name, or 'N/A'."
                            },
                            "signature": {
                            "type": "string",
                            "description": "Method signature, or 'N/A'."
                            },
                            "description": {
                            "type": "string",
                            "description": "Method description, or 'N/A'."
                            },
                            "parameters": {
                            "type": "array",
                            "description": "List of parameters / arguments for the method.",
                            "items": {
                                "type": "object",
                                "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Parameter name, or 'N/A'."
                                },
                                "type": {
                                    "type": "string",
                                    "description": "Parameter type, or 'N/A'."
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Parameter description, or 'N/A'."
                                },
                                "additional_information": {
                                    "type": "string",
                                    "description": "Supplementary information, or 'N/A'."
                                }
                                },
                                "required": [
                                "name",
                                "type",
                                "description",
                                "additional_information"
                                ],
                                "additionalProperties": False
                            }
                            },
                            "returns": {
                            "type": "object",
                            "properties": {
                                "type": {
                                "type": "string",
                                "description": "The type of the return value, or 'N/A'."
                                },
                                "description": {
                                "type": "string",
                                "description": "The description for the value returned, or 'N/A'."
                                }
                            },
                            "required": [
                                "type",
                                "description"
                            ],
                            "additionalProperties": False
                            },
                            "additional_information": {
                            "type": "string",
                            "description": "Supplementary information, or 'N/A'."
                            }
                        },
                        "required": [
                            "name",
                            "signature",
                            "description",
                            "parameters",
                            "returns",
                            "additional_information"
                        ],
                        "additionalProperties": False
                        }
                    },
                    "examples": {
                        "type": "array",
                        "description": "List of examples related to the class.",
                        "items": {
                        "type": "object",
                        "properties": {
                            "example": {
                            "type": "string",
                            "description": "Example content, or 'N/A'."
                            },
                            "additional_information": {
                            "type": "string",
                            "description": "Additional notes, or 'N/A'."
                            }
                        },
                        "required": [
                            "example",
                            "additional_information"
                        ],
                        "additionalProperties": False
                        }
                    },
                    "additional_notes": {
                        "type": "object",
                        "description": "Additional notes related to the class.",
                        "properties": {
                        "supplementary_information": {
                            "type": "array",
                            "description": "Array of general supplementary information or remarks about the class.",
                            "items": {
                            "type": "string",
                            "description": "A piece of supplementary information."
                            }
                        },
                        "edge_cases": {
                            "type": "array",
                            "description": "Array of notes describing edge cases, limitations, or potential pitfalls.",
                            "items": {
                            "type": "string",
                            "description": "A description of an edge case or limitation."
                            }
                        }
                        },
                        "required": [
                        "supplementary_information",
                        "edge_cases"
                        ],
                        "additionalProperties": False
                    }
                    },
                    "required": [
                    "module_member_signature",
                    "module_member_description",
                    "parameters",
                    "attributes",
                    "methods",
                    "examples",
                    "additional_notes"
                    ],
                    "additionalProperties": False
                }
                }
            }

        elif self.MM_type in ['function', 'method']:
            self.system_prompt = """
You are tasked with accurately and **comprehensively** extracting the reference documentation for a Python function or method from text scraped from its official documentation website. You will be provided with two inputs:

1. **Source Code of the Module Member (Function/Method)**: This could be:
   - The complete code body of the function/method.
   - The function/method signature only.

2. **Scraped Documentation Text**: Text extracted from the official documentation.

### Your Task:

- **Exact Extraction Only**: Extract information **exactly** as it appears in the scraped text, without any additions, inferences, or interpretations. Do not include any information that is not present in the scraped text.

- **Preserve Formatting**: Retain all original formatting, including new lines, spaces, indentation, and especially the formatting of code examples and blocks.

- **Include URLs Appropriately**: Ensure that each URL is included exactly where it appear in the scraped text and within the appropriate sections of its surrounding content.

- **Correct Use of Fields**:
  - **Signature**: Only include the function/method signature if it is explicitly provided in the scraped text as a signature. If not, set the `module_member_signature` field to "N/A".
  - **Name**: The `module_member_name` field (if applicable) should include the function/method name only if it is explicitly provided as such in the scraped text.
  - **Additional Information**: Use the `additional_information` fields only for supplementary content that is explicitly separate from the main description within that specific parameter, return value, or example.

- **Additional Notes Structure**:
  - The `additional_notes` section must be an object containing two arrays:
    - **supplementary_information**: An array containing any general supplementary information about the function/method that is not tied to a specific section.
    - **edge_cases**: An array containing any notes about edge cases, limitations, or potential pitfalls related to the function/method.

### Key Guidelines:

- **Assigning Additional Notes Appropriately**:
  - **Within Sections/Subsections**: Any `additional_information` provided under parameters, return values, or examples should remain within those sections and should not be moved to the `additional_notes` section.
  - **General Notes**: Any notes not specific to a particular parameter or example but related to the function/method as a whole should be included in the `additional_notes` section.
    - Use the definitions provided to distinguish between `supplementary_information` and `edge_cases`.

- **Identifying Content for `additional_notes`**:
  - **Supplementary Information**: Look for general notes, remarks, or additional context that enhance understanding of the function/method.
  - **Edge Cases**: Identify any content that highlights limitations, warnings, exceptions, or unusual usage scenarios.

- **Do Not Restructure Content**:
  - Maintain the original grouping and order of the content as it appears in the scraped text.
  - Do not split or combine content in ways that alter the original meaning or context.

- **Example Handling**:
  - Preserve the content and formatting of examples as they appear.
  - Do not split examples or reassign their descriptions.

- **Completeness**:
  - Include all relevant information from the scraped text.
  - If a particular field is absent, confirm its absence before marking it as "N/A".

- **Verification**:
  - Carefully review the extracted information to ensure accuracy, completeness, and adherence to the guidelines.

### Remember:

- **Authenticity is Crucial**: Your response should be a faithful representation of the scraped text, formatted according to the schema, without additions or omissions.
- **Clarity in Structuring**: Ensure that all content is placed in the correct fields as per the schema definitions, including correctly categorizing notes in the `additional_notes` section.
- **Consistency**: Your extraction should be consistent with the original scraped text in both content and formatting.
"""
            self.user_prompt = f"""
### Inputs:

1. **Source Code Information for the Module Member (Function/Method)**:
```python
{source_code_info}
```

2. **Scraped Documentation Text:**
{self.scraped_doc}

### Task:

1. **Extract All Relevant Documentation**: Extract the complete reference documentation for the specified function or method from the scraped text, ensuring that no information is omitted or inferred.

2. **Adhere to the JSON Schema**: Format your extraction according to the JSON schema provided in the response format. Ensure each field is correctly populated:
   - Only include information present in the scraped text.
   - For any missing data, set the field to "N/A" after confirming its absence.
   - In the `additional_notes` section, populate the `supplementary_information` and `edge_cases` arrays with the corresponding notes from the documentation.

3. **Preserve Original Formatting**: Maintain all original formatting from the scraped text, including new lines, spaces, and indentation, especially in code examples and blocks.

4. **Include URLs in Context**: Ensure that any URLs are included exactly where they appear in the scraped text and within the appropriate sections.

5. **Assign Additional Notes Appropriately**:
   - **Within Sections/Subsections**: Keep any `additional_information` that is specific to a parameter, return value, or example within its respective section.
   - **In the `additional_notes` Section**:
     - **Supplementary Information**: Collect general notes or remarks about the function/method that are not specific to any one component.
     - **Edge Cases**: Include any notes about limitations, warnings, exceptions, or unusual usage scenarios related to the function/method as a whole.

6. **Avoid Restructuring or Inferring Content**:
   - Do not split or rearrange content from how it appears in the scraped text.
   - Do not infer additional descriptions or explanations.

7. **Careful Verification**: Review your extraction to confirm that it is an exact replica of the scraped text within the structure of the JSON schema, with no additions or omissions.

### Remember:

- **Authenticity is Crucial**: The goal is to create a precise and complete extraction that mirrors the scraped documentation.
- **Clarity in Structuring**: Ensure that all content is placed in the correct fields as per the schema definitions, including correctly categorizing notes in the `additional_notes` section.
- **Consistency**: Your extraction should be consistent with the original scraped text in both content and formatting.
"""

            self.json_schema={
                "type": "json_schema",
                "json_schema": {
                "name": "python_function_method_documentation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                    "module_member_signature": {
                        "type": "string",
                        "description": "Function or method signature if available, otherwise 'N/A'."
                    },
                    "module_member_description": {
                        "type": "string",
                        "description": "Function or method description or overview, otherwise 'N/A'."
                    },
                    "parameters": {
                        "type": "array",
                        "description": "List of parameters / arguments for the function or method.",
                        "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                            "type": "string",
                            "description": "Parameter name, or 'N/A'."
                            },
                            "type": {
                            "type": "string",
                            "description": "Parameter type, or 'N/A'."
                            },
                            "description": {
                            "type": "string",
                            "description": "Parameter description, or 'N/A'."
                            },
                            "additional_information": {
                            "type": "string",
                            "description": "Supplementary information related to the parameter, or 'N/A'."
                            }
                        },
                        "required": [
                            "name",
                            "type",
                            "description",
                            "additional_information"
                        ],
                        "additionalProperties": False
                        }
                    },
                    "returns": {
                        "type": "object",
                        "properties": {
                        "type": {
                            "type": "string",
                            "description": "Type of the return value, or 'N/A'."
                        },
                        "description": {
                            "type": "string",
                            "description": "Description of the return value, or 'N/A'."
                        },
                        "additional_information": {
                            "type": "string",
                            "description": "Supplementary information related to the return value, or 'N/A'."
                        }
                        },
                        "required": [
                        "type",
                        "description",
                        "additional_information"
                        ],
                        "additionalProperties": False
                    },
                    "examples": {
                        "type": "array",
                        "description": "List of examples showing usage of the function or method.",
                        "items": {
                        "type": "object",
                        "properties": {
                            "example": {
                            "type": "string",
                            "description": "Content of the example, or 'N/A'."
                            },
                            "additional_information": {
                            "type": "string",
                            "description": "Additional information related to the example, or 'N/A'."
                            }
                        },
                        "required": [
                            "example",
                            "additional_information"
                        ],
                        "additionalProperties": False
                        }
                    },
                    "additional_notes": {
                        "type": "object",
                        "description": "Additional notes related to the function or method.",
                        "properties": {
                        "supplementary_information": {
                            "type": "array",
                            "description": "Array of general supplementary information about the function or method.",
                            "items": {
                            "type": "string",
                            "description": "A piece of supplementary information."
                            }
                        },
                        "edge_cases": {
                            "type": "array",
                            "description": "Array of notes describing edge cases, limitations, or potential pitfalls.",
                            "items": {
                            "type": "string",
                            "description": "A description of an edge case or limitation."
                            }
                        }
                        },
                        "required": [
                        "supplementary_information",
                        "edge_cases"
                        ],
                        "additionalProperties": False
                    }
                    },
                    "required": [
                    "module_member_signature",
                    "module_member_description",
                    "parameters",
                    "returns",
                    "examples",
                    "additional_notes"
                    ],
                    "additionalProperties": False
                }
                }
            }

        else:
            raise ValueError(f"Unknown module member type provided: '{self.MM_type}'. Must be 'class', 'function', or 'method'.")

    def _call_openai_api(self):
        """
        Calls the OpenAI API with the generated prompts to perform data extraction.
        """
        try:
            response = openai.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self.user_prompt}
                ],
                response_format=self.json_schema,
                temperature=0.0,
                max_tokens=32768,
                top_p=1,
                frequency_penalty=0,
                presence_penalty=0,
            )
            self.extracted_doc = response.choices[0].message.content.strip()
        except openai.OpenAIError as e:
            print(f"OpenAI API error: {e}")
            self.extracted_doc = ''
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            self.extracted_doc = ''


class ConcurrentDocExtractor:
    """
    Fast concurrent documentation extraction using async real-time API.
    
    Benefits over batch:
        - Results in minutes, not hours
        - Immediate feedback on failures
        - Progress tracking per-request
    
    Benefits over sequential:
        - 10-50x faster with concurrent requests
        - Respects rate limits automatically
    """
    
    def __init__(self, api_key: str, max_concurrent: int = 10):
        """
        Args:
            api_key: OpenAI API key
            max_concurrent: Max simultaneous requests (respect rate limits)
        """
        self.client = AsyncOpenAI(api_key=api_key)
        self.semaphore = asyncio.Semaphore(max_concurrent)
    
    async def _extract_single(
        self,
        api_name: str,
        member_type: str,
        signature: str,
        scraped_doc: str,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict
    ) -> Dict:
        """Extract documentation for a single member."""
        async with self.semaphore:  # Limit concurrency
            try:
                response = await self.client.chat.completions.create(
                    model=MODEL_NAME, 
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    response_format=json_schema,
                    temperature=0.0,
                    max_tokens=32768
                )
                return {
                    "api_name": api_name,
                    "success": True,
                    "result": response.choices[0].message.content
                }
            except Exception as e:
                logger.warning(f"Failed to extract {api_name}: {e}")
                return {
                    "api_name": api_name,
                    "success": False,
                    "error": str(e)
                }
    
    async def extract_all(self, members: List[Dict], progress_callback: Optional[callable] = None) -> Dict[str, str]:
        """
        Extract documentation for all members concurrently.
        
        Args:
            members: List of dicts with: api_name, member_type, signature, 
                     system_prompt, user_prompt, json_schema
            progress_callback: Optional function(completed, total) for progress
        
        Returns:
            Dict mapping api_name -> extracted JSON string (or None if failed)
        """
        tasks = []
        for m in members:
            task = self._extract_single(
                api_name=m['api_name'],
                member_type=m['member_type'],
                signature=m['signature'],
                scraped_doc=m.get('scraped_doc', ''),
                system_prompt=m['system_prompt'],
                user_prompt=m['user_prompt'],
                json_schema=m['json_schema']
            )
            tasks.append(task)
        
        results = {}
        completed = 0
        total = len(tasks)
        
        # Process with progress tracking
        for coro in asyncio.as_completed(tasks):
            result = await coro
            completed += 1
            
            if result['success']:
                results[result['api_name']] = result['result']
            else:
                results[result['api_name']] = None
            
            if progress_callback:
                progress_callback(completed, total)
            else:
                logger.info(f"Progress: {completed}/{total} ({result['api_name']})")
        
        return results


def run_concurrent_extraction(members: List[Dict], api_key: str, max_concurrent: int = 10):
    """Synchronous wrapper for async extraction."""
    extractor = ConcurrentDocExtractor(api_key, max_concurrent)
    return asyncio.run(extractor.extract_all(members))
