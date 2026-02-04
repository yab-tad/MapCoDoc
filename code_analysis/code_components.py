"""
Code components module with comprehensive decorator handling,
nested class support, and improved signature tracking.
"""

import ast
import inspect
import logging
from abc import ABC
from functools import wraps
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set, Callable


logger = logging.getLogger(__name__)


@dataclass
class DecoratorInfo:
    """Information about a decorator and its effects."""
    name: str
    is_factory: bool = False
    args: List[Any] = field(default_factory=list)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    source_code: Optional[str] = None
    affects_signature: bool = False
    affects_return_type: bool = False
    preserves_metadata: bool = True

@dataclass
class UnwrappedFunction:
    """
    Represents an unwrapped function with original metadata.
    Tracks decorator chain and preserved attributes.
    """
    original_func: Callable
    unwrapped_func: Callable
    decorator_chain: List[DecoratorInfo]
    preserved_attributes: Dict[str, Any]
    original_signature: Optional[inspect.Signature] = None
    modified_signature: Optional[inspect.Signature] = None
    
    @classmethod
    def from_function(cls, func: Callable, max_depth: int = 3) -> 'UnwrappedFunction':
        """Create UnwrappedFunction from a decorated function."""
        decorator_chain = []
        preserved_attrs = {}
        current_func = func
        
        # Get original signature before unwrapping
        try:
            original_sig = inspect.signature(func)
        except ValueError:
            original_sig = None
        
        # Unwrap decorators
        for _ in range(max_depth):
            if not hasattr(current_func, '__wrapped__'):
                break
                
            # Get decorator info
            decorator = cls._extract_decorator_info(current_func)
            if decorator:
                decorator_chain.append(decorator)
                
            # Preserve attributes
            cls._preserve_attributes(current_func, preserved_attrs)
            
            # Move to wrapped function
            current_func = current_func.__wrapped__
            
        # Get modified signature after unwrapping
        try:
            modified_sig = inspect.signature(current_func)
        except ValueError:
            modified_sig = None
            
        return cls(
            original_func=func,
            unwrapped_func=current_func,
            decorator_chain=decorator_chain,
            preserved_attributes=preserved_attrs,
            original_signature=original_sig,
            modified_signature=modified_sig
        )
    
    @staticmethod
    def _extract_decorator_info(func: Callable) -> Optional[DecoratorInfo]:
        """Extract information about the decorator applied to a function."""
        try:
            # Try to get decorator name
            if hasattr(func, '__decorator_name__'):
                name = func.__decorator_name__
            else:
                name = func.__class__.__name__
                
            # Check if it's a factory
            is_factory = hasattr(func, '__decorator_factory__')
            
            # Try to get args/kwargs
            args = getattr(func, '__decorator_args__', [])
            kwargs = getattr(func, '__decorator_kwargs__', {})
            
            # Try to get source
            try:
                source = inspect.getsource(func)
            except (TypeError, OSError):
                source = None
                
            return DecoratorInfo(
                name=name,
                is_factory=is_factory,
                args=args,
                kwargs=kwargs,
                source_code=source,
                affects_signature=hasattr(func, '__signature__'),
                affects_return_type=hasattr(func, '__annotations__'),
                preserves_metadata=hasattr(func, '__wrapped__')
            )
        except Exception as e:
            logger.debug(f"Failed to extract decorator info: {e}")
            return None
            
    @staticmethod
    def _preserve_attributes(func: Callable, attrs: Dict[str, Any]) -> None:
        """Preserve important function attributes."""
        # Standard attributes to preserve
        for attr in ['__doc__', '__name__', '__module__', '__annotations__',
                    '__qualname__', '__defaults__', '__kwdefaults__']:
            if hasattr(func, attr):
                attrs[attr] = getattr(func, attr)
        
        # Custom attributes
        for attr in getattr(func, '__dict__', {}):
            if not attr.startswith('__'):
                attrs[attr] = getattr(func, attr)



@dataclass
class CodeComponent(ABC):
    """
    Base class for code components with improved metadata tracking.
    
    Attributes:
        name: Component name
        body: Full source code
        fully_qualified_name: Static import/implementation path
        API_name: Public API name (may differ from fqn due to re-exports)
        API_names: Possible Public API paths a member is exposed through
        best_export_chain: The export chain for a module member facing the public API
        all_export_chains: All possible export chains for a module member
        is_chain_candidate: Whether the module member has an export chain beyond its definition module
        docstring: Documentation string
        doc_url: Documentation URL
        reference_documentation: Additional documentation
        signature: Signature variations
        is_public: Whether component is considered public API
        source_file: Source file path
        line_number: Line number in source
        end_line: End line number in source
        dependencies: Component dependencies
        metadata: Additional metadata
        is_module_with_all: Whether component's module has __all__
        exported_by_default: Whether component would be exported without __all__
        export_chain: Sequence of steps showing how component reaches public API
    """
    name: str
    body: str
    fully_qualified_name: str
    is_public: bool = True
    
    API_name: Optional[str] = None # Primary API path
    API_names: List[str] = field(default_factory=list)  # Multiple API paths
    best_export_chain: List[Dict] = field(default_factory=list) # Stores list of ExportStep dicts
    all_export_chains: List[List[Dict]] = field(default_factory=list) # Stores list of lists of ExportStep dicts
    is_chain_candidate: bool = False
    
    docstring: Optional[str] = None
    source_file: Optional[str] = None
    line_number: Optional[int] = None
    end_line: Optional[int] = None
    doc_url: Optional[str] = None
    reference_documentation: Optional[Dict[str, Any]] = None
    dependencies: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)
    signature: Dict[str, str] = field(default_factory=dict)
    
    
    is_module_with_all: bool = False  # Whether component's module has __all__
    exported_by_default: bool = False  # Whether it would be exported without __all__
    # export_chain = field(default_factory=list)
    
    definition_module_fqn: Optional[str] = None # FQN of the module where *defined*
    parent_fqn: Optional[str] = None # FQN of the parent component (e.g., class for a method)
    
    _access_modifier: Optional[str] = field(init=False, default=None)
    _source_ast: Optional[ast.AST] = field(init=False, default=None)
    

    def __post_init__(self):
        """Initialize derived attributes."""
        self._access_modifier = self.get_access_modifier(self.name)
        
        if self.API_name is None:
            self.API_name = self.fully_qualified_name
        if not self.API_names:
            self.API_names = [self.API_name]  # Initialize with primary API path
                
        self._initialize_metadata()

    
    def _initialize_metadata(self):
        """Initialize component metadata."""
        self.metadata.update({
            'component_type': self.__class__.__name__,
            'access_modifier': self.access_modifier,
            'has_docstring': bool(self.docstring),
            'has_signature': bool(self.signature),
            'source_info': {
                'file': self.source_file,
                'line_start': self.line_number,
                'line_end': self.end_line
            },
            'definition_module_fqn': self.definition_module_fqn,
            'parent_fqn': self.parent_fqn,
            'is_chain_candidate': self.is_chain_candidate,
        })
        if self.API_name:
            self.metadata['resolved_api_path'] = self.API_name
    
    
    @property
    def access_modifier(self) -> str:
        """Get access modifier based on name."""
        if self._access_modifier is None:
            self._access_modifier = self.get_access_modifier(self.name)
        return self._access_modifier

    
    @staticmethod
    def get_access_modifier(name: str) -> str:
        """Access modifier detection."""
        if name.startswith("__") and name.endswith("__"): return "special"
        if name.startswith("__"): return "private"
        if name.startswith("_"): return "protected"
        return "public"

    
    def to_dict(self) -> Dict[str, Any]:
        """Dictionary representation."""
        
        sig_dict = self.signature if isinstance(self.signature, dict) else {}
        if not sig_dict and hasattr(self, 'name'):
             if isinstance(self, (Function, Method)):
                 sig_dict = {'full': f"{self.name}()"}
            
        return {
            "component_kind": self.__class__.__name__.lower(),
            "access_modifier": self.access_modifier,
            "is_public": self.is_public,
            "name": self.name,
            "API_name": self.API_name,
            "API_names": self.API_names,
            "fully_qualified_name": self.fully_qualified_name,
            "signature": sig_dict,
            "docstring": self.docstring,
            "body": self.body,
            "source_file": self.source_file,
            "line_number": self.line_number,
            "end_line": self.end_line,
            "definition_module_fqn": self.definition_module_fqn,
            "is_module_with_all": self.is_module_with_all,
            "exported_by_default": self.exported_by_default,
            "best_export_chain": self.best_export_chain,
            "all_export_chains": self.all_export_chains,
            "is_chain_candidate": self.is_chain_candidate,
            "parent_fqn": self.parent_fqn,
            "dependencies": list(self.dependencies),
            "doc_url": self.doc_url,
            "reference_documentation": self.reference_documentation,
            "metadata": self.metadata,
        }


@dataclass
class Function(CodeComponent):
    """
    Function component with decorator support and structured parameter information.
    
    Additional Attributes:
        returns: Return type annotation
        is_async: Whether function is async
        decorator_chain: Decorator information
        unwrapped_function: Unwrapped function data
        original_function: Original function reference
        parameters: Structured parameter information
    """
    is_async: bool = False
    returns: Optional[str] = None
    _func_obj: Optional[Callable] = None
    original_function: Optional['Function'] = None
    unwrapped_function: Optional[UnwrappedFunction] = None
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    decorator_chain: List[DecoratorInfo] = field(default_factory=list)

    def __post_init__(self):
        """Initialize function-specific attributes."""
        super().__post_init__()
        if self._func_obj and not self.unwrapped_function:
            self._initialize_unwrapped_function()
            
        # Extract parameter information from signature
        if isinstance(self.signature, dict) and 'parameters' in self.signature:
            self.parameters = self.signature['parameters']

    def _initialize_unwrapped_function(self):
        """Initialize unwrapped function data."""
        try:
            self.unwrapped_function = UnwrappedFunction.from_function(
                self._func_obj,
                max_depth=3  # Configurable depth
            )
        except Exception as e:
            logger.warning(f"Failed to unwrap function {self.name}: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Dictionary representation with function details and parameters."""
        base_dict = super().to_dict()
        function_dict = {
            "parameters": self.parameters,
            "returns": self.returns,
            "is_async": self.is_async,
            "decorators": [
                {
                    "name": d.name,
                    "is_factory": d.is_factory,
                    "args": d.args,
                    "kwargs": d.kwargs,
                    "affects_signature": d.affects_signature
                }
                for d in self.decorator_chain
            ]
        }
        
        if self.unwrapped_function:
            function_dict["unwrapped"] = {
                "original_signature": str(self.unwrapped_function.original_signature),
                "modified_signature": str(self.unwrapped_function.modified_signature),
                "preserved_attributes": self.unwrapped_function.preserved_attributes
            }
            
        base_dict.update(function_dict)
        return base_dict
    
    def __contains__(self, item):
        """Allow 'in' operator to check for attributes."""
        return hasattr(self, item)


@dataclass
class Method(Function):
    """
    Method component with property and inheritance support.
    
    Additional Attributes:
        instance_var: Instance variable name
        class_name: Containing class name
        is_static: Whether method is static
        is_property: Whether method is a property
        property_type: Type of property (getter/setter/deleter)
        is_abstract: Whether method is abstract
        is_override: Whether method overrides a base class method
        base_method: Reference to overridden base method
    """
    instance_var: str = ""
    class_name: str = ""
    is_static: bool = False
    is_property: bool = False
    property_type: Optional[str] = None
    is_abstract: bool = False
    is_override: bool = False
    base_method: Optional['Method'] = None

    def __post_init__(self):
        """Initialize method-specific attributes."""
        super().__post_init__()
        self._analyze_method_type()

    def _analyze_method_type(self):
        """Analyze method type from decorators."""
        for decorator in self.decorator_chain:
            if decorator.name == 'abstractmethod':
                self.is_abstract = True
            elif decorator.name == 'property':
                self.is_property = True
                self.property_type = 'getter'
            elif decorator.name in {'getter', 'setter', 'deleter'}:
                self.is_property = True
                self.property_type = decorator.name

    def to_dict(self) -> Dict[str, Any]:
        """Dictionary representation with method details."""
        base_dict = super().to_dict()
        method_dict = {
            "is_static": self.is_static,
            "is_property": self.is_property,
            "property_type": self.property_type,
            "is_abstract": self.is_abstract,
            "is_override": self.is_override,
            "instance_var": self.instance_var,
            "class": self.class_name
        }
        
        if self.base_method:
            method_dict["overrides"] = {
                "class": self.base_method.class_name,
                "method": self.base_method.name
            }
            
        base_dict.update(method_dict)
        return base_dict


@dataclass
class Class(CodeComponent):
    """
    Class component with improved inheritance and nested class support.
    
    Additional Attributes:
        methods: Class methods
        constructor: Constructor method
        bases: Base class names
        base_fqns: Fully qualified base class names
        class_variables: Class variables with type info
        instance_variables: Instance variables
        nested_classes: Nested class definitions
        is_nested: Whether class is nested
        outer_class: Containing class if nested
        abstract_methods: Abstract method requirements
        property_info: Property inheritance info
        is_exception_fallback: Whether class is a fallback definition inside an except block
    """
    methods: List[Method] = field(default_factory=list)
    constructor: Optional[Method] = None
    bases: List[str] = field(default_factory=list)
    base_fqns: List[str] = field(default_factory=list)
    class_variables: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    instance_variables: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    nested_classes: List['Class'] = field(default_factory=list)
    is_nested: bool = False
    outer_class: Optional[str] = None
    outer_class_fqn: Optional[str] = None
    abstract_methods: Set[str] = field(default_factory=set)
    property_info: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    is_exception_fallback: bool = False 

    def __post_init__(self):
        """Initialize class-specific attributes."""
        super().__post_init__()
        self._analyze_class_structure()

    def _analyze_class_structure(self):
        """Analyze class structure and relationships."""
        # Track abstract methods
        for method in self.methods:
            if method.is_abstract:
                self.abstract_methods.add(method.name)
                
        # Track properties
        for method in self.methods:
            if method.is_property:
                if method.name not in self.property_info:
                    self.property_info[method.name] = {}
                self.property_info[method.name][method.property_type or 'getter'] = method
                
        # Track nested relationships
        if self.is_nested:
            self.metadata['nesting_level'] = len(self.fully_qualified_name.split('.')) - 1

    def get_property_methods(self, name: str) -> Dict[str, Method]:
        """Get all methods for a property."""
        return self.property_info.get(name, {})

    def get_concrete_methods(self) -> List[Method]:
        """Get non-abstract methods."""
        return [m for m in self.methods if not m.is_abstract]

    def to_dict(self) -> Dict[str, Any]:
        """Dictionary representation with class details."""
        
        base_dict = super().to_dict()
        
        # Generate constructor signature from __init__ method
        constructor_signature = {}
        if self.constructor:
            # Extract constructor signature but replace __init__ with class name
            for variant, sig in self.constructor.signature.items():
                if sig.startswith('__init__'):
                    # Replace __init__ with class name
                    constructor_signature[variant] = sig.replace('__init__', self.name, 1)
                elif sig.startswith('__new__'):
                    # Replace __new__ with class name
                    constructor_signature[variant] = sig.replace('__new__', self.name, 1)
                else:
                    constructor_signature[variant] = sig
        else:
            # Default constructor signature if not found
            constructor_signature = {'full': f"{self.name}()"}
        
        # Use constructor signature as the class signature
        base_dict["signature"] = constructor_signature
        
        class_dict = {
            "constructor": self.constructor.to_dict() if self.constructor else None,
            "methods": [method.to_dict() for method in self.methods],
            "nested_classes": [cls.to_dict() for cls in self.nested_classes],
            "bases": self.bases,
            "base_fqns": self.base_fqns,
            "class_variables": self.class_variables,
            "instance_variables": self.instance_variables,
            "is_nested": self.is_nested,
            "outer_class": self.outer_class,
            "outer_class_fqn": self.outer_class_fqn,
            "abstract_methods": list(self.abstract_methods),
            "is_exception_fallback": self.is_exception_fallback,
            "properties": {
                name: {
                    type_: method.name
                    for type_, method in methods.items()
                }
                for name, methods in self.property_info.items()
            }
        }
        
        base_dict.update(class_dict)
        return base_dict
    
    def __iter__(self):
        """Make ModuleDefinition iterable like a dictionary."""
        # Return iterator over the public data attributes 
        for key in self.__dict__:
            if not key.startswith('_'):
                yield key
    
    def __getitem__(self, key):
        """Allow dictionary-style access to attributes."""
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(f"ModuleDefinition has no attribute '{key}'")
    
    def __contains__(self, item):
        """Allow 'in' operator to check for attributes."""
        return hasattr(self, item)

@dataclass
class Variable(CodeComponent):
    """
    Module-level variable/constant.
    """
    value_repr: Optional[str] = None        # Unparsed RHS or literal repr
    type_annotation: Optional[str] = None   # Annotation text for AnnAssign

    def __post_init__(self):
        super().__post_init__()

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "component_kind": "variable",
            "value": self.value_repr,
            "type_annotation": self.type_annotation,
        })
        return base