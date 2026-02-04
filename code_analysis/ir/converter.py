"""
Converter functions to translate between internal analysis result dictionaries and the Intermediate Representation (IR) models.
"""

import logging
from typing import Optional, List, Union, Dict, Any
import inspect
import datetime # Added for metadata timestamp

# Assuming IR models are in code_analysis.ir.models
from .models import (
    IRModule, IRComponent, IRFunction, IRClass, IRVariable,
    IRImport, IRExport, IRLocation, IRMetadata, IRParameter
)
from .validation import fully_validate_irmodule, IRValidationError # Import validation functions
from code_analysis.config import AnalysisConfig


logger = logging.getLogger(__name__)

# --- Helper Functions (Updated to accept dictionaries) ---

def _convert_location(component_dict: Dict[str, Any]) -> Optional[IRLocation]:
    """Converts location info from a component dictionary to IRLocation."""
    metadata_dict = component_dict.get('metadata', {})
    source_info = metadata_dict.get('source_info', {})

    file_path = source_info.get('file')
    line_start = source_info.get('line_start')
    line_end = source_info.get('line_end')

    if file_path and line_start is not None and line_end is not None:
        return IRLocation(
            file_path=file_path,
            line_start=line_start,
            line_end=line_end,
            # Add column info if available
        )
    logger.debug(f"Could not create IRLocation for component: {component_dict.get('name')}. Missing file/line info in metadata.source_info. Source Info: {source_info}")
    return None

def _convert_metadata(component_dict: Dict[str, Any]) -> IRMetadata:
    """Converts metadata from a component dictionary to IRMetadata."""
    # Extract specific metadata if needed, otherwise copy custom metadata
    custom_meta = component_dict.get('metadata', {}).copy()
    # Ensure standard fields aren't duplicated in custom
    custom_meta.pop('analysis_timestamp', None)
    custom_meta.pop('tool_version', None)

    return IRMetadata(
        # analysis_timestamp is handled by default factory in IRMetadata
        # tool_version could be added here if available
        custom=custom_meta
    )

def _convert_parameters(func_dict: Dict[str, Any]) -> List[IRParameter]:
    """Converts parameter list from a function/method dictionary to IRParameter list."""
    ir_params = []
    param_list = func_dict.get('parameters', []) # Get parameters list directly

    kind_map = {
        inspect.Parameter.POSITIONAL_ONLY: "POSITIONAL_ONLY",
        inspect.Parameter.POSITIONAL_OR_KEYWORD: "POSITIONAL_OR_KEYWORD",
        inspect.Parameter.VAR_POSITIONAL: "VAR_POSITIONAL",
        inspect.Parameter.KEYWORD_ONLY: "KEYWORD_ONLY",
        inspect.Parameter.VAR_KEYWORD: "VAR_KEYWORD",
    }

    for param_info in param_list:
        if not isinstance(param_info, dict): continue # Skip invalid entries

        # Parameter kind might be stored as string or enum value, handle both
        kind_val = param_info.get('kind')
        kind_str = kind_map.get(kind_val, str(kind_val).split('.')[-1] if kind_val else "UNKNOWN") # Convert enum or use string

        default_val = param_info.get('default')
        default_str = str(default_val) if default_val is not inspect.Parameter.empty and default_val is not None else None

        ir_params.append(IRParameter(
            name=param_info.get('name', 'unknown'),
            annotation=param_info.get('annotation'),
            default_value=default_str,
            kind=kind_str
        ))
    return ir_params

def _convert_decorators(decorators_list: List[Dict[str, Any]]) -> List[str]:
    """Converts a list of decorator dictionaries to a list of strings (decorator names)."""
    # Represent decorators simply as their names for now
    return [d.get('name', 'unknown_decorator') for d in decorators_list if isinstance(d, dict)]


# --- Component Conversion Functions (Updated to accept dictionaries) ---

def convert_function_to_ir(func_dict: Dict[str, Any]) -> IRFunction:
    """Converts a function/method dictionary to an IRFunction."""
    fqn = func_dict.get('fully_qualified_name', 'unknown_function')
    logger.debug(f"Converting function/method dict to IR: {fqn}")
    return IRFunction(
        name=func_dict.get('name', 'unknown'),
        qualified_name=fqn,
        location=_convert_location(func_dict),
        docstring=func_dict.get('docstring'),
        metadata=_convert_metadata(func_dict),
        parameters=_convert_parameters(func_dict),
        return_annotation=func_dict.get('returns'),
        is_async=func_dict.get('is_async', False),
        decorators=_convert_decorators(func_dict.get('decorators', []))
    )

def convert_class_to_ir(cls_dict: Dict[str, Any]) -> IRClass:
    """Converts a class dictionary to an IRClass."""
    fqn = cls_dict.get('fully_qualified_name', 'unknown_class')
    logger.debug(f"Converting class dict to IR: {fqn}")

    # Recursively convert methods and nested classes from their dictionary representations
    methods_ir = [convert_function_to_ir(m) for m in cls_dict.get('methods', []) if isinstance(m, dict)]
    nested_classes_ir = [convert_class_to_ir(nc) for nc in cls_dict.get('nested_classes', []) if isinstance(nc, dict)]
    # TODO: Add conversion for class_variables if they are included and needed in IR

    return IRClass(
        name=cls_dict.get('name', 'unknown'),
        qualified_name=fqn,
        location=_convert_location(cls_dict),
        docstring=cls_dict.get('docstring'),
        metadata=_convert_metadata(cls_dict),
        base_classes=cls_dict.get('base_fqns') or cls_dict.get('bases', []), # Prefer FQNs
        methods=methods_ir,
        nested_classes=nested_classes_ir,
        # class_variables=[convert_variable_to_ir(cv) for cv in cls_dict.get('class_variables', [])], # Needs IRVariable conversion
    )

# Placeholder for variable conversion if needed
# def convert_variable_to_ir(var_dict: Dict[str, Any]) -> IRVariable:
#     # Implementation depends on how variables are represented in the dict
#     pass


# --- Module Conversion Function (New Entry Point) ---

def convert_analysis_result_to_ir(analysis_result: Dict[str, Any], config: Optional[AnalysisConfig] = None) -> Optional[IRModule]:
    """
    Converts the analysis result dictionary from CodeVisitor into an IRModule.
    Includes refined re-export detection using the imported_names_map.
    Validates the created IRModule before returning.
    """
    
    module_name = analysis_result.get("module_name", "unknown_module")
    source_file = analysis_result.get("source_file")
    components_dict = analysis_result.get("components", {})
    module_interface = analysis_result.get("module_interface", {})
    imported_names_map = analysis_result.get("imported_names_map", {}) # <<< Get the map

    ir_components = []
    for comp_fqn, comp_dict in components_dict.items():
        comp_type = comp_dict.get("type")
        try:
            if comp_type == "function" or comp_type == "method": # Treat methods as functions in IR for now
                ir_components.append(convert_function_to_ir(comp_dict))
            elif comp_type == "class":
                ir_components.append(convert_class_to_ir(comp_dict))
            # Add variable conversion if needed
            # elif comp_type == "variable":
            #     ir_components.append(convert_variable_to_ir(comp_dict))
        except Exception as e:
            logger.error(f"Error converting component {comp_fqn} to IR: {e}", exc_info=True)
            # Optionally add error metadata to the IRModule or skip component

    ir_imports = []
    for imp_dict in module_interface.get("imports", []):
        # Basic conversion, assuming imp_dict matches ImportInfo structure
        ir_imports.append(IRImport(
            source_module=imp_dict.get("source_module_fqn"), # Use resolved FQN
            imported_name=imp_dict.get("imported_name"),
            alias=imp_dict.get("alias"),
            is_relative=imp_dict.get("is_relative", False),
            relative_level=imp_dict.get("relative_level"),
            is_star_import=imp_dict.get("is_wildcard", False),
            location=IRLocation(file_path=source_file, line_start=imp_dict.get("source_line", 0), line_end=imp_dict.get("source_line", 0)) if source_file and imp_dict.get("source_line") else None
        ))

    ir_exports: Optional[List[IRExport]] = None
    if module_interface.get("has_all"):
        ir_exports = []
        static_all_values = module_interface.get("all_values")
        if static_all_values is not None: # Static __all__ list
            for export_name in static_all_values:
                original_fqn = imported_names_map.get(export_name) # <<< Check if name is imported
                is_reexport = original_fqn is not None
                if not is_reexport:
                    # Assume defined locally if not in imported_names_map
                    # Construct potential local FQN
                    original_fqn = f"{module_name}.{export_name}"
                    # Optional: Verify this FQN exists in components_dict?

                ir_exports.append(IRExport(
                    name=export_name,
                    original_qualified_name=original_fqn,
                    is_reexport=is_reexport
                ))
        else: # Dynamic __all__ or error state
            # Cannot reliably determine exports statically for dynamic __all__
            # Option 1: Leave ir_exports as empty list (conservative)
            # Option 2: Add placeholder or metadata indicating dynamic nature
            # Option 3: If dynamic analysis results were included, use them here.
            logger.warning(f"Module {module_name} has dynamic __all__, static IR export list may be incomplete.")
            # For now, leave ir_exports as []

    else:
        # No __all__: Export all non-private top-level components defined locally
        ir_exports = []
        for comp_fqn, comp_dict in components_dict.items():
            # Check if it's a top-level component (parent is the module itself)
            # Heuristic: FQN has only one dot more than module_name, or no dots if module is top-level
            is_top_level = comp_fqn.count('.') == module_name.count('.') + 1 or ('.' not in module_name and '.' not in comp_fqn)

            access_modifier = comp_dict.get("access_modifier", "public")
            comp_name = comp_dict.get("name")

            # Include public components, potentially special based on config (config access needed here)
            # Simplified: Include public only for now
            if is_top_level and access_modifier == "public" and comp_name:
                ir_exports.append(IRExport(
                    name=comp_name,
                    original_qualified_name=comp_fqn, # Defined locally
                    is_reexport=False
                ))


    ir_module = IRModule(
        name=module_name.split('.')[-1],
        qualified_name=module_name,
        location=IRLocation(file_path=source_file, line_start=1, line_end=len(analysis_result.get("code","").splitlines())) if source_file else None, # Approximate location
        docstring=module_interface.get("docstring"),
        imports=ir_imports,
        exports=ir_exports,
        components=ir_components,
        metadata=IRMetadata(custom={"source_file": source_file}) # add source file to metadata
    )
    
    # --- IR Validation Step ---
    # Default to True if config is not provided or doesn't have the setting
    should_validate_ir = getattr(config, 'validate_ir_after_conversion', True) if config else True
    
    if should_validate_ir:
        logger.debug(f"Validating IR for module: {ir_module.qualified_name}")
        try:
            # Use attempt_schema_revalidation=False as it's a newly created object from trusted source
            # Pydantic itself would have raised errors during construction if schema was bad.
            # Semantic validation is the key here.
            validation_issues = fully_validate_irmodule(ir_module, attempt_schema_revalidation=False) 
            if validation_issues:
                logger.warning(
                    f"IRModule for '{ir_module.qualified_name}' failed semantic validation with {len(validation_issues)} issues after conversion:"
                )
                for issue in validation_issues:
                    logger.warning(f"  - {issue}")
                
                # Policy on validation failure:
                # Option 1: Return the (potentially flawed) IR module with logged errors
                # Option 2: Return None
                # Option 3: Raise an exception (e.g., IRValidationError or a custom one)
                # For now, let's go with Option 1 (return flawed module)
                # If stricter handling is needed, AnalysisConfig could control this.
                # For example: if config.raise_on_ir_validation_error: raise IRValidationError(...)
                
            else:
                logger.info(f"IR for module {ir_module.qualified_name} created and validated successfully.")
        except IRValidationError as ive: # Should not happen if attempt_schema_revalidation=False and Pydantic construction passed
            logger.error(f"Schema validation error during IRModule post-validation for {ir_module.qualified_name}: {ive}", exc_info=True)
            # Treat as major validation failure
            return None # Or raise
        except Exception as e:
            logger.error(f"Unexpected error during IR validation for {ir_module.qualified_name}: {e}", exc_info=True)
            # Treat as major validation failure
            return None # Or raise
    
    return ir_module
