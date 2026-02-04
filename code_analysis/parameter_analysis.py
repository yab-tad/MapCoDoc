"""
Parameter analysis module for extracting and managing function/method signatures.
Provides parameter tracking and signature generation with structured data.
"""

import ast
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set, Union, Tuple


logger = logging.getLogger(__name__)


@dataclass
class ParameterInfo:
    """Structured information about a single parameter."""
    name: str
    type_annotation: Optional[str] = None
    default_value: Optional[str] = None
    is_positional_only: bool = False
    is_keyword_only: bool = False
    is_vararg: bool = False  # *args
    is_kwarg: bool = False   # **kwargs
    
    @property
    def has_default(self) -> bool:
        """Check if parameter has a default value."""
        return self.default_value is not None
    
    @property
    def specificity_score(self) -> int:
        """
        Calculate how specific this parameter is for overload resolution.
        Lower scores mean more general parameters.
        """
        score = 0
        
        # No type annotation is most general
        if not self.type_annotation:
            score += 0
        # Union and Optional types are somewhat general
        elif 'Union' in self.type_annotation or 'Optional' in self.type_annotation:
            score += 5
        # Generic types are somewhat general
        elif self.type_annotation in ['Any', 'object', 'typing.Any']:
            score += 2
        # Concrete types are most specific
        else:
            score += 10
            
        # Default values make a parameter more general
        if self.has_default:
            score -= 3
            
        # Variadic parameters are very general
        if self.is_vararg or self.is_kwarg:
            score -= 5
            
        return score


@dataclass
class Parameter:
    """
    Handles complete parameter variation extraction for a function/method node.
    Parameters can include:
    - Regular parameters (x, y, z)
    - Type-annotated parameters (x: int, y: str)
    - Default values (x = 1, y = "default")
    - Variable arguments (*args, **kwargs)
    - Position-only parameters with /
    - Keyword-only parameters with *
    """
    
    node: ast.FunctionDef
    variations: Dict[str, List[str]] = field(default_factory=dict)
    parameters: List[ParameterInfo] = field(default_factory=list)
    
    def __post_init__(self):
        """Extract parameter variations after initialization."""
        self._extract_structured_parameters()
        self._extract_variations()
    
    def _get_param_str(self, arg: ast.arg, annotation: bool = True) -> str:
        """Generate parameter string with optional type annotation."""
        arg_str = arg.arg
        if annotation and arg.annotation:
            arg_type = ast.unparse(arg.annotation)
            arg_str += f': {arg_type}'
        return arg_str
    
    def _get_default_str(self, default_node: ast.AST) -> str:
        """Get default value string."""
        return ast.unparse(default_node)
    
    
    def _extract_structured_parameters(self) -> None:
        """
        Extract structured parameter information.
        This builds a list of ParameterInfo objects with detailed information.
        """
        
        self.parameters = []
        
        # Process positional-only parameters
        for i, arg in enumerate(self.node.args.posonlyargs):
            param_info = ParameterInfo(
                name=arg.arg,
                type_annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                is_positional_only=True
            )
            
            # Check for default value
            if i >= len(self.node.args.posonlyargs) - len(self.node.args.defaults):
                default_index = i - (len(self.node.args.posonlyargs) - len(self.node.args.defaults))
                param_info.default_value = self._get_default_str(self.node.args.defaults[default_index])
                
            self.parameters.append(param_info)
        
        # Process regular parameters
        for i, arg in enumerate(self.node.args.args):
            param_info = ParameterInfo(
                name=arg.arg,
                type_annotation=ast.unparse(arg.annotation) if arg.annotation else None
            )
            
            # Check for default value
            if i >= len(self.node.args.args) - len(self.node.args.defaults):
                default_index = i - (len(self.node.args.args) - len(self.node.args.defaults))
                param_info.default_value = self._get_default_str(self.node.args.defaults[default_index])
                
            self.parameters.append(param_info)
        
        # Process *args
        if self.node.args.vararg:
            param_info = ParameterInfo(
                name=self.node.args.vararg.arg,
                type_annotation=ast.unparse(self.node.args.vararg.annotation) if self.node.args.vararg.annotation else None,
                is_vararg=True
            )
            self.parameters.append(param_info)
        
        # Process keyword-only parameters
        for i, arg in enumerate(self.node.args.kwonlyargs):
            param_info = ParameterInfo(
                name=arg.arg,
                type_annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                is_keyword_only=True
            )
            
            # Check for default value
            if self.node.args.kw_defaults[i]:
                param_info.default_value = self._get_default_str(self.node.args.kw_defaults[i])
                
            self.parameters.append(param_info)
        
        # Process **kwargs
        if self.node.args.kwarg:
            param_info = ParameterInfo(
                name=self.node.args.kwarg.arg,
                type_annotation=ast.unparse(self.node.args.kwarg.annotation) if self.node.args.kwarg.annotation else None,
                is_kwarg=True
            )
            self.parameters.append(param_info)
    
    
    def _extract_base_params(self, include_types: bool = True, include_asterisk: bool = True, include_slash: bool = True) -> List[str]:
        """
        Extract parameters with specified variation flags.
        Handles:
        - Regular parameters
        - Type annotations (if include_types is True)
        - Position-only parameters and slash separator (if include_slash is True)
        - Keyword-only parameters and asterisk separator (if include_asterisk is True)
        """
        
        params = []
        
        # Handle positional-only arguments and the slash separator
        if self.node.args.posonlyargs:
            params.extend([self._get_param_str(arg, include_types) for arg in self.node.args.posonlyargs])
            if include_slash:
                params.append('/')
        
        # Handle positional and optional arguments
        for i, arg in enumerate(self.node.args.args):
            arg_str = self._get_param_str(arg, include_types)
            # Add default value if exists
            if i >= len(self.node.args.args) - len(self.node.args.defaults):
                default_index = i - (len(self.node.args.args) - len(self.node.args.defaults))
                default_value = self._get_default_str(self.node.args.defaults[default_index])
                arg_str += f'={default_value}'
            params.append(arg_str)
        
        # Add bare '*' for keyword-only arguments if needed
        if self.node.args.kwonlyargs and not self.node.args.vararg and include_asterisk:
            params.append('*')
        
        # Handle keyword-only arguments
        for j, kwarg in enumerate(self.node.args.kwonlyargs):
            kwarg_str = self._get_param_str(kwarg, include_types)
            if self.node.args.kw_defaults[j]:
                default_value = self._get_default_str(self.node.args.kw_defaults[j])
                kwarg_str += f'={default_value}'
            params.append(kwarg_str)
        
        # Handle *args
        if self.node.args.vararg:
            vararg = f'*{self._get_param_str(self.node.args.vararg, include_types)}'
            params.append(vararg)
        
        # Handle **kwargs
        if self.node.args.kwarg:
            kwarg = f'**{self._get_param_str(self.node.args.kwarg, include_types)}'
            params.append(kwarg)
        
        return params
    
                
    def _extract_variations(self) -> None:
        """Extract all parameter variations."""
        
        # Check what features the function actually uses
        has_types = any(arg.annotation for arg in (
            self.node.args.posonlyargs + 
            self.node.args.args + 
            self.node.args.kwonlyargs + 
            ([self.node.args.vararg] if self.node.args.vararg else []) +
            ([self.node.args.kwarg] if self.node.args.kwarg else [])
        ))
        
        # Detect actual use of / and * in the signature
        has_slash_separator = bool(self.node.args.posonlyargs)
        has_bare_asterisk = bool(self.node.args.kwonlyargs) and not self.node.args.vararg
        
        # Initialize variations with full version
        full = self._extract_base_params(True, True, True)
        default_only = self._extract_base_params(False, False, False)
        
        # Dictionary to track unique parameter lists with their variation names
        unique_variations = {}
        
        # Helper function to add variation only if its parameter list is unique
        def add_variation(name: str, params: List[str]) -> None:
            # Convert params list to tuple for hashability
            params_tuple = tuple(params)
            # Only add if this parameter list is new
            if params_tuple not in unique_variations.values():
                unique_variations[name] = params_tuple
        
        # Add initial variations
        add_variation('full', full)
        if default_only and tuple(full) != tuple(default_only):
            add_variation('defaults_only', default_only)
        
        # Add other variations only if they produce unique parameter lists
        if has_slash_separator and has_bare_asterisk:
            add_variation('no_special', self._extract_base_params(True, False, False))
        if has_slash_separator:
            add_variation('no_slash', self._extract_base_params(True, True, False))
        if has_bare_asterisk:
            add_variation('no_asterisk', self._extract_base_params(True, False, True))
        if has_types:
            add_variation('no_types', self._extract_base_params(False, True, True))
            if has_slash_separator:
                add_variation('no_types_no_slash', self._extract_base_params(False, True, False))
            if has_bare_asterisk:
                add_variation('no_types_no_asterisk', self._extract_base_params(False, False, True))
        
        # Convert back to the original format with lists
        self.variations = {name: list(params) for name, params in unique_variations.items()}
        
        # Always ensure at least one variation exists
        if not self.variations:
            self.variations = {'full': []}
    
    
    def get_variations(self) -> Dict[str, List[str]]:
        """Get all parameter variations."""
        return self.variations
    
    
    def get_parameters(self) -> List[ParameterInfo]:
        """Get structured parameter information."""
        return self.parameters
    
    
    def calculate_signature_specificity(self) -> int:
        """
        Calculate overall signature specificity for overload analysis.
        Lower scores mean more general signatures.
        """
        # Base score
        score = 0
        
        # Add up parameter specificity
        for param in self.parameters:
            score += param.specificity_score
        
        # Variadic parameters (*args, **kwargs) make a signature more general
        has_vararg = any(p.is_vararg for p in self.parameters)
        has_kwarg = any(p.is_kwarg for p in self.parameters)
        
        if has_vararg:
            score -= 15  # *args makes a signature much more general
        if has_kwarg:
            score -= 15  # **kwargs makes a signature much more general
            
        # Signatures with more parameters are generally more specific
        score += len(self.parameters) * 2
        
        return score



@dataclass
class Signature:
    """
    Function/method signature manager.
    
    Handles signature generation and variations for functions and methods.
    """
    node_name: str
    param_variations: Dict[str, List[str]]
    parameters: List[ParameterInfo]
    instance_var: Optional[str] = None
    return_annotation: Optional[str] = None
    specificity_score: int = 0
    
    def generate_signatures(self) -> Dict[str, str]:
        """
        Generate all signature variations.
        
        Returns:
            Dictionary mapping variation names to signature strings
        """
        
        signatures = {}
        
        for variation, params in self.param_variations.items():
            # Skip instance variable for methods
            param_list = params[1:] if params and self.instance_var and params[0] == self.instance_var else params
            param_str = ', '.join(param_list) if param_list else ''
            
            signatures[variation] = f"{self.node_name}({param_str})"
            
        # Ensure we always have at least one signature
        if not signatures:
            signatures['full'] = f"{self.node_name}()"    
                            
        return signatures
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert signature to dictionary for serialization.
        
        Returns:
            Dictionary with signature information
        """
        return {
            'name': self.node_name,
            'signatures': self.generate_signatures(),
            'parameters': [
                {
                    'name': p.name,
                    'type': p.type_annotation,
                    'default': p.default_value,
                    'is_positional_only': p.is_positional_only,
                    'is_keyword_only': p.is_keyword_only,
                    'is_vararg': p.is_vararg,
                    'is_kwarg': p.is_kwarg
                } for p in self.parameters
            ],
            'return_type': self.return_annotation,
            'specificity_score': self.specificity_score,
            'overload_category': self._categorize_overload()
        }
    
    def _categorize_overload(self) -> str:
        """
        Categorize overload by its pattern.
        
        Returns:
            String category: 'primary', 'secondary', or 'specific'
        """
        # Check for patterns that indicate a primary implementation
        has_vararg = any(p.is_vararg for p in self.parameters)
        has_kwarg = any(p.is_kwarg for p in self.parameters)
        all_params_have_defaults = all(p.has_default for p in self.parameters 
                                      if not (p.is_vararg or p.is_kwarg))
        
        if (has_vararg and has_kwarg) or \
           (all_params_have_defaults and len(self.parameters) >= 3):
            return 'primary'  # Likely the main implementation
            
        # Check for patterns that indicate a very specific overload
        all_params_concrete_types = all(p.type_annotation and 
                                       not ('Union' in (p.type_annotation or '') or 
                                           'Any' in (p.type_annotation or ''))
                                       for p in self.parameters 
                                       if not (p.is_vararg or p.is_kwarg))
        
        if all_params_concrete_types and len(self.parameters) >= 2:
            return 'specific'  # Highly specific variant
            
        # Default is secondary
        return 'secondary'


def analyze_signature(node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
                     include_instance_var: bool = True) -> Dict[str, Any]:
    """
    Analyze function/method signature with structured parameter information.
    
    Args:
        node: AST node to analyze
        include_instance_var: Whether to include instance variable (self)
        
    Returns:
        Dictionary with signature information including structured parameters
    """
    # Extract parameters
    param_handler = Parameter(node)
    parameters = param_handler.get_parameters()
    
    # Get instance variable if present and requested
    instance_var = None
    if include_instance_var and node.args.args:
        instance_var = node.args.args[0].arg
        
    # Calculate signature specificity
    specificity_score = param_handler.calculate_signature_specificity()
        
    # Create signature
    signature = Signature(
        node_name=node.name,
        param_variations=param_handler.variations,
        parameters=parameters,
        instance_var=instance_var,
        return_annotation=ast.unparse(node.returns) if node.returns else None,
        specificity_score=specificity_score
    )
    
    # Return serializable dictionary with all signature information
    return signature.to_dict()