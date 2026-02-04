"""
Validation utilities for Intermediate Representation (IR) models.

This module provides functions to validate IRModule instances, including
schema validation (delegated to Pydantic) and more complex semantic
consistency checks.
"""

import logging
from typing import List, Dict, Any, Set, Tuple, Union

from pydantic import ValidationError
from .models import IRModule, IRComponent, IRFunction, IRClass, IRVariable, IRImport, IRExport, IRLocation

logger = logging.getLogger(__name__)

class IRValidationError(ValueError):
    """
    Custom exception for IR validation errors.
    Contains a message and a list of detailed issues.
    """
    def __init__(self, message: str, issues: List[Dict[str, Any]]):
        super().__init__(message)
        self.issues = issues

    def __str__(self) -> str:
        return f"{super().__str__()} Issues: {self.issues}"


def validate_irmodule_schema_from_dict(data: Dict[str, Any]) -> IRModule:
    """
    Validates raw dictionary data against the IRModule Pydantic schema.

    Args:
        data: A dictionary potentially representing an IRModule.

    Returns:
        A validated IRModule instance if data conforms to the schema.

    Raises:
        IRValidationError: If Pydantic validation fails, containing details of the issues.
    """
    try:
        return IRModule.model_validate(data)
    except ValidationError as ve:
        logger.warning(f"IRModule schema validation failed from dict: {ve.errors()}")
        raise IRValidationError(
            message=f"IRModule data failed schema validation. Found {len(ve.errors())} issue(s).",
            issues=ve.errors()
        ) from ve

def _collect_all_components(ir_element: Union[IRModule, IRClass]) -> List[IRComponent]:
    """
    Recursively collects all IRComponent instances from an IRModule or IRClass.
    This includes the element itself (if it's an IRComponent, like IRModule or IRClass)
    and all its nested components.
    """
    components: List[IRComponent] = []
    
    if isinstance(ir_element, IRComponent): # IRModule and IRClass are IRComponents
        components.append(ir_element)

    if hasattr(ir_element, 'components') and ir_element.components: # For IRModule
        for comp in ir_element.components:
            components.extend(_collect_all_components(comp)) # type: ignore
            # comp can be IRFunction, IRClass, IRVariable. If IRClass, it's handled.
            
    if isinstance(ir_element, IRClass): # For IRClass specific nested components
        for method in ir_element.methods:
            components.extend(_collect_all_components(method))
        for var in ir_element.class_variables:
            components.extend(_collect_all_components(var))
        for nested_class in ir_element.nested_classes:
            components.extend(_collect_all_components(nested_class))
            
    return components


def validate_irmodule_semantic(ir_module: IRModule) -> List[str]:
    """
    Performs semantic validation checks on an IRModule instance.
    This goes beyond Pydantic's basic schema validation.

    Args:
        ir_module: The IRModule instance to validate.

    Returns:
        A list of strings, where each string describes a validation issue.
        An empty list indicates no issues were found.
    """
    issues: List[str] = []
    logger.debug(f"Performing semantic validation for IRModule: {ir_module.qualified_name}")

    all_components = _collect_all_components(ir_module)
    all_fqns: Set[str] = set()
    fqn_counts: Dict[str, int] = {}

    for comp in all_components:
        all_fqns.add(comp.qualified_name)
        fqn_counts[comp.qualified_name] = fqn_counts.get(comp.qualified_name, 0) + 1

    # 1. Check for duplicate qualified names
    for fqn, count in fqn_counts.items():
        if count > 1:
            issues.append(
                f"Duplicate qualified name '{fqn}' found {count} times within module '{ir_module.qualified_name}'."
            )

    # 2. Validate individual components
    for component in all_components:
        # Validate location if present
        if component.location:
            loc = component.location
            if loc.line_start > loc.line_end:
                issues.append(
                    f"Component '{component.qualified_name}': Location line_start ({loc.line_start}) "
                    f"is after line_end ({loc.line_end})."
                )
            if loc.col_start is not None and loc.col_end is not None and \
               loc.line_start == loc.line_end and loc.col_start > loc.col_end:
                issues.append(
                    f"Component '{component.qualified_name}': Location col_start ({loc.col_start}) "
                    f"is after col_end ({loc.col_end}) on the same line."
                )
        
        # Validate based on component type (Pydantic's Literal types ensure component_type is valid)
        if isinstance(component, IRFunction):
            if component.component_type != "function": # Should be guaranteed by Pydantic if models are correct
                 issues.append(f"Function '{component.qualified_name}' has incorrect component_type '{component.component_type}'.")
            for param in component.parameters:
                if not param.name.isidentifier(): # Basic check for parameter name
                    issues.append(f"Function '{component.qualified_name}', parameter '{param.name}' is not a valid identifier.")
        
        elif isinstance(component, IRClass):
            if component.component_type != "class":
                 issues.append(f"Class '{component.qualified_name}' has incorrect component_type '{component.component_type}'.")
            for base_fqn in component.base_classes:
                if not all(part.isidentifier() for part in base_fqn.split('.')):
                    issues.append(f"Class '{component.qualified_name}': Base class FQN '{base_fqn}' is not a valid qualified name.")
            # Further checks for methods, class_vars, nested_classes are implicitly handled
            # as they are also in `all_components` and validated individually.

        elif isinstance(component, IRVariable):
            if component.component_type != "variable":
                 issues.append(f"Variable '{component.qualified_name}' has incorrect component_type '{component.component_type}'.")
        
        elif isinstance(component, IRModule): # The top-level module itself
             if component.component_type != "module":
                 issues.append(f"Module '{component.qualified_name}' has incorrect component_type '{component.component_type}'.")


    # 3. Validate IRModule-specific fields (imports, exports)
    if ir_module.imports:
        for imp_item in ir_module.imports:
            if imp_item.is_relative and (imp_item.relative_level is None or imp_item.relative_level < 0):
                issues.append(
                    f"Module '{ir_module.qualified_name}': Relative import '{imp_item.imported_name}' "
                    f"has invalid relative_level ({imp_item.relative_level})."
                )
            if not imp_item.imported_name.isidentifier() and not imp_item.is_star_import and '.' not in imp_item.imported_name : # e.g. os.path
                 issues.append(
                    f"Module '{ir_module.qualified_name}': Imported name '{imp_item.imported_name}' is not a valid identifier or path."
                )
            if imp_item.alias and not imp_item.alias.isidentifier():
                 issues.append(
                    f"Module '{ir_module.qualified_name}': Import alias '{imp_item.alias}' for '{imp_item.imported_name}' is not a valid identifier."
                )

    if ir_module.exports: # `exports` is Optional[List[IRExport]]
        for exp_item in ir_module.exports:
            if not exp_item.name.isidentifier():
                issues.append(
                    f"Module '{ir_module.qualified_name}': Exported name '{exp_item.name}' is not a valid identifier."
                )
            if exp_item.is_reexport:
                if not exp_item.original_qualified_name:
                    issues.append(
                        f"Module '{ir_module.qualified_name}': Re-export '{exp_item.name}' is missing 'original_qualified_name'."
                    )
                elif exp_item.original_qualified_name not in all_fqns:
                    # This is a strict check: assumes re-exported items must be defined (or imported and then defined)
                    # within the module's own component tree. This might not always hold for complex re-exports
                    # that are resolved dynamically or come from star imports not fully expanded in this IR.
                    # For a pure IR validation, checking against known FQNs in *this* IR is a reasonable first step.
                    logger.debug(
                        f"Module '{ir_module.qualified_name}': Re-export '{exp_item.name}' "
                        f"points to '{exp_item.original_qualified_name}', which is not found as a defined FQN in this IR. "
                        f"This could be an item imported and then re-exported without being explicitly defined in the IR's component tree."
                    )
                    # issues.append(
                    #     f"Module '{ir_module.qualified_name}': Re-export '{exp_item.name}' "
                    #     f"points to original_qualified_name '{exp_item.original_qualified_name}' which is not defined within this IR."
                    # )
            # else: # Item is from __all__ but not marked as re-export
            #     if exp_item.name not in all_fqns:
            #          # Similar to above, if __all__ refers to an imported item directly
            #         pass


    if issues:
        logger.warning(
            f"Semantic validation for IRModule '{ir_module.qualified_name}' found {len(issues)} issue(s):"
        )
        for issue in issues:
            logger.warning(f"  - {issue}")
    else:
        logger.info(f"Semantic validation for IRModule '{ir_module.qualified_name}' passed successfully.")

    return issues


def fully_validate_irmodule(ir_module: IRModule, attempt_schema_revalidation: bool = False) -> List[str]:
    """
    Performs comprehensive validation on an IRModule instance.
    This includes optional Pydantic schema re-validation and detailed semantic checks.

    Args:
        ir_module: The IRModule instance to validate.
        attempt_schema_revalidation: If True, tries to re-validate the IRModule
                                     by dumping it to a dict and then parsing it back.
                                     Useful for ensuring data integrity after potential
                                     modifications or if loaded from an untrusted source.

    Returns:
        A list of strings describing validation issues. An empty list means
        the IRModule is considered valid based on the checks performed.
    """
    all_issues: List[str] = []

    if attempt_schema_revalidation:
        try:
            logger.debug(f"Attempting schema re-validation for IRModule: {ir_module.qualified_name}")
            # Dump and re-parse to trigger Pydantic's validation
            validated_module = validate_irmodule_schema_from_dict(ir_module.model_dump())
            if validated_module != ir_module : # pragma: no cover (should ideally be same)
                 logger.warning(f"Schema re-validation of {ir_module.qualified_name} resulted in a different object. This is unexpected.")
                 all_issues.append("Schema re-validation resulted in a non-equivalent object (unexpected).")
            logger.debug(f"Schema re-validation for {ir_module.qualified_name} passed.")
        except IRValidationError as ive:
            logger.warning(f"Schema re-validation failed for {ir_module.qualified_name}: {ive.issues}")
            all_issues.extend([f"Schema issue ({err.get('loc', 'N/A')}): {err.get('msg', 'Unknown')}" for err in ive.issues])
            return all_issues # If schema fails, semantic checks might be unreliable

    semantic_issues = validate_irmodule_semantic(ir_module)
    all_issues.extend(semantic_issues)

    if not all_issues:
        logger.info(f"Full validation for IRModule '{ir_module.qualified_name}' passed successfully.")
    else:
        logger.warning(f"Full validation for IRModule '{ir_module.qualified_name}' found {len(all_issues)} total issues.")
    return all_issues
