"""
Analyzes decorators and their effects on component names for documentation matching.
Handles common Python decorators and their impact on API documentation.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
import ast
import logging
from .code_components import DecoratorInfo

logger = logging.getLogger(__name__)


@dataclass
class DecoratorEffect:
    """
    Information about how a decorator affects a component.
    
    Attributes:
        original_name: The original function/method name
        modified_name: Name modified by decorator (e.g., property name)
        affects_name: If modifier affects name (boolean)
        property_type: Type of property (getter, setter, deleter)
        method_type: Type of method (static, class, instance)
        additional_names: Set of alternative names to consider
        decorator_chain: List of decorator information objects
    """
    original_name: str
    modified_name: Optional[str] = None
    affects_name: bool = False
    property_type: Optional[str] = None
    method_type: str = 'instance'  # default to instance method
    additional_names: Set[str] = field(default_factory=set)
    decorator_chain: List[Any] = field(default_factory=list)


class AttrDict(dict):
    """Dictionary that allows attribute-like access to its keys."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self
        
        self.setdefault('name', 'unknown')
        self.setdefault('is_factory', False)
        self.setdefault('args', [])
        self.setdefault('kwargs', {})
        self.setdefault('affects_signature', False)
        self.setdefault('affects_return_type', False)
        self.setdefault('preserves_metadata', True)
        self.setdefault('source_code', None)


class DecoratorAnalyzer:
    """
    Analyzes decorators and their effects on component names.
    Handles Python's built-in decorators and common patterns.
    """
    
    PROPERTY_METHODS = {'getter', 'setter', 'deleter'}
    METHOD_DECORATORS = {'staticmethod', 'classmethod', 'abstractmethod'}
    
    def analyze_decorators(self, node: ast.FunctionDef) -> DecoratorEffect:
        """
        Analyze decorators and their effects on a function/method.
        
        Args:
            node: AST node of the function/method
            
        Returns:
            DecoratorEffect containing name modifications
        """
        
        effect = DecoratorEffect(original_name=node.name)
        
        # Create decorator_chain entries
        for decorator in node.decorator_list:
            try:
                decorator_info = self._extract_decorator_info(decorator)
                effect.decorator_chain.append(decorator_info)
                
                # Check for name-affecting decorators
                if decorator_info.name == 'property':
                    effect.affects_name = True
                    # For property, check for get_* naming pattern
                    if node.name.startswith('get_'):
                        effect.modified_name = node.name[4:]  # Strip 'get_' prefix
                
                elif decorator_info.name in {'setter', 'deleter'}:
                    effect.affects_name = True
                    # For property accessors, extract the property name
                    if '.' in decorator_info.name:
                        # property_name.setter pattern
                        effect.modified_name = decorator_info.name.split('.')[0]
                    else:
                        # Standalone accessor - might affect name
                        pass
                
                # Add more name-affecting decorators as needed
                
            except Exception as e:
                logger.warning(f"Error extracting decorator info for {node.name}: {e}")
        
        # Process decorators in reverse order (as they are applied)
        for decorator in reversed(node.decorator_list):
            try:
                self._analyze_single_decorator(decorator, effect)
            except Exception as e:
                logger.warning(f"Error analyzing decorator for {node.name}: {e}")
                
        return effect
    
    def _extract_decorator_info(self, decorator: ast.AST) -> AttrDict:
        """Extract basic information about a decorator with attributes."""
        
        info = AttrDict()  # Now has all required attributes
    
        try:
            if isinstance(decorator, ast.Name):
                info.name = decorator.id
                # Check if known decorator that affects signature
                if info.name in {'property', 'staticmethod', 'classmethod', 'cached_property', 
                            'abstractmethod', 'abstractproperty'}:
                    info.affects_signature = True
                    
            elif isinstance(decorator, ast.Attribute):
                # Handle attribute decorators
                if isinstance(decorator.value, ast.Name):
                    info.name = f"{decorator.value.id}.{decorator.attr}"
                else:
                    info.name = decorator.attr
                    
                # Check for property operations
                if info.name.endswith('.setter') or info.name.endswith('.deleter') or info.name.endswith('.getter'):
                    info.affects_signature = True
                    
            elif isinstance(decorator, ast.Call):
                # Handle decorator calls
                if isinstance(decorator.func, ast.Name):
                    info.name = decorator.func.id
                    info.is_factory = True
                elif isinstance(decorator.func, ast.Attribute):
                    if isinstance(decorator.func.value, ast.Name):
                        info.name = f"{decorator.func.value.id}.{decorator.func.attr}"
                    else:
                        info.name = decorator.func.attr
                    info.is_factory = True
                    
                # Extract args and kwargs
                info.args = []
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant):
                        info.args.append(arg.value)
                    else:
                        # Non-constant arg, use placeholder
                        info.args.append(None)
                        
                info.kwargs = {}
                for kw in decorator.keywords:
                    if isinstance(kw.value, ast.Constant):
                        info.kwargs[kw.arg] = kw.value.value
                    else:
                        # Non-constant kwarg, use placeholder
                        info.kwargs[kw.arg] = None
                
                # Check if known decorator that affects signature
                if info.name in {'wraps', 'lru_cache', 'cache', 'retry', 'deprecated', 
                            'overload', 'singledispatch'}:
                    info.affects_signature = True
                    
            # Set source code if possible
            if hasattr(decorator, 'lineno'):
                info.source_code = f"Decorator at line {decorator.lineno}"
                
        except Exception as e:
            logger.debug(f"Error extracting decorator info: {e}")
            # The AttrDict will still have default values for all attributes
            
        return info
    
    def _analyze_single_decorator(self, decorator: ast.AST, effect: DecoratorEffect) -> None:
        """
        Analyze a single decorator's effect on the component.
        
        Args:
            decorator: AST node of the decorator
            effect: DecoratorEffect to update
        """
        if isinstance(decorator, ast.Name):
            self._handle_simple_decorator(decorator.id, effect)
            
        elif isinstance(decorator, ast.Attribute):
            self._handle_attribute_decorator(decorator, effect)
            
        elif isinstance(decorator, ast.Call):
            self._handle_call_decorator(decorator, effect)
    
    def _handle_simple_decorator(self, name: str, effect: DecoratorEffect) -> None:
        """Handle simple name decorators (@property, @staticmethod, etc.)."""
        if name == 'property':
            effect.property_type = 'getter'
            effect.method_type = 'property'
            effect.additional_names.add(f"{effect.original_name}_property")
            
        elif name == 'staticmethod':
            effect.method_type = 'static'
            effect.additional_names.add(f"static_{effect.original_name}")
            
        elif name == 'classmethod':
            effect.method_type = 'class'
            effect.additional_names.add(f"class_{effect.original_name}")
            
        elif name == 'abstractmethod':
            effect.additional_names.add(f"abstract_{effect.original_name}")
    
    def _handle_attribute_decorator(self, node: ast.Attribute, effect: DecoratorEffect) -> None:
        """Handle attribute-based decorators (@property.setter, etc.)."""
        if isinstance(node.value, ast.Name):
            base_name = node.value.id
            attr_name = node.attr
            
            if base_name == effect.original_name and attr_name in self.PROPERTY_METHODS:
                # Handle @name.setter type decorators
                effect.property_type = attr_name
                effect.method_type = 'property'
                effect.modified_name = base_name
                effect.additional_names.add(f"{base_name}.{attr_name}")
                effect.additional_names.add(f"{base_name}_{attr_name}")
            
            elif base_name == 'property' and attr_name in self.PROPERTY_METHODS:
                # Handle @property.setter type decorators
                effect.property_type = attr_name
                effect.method_type = 'property'
                effect.additional_names.add(f"{effect.original_name}_{attr_name}")
    
    def _handle_call_decorator(self, node: ast.Call, effect: DecoratorEffect) -> None:
        """Handle decorator calls (@decorator(), etc.)."""
        func = node.func
        if isinstance(func, ast.Name):
            self._handle_simple_call_decorator(func.id, node.args, effect)
        elif isinstance(func, ast.Attribute):
            self._handle_attribute_call_decorator(func, node.args, effect)
    
    def _handle_simple_call_decorator(self, name: str, args: List[ast.AST], effect: DecoratorEffect) -> None:
        """Handle simple decorator calls."""
        if name == 'deprecated':
            effect.additional_names.add(f"deprecated_{effect.original_name}")
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    effect.additional_names.add(f"deprecated_{arg.value}")
                    break
    
    def _handle_attribute_call_decorator(self, func: ast.Attribute, args: List[ast.AST], effect: DecoratorEffect) -> None:
        """Handle attribute-based decorator calls."""
        if isinstance(func.value, ast.Name):
            base = func.value.id
            attr = func.attr
            
            # Handle common framework patterns
            if base in {'app', 'router'} and attr in {'route', 'get', 'post', 'put', 'delete'}:
                for arg in args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        route = arg.value.lstrip('/').replace('/', '_')
                        effect.additional_names.add(f"{attr}_{route}")
                        break


def get_documentation_names(effect: DecoratorEffect) -> Set[str]:
    """
    Get all possible names for documentation matching.
    
    Args:
        effect: Decorator analysis results
        
    Returns:
        Set of all possible names to try when matching documentation
    """
    names = {effect.original_name}
    
    if effect.modified_name:
        names.add(effect.modified_name)
    
    if effect.property_type:
        base_name = effect.modified_name or effect.original_name
        if effect.property_type == 'getter':
            names.add(f"{base_name}_property")
        names.add(f"{base_name}_{effect.property_type}")
        names.add(f"{base_name}.{effect.property_type}")
    
    if effect.method_type != 'instance':
        names.add(f"{effect.method_type}_{effect.original_name}")
    
    names.update(effect.additional_names)
    
    return names