"""
AST visitor for comprehensive code analysis with definition registry integration for accurate definition tracking.
Provides thorough analysis of Python source code with component extraction, decorator handling, and relationship tracking.
"""

import os
import ast
import time
import logging
from pathlib import Path
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Any, Tuple, Deque, Union


from .config import AnalysisConfig, default_config
from .graph.models import ImportRecord
from .graph.inheritance_tracker import InheritanceTracker
from .graph.call_graph import CallGraphTracker
from .feature_flags import Feature, is_enabled
from .parameter_analysis import analyze_signature
from .definition_registry import DefinitionRegistry
from .utils import ParseError, AnalysisError, safe_read_file
from .decorator_analysis import DecoratorAnalyzer, DecoratorEffect
from .code_components import CodeComponent, Function, Method, Class, Variable, UnwrappedFunction#, DecoratorInfo


logger = logging.getLogger(__name__)


@dataclass
class AnalysisContext:
    """Context for current analysis state."""
    
    module_name: str
    package_name: str
    config: AnalysisConfig
    in_async_def: bool = False
    in_except_handler: bool = False  # Track if we're inside an except block
    source_file: Optional[str] = None
    current_class: Optional[str] = None
    current_node_type: Optional[str] = None  # To track current AST node type being processed
    exports: Set[str] = field(default_factory=set) # Keep track of explicitly defined names in scope
    scope_stack: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    imported_names: Dict[str, str] = field(default_factory=dict) # Maps local name -> resolved FQN
    
    definition_registry: Optional[DefinitionRegistry] = None
    
    module_docstring: Optional[str] = None
    imports: List[ImportRecord] = field(default_factory=list)
    wildcard_imports: Set[str] = field(default_factory=set) # Stores FQNs of modules imported with *
    has_all: bool = False
    all_is_dynamic: bool = False
    all_values: Optional[Set[str]] = None # Stores names if __all__ is static list/tuple of strings
    needs_dynamic_analysis: bool = False # Flag if dynamic __all__ or importlib is used
    is_init_file: bool = False
    top_level_packages: Set[str] = field(default_factory=set)
    known_modules: Optional[Dict[str, Dict[str, Any]]] = None
    
    all_aggregation_sources: Set[str] = field(default_factory=set)  # tracks module refs like "lib._shape_base_impl"
    
    @property
    def current_scope(self) -> str:
        """Get current scope name."""
        # Ensure scope stack includes module name if empty
        if not self.scope_stack:
            return self.module_name
        # Join module name with scope stack parts
        return '.'.join([self.module_name] + self.scope_stack)

    def enter_scope(self, name: str):
        """Enter a new scope."""
        self.scope_stack.append(name)

    def exit_scope(self):
        """Exit current scope."""
        if self.scope_stack:
            self.scope_stack.pop()

    def _get_module_basename(self) -> str:
        """
        Get the base name of the current module (last part of the module path).
        
        Returns:
            Base name of the module
        """
        return self.module_name.rsplit('.', 1)[-1] if '.' in self.module_name else self.module_name
    
    def get_fully_qualified_name(self, name: str) -> str:
        """
        Get fully qualified name that accurately represent code structure.
        
        This method preserves the true import path of components, including only legitimate duplications (context-aware duplicate prevention) that exist in the code structure.
        
        Args:
            name: Name to qualify
            
        Returns:
            Fully qualified name 
        """
        
        if not name:
            logger.warning(f"Attempted to get FQN for empty name in module {self.module_name}")
            return self.module_name # Return at least the module name
        
        # Build the base path using the current scope
        scope_path = self.current_scope

        # Handle potential duplication (e.g., module name same as function name)
        path_parts = scope_path.split('.')
        if path_parts and path_parts[-1] == name:
            # Potential duplication: module_name.name or class_name.name
            # Let's refine the logic based on context
            
            # Get file and module information
            module_basename = self._get_module_basename()
            file_basename = None
            is_init_file = False
            if self.source_file:
                file_basename = os.path.splitext(os.path.basename(self.source_file))[0]
                is_init_file = os.path.basename(self.source_file) == '__init__.py'

            # Case 1: Legitimate module-function duplication (e.g., data_parallel.py has data_parallel())
            # Allow duplication if name matches module/file base name AND it's not an __init__.py
            if module_basename == name and file_basename == name and not is_init_file:
                return f"{scope_path}.{name}" # Append the name

            # Case 2: Component defined in __init__.py with same name as parent package/module
            # Allow duplication if defined here (not just imported)
            if is_init_file and name in self.exports: # Check if defined in current scope
                return f"{scope_path}.{name}"

            # Case 3: Class method or nested class with same name as outer class
            if self.current_class and path_parts[-1] == self.current_class and name == self.current_class:
                # This check might be too simple, but aims to prevent Class.Class
                # If 'name' is a method/nested class, it should be appended
                # Let's assume if we are inside a class scope, appending is correct
                return f"{scope_path}.{name}"


            # Default: Avoid duplication if name matches last part of scope path
            # This covers cases like ClassName defined in module ModuleName where scope is ModuleName.ClassName and we are trying to qualify ClassName again.
            return scope_path

        # Normal case: Append name to the current scope path
        result = f"{scope_path}.{name}" if scope_path else name
        
        if not result:
            logger.warning(f"Generated empty FQN for name '{name}' in module {self.module_name}")
            # Fallback to module_name.name as a last resort
            return f"{self.module_name}.{name}"

        return result


class CodeVisitor(ast.NodeVisitor):
    """Visits AST nodes to extract code components, relationships, and module interface details."""

    # import_tracker: Optional[ImportTracker]
    # export_tracker: Optional[ExportTracker]
    inheritance_tracker: Optional[InheritanceTracker]
    call_tracker: Optional[CallGraphTracker]

    def __init__(self,
                 code: str,
                 module_name: str,
                 package_name: str,
                 source_file: Optional[str] = None,
                 config: Optional[AnalysisConfig] = None,
                 definition_registry: Optional['DefinitionRegistry'] = None,
                 inheritance_tracker: Optional[InheritanceTracker] = None,
                 call_tracker: Optional[CallGraphTracker] = None,
                 top_level_packages: Optional[Set[str]] = None,
                 known_modules: Optional[Dict[str, Dict[str, Any]]] = None):
        """
        Initialize the visitor.
        
        Args:
            code: Source code to analyze
            module_name: Module name
            package_name: Package name
            source_file: Source file path
            config: Analysis configuration
            definition_registry: Optional definition registry for direct integration
            inheritance_tracker: Optional inheritance tracker for inheritance relationships
            call_tracker: Optional call graph tracker for call relationships
            top_level_packages: Optional set of top-level package/module names.
            known_modules: Optional dictionary of known packages and modules in the codebase under analysis.
        """
        
        self.code = code
        self.source_file = source_file
        self.config = config or default_config
        self.definition_registry = definition_registry
        self.inheritance_tracker = inheritance_tracker
        self.call_tracker = call_tracker
        self.components: Dict[str, CodeComponent] = {}
        self.classes: Dict[str, Class] = {}
        self.current_component: Optional[CodeComponent] = None
        self.decorator_analyzer = DecoratorAnalyzer()
        self.last_line: int = 0
        self._current_lineno: Optional[int] = None # Track line numbers
        self._end_lineno: Optional[int] = None   # Track end line numbers
        self.imports: List[ImportRecord] = [] # Keep track of imports
        self.nested_classes: Dict[str, List[str]] = defaultdict(list) # track nested classes
        self.method_overrides: Dict[str, Dict[str, str]] = defaultdict(dict) # track method overrides
        
        self.context = AnalysisContext(
            module_name=module_name,
            package_name=package_name,
            config=self.config or AnalysisConfig(),
            source_file=source_file,
            definition_registry=definition_registry,
            is_init_file = os.path.basename(source_file) == "__init__.py" if source_file else False,
            top_level_packages=top_level_packages or set(),
            known_modules=known_modules
        )
        # Flag to indicate module needs linking (export expansion) if wildcard import is present and it implicitly exports members
        self.module_needs_linking = False
        
        # Parse the code into AST
        try:
            self.tree = ast.parse(self.code, filename=self.source_file or '<unknown>')
            self.visit(self.tree)
            # Note: _post_process_components moved to get_analysis_results
        
        except SyntaxError as e:
            self.context.errors.append({
                "type": "SyntaxError",
                "message": str(e),
                "file": self.source_file,
                "line": e.lineno,
            })
            logger.error(f"Syntax error parsing {self.source_file or module_name}: {e}")
            
        except Exception as e:
            self.context.errors.append({
                "type": "AnalysisError",
                "message": f"Unexpected error during AST visit: {e}",
                "file": self.source_file,
                "line": getattr(e, 'lineno', None), # Try to get line number if available
            })
            logger.error(f"Error visiting AST for {self.source_file or module_name}: {e}", exc_info=True)


    def visit_Module(self, node: ast.Module) -> None:
        """Visit the root Module node."""
        self.context.module_docstring = ast.get_docstring(node)
        self.generic_visit(node) # Continue traversal

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """
        Track when we're inside an except handler block.
        
        This allows us to identify fallback class definitions like:
            try:
                from sklearn.base import ClassifierMixin as XGBClassifierBase
            except ImportError:
                class XGBClassifierBase:  # This is a fallback
                    pass
        """
        prev_in_except = self.context.in_except_handler
        self.context.in_except_handler = True
        try:
            self.generic_visit(node)
        finally:
            self.context.in_except_handler = prev_in_except
    
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Process class definition, adding nodes/edges to graph tracker."""
        
        prev_class = self.context.current_class # Store previous class context
        
        try:
            logger.debug(f"Processing class: {node.name}")
            self._update_line_numbers(node) # Update line numbers
            
            # Track node type for context-aware name resolution
            prev_node_type = self.context.current_node_type
            self.context.current_node_type = 'class'
            
            # FQN and Parent Calculation
            class_name = node.name
            component_fqn = self.context.get_fully_qualified_name(class_name)
            parent_fqn = self.context.current_scope # Parent is the current scope (module or outer class)
            definition_module_fqn = self.context.module_name # Defined in this module
            
            # Add to defined names in context
            self.context.exports.add(class_name)
            
            # --- PREPARE CLASS COMPONENT ---
            decorator_effect = self.decorator_analyzer.analyze_decorators(node)
            base_names, base_fqns = self._process_bases(node) # Ensure this resolves FQNs
            is_nested_class = bool(prev_class) # Determine nesting based on prev_class
            
            class_component = Class(
                name=class_name,
                body=ast.get_source_segment(self.code, node) or "",
                fully_qualified_name=component_fqn,
                docstring=ast.get_docstring(node),
                source_file=self.source_file,
                line_number=self._current_lineno,
                end_line=self._end_lineno,
                bases=base_names,
                base_fqns=base_fqns,
                is_nested=is_nested_class,
                outer_class=prev_class if is_nested_class else None, # Set outer_class only if nested
                parent_fqn=parent_fqn if parent_fqn != definition_module_fqn else None, # Avoid self-reference for top-level
                is_public=self._is_public_component(class_name), # Re-check publicity later based on __all__
                definition_module_fqn=definition_module_fqn,
                is_exception_fallback=self.context.in_except_handler
            )
            
            # Add is_init_file context to metadata
            class_component.metadata['is_defined_in_init'] = self.context.is_init_file
            
            # Store decorator information in metadata
            if decorator_effect and decorator_effect.decorator_chain:
                class_component.metadata['decorator_chain'] = decorator_effect.decorator_chain
            if decorator_effect and decorator_effect.modified_name:
                class_component.metadata['modified_name'] = decorator_effect.modified_name

            # --- STORE CLASS COMPONENT (in self.classes and self.components) ---
            # Store class before visiting methods so context is available
            self.classes[component_fqn] = class_component # Store in self.classes dictionary
            self.components[component_fqn] = class_component  # also store in general components dict

            # --- Add Inheritance Relationships ---
            if self.inheritance_tracker:
                for base_fqn in base_fqns:
                    if base_fqn: # Ensure base FQN was resolved
                        try:
                            self.inheritance_tracker.add_inheritance(
                                child_fqn=component_fqn,
                                parent_fqn=base_fqn,
                                metadata={'module_path': self.context.module_name}
                            )
                        except Exception as e:
                            logger.error(f"Error adding inheritance {component_fqn} -> {base_fqn}: {e}", exc_info=True)
                            self.context.errors.append({
                                "type": "InheritanceTrackingError",
                                "message": f"Failed to add inheritance: {e}",
                                "child": component_fqn,
                                "parent": base_fqn,
                                "file": self.source_file,
                                "line": node.lineno,
                            })

            # --- Register definition ---
            if self.definition_registry and not self._is_imported_name(node.name):
                reg_metadata = {'base_fqns': base_fqns, 'is_defined_in_init': self.context.is_init_file}
                try:
                    self.definition_registry.register_definition(
                        module_name=self.context.module_name,
                        simple_name=node.name,
                        fully_qualified_name=component_fqn,
                        component_type='class',
                        line_number=node.lineno,
                        source_file=self.source_file,
                        # ast_node=node,
                        metadata=reg_metadata
                    )
                    logger.debug(f"Registered Class Def: {component_fqn}")
                except Exception as reg_e:
                     self._handle_error(reg_e, f"registering definition for class {component_fqn}")

            # Visit methods and nested classes
            self.context.enter_scope(class_name)
            self.context.current_class = component_fqn # Store FQN of current class
            self.context.current_node_type = 'class' # Ensure node type is set for children
            
            # Process body
            for child_node in node.body:
                self.visit(child_node)
                
            self.context.exit_scope()
            self.context.current_class = prev_class # Restore outer class context
            self.context.current_node_type = prev_node_type # Restore node type


        except Exception as e:
            self._handle_error(e, f"processing class {node.name}")
            # Ensure context is reset even on error
            self.context.current_class = prev_class
            if self.context.scope_stack and self.context.scope_stack[-1] == node.name:
                self.context.exit_scope() # Make sure scope is exited if entered
            self.context.current_node_type = prev_node_type
            
        
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Process function definition with definition registry integration."""
        
        # Track node type
        prev_node_type = self.context.current_node_type
        self.context.current_node_type = 'function' # Or 'method' depending on context
        
        try:
            self._update_line_numbers(node)
            # Trigger dynamic analysis flag if module-level lazy export hooks are present
            if not self.context.current_class and not self.context.scope_stack and node.name in ('__getattr__', '__dir__'):
                self.context.needs_dynamic_analysis = True
                logger.info(f"Dynamic analysis needed due to module-level {node.name} in {self.context.module_name}")
            
            # Process function/method details first (creates component object, etc.)
            # This call now handles graph population, etc.
            self._process_function_or_method(node)

            # Visit function body after processing the definition
            self.context.enter_scope(node.name)
            original_component = self.current_component # Store component before visiting body
            self.generic_visit(node) # Visit the function body for calls etc.
            self.current_component = original_component # Restore component context
            self.context.exit_scope()
                    
            
        except Exception as e:
            # Log error but don't re-raise
            self._handle_error(e, f"processing function {node.name}")
            # Ensure scope is exited on error
            if self.context.scope_stack and self.context.scope_stack[-1] == node.name:
                self.context.exit_scope()
        
        finally:
            # Reset node type when done
            self.context.current_node_type = prev_node_type

    
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Process async function definition."""
        
        # Track node type
        prev_node_type = self.context.current_node_type
        self.context.current_node_type = 'function' # Treat as function for FQN
        
        was_async = self.context.in_async_def
        self.context.in_async_def = True
        
        try:
            self._update_line_numbers(node)
            self._process_function_or_method(node)
            
            # Visit function body *after* processing the definition
            self.context.enter_scope(node.name)
            original_component = self.current_component # Store component before visiting body
            self.generic_visit(node) # Visit the function body
            self.current_component = original_component # Restore component context
            self.context.exit_scope()
        
        except Exception as e:
            # Log error but don't re-raise
            self._handle_error(e, f"processing async function {node.name}")
            # Ensure scope is exited on error
            if self.context.scope_stack and self.context.scope_stack[-1] == node.name:
                self.context.exit_scope()
        
        finally:
            # Reset node type and restore previous async state
            self.context.in_async_def = was_async
            self.context.current_node_type = prev_node_type

    
    def _is_imported_name(self, name: str) -> bool:
        """Check if a name is imported rather than defined in this module."""
        return (hasattr(self.context, 'imported_names') and name in self.context.imported_names)
    
    
    def _process_function_or_method(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        """Process function or method definition, adding nodes/edges to graph tracker."""
        try:
            # if not self._is_public_component(node.name):
            #     logger.debug(f"Skipping non-public function/method: {node.name}")
            #     return
            
            # Analyze decorators
            decorator_effect = self.decorator_analyzer.analyze_decorators(node)
            
            # --- FQN and Parent Calculation ---
            component_fqn = self.context.get_fully_qualified_name(node.name)
            parent_component_fqn = self.context.current_scope # Class FQN if method, Module FQN if function
            is_method = bool(self.context.current_class)
            component_type = 'method' if is_method else 'function'
            
            # --- Register Definition with FQN ---
            definition_registered = False
            if self.context.definition_registry and not self._is_imported_name(node.name):
                try:
                    reg_metadata = {
                        'docstring': ast.get_docstring(node),
                        'is_async': isinstance(node, ast.AsyncFunctionDef),
                        'class_name': self.context.current_class, # Store class context if method
                        'is_defined_in_init': self.context.is_init_file # Add context
                    }
                    line_num = getattr(node, 'lineno', self._current_lineno or 0)
                    self.definition_registry.register_definition(
                        module_name=self.context.module_name,
                        simple_name=node.name,
                        fully_qualified_name=component_fqn,
                        component_type=component_type,
                        line_number=line_num,
                        # ast_node=node,
                        source_file=self.context.source_file,
                        # confidence=1.0,
                        metadata=reg_metadata
                    )
                    
                    definition_registered = True
                    logger.debug(f"Registered {component_type} Def: {component_fqn}")
                except Exception as reg_e:
                     self._handle_error(reg_e, f"registering definition for {component_type} {component_fqn}")
            
            # --- Determine Definition Module FQN ---
            # Definition module is always the current module being processed
            definition_module_fqn = self.context.module_name
            
            if is_method:
                # Use self.context.current_class which should hold the FQN of the containing class
                class_fqn_to_lookup = self.context.current_class
                if class_fqn_to_lookup and class_fqn_to_lookup in self.classes:
                    # Process as method, passing FQN info
                    self._process_method(node, self.classes[class_fqn_to_lookup], component_fqn, class_fqn_to_lookup, definition_module_fqn)
                else:
                    logger.warning(f"Containing class FQN '{class_fqn_to_lookup}' not found in self.classes for method {node.name}")
            else:
                # Process as function, passing FQN info
                # Parent FQN for top-level function is module FQN, but often represented as None or empty string for parent link
                func_parent_fqn = parent_component_fqn if parent_component_fqn != definition_module_fqn else None
                self._process_function(node, decorator_effect, component_fqn, func_parent_fqn, definition_module_fqn)

            # Set current_component *after* creating it in _process_method/_process_function
            if component_fqn in self.components:
                self.current_component = self.components[component_fqn]

        except Exception as e:
            self._handle_error(e, f"processing function/method {node.name}")

    
    def _process_method(self, 
                       node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
                       class_component: Class,
                       component_fqn: str,
                       parent_fqn: str,
                       definition_module_fqn: str) -> None:
        """Process method definition with decorator handling."""
        
        try:
            self._update_line_numbers(node)
            
            # Analyze decorators with decorator effect tracking
            decorator_effect = self.decorator_analyzer.analyze_decorators(node)
            
            # Determine method type
            is_static = any(d.name == 'staticmethod' for d in decorator_effect.decorator_chain)
            is_class = any(d.name == 'classmethod' for d in decorator_effect.decorator_chain)
            is_property = any(d.name == 'property' for d in decorator_effect.decorator_chain)
            is_abstract = any(d.name == 'abstractmethod' for d in decorator_effect.decorator_chain)
            
            # Get signature and parameter information
            signature, parameters = list(map(analyze_signature(node, include_instance_var=not is_static).get, ['signatures', 'parameters']))
            
            # Special handling for constructor (__init__, __new__)
            is_constructor = node.name == '__init__' or node.name == '__new__'
            
            # Check for property variations
            property_type = None
            if is_property:
                property_type = 'getter'
            elif any(d.name in {'setter', 'deleter'} for d in decorator_effect.decorator_chain):
                # Find the specific property decorator name
                prop_decorator = next((d for d in decorator_effect.decorator_chain if d.name in {'setter', 'deleter'}), None)
                if prop_decorator:
                    property_type = prop_decorator.name # Use 'setter' or 'deleter'
                    is_property = True # Mark as property if setter/deleter found
            
            # Check for method override
            is_override = False
            base_method = None
            # Check base classes stored in the class component
            for base_fqn in class_component.base_fqns:
                if base_fqn in self.classes: # Check against classes found *so far*
                    base_class_comp = self.classes[base_fqn]
                    # Check methods of the base class component
                    for method_comp in base_class_comp.methods:
                        if method_comp.name == node.name:
                            is_override = True
                            base_method = method_comp # Store the base method component
                            # Track override
                            if base_class_comp.name not in self.method_overrides:
                                self.method_overrides[base_class_comp.name] = {}
                            self.method_overrides[base_class_comp.name][node.name] = class_component.name
                            break # Found override, no need to check other methods in this base
                    if is_override: break # Found override, no need to check other base classes
            
            # Track name change decorators
            modified_name = decorator_effect.modified_name # Get from effect object
            
                            
            # Create method component
            method = Method(
                name=node.name,
                body=ast.get_source_segment(self.code, node),
                fully_qualified_name=component_fqn,
                docstring=ast.get_docstring(node),
                source_file=self.context.source_file,
                line_number=self._current_lineno,
                end_line=self._end_lineno,
                returns=ast.unparse(node.returns) if node.returns else None,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                signature=signature or {},
                parameters = parameters or [],
                decorator_chain=decorator_effect.decorator_chain,
                class_name=class_component.name,
                is_static=is_static,
                is_property=is_property,
                property_type=property_type,
                is_abstract=is_abstract,
                is_override=is_override,
                base_method=base_method,
                parent_fqn=parent_fqn,
                definition_module_fqn=definition_module_fqn,
                is_public=self._is_public_component(node.name)
            )
            
            # Add is_init_file context to metadata
            method.metadata['is_defined_in_init'] = self.context.is_init_file
            
            # Store modified name if found
            if modified_name:
                method.metadata['modified_name'] = modified_name
            
            # Add unwrapped function analysis if enabled and decorators are present
            if self.context.config.unwrap_decorators and decorator_effect.decorator_chain:
                self._process_unwrapped_function(node, method, decorator_effect)
            
            # Add to class - special handling for constructors
            if is_constructor:
                # For constructors, prioritize __init__ over __new__
                if node.name == '__init__' or class_component.constructor is None:
                    class_component.constructor = method
                    logger.debug(f"Found constructor {node.name} for class {class_component.name}")
            else:
                class_component.methods.append(method)
            
            # Store in main components dict
            self.components[component_fqn] = method
            
            # Track abstract methods
            if is_abstract:
                class_component.abstract_methods.add(node.name)
                
            # Track property information
            if is_property or property_type:
                if node.name not in class_component.property_info:
                    class_component.property_info[node.name] = {}
                class_component.property_info[node.name][property_type or 'getter'] = method
                
        except Exception as e:
            self._handle_error(e, f"processing method {node.name}")

    
    def _process_function(self,
                        node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
                        decorator_effect: DecoratorEffect,
                        component_fqn: str,
                        parent_fqn: Optional[str], # Can be None for top-level functions
                        definition_module_fqn: str) -> None:
        """Process function definition with UnwrappedFunction integration."""
        
        try:
            self._update_line_numbers(node)
            # Get signature and parameter information
            signature, parameters = list(map(analyze_signature(node, include_instance_var=bool(self.context.current_class)).get,['signatures', 'parameters']))
            
            function = Function(
                name=node.name,
                body=ast.get_source_segment(self.code, node) or "",
                fully_qualified_name=component_fqn,
                docstring=ast.get_docstring(node),
                source_file=self.context.source_file,
                line_number=self._current_lineno,
                end_line=self._end_lineno,
                returns=ast.unparse(node.returns) if node.returns else None,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                signature=signature or {},
                parameters = parameters or [],
                decorator_chain=decorator_effect.decorator_chain,
                parent_fqn=parent_fqn, # Module FQN or None
                definition_module_fqn=definition_module_fqn,
                is_public=self._is_public_component(node.name)
            )
            
            # Add is_init_file context to metadata
            function.metadata['is_defined_in_init'] = self.context.is_init_file
            
            # Add modified name from decorator effect
            if decorator_effect.modified_name:
                function.metadata['modified_name'] = decorator_effect.modified_name
            
            # Add unwrapped function analysis if enabled and decorators are present
            if self.context.config.unwrap_decorators and decorator_effect.decorator_chain:
                self._process_unwrapped_function(node, function, decorator_effect)
            
            # Add to components
            self.components[component_fqn] = function # Add to main components dictionary
            
        except Exception as e:
            self._handle_error(e, f"processing function {node.name}")
    
    
    def _process_bases(self, node: ast.ClassDef) -> Tuple[List[str], List[str]]:
        """
        Process base classes to extract names and preliminary FQNs.
        
        The FQNs generated here are preliminary estimates based on:
            - For simple names (ast.Name): Assume local to module
            - For attribute chains (ast.Attribute): Preserve the chain as-is
            - For complex expressions: Use string representation
        
        These FQNs will be corrected by _finalize_all_base_fqns() in AnalyzerIntegration 
        after all modules are analyzed and import records are finalized.
        
        Args:
            node: Class definition node
            
        Returns:
            Tuple of (base class names as written, preliminary FQNs)
        """
        bases = []
        base_fqns = []
        
        for base in node.bases:
            try:
                base_str = ast.unparse(base)
                bases.append(base_str)
                
                preliminary_fqn = None
                
                if isinstance(base, ast.Name):
                    # Simple name: MyClass, ABC, etc.
                    # Store as local FQN - will be corrected if actually imported
                    preliminary_fqn = f"{self.context.module_name}.{base.id}"
                    
                elif isinstance(base, ast.Attribute):
                    # Attribute chain: module.Class, pkg.mod.Class, etc.
                    # Extract the full chain as a preliminary FQN
                    parts = []
                    value = base
                    while isinstance(value, ast.Attribute):
                        parts.insert(0, value.attr)
                        value = value.value
                    
                    if isinstance(value, ast.Name):
                        parts.insert(0, value.id)
                        # Store the full chain - will be resolved later
                        preliminary_fqn = '.'.join(parts)
                    else:
                        # Complex base object (e.g., func().attr)
                        preliminary_fqn = f"{self.context.module_name}.{base_str}"
                        
                else:
                    # Complex base (Subscript like Generic[T], Call, etc.)
                    # Can't resolve FQN for these - store with module prefix
                    preliminary_fqn = f"{self.context.module_name}.{base_str}"
                
                base_fqns.append(preliminary_fqn if preliminary_fqn else base_str)
                
            except Exception as e:
                self._handle_error(e, f"processing base class {ast.dump(base)}")
                base_fqns.append(ast.unparse(base))
        
        return bases, base_fqns
    
    
    def _is_public_component(self, name: str) -> bool:
        """
        Check if component should be included in public API based on naming conventions.
        NOTE: Final decision depends on __all__ if present.
        
        Args:
            name: Component name to check
            
        Returns:
            True if component seems public based on name
        """
        
        # Check access modifier
        access = CodeComponent.get_access_modifier(name)
        
        # Handle special methods
        if access == "special":
            if not self.context.config.include_special_members:
                return name in self.context.config.special_whitelist
            return True
        
        # Exclude private and protected by default
        if access == "protected":
            if not self.context.config.include_special_members:
                return name in self.context.config.special_whitelist
            return False
        if access == "private":
            return False
        
        # Default to public for normal names
        return access == "public"
            

    def _update_line_numbers(self, node: ast.AST):
        """Update current line number tracking."""
        self._current_lineno = getattr(node, 'lineno', None)
        self._end_lineno = getattr(node, 'end_lineno', None)

    def _handle_error(self, error: Exception, context: str):
        """Handle analysis error."""
        
        error_msg = f"Error in {context}: {str(error)}"
        logger.error(error_msg, exc_info=True) # Log with traceback
        
        if self.context.config.strict_mode:
            # In strict mode, wrap the original error
            raise AnalysisError(error_msg) from error
        
        # Record non-fatal error
        self.context.errors.append({
            'context': context,
            'error': str(error),
            'type': type(error).__name__,
            'line': self._current_lineno
        })

    
    def _process_unwrapped_function(self, 
                              node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
                              function: Function,
                              decorator_effect: DecoratorEffect) -> None:
        """Process unwrapped function analysis for decorated functions."""
        
        try:
            # Extract original name if it differs from the implementation name
            original_name = None
            for decorator in decorator_effect.decorator_chain:
                if hasattr(decorator, 'original_name') and decorator.original_name:
                    original_name = decorator.original_name
                    break
            
            # Check if any decorators affect signature
            has_signature_affecting_decorators = any(
                getattr(d, 'affects_signature', False) 
                for d in decorator_effect.decorator_chain
            )
            
            # Check if any decorators affect name
            affects_name = any(
                getattr(d, 'affects_name', False)
                for d in decorator_effect.decorator_chain
            )
            
            if has_signature_affecting_decorators or affects_name:
                # Create a copy of the node without decorators
                import copy
                undecorated_node = copy.deepcopy(node)
                undecorated_node.decorator_list = []
                
                # Generate unwrapped signature
                try:
                    unwrapped_signature = analyze_signature(
                        undecorated_node, 
                        include_instance_var=False
                    )['signatures']
                except Exception as e:
                    logger.debug(f"Error generating unwrapped signature: {e}")
                    unwrapped_signature = {
                        'default': f"{node.name}({', '.join(arg.arg for arg in node.args.args)})"
                    }
                
                # Create preserved attributes
                preserved_attrs = {
                    '__doc__': function.docstring,
                    '__name__': function.name,
                    '__qualname__': function.fully_qualified_name
                }
                if node.returns:
                    preserved_attrs['__annotations__'] = {'return': ast.unparse(node.returns)}
                
                # Create UnwrappedFunction data
                function.unwrapped_function = UnwrappedFunction(
                    original_func=None,
                    unwrapped_func=None,
                    decorator_chain=decorator_effect.decorator_chain,
                    preserved_attributes=preserved_attrs,
                    original_signature=None,
                    modified_signature=None
                )
                
                # Store unwrapped signature for documentation
                function.metadata['unwrapped_signature'] = unwrapped_signature
                if original_name:
                    function.unwrapped_function.original_name = original_name
                    function.metadata['original_name'] = original_name
                
        except Exception as e:
            logger.debug(f"Failed to process unwrapped function {node.name}: {e}")
            # Don't re-raise - this is optional enhancement

    
    def visit_Import(self, node: ast.Import) -> None:
        """Visit 'import module' or 'import module as alias'."""
        
        self._update_line_numbers(node)
        line_num = self._current_lineno
        
        for alias_node in node.names:
            raw_module_specifier = alias_node.name
            raw_alias = alias_node.asname

            # --- Determine if internal ---
            is_internal = False
            if '.' in raw_module_specifier:
                root_package = raw_module_specifier.split('.')[0]
                is_internal = root_package in self.context.top_level_packages
            else:
                is_internal = raw_module_specifier in self.context.top_level_packages
            # ---
            
            # --- Resolve FQNs ---
            # For 'import X.Y.Z', the source and imported entity are the module X.Y.Z itself.
            source_module_fqn = raw_module_specifier
            imported_entity_fqn = raw_module_specifier

            # --- Determine name_bound_in_importer and name_bound_points_to_fqn ---
            if raw_alias:
                name_bound_in_importer = raw_alias
                name_bound_points_to_fqn = imported_entity_fqn # Alias points to the full module FQN
            else:
                # For 'import a.b.c', 'a' is bound in the scope, pointing to FQN 'a'.
                # For 'import a', 'a' is bound, pointing to FQN 'a'.
                name_bound_in_importer = raw_module_specifier.split('.')[0]
                name_bound_points_to_fqn = name_bound_in_importer # The first part is the FQN of the package/module made available

            import_record = ImportRecord(
                importer_module_fqn=self.context.module_name,
                line_number=line_num,
                raw_module_specifier=raw_module_specifier,
                raw_imported_name=raw_module_specifier, # For ast.Import, module path is the "name"
                raw_alias=raw_alias,
                is_relative=False,
                level=0,
                is_wildcard=False,
                source_module_fqn=source_module_fqn,
                imported_entity_fqn=imported_entity_fqn,
                is_source_internal=is_internal,
                name_bound_in_importer=name_bound_in_importer,
                name_bound_points_to_fqn=name_bound_points_to_fqn
            )
            
            self.imports.append(import_record)
            self.context.imports.append(import_record)
            
            # Classify the import kind
            self._classify_import_record(import_record)

            # Update imported_names: local name -> FQN it points to
            self.context.imported_names[name_bound_in_importer] = name_bound_points_to_fqn
            logger.debug(
                f"In {self.context.module_name}: Processed import: '{name_bound_in_importer}' points to '{name_bound_points_to_fqn}' "
                f"(raw: import {raw_module_specifier}{' as ' + raw_alias if raw_alias else ''})"
            )

        self.generic_visit(node)
    
    
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Visit 'from module import name' or 'from .module import name'."""
        
        self._update_line_numbers(node)
        line_num = self._current_lineno
        
        level = node.level
        # node.module can be None for 'from . import something'
        raw_module_text_from_statement = node.module # e.g., "my_package.utils", ".utils", None

        # Resolve the source module FQN
        resolved_source_module_fqn = self._resolve_relative_import(raw_module_text_from_statement, level)

        # --- Determine if internal ---
        is_internal_source = False
        if level > 0: # Relative imports are always internal to the project
            is_internal_source = True
        elif resolved_source_module_fqn:
            root_package = resolved_source_module_fqn.split('.')[0]
            is_internal_source = root_package in self.context.top_level_packages
        # ---
        
        if not resolved_source_module_fqn:
            error_module_text = f"{'.' * level}{raw_module_text_from_statement or ''}"
            logger.warning(
                f"Could not resolve source module for import: from {error_module_text} "
                f"in {self.context.module_name} at line {line_num}. Using placeholder."
            )
            self.context.errors.append({
                "type": "ImportResolutionError",
                "message": f"Could not resolve source module for 'from {error_module_text} import ...'",
                "file": self.source_file, "line": line_num,
            })
            # Use a placeholder if resolution fails to avoid None issues later
            resolved_source_module_fqn = f"UNKNOWN_MODULE_L{level}_{raw_module_text_from_statement or '?'}" # Placeholder


        for alias_node in node.names:
            raw_imported_item_name = alias_node.name  # e.g., "my_function", "*"
            raw_item_alias = alias_node.asname      # e.g., "mf", None

            is_wildcard = (raw_imported_item_name == '*')

            # --- Resolve imported_entity_fqn ---
            if is_wildcard:
                imported_entity_fqn = resolved_source_module_fqn # Wildcard points to the module itself
            elif resolved_source_module_fqn and not resolved_source_module_fqn.startswith("UNKNOWN_MODULE"):
                imported_entity_fqn = f"{resolved_source_module_fqn}.{raw_imported_item_name}"
            else: # If source FQN is unknown, entity FQN is also effectively unknown
                imported_entity_fqn = f"{resolved_source_module_fqn}.{raw_imported_item_name}" if not is_wildcard else resolved_source_module_fqn


            # --- Determine name_bound_in_importer and name_bound_points_to_fqn ---
            if is_wildcard:
                name_bound_in_importer = "*" # Special marker
                name_bound_points_to_fqn = resolved_source_module_fqn
            elif raw_item_alias:
                name_bound_in_importer = raw_item_alias
                name_bound_points_to_fqn = imported_entity_fqn
            else:
                name_bound_in_importer = raw_imported_item_name
                name_bound_points_to_fqn = imported_entity_fqn

            import_record = ImportRecord(
                importer_module_fqn=self.context.module_name,
                line_number=line_num,
                raw_module_specifier=raw_module_text_from_statement,
                raw_imported_name=raw_imported_item_name,
                raw_alias=raw_item_alias,
                is_relative=(level > 0),
                level=level,
                is_wildcard=is_wildcard,
                source_module_fqn=resolved_source_module_fqn,
                imported_entity_fqn=imported_entity_fqn,
                is_source_internal=is_internal_source,
                name_bound_in_importer=name_bound_in_importer,
                name_bound_points_to_fqn=name_bound_points_to_fqn
            )

            self.imports.append(import_record)
            self.context.imports.append(import_record)
            # Classify the import kind
            self._classify_import_record(import_record)

            if is_wildcard:
                if resolved_source_module_fqn and not resolved_source_module_fqn.startswith("UNKNOWN_MODULE"):
                    self.context.wildcard_imports.add(resolved_source_module_fqn)
                logger.debug(f"In {self.context.module_name}: Processed wildcard import from '{resolved_source_module_fqn}'")
            else:
                # Update imported_names: local name -> FQN it points to
                self.context.imported_names[name_bound_in_importer] = name_bound_points_to_fqn
                logger.debug(
                    f"In {self.context.module_name}: Processed from-import: '{name_bound_in_importer}' points to '{name_bound_points_to_fqn}' "
                    f"(raw: from {raw_module_text_from_statement or '.'} import {raw_imported_item_name}{' as ' + raw_item_alias if raw_item_alias else ''})"
                )
                
        self.generic_visit(node)
        
    
    def _classify_import_record(self, record: ImportRecord) -> None:
        """Classifies the import kind based on known_modules."""
              
        imported_fqn = record.imported_entity_fqn
        if not imported_fqn:
            return
        
        known_info = self.context.known_modules.get(imported_fqn) if self.context.known_modules else None
        if known_info:
            is_package = known_info.get("is_package", False)                
            record.imported_is_module = not is_package
            record.imported_is_package = is_package
        else:
            # Not a known module/package, so it's likely a member (class, function, etc.)
            record.imported_is_member = True
    
    
    def _check_all_modification(self, node: Union[ast.Call, ast.AugAssign], method_name: Optional[str] = None):
        """
        Checks if an __all__ modification requires dynamic analysis based on added values.
        Called from visit_Call and visit_AugAssign.
        """
        self.context.has_all = True # Ensure flag is set

        if self.context.all_is_dynamic:
            # Already marked as dynamic, but still try to extract more aggregation sources
            if isinstance(node, ast.AugAssign) and not isinstance(node.value, (ast.List, ast.Tuple)):
                self._extract_all_aggregation_sources(node.value)
            elif isinstance(node, ast.Call) and method_name == 'extend' and node.args:
                self._extract_all_aggregation_sources(node.args[0])
            return

        # Determine the values being added
        added_values_nodes: List[ast.AST] = []
        if isinstance(node, ast.Call) and method_name:
            # Handle .append(value) and .extend(iterable)
            if method_name == 'append' and node.args:
                added_values_nodes.append(node.args[0])
            elif method_name == 'extend' and node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
                added_values_nodes.extend(node.args[0].elts)
            elif method_name == 'extend' and node.args:
                # Handle .extend(module.__all__)
                self.context.needs_dynamic_analysis = True
                self.context.all_is_dynamic = True
                self._extract_all_aggregation_sources(node.args[0])
                logger.info(f"Dynamic __all__ detected (extend with non-list) in {self.context.module_name}. Aggregation sources: {self.context.all_aggregation_sources}")
                return
            else: # Unknown method or arguments structure
                self.context.needs_dynamic_analysis = True
                self.context.all_is_dynamic = True # Mark as dynamic if structure is unknown
                logger.info(f"Dynamic __all__ detected (unknown call structure: {method_name}) in {self.context.module_name}")
                return
        elif isinstance(node, ast.AugAssign):
            # Handle __all__ += iterable
            if isinstance(node.value, (ast.List, ast.Tuple)):
                added_values_nodes.extend(node.value.elts)
            else: # Augmented with non-list/tuple
                self.context.needs_dynamic_analysis = True
                self.context.all_is_dynamic = True # Mark as dynamic
                self._extract_all_aggregation_sources(node.value)
                logger.info(f"Dynamic __all__ detected (augmented with non-list/tuple) in {self.context.module_name}. Aggregation sources: {self.context.all_aggregation_sources}")
                return

        # Analyze the added values
        requires_dynamic = False
        static_additions = set()
        for val_node in added_values_nodes:
            if isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
                added_name = val_node.value
                static_additions.add(added_name)

                # Check origin: local definition or internal import?
                is_local_def = any(comp.name == added_name and comp.fully_qualified_name.startswith(self.context.module_name + '.') 
                                   for comp in self.components.values())

                if is_local_def:
                    continue # Adding local definition name statically is fine

                # Check if 'added_name' is a name bound by an import
                if added_name in self.context.imported_names:
                    # Find the corresponding ImportRecord
                    # 'added_name' here is the name_bound_in_importer
                    import_record = next((imp for imp in self.context.imports if imp.name_bound_in_importer == added_name), None)
                    if import_record:
                        continue
                else:
                    logger.warning(f"Name '{added_name}' added to __all__ in {self.context.module_name} but not found as a local definition or an imported name.")
            else:
                # Added value is not a string constant
                requires_dynamic = True
                logger.info(f"Dynamic __all__ triggered by adding non-string value in {self.context.module_name}")
                break

        if requires_dynamic:
            self.context.all_is_dynamic = True
            self.context.needs_dynamic_analysis = True
        elif self.context.all_values is not None and not self.context.all_is_dynamic:
            # Only update static list if not marked dynamic by other means (e.g. initial assignment)
            self.context.all_values.update(static_additions)
    
     
    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit assignment statements, checking for __all__."""
        
        # Check for __all__ assignment
        is_all_assign = False
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == '__all__':
                is_all_assign = True
                self.context.has_all = True
                self.context.all_values = set() # Initialize or overwrite
                
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    all_static = True
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            self.context.all_values.add(elt.value)
                        else:
                            all_static = False
                            break # Contains non-string constant or complex expression
                    if all_static:
                        self.context.all_is_dynamic = False
                    else:
                        # List/tuple contains non-strings, treat as dynamic
                        self.context.all_is_dynamic = True
                        self.context.needs_dynamic_analysis = True
                        logger.info(f"Dynamic __all__ detected (non-string elements) in {self.context.module_name}")

                else:
                    # Assigned something other than List/Tuple (variable, function call, etc.)
                    self.context.all_is_dynamic = True
                    self.context.needs_dynamic_analysis = True
                    
                    # Extract aggregation sources from AST
                    self._extract_all_aggregation_sources(node.value)
                    
                    logger.info(f"Dynamic __all__ detected in {self.context.module_name}. Aggregation sources: {self.context.all_aggregation_sources}")
                    
                break # Only process the first __all__ target

        # Record module-level variable (excluding __all__) 
        if not self.context.current_class and not self.context.scope_stack:
            names: List[str] = []
            for tgt in node.targets:
                names.extend(n for n in self._extract_assigned_names(tgt) if n != '__all__')
            if names:
                try:
                    value_repr = ast.unparse(node.value) if hasattr(ast, "unparse") else None
                except Exception:
                    value_repr = None
                for simple_name in names:
                    self._record_module_variable(simple_name, value_repr, node)
        
        if not is_all_assign:
            self.generic_visit(node) # Visit children only if not __all__ assignment
    
    def _extract_all_aggregation_sources(self, node: ast.AST) -> None:
        """
        Recursively extract module references from __all__ aggregation expressions.
        
        Handles patterns like:
        - list(set(lib.sub.__all__) | set(fft.__all__))
        - lib.__all__ + fft.__all__
        - sorted(lib.__all__)
        - [*lib.__all__, *fft.__all__]
        """
        if isinstance(node, ast.BinOp):
            # Handle: set(a.__all__) | set(b.__all__) or a.__all__ + b.__all__
            self._extract_all_aggregation_sources(node.left)
            self._extract_all_aggregation_sources(node.right)
            
        elif isinstance(node, ast.Call):
            # Handle: list(...), set(...), sorted(...), tuple(...)
            for arg in node.args:
                self._extract_all_aggregation_sources(arg)
                
        elif isinstance(node, ast.Attribute):
            if node.attr == '__all__':
                # Found something.__all__ - extract the module reference
                module_ref = self._extract_module_reference(node.value)
                if module_ref:
                    self.context.all_aggregation_sources.add(module_ref)
                    logger.debug(f"Found __all__ aggregation source: {module_ref}")
                    
        elif isinstance(node, (ast.List, ast.Tuple)):
            # Handle: [*lib.__all__, *fft.__all__]
            for elt in node.elts:
                self._extract_all_aggregation_sources(elt)
                
        elif isinstance(node, ast.Starred):
            # Handle: *lib.__all__ (unpacking)
            self._extract_all_aggregation_sources(node.value)


    def _extract_module_reference(self, node: ast.AST) -> Optional[str]:
        """
        Extract dotted module reference from an AST node.
        
        Examples:
            - ast.Name('lib') -> 'lib'
            - ast.Attribute(ast.Name('lib'), '_shape_base_impl') -> 'lib._shape_base_impl'
        """
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            base = self._extract_module_reference(node.value)
            if base:
                return f"{base}.{node.attr}"
        return None
    
    
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Visit annotated assignments like X: int = 5."""
        self._update_line_numbers(node)
        
        if isinstance(node.target, ast.Name):
            simple_name = node.target.id
            if simple_name == "__all__":
                self.generic_visit(node)
                return
            
            # Get type annotation and value
            type_ann = None
            value_repr = None
            try:
                type_ann = ast.unparse(node.annotation) if hasattr(ast, "unparse") and node.annotation else None
            except Exception:
                type_ann = None
            try:
                value_repr = ast.unparse(node.value) if hasattr(ast, "unparse") and node.value else None
            except Exception:
                value_repr = None
            
            # Module-level variable
            if not self.context.current_class and not self.context.scope_stack:
                self._record_module_variable(simple_name, value_repr, node, type_annotation=type_ann)
            
            # Class-level annotated assignment (e.g., dataclass field)
            elif self.context.current_class and len(self.context.scope_stack) == 1:
                self._record_class_variable(simple_name, value_repr, node, type_annotation=type_ann)
        
        self.generic_visit(node)

    def _record_class_variable(self, simple_name: str, value_repr: Optional[str], node: ast.AST, type_annotation: Optional[str] = None) -> None:
        """Record a class-level variable (e.g., dataclass field)."""
        class_fqn = f"{self.context.current_class}"
        var_fqn = f"{class_fqn}.{simple_name}"
        
        if var_fqn in self.components:
            return
        
        start_line = getattr(node, "lineno", self._current_lineno or 0)
        end_line = getattr(node, "end_lineno", start_line) or start_line
        
        var = Variable(
            name=simple_name,
            body="",
            fully_qualified_name=var_fqn,
            source_file=self.source_file,
            line_number=start_line,
            end_line=end_line,
            value_repr=value_repr,
            type_annotation=type_annotation,
            definition_module_fqn=self.context.module_name,
            parent_fqn=class_fqn,
        )
        self.components[var_fqn] = var
        
        # Also add to the class's class_variables
        if class_fqn in self.classes:
            self.classes[class_fqn].class_variables[simple_name] = {
                "name": simple_name,
                "type": type_annotation,
                "default": value_repr,
                "fqn": var_fqn
            }
        
        # Register in DefinitionRegistry
        if self.definition_registry:
            self.definition_registry.register_definition(
                module_name=self.context.module_name,
                simple_name=simple_name,
                fully_qualified_name=var_fqn,
                component_type='class_variable',
                line_number=start_line,
                source_file=self.source_file
            )
        
    def _extract_assigned_names(self, target) -> List[str]:
        """Extract plain variable names from assignment targets at module scope."""
        names: List[str] = []
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Tuple):
            for elt in target.elts:
                if isinstance(elt, ast.Name):
                    names.append(elt.id)
        # Ignore attributes/subscripts: not module-level variable definitions
        return names
    
    def _record_module_variable(self, simple_name: str, value_repr: Optional[str], node: ast.AST, type_annotation: Optional[str] = None) -> None:
        """Create a Variable component and register its definition."""
        fqn = f"{self.context.module_name}.{simple_name}"
        if fqn in self.components:
            return
        start_line = getattr(node, "lineno", self._current_lineno or 0)
        # Ensuring end_line is always an integer
        end_line = getattr(node, "end_lineno", start_line) or start_line
        try:
            var = Variable(
                name=simple_name,
                body="", # No full body needed for variables
                fully_qualified_name=fqn,
                source_file=self.source_file,
                line_number=start_line,
                end_line=end_line,
                value_repr=value_repr,
                type_annotation=type_annotation,
                definition_module_fqn=self.context.module_name,
                parent_fqn=self.context.module_name,
            )
            self.components[fqn] = var
            # Register in DefinitionRegistry for downstream resolution
            if self.definition_registry:
                self.definition_registry.register_definition(
                    module_name=self.context.module_name,
                    simple_name=simple_name,
                    fully_qualified_name=fqn,
                    component_type="variable",
                    line_number=var.line_number,
                    source_file=self.source_file,
                    metadata={"type_annotation": type_annotation}
                )
            logger.debug(f"Recorded module variable {fqn} (type={type_annotation}, value={value_repr})")
        except Exception as e:
            self._handle_error(e, f"recording variable {fqn}")
    
    
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """Visit augmented assignments (e.g., __all__ += ...)."""
        
        if isinstance(node.target, ast.Name) and node.target.id == '__all__':
            self._check_all_modification(node)
            # Don't call generic_visit for __all__ modification itself
        else:
            self.generic_visit(node)
    
    
    def visit_Call(self, node: ast.Call) -> None:
        """Visit function calls."""
        
        method_name = None
        is_all_call = False
        
        # --- Check for __all__ modifications ---
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == '__all__':
                method_name = node.func.attr
                if method_name in ('append', 'extend'): # Only check methods known to modify list content
                    is_all_call = True
                    self._check_all_modification(node, method_name)
                    # Don't visit children of the __all__ modification call itself

        # --- Check for importlib.import_module ---
        func_name = ""
        if isinstance(node.func, ast.Attribute):
            # Check for module.import_module pattern
            if isinstance(node.func.value, ast.Name) and node.func.value.id == 'importlib' and node.func.attr == 'import_module':
                func_name = 'importlib.import_module'
        elif isinstance(node.func, ast.Name):
            # Check if import_module was imported directly
            resolved_name = self.context.imported_names.get(node.func.id)
            if resolved_name == 'importlib.import_module':
                func_name = 'importlib.import_module'

        if func_name == 'importlib.import_module':
            self.context.needs_dynamic_analysis = True
            logger.info(f"Dynamic analysis needed due to importlib.import_module call in {self.context.module_name}")

        # --- Also detect runtime-discovery/import patterns ---
        
        # __import__ builtin
        if isinstance(node.func, ast.Name) and node.func.id == '__import__':
            self.context.needs_dynamic_analysis = True
            logger.info(f"Dynamic analysis needed due to __import__ call in {self.context.module_name}")
        
        # pkgutil.iter_modules / pkgutil.walk_packages patterns
        pkgutil_dynamic = False
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'pkgutil' and node.func.attr in ('iter_modules', 'walk_packages'):
            pkgutil_dynamic = True
        elif isinstance(node.func, ast.Name):
            resolved_name2 = self.context.imported_names.get(node.func.id)
            if resolved_name2 in ('pkgutil.iter_modules', 'pkgutil.walk_packages'):
                pkgutil_dynamic = True
        if pkgutil_dynamic:
            self.context.needs_dynamic_analysis = True
            logger.info(f"Dynamic analysis needed due to runtime discovery via pkgutil in {self.context.module_name}")

        # --- General Call Tracking ---
        if is_enabled(Feature.CALL_GRAPH_ANALYSIS) and self.call_tracker:
            caller_fqn = self.context.current_scope # FQN of the function/method containing the call
            resolved_callee_fqn = self._resolve_call_target(node)

            if caller_fqn and resolved_callee_fqn:
                try:
                    # Extract basic call site info
                    call_site = f"{self.source_file}:{node.lineno}" if self.source_file else f"{self.context.module_name}:{node.lineno}"
                    args_count = len(node.args)
                    kwargs_count = len(node.keywords)

                    # --- Basic Data Flow Analysis for arguments ---
                    call_metadata = {'line': node.lineno}
                    
                    analyzed_pos_args = []
                    for arg_idx, arg_node in enumerate(node.args):
                        arg_info = {"position": arg_idx}
                        if isinstance(arg_node, ast.Constant): # Handles str, num, bool, None
                            arg_info.update({"source_type": "constant", "value": repr(arg_node.value)})
                        elif isinstance(arg_node, ast.Name):
                            arg_info.update({"source_type": "variable", "name": arg_node.id})
                        elif isinstance(arg_node, ast.Attribute):
                            try:
                                # ast.unparse is Python 3.9+
                                attr_str = ast.unparse(arg_node)
                            except AttributeError: 
                                # Basic fallback for older Python or if unparse fails
                                current_attr = arg_node
                                parts = []
                                while isinstance(current_attr, ast.Attribute):
                                    parts.insert(0, current_attr.attr)
                                    current_attr = current_attr.value
                                if isinstance(current_attr, ast.Name):
                                    parts.insert(0, current_attr.id)
                                    attr_str = ".".join(parts)
                                else:
                                    attr_str = "complex_attribute" # Fallback if base is not Name
                            arg_info.update({"source_type": "attribute", "name": attr_str})
                        elif isinstance(arg_node, ast.Call):
                            arg_info.update({"source_type": "call_result", "callee": self._resolve_call_target(arg_node)})
                        else:
                            arg_info.update({"source_type": "expression", "expression_type": type(arg_node).__name__})
                        analyzed_pos_args.append(arg_info)
                    
                    call_metadata["arguments"] = analyzed_pos_args

                    analyzed_kw_args = []
                    for kw_node in node.keywords:
                        # kw_node.arg is the keyword name string, or None for **kwargs
                        kw_name = kw_node.arg if kw_node.arg else "**kwargs_unpack" 
                        kw_info = {"name": kw_name}
                        value_node = kw_node.value
                        if isinstance(value_node, ast.Constant):
                            kw_info.update({"source_type": "constant", "value": repr(value_node.value)})
                        elif isinstance(value_node, ast.Name):
                            kw_info.update({"source_type": "variable", "name": value_node.id})
                        elif isinstance(value_node, ast.Attribute):
                            try:
                                attr_str = ast.unparse(value_node) # Python 3.9+
                            except AttributeError:
                                current_attr = value_node
                                parts = []
                                while isinstance(current_attr, ast.Attribute):
                                    parts.insert(0, current_attr.attr)
                                    current_attr = current_attr.value
                                if isinstance(current_attr, ast.Name):
                                    parts.insert(0, current_attr.id)
                                    attr_str = ".".join(parts)
                                else:
                                    attr_str = "complex_attribute"
                            kw_info.update({"source_type": "attribute", "name": attr_str})
                        elif isinstance(value_node, ast.Call):
                            kw_info.update({"source_type": "call_result", "callee": self._resolve_call_target(value_node)})
                        else:
                            kw_info.update({"source_type": "expression", "expression_type": type(value_node).__name__})
                        analyzed_kw_args.append(kw_info)
                    call_metadata["keyword_arguments"] = analyzed_kw_args
                    # ---

                    self.call_tracker.add_call(
                        caller=caller_fqn,
                        callee=resolved_callee_fqn,
                        call_site=call_site,
                        args_count=args_count,
                        kwargs_count=kwargs_count,
                        # is_direct: bool will use default True from CallGraphTracker.add_call
                        # To be more precise, 'is_direct' could be determined here based on how callee was resolved.
                        metadata=call_metadata
                    )
                except Exception as e:
                    logger.error(f"Error adding call {caller_fqn} -> {resolved_callee_fqn}: {e}", exc_info=True)
                    self.context.errors.append({
                        "type": "CallTrackingError",
                        "message": f"Failed to add call: {e}",
                        "caller": caller_fqn,
                        "callee": resolved_callee_fqn,
                        "file": self.source_file,
                        "line": node.lineno,
                    })
            # else:
                 # logger.debug(f"Could not resolve call target or caller scope for call at {node.lineno}")

        if not is_all_call: # Only visit children if it wasn't an __all__ modification call
            self.generic_visit(node)
    
    
    def _resolve_relative_import(self, module_name: Optional[str], level: int) -> Optional[str]:
        """
        Resolve a relative import to an absolute import according to PEP 328.

        For an import like `from ..b import c` in module `pkg.sub.mod`:
        - `module_name` would be "b"
        - `level` would be 2

        For an import like `from . import d` in module `pkg.sub`:
        - `module_name` would be None
        - `level` would be 1
        """
        
        if level == 0:
            # This is an absolute import (e.g., `import pkg.mod`), no resolution needed.
            return module_name

        current_module_parts = self.context.module_name.split('.')
        
        # Determine the base for the relative import.
        # If we are in `pkg/sub/__init__.py`, the module name is `pkg.sub`, and this is our base.
        # If we are in `pkg/sub/mod.py`, the module name is `pkg.sub.mod`, and the base is `pkg.sub`.
        if self.context.is_init_file:
            base_parts = current_module_parts
        else:
            base_parts = current_module_parts[:-1]

        # A level of 1 means "from the current package".
        # A level of 2 means "from the parent package", and so on.
        # So we need to go up `level - 1` directories from our calculated base.
        num_levels_to_ascend = level - 1
        
        if num_levels_to_ascend > len(base_parts):
            logger.warning(
                f"Attempted relative import beyond top-level package in module '{self.context.module_name}': "
                f"level={level} is too high."
            )
            self.context.errors.append({
                "type": "ImportResolutionError",
                "message": f"Relative import level ({level}) exceeds package depth.",
                "module": self.context.module_name,
                "file": self.source_file,
            })
            return None # Cannot resolve

        # Ascend the required number of levels.
        if num_levels_to_ascend > 0:
            final_base_parts = base_parts[:-num_levels_to_ascend]
        else:
            final_base_parts = base_parts

        base_path = '.'.join(final_base_parts)

        if module_name:
            # Case: `from .foo import bar` -> base_path = `pkg.sub`, module_name = `foo` -> `pkg.sub.foo`
            resolved_path = f"{base_path}.{module_name}" if base_path else module_name
        else:
            # Case: `from . import foo` -> base_path = `pkg.sub`, module_name = None -> `pkg.sub`
            resolved_path = base_path
            
        return resolved_path
    
        
    def _is_internal_import(self, import_record) -> bool:
        """Check if an import record represents an internal (repository-local) import."""
        if not import_record.source_module_fqn:
            return False
            
        # Check if the import source starts with any known top-level package
        if self.context.top_level_packages:
            import_top_level = import_record.source_module_fqn.split('.')[0]
            return import_top_level in self.context.top_level_packages
            
        # Fallback: assume imports with relative patterns are internal
        return '.' in import_record.source_module_fqn
    
    
    def _resolve_call_target(self, node: ast.Call) -> Optional[str]:
        """
        Attempts to resolve the fully qualified name of the function or method being called.
        Returns the FQN string or None if resolution fails.
        This is a static analysis approach and has limitations with dynamic code.
        """
        func_node = node.func # The AST node representing what is being called

        # 1. Direct function/class call (e.g., my_function(), MyClass())
        if isinstance(func_node, ast.Name):
            call_name = func_node.id
            current_module_fqn = self.context.module_name

            # Priority 1: Is it defined at the top level of the current module?
            # (e.g., calling 'bar()' from 'foo()' in the same module, or instantiating 'MyClass()')
            module_level_fqn = f"{current_module_fqn}.{call_name}"
            if module_level_fqn in self.components:
                component = self.components[module_level_fqn]
                # Ensure it's a callable type (Function or Class for __init__/__new__)
                if isinstance(component, (Function, Class)): 
                    return module_level_fqn

            # Priority 2: Is it an imported name?
            # self.context.imported_names maps the local alias/name to its original FQN.
            imported_fqn = self.context.imported_names.get(call_name)
            if imported_fqn:
                # We assume the imported FQN points to a callable entity.
                # Further validation (e.g., checking if 'imported_fqn' is a function/class in DefinitionRegistry)
                # could be done here if needed, but might be too slow for general call resolution.
                return imported_fqn 

            # Priority 3: Is it defined in the current lexical scope (e.g., a nested function)?
            # self.context.get_fully_qualified_name will use the current_scope (e.g., module.func.nested_func_call)
            lexical_fqn = self.context.get_fully_qualified_name(call_name)
            if lexical_fqn != module_level_fqn and lexical_fqn in self.components: # Avoid re-checking if same as module_level
                component = self.components[lexical_fqn]
                if isinstance(component, (Function, Class)):
                    return lexical_fqn
            
            # Priority 4: Check for built-in functions (optional, can be noisy if not filtered)
            # if self.config.resolve_builtins and hasattr(__builtins__, call_name):
            #     return f"builtins.{call_name}"

            logger.debug(f"Could not resolve direct call name '{call_name}' in module {current_module_fqn}, current scope {self.context.current_scope}, or via imports.")
            return None

        # 2. Attribute call (e.g., obj.method(), module.function(), cls.method())
        elif isinstance(func_node, ast.Attribute):
            method_name = func_node.attr
            base_object_node = func_node.value

            # Case 2a: self.method() or cls.method()
            if isinstance(base_object_node, ast.Name) and base_object_node.id in ('self', 'cls'):
                if self.context.current_class: # FQN of the class currently being processed
                    return f"{self.context.current_class}.{method_name}"
                else:
                    # This should ideally not happen in valid code if AST is correct.
                    logger.warning(f"Call to '{base_object_node.id}.{method_name}' encountered outside of a class scope in {self.context.module_name} at line {node.lineno}.")
                    return None

            # Case 2b: Attempt to resolve the base of the attribute access.
            # This is where it gets complex due to lack of type information for arbitrary variables.
            # We try to reconstruct the base expression as a string (e.g., "my_module.MyClass" or "instance_var")
            
            base_expr_str = ""
            temp_node = base_object_node
            while isinstance(temp_node, ast.Attribute):
                base_expr_str = f".{temp_node.attr}{base_expr_str}"
                temp_node = temp_node.value
            
            if isinstance(temp_node, ast.Name):
                base_expr_str = temp_node.id + base_expr_str # e.g., "os.path" or "my_obj"
            else:
                # Base is not a simple name chain (e.g., (func_call()).attribute)
                logger.debug(f"Cannot resolve complex base for attribute call '.{method_name}' in {self.context.module_name} at line {node.lineno}.")
                return None # Cannot statically resolve the type of the complex base

            # Now 'base_expr_str' holds something like "os.path", "my_module.MyClass", "instance_var"

            # Try resolving 'base_expr_str' as an imported name first.
            # This covers 'import my_pkg.mod; my_pkg.mod.func()' or 'from my_pkg import mod; mod.func()'
            resolved_base_import_fqn = self.context.imported_names.get(base_expr_str)
            if resolved_base_import_fqn:
                return f"{resolved_base_import_fqn}.{method_name}"

            # If base_expr_str wasn't directly an alias, check if its first part was an alias.
            # e.g., import my_pkg; my_pkg.mod.func() -> base_expr_str = "my_pkg.mod"
            # Here, "my_pkg" would be in imported_names.
            if '.' in base_expr_str:
                first_part = base_expr_str.split('.')[0]
                rest_of_parts = base_expr_str.split('.')[1:]
                
                resolved_first_part_import = self.context.imported_names.get(first_part)
                if resolved_first_part_import: # e.g. first_part = 'os', resolved_first_part_import = 'os'
                    # Reconstruct the FQN: imported_module_fqn + . + rest_of_parts + . + method_name
                    # Example: import os; os.path.join()
                    # first_part="os", resolved_first_part_import="os"
                    # rest_of_parts=["path"]
                    # method_name="join" -> "os.path.join"
                    # Example: from some_lib import sub_pkg; sub_pkg.mod.func()
                    # first_part="sub_pkg", resolved_first_part_import="some_lib.sub_pkg"
                    # rest_of_parts=["mod"]
                    # method_name="func" -> "some_lib.sub_pkg.mod.func"
                    
                    # Ensure the resolved_first_part_import is the actual module/package FQN
                    # and not an alias to something deeper already.
                    # If 'first_part' was an alias like 'import some.deep.module as sdm', then
                    # resolved_first_part_import is 'some.deep.module'.
                    # If base_expr_str was 'sdm.sub_attr', then this becomes 'some.deep.module.sub_attr.method_name'.
                    
                    # This logic handles cases like:
                    # 1. `import package; package.module.Class.method()`
                    #    base_expr_str = "package.module.Class", first_part="package" (points to "package")
                    #    -> "package.module.Class.method_name"
                    # 2. `from some import pkg_alias; pkg_alias.mod.func()`
                    #    base_expr_str = "pkg_alias.mod", first_part="pkg_alias" (points to "some.actual_pkg_name")
                    #    -> "some.actual_pkg_name.mod.method_name"

                    # Construct the FQN based on the resolved first part
                    final_base_fqn_parts = [resolved_first_part_import] + rest_of_parts
                    final_base_fqn = ".".join(final_base_fqn_parts)
                    return f"{final_base_fqn}.{method_name}"

            # If not resolved via imports, check if 'base_expr_str' is a locally defined class
            # in the current module (e.g. MyLocalClass.static_method()).
            potential_local_class_fqn = f"{self.context.module_name}.{base_expr_str}"
            if potential_local_class_fqn in self.components and isinstance(self.components[potential_local_class_fqn], Class):
                return f"{potential_local_class_fqn}.{method_name}"

            # If 'base_expr_str' is a variable in the current scope (e.g. my_instance.method()):
            # This requires type inference for 'my_instance', which is beyond simple static AST analysis.
            # We cannot reliably determine the FQN of 'my_instance.method()' without knowing 'my_instance's type.
            # For this simplified resolver, we might not resolve this case unless 'my_instance' itself
            # was an imported name or a locally defined class (covered above).
            logger.debug(f"Cannot resolve attribute call on base '{base_expr_str}'.{method_name} in {self.context.module_name} at line {node.lineno} without type info for the base.")
            return None

        # 3. Other complex callable expressions (e.g., (lambda x: x+1)(5), list_of_funcs[0]())
        else:
            # These are generally not resolvable to a static FQN easily.
            try:
                unparsed_func = ast.unparse(func_node)
                logger.debug(f"Cannot resolve call target for complex callable expression: {unparsed_func} in {self.context.module_name} at line {node.lineno}.")
            except AttributeError: # ast.unparse might not be available or fail
                logger.debug(f"Cannot resolve call target for complex callable expression of type: {type(func_node)} in {self.context.module_name} at line {node.lineno}.")
            return None
    
    
    def _should_include_member(self, name: str) -> bool:
        """Determine if a member should be included based on __all__ or naming conventions."""
        
        if self.context.has_all:
            if self.context.all_is_dynamic:
                # Dynamic __all__: Assume public unless starts with '_' (conservative)
                # Or could return True always and let downstream handle it.
                # Let's be conservative for now.
                return not name.startswith('_')
            
            elif self.context.all_values is not None:
                # Static __all__: Must be present in the set
                return name in self.context.all_values
            
            else:
                return self._is_public_component(name)
        else:
            return self._is_public_component(name)
    
    
    def _determine_exports(self) -> Set[str]:
        """
        Determines the set of simple names exported by the module based on __all__ or implicit export rules.
        """
        exported_names: Set[str] = set()

        if self.context.has_all:
            if self.context.all_is_dynamic:
                logger.warning(f"Module {self.context.module_name} has a dynamic __all__. Cannot statically determine exact exports.")
                # Return empty set for dynamic __all__ to avoid adding potentially incorrect relationships
                # Downstream dynamic analysis would be needed for full accuracy
                return set()
            elif self.context.all_values is not None:
                # Static __all__ list/tuple
                exported_names = self.context.all_values
                logger.debug(f"Using static __all__ for {self.context.module_name}: {exported_names}")
            else:
                # Should not happen if has_all is True and not dynamic, but handle defensively
                logger.warning(f"Inconsistent __all__ state for {self.context.module_name}. Assuming empty exports.")
                return set()
        else:
            # No __all__: Implicit exports - include non-private top-level definitions
            logger.debug(f"No __all__ found in {self.context.module_name}. Determining implicit exports.")
            for fqn, component in self.components.items():
                # Check if it's a top-level component (parent is the module itself)
                # A simple check: FQN should be module_name.simple_name
                if '.' in fqn and fqn.rsplit('.', 1)[0] == self.context.module_name:
                    simple_name = component.name
                    # Check naming convention
                    if self._is_public_component(simple_name):
                        exported_names.add(simple_name)
            
            # Include imports (direct, alias)
            for import_record in self.context.imports:
                if import_record.is_source_internal:
                    # If wildcard import is present, mark module for export expansion
                    if import_record.is_wildcard:
                        self.module_needs_linking = True
                if not import_record.is_wildcard and self._is_public_component(import_record.name_bound_in_importer) and import_record.name_bound_in_importer not in exported_names:
                    exported_names.add(import_record.name_bound_in_importer)
                        
            self.context.all_values = exported_names

            logger.debug(f"Implicit exports for {self.context.module_name}: {exported_names}")

        return exported_names
    
    
    def get_analysis_results(self) -> Dict[str, Any]:
        """
        Return the collected analysis results.
        All export records, including those that need further linking (e.g., from wildcards), are returned in a single list.
        """

        # --- Populate ExportRecords ---
        
        export_records: List[Dict[str, Any]] = []
        exported_names = self._determine_exports()
        
        # If there are no exported names identified (module could have just a wildcard import or completely empty), add a dummy export record for export expansion to be handled by AnalyzerIntegration
        if not exported_names and self.context.wildcard_imports:
            export_records.append({
                "exporting_package_fqn": self.context.package_name,
                'exporting_module_fqn': self.context.module_name,
                'exported_name': None,
                'target_item_fqn': None,
                'wildcard_sources': list(self.context.wildcard_imports),
                'needs_linking': False,
                'is_dummy': True,
            })
        
        for name in exported_names:
            potential_local_fqn = f"{self.context.module_name}.{name}"
            
            # --- Case 1: Direct export of a locally-defined member ---
            if potential_local_fqn in self.components:
                component = self.components[potential_local_fqn]
                metadata = {
                    'line': component.line_number,
                    'component_type': component.__class__.__name__.lower(),
                    'in_init': self.context.is_init_file
                }
                export_records.append({
                    "exporting_package_fqn": self.context.package_name,
                    "exporting_module_fqn": self.context.module_name,
                    "target_item_fqn": potential_local_fqn,
                    "exported_name": name,
                    "is_explicit": self.context.has_all,
                    "is_reexport": False,
                    'is_wildcard_reexport': False,
                    'is_internal': True,
                    'needs_linking': False,
                    'wildcard_sources': list(self.context.wildcard_imports),
                    "metadata": metadata,
                    'needs_linking': False,
                    'source_module': self.context.module_name,
                    'component_kind': 'member'
                })
                           
            # --- Case 2: Re-export of a directly imported name ---
            elif name in self.context.imported_names:
                # 'name' is the name bound in the current module's scope.
                # Find the corresponding ImportRecord to get the original FQN.
                import_record = next((imp for imp in self.context.imports if imp.name_bound_in_importer == name), None)
                if import_record:
                    source_module = import_record.source_module_fqn # source of the re-exported item
                    # Identify the kind of re-exported item
                    if import_record.imported_is_package: component_kind = 'package'
                    elif import_record.imported_is_module: component_kind = 'module'
                    else: component_kind = 'member'
                    
                    metadata = {'line': import_record.line_number, 'in_init': self.context.is_init_file, 'source_module_if_reexport': import_record.source_module_fqn}
                    export_records.append({
                        "exporting_package_fqn": self.context.package_name,
                        'exporting_module_fqn': self.context.module_name,
                        'exported_name': name,
                        'target_item_fqn': import_record.name_bound_points_to_fqn,
                        'is_explicit': self.context.has_all,
                        'is_reexport': True,
                        'is_wildcard_reexport': False,
                        'is_internal': import_record.is_source_internal,
                        'needs_linking': False, # Link is known from direct import
                        'wildcard_sources': list(self.context.wildcard_imports),
                        'needs_linking': False,
                        'source_module': source_module,
                        'component_kind': component_kind,
                        'metadata': metadata,
                    })
                    
                    
                
            # --- Case 3: Potential re-export from wildcard or dynamic context ---
            else:
                # This name is exported, but not defined locally and not a direct import.
                # It must be from a wildcard import or dynamically injected.
                # We flag it for resolution by AnalyzerIntegration.
                export_records.append({
                    "exporting_package_fqn": self.context.package_name,
                    'exporting_module_fqn': self.context.module_name,
                    'exported_name': name,
                    'target_item_fqn': None, # We don't know the target yet
                    'module_sources': None, # We don't know the sources yet
                    'wildcard_sources': list(self.context.wildcard_imports), # Provide potential sources
                    "file": self.source_file,
                    'is_explicit': self.context.has_all,
                    'is_reexport': True, # It's a re-export by definition
                    'is_wildcard_reexport': None,
                    'needs_linking': True, # CRITICAL FLAG
                    'component_kind': None
                })
                logger.debug(f"Flagged unresolved export '{name}' in {self.context.module_name} for later linking.")
        
        
        # Determine public components (export list is the single source of truth)
        final_exported_names = exported_names
        public_component_fqns = []

        for fqn, component in self.components.items():
            # A component is part of the public API if its simple name is in the final export list and it's a top-level definition within this module
            # Example: fqn="module.name", component.name="name"
            # Example: fqn="module.Class.method", component.name="method" -> not top-level for module's public API
            
            # Check if the component's FQN implies it's a direct member of this module (not a method or nested class, which have longer FQNs)
            is_top_level_in_module = fqn.startswith(self.context.module_name) and fqn.count('.') == self.context.module_name.count('.') + 1
            
            if is_top_level_in_module and component.name in final_exported_names:
                public_component_fqns.append(fqn)
                component.is_public = True 
            elif is_top_level_in_module: # Top-level but not exported
                component.is_public = False
            # For nested components (methods, nested classes), their publicity is implicitly tied to their container's publicity. No explicit marking here

        # --- Calculate Module Statistics for API Boundary Detection ---
        module_stats = self._calculate_module_statistics(export_records)
        
        # Prepare module interface details
        module_interface = {
            "docstring": self.context.module_docstring,
            # "imports": [imp.__dict__ for imp in self.context.imports],
            "wildcard_imports": list(self.context.wildcard_imports),
            "has_all": self.context.has_all,
            "all_is_dynamic": self.context.all_is_dynamic,
            "all_values": list(self.context.all_values) if self.context.all_values is not None else None,
            "needs_dynamic_analysis": self.context.needs_dynamic_analysis,
            "module_needs_linking": self.module_needs_linking,
            "is_init_file": self.context.is_init_file,
            "all_aggregation_sources": list(self.context.all_aggregation_sources) if self.context.all_aggregation_sources else None, # include aggregation sources
        }

        return {
            "code": self.code, # added raw code for IRModule.location
            "module_name": self.context.module_name,
            "package_name": self.context.package_name,
            "source_file": self.source_file,
            "components": {fqn: comp.to_dict() for fqn, comp in self.components.items()},
            "public_component_fqns": public_component_fqns,
            "errors": self.context.errors,
            "module_interface": module_interface,
            # "imported_names_map": self.context.imported_names.copy(),
            "import_records": [imp.__dict__ for imp in self.context.imports],
            "export_records": export_records,
            "module_statistics": module_stats,
        }

    
    def get_public_components(self) -> List[CodeComponent]:
        """Get list of public components based on the is_public flag set in get_analysis_results."""
        return [comp for comp in self.components.values() if getattr(comp, 'is_public', False)]
    
    
    def _calculate_module_statistics(self, export_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Calculate module-level raw statistics for later API boundary scoring and general analysis.
        This method does NOT assign a boundary score; that is deferred to APIPathResolver.
        
        Args:
            export_records: List of export record dictionaries from this module.
            
        Returns:
            Dictionary containing various raw module statistics.
        """
        # Export statistics from the provided records for this module
        export_count = len(export_records)
        implicit_export_count = len([e for e in export_records if not e.get("is_explicit", False)])
        explicit_export_count = export_count - implicit_export_count
        reexport_count = len([e for e in export_records if e.get("is_reexport", False)])
        direct_export_count = export_count - reexport_count
        
        # Import statistics from the context of this module
        import_count = len(self.context.imports)
        external_import_count = len([imp_rec for imp_rec in self.context.imports if not self._is_internal_import(imp_rec)])
        internal_import_count = import_count - external_import_count
        wildcard_import_count = len(self.context.wildcard_imports)
        
        # Calculate ratios (handle division by zero)
        export_import_ratio = export_count / import_count if import_count > 0 else float(export_count) # Ensure float
        external_import_ratio = external_import_count / import_count if import_count > 0 else 0.0
        reexport_ratio = reexport_count / export_count if export_count > 0 else 0.0
        
        # Raw Module characteristics for boundary scoring by APIPathResolver
        is_init_file = self.context.is_init_file
        has_docstring = bool(self.context.module_docstring)
        has_explicit_all = self.context.has_all and not self.context.all_is_dynamic
        module_depth = self.context.module_name.count('.') # a.b.c -> depth 2
        
        # Component counts by type
        component_counts = defaultdict(int)
        for component in self.components.values():
            component_type = component.__class__.__name__.lower()
            component_counts[component_type] += 1
        
        # Lines of code (approximate from source ranges)
        total_loc = 0
        public_component_count = 0
        documented_component_count = 0
        
        for component in self.components.values():
            # LOC from line numbers
            if hasattr(component, 'line_number') and hasattr(component, 'end_line'):
                if component.line_number and component.end_line:
                    total_loc += (component.end_line - component.line_number + 1)
            
            # Public members
            if getattr(component, 'is_public', False):
                public_component_count += 1
            
            # Documented members
            if getattr(component, 'docstring', None):
                documented_component_count += 1
        
        # Docstring coverage ratio
        docstring_coverage = documented_component_count / len(self.components) if self.components else 0.0
        
        return {
            # Export statistics
            "export_count": export_count,
            "implicit_export_count": implicit_export_count,
            "explicit_export_count": explicit_export_count,
            "reexport_count": reexport_count,
            "direct_export_count": direct_export_count,
            
            # Import statistics
            "import_count": import_count,
            "external_import_count": external_import_count,
            "internal_import_count": internal_import_count,
            "wildcard_import_count": wildcard_import_count,
            
            # Ratios
            "export_import_ratio": export_import_ratio,
            "external_import_ratio": external_import_ratio,
            "reexport_ratio": reexport_ratio,
            
            # Raw Module characteristics for boundary scoring by APIPathResolver
            "is_init_file": is_init_file,
            "has_docstring": has_docstring,
            "has_explicit_all": has_explicit_all,
            "module_depth": module_depth,
            "module_name": self.context.module_name, # For context in APIPathResolver
            
            "needs_dynamic_analysis": self.context.needs_dynamic_analysis,
            
            # Component statistics
            "component_counts": dict(component_counts),
            "total_component_count": len(self.components),
            
            "lines_of_code": total_loc,
            "public_component_count": public_component_count,
            "private_component_count": len(self.components) - public_component_count,
            "documented_component_count": documented_component_count,
            "docstring_coverage": docstring_coverage,
        }



def analyze_code(code: str,
                 module_name: str,
                 package_name: str = "",
                 source_file: Optional[str] = None,
                 config: Optional[AnalysisConfig] = None,
                 definition_registry: Optional[DefinitionRegistry] = None,
                 inheritance_tracker: Optional[InheritanceTracker] = None,
                 call_tracker: Optional[CallGraphTracker] = None,
                 top_level_packages: Optional[Set[str]] = None,
                 known_modules: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Analyzes Python code using CodeVisitor and returns structured results.
    
    Args:
        code: The Python code string.
        module_name: The fully qualified name of the module.
        package_name: The name of the package containing the module.
        source_file: Optional path to the source file.
        config: Optional AnalysisConfig instance.
        definition_registry: Optional DefinitionRegistry instance.
        inheritance_tracker: Optional InheritanceTracker instance.
        call_tracker: Optional CallGraphTracker instance.
        top_level_packages: Optional set of top-level package/module names in the repository.
        known_modules: Optional dictionary of known packages and modules in the codebase under analysis.
    
    Returns:
        A dictionary containing analysis results including components,
        public component FQNs, errors, and module interface details.
    """
    
    logger.info(f"Starting static analysis for module: {module_name}")
    start_time = time.time()

    try:
        visitor = CodeVisitor(
            code=code,
            module_name=module_name,
            package_name=package_name,
            source_file=source_file,
            config=config,
            definition_registry=definition_registry,
            inheritance_tracker=inheritance_tracker,
            call_tracker=call_tracker,
            top_level_packages=top_level_packages,
            known_modules=known_modules
        ) 
        results = visitor.get_analysis_results()
        
    except Exception as e:
        logger.error(f"Unexpected error during static analysis of {module_name}: {e}", exc_info=True)
        results = {
            "module_name": module_name,
            "package_name": package_name,
            "source_file": source_file,
            "components": {},
            "public_component_fqns": [],
            "errors": [{
                "type": "FatalAnalysisError",
                "message": f"Unexpected error: {e}",
                "file": source_file,
                "line": None,
            }],
            "module_interface": {},
        }

    end_time = time.time()
    logger.info(f"Finished static analysis for module: {module_name} in {end_time - start_time:.2f} seconds")
    return results