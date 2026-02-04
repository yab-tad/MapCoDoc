"""
Tests for call analysis and Data Flow Analysis (DFA) within CodeVisitor.
Focuses on:
- Correct FQN resolution for callees (_resolve_call_target).
- Accurate extraction of argument sources for DFA metadata.
- Ensuring CallGraphTracker.add_call is invoked with correct parameters.
"""
import pytest
from unittest.mock import MagicMock, call

from code_analysis.code_visitor import analyze_code
from code_analysis.graph.call_graph import CallGraphTracker
from code_analysis.config import AnalysisConfig
from code_analysis.definition_registry import DefinitionRegistry

# --- Fixtures ---

@pytest.fixture
def mock_call_tracker() -> MagicMock:
    """Provides a MagicMock for CallGraphTracker."""
    return MagicMock(spec=CallGraphTracker)

@pytest.fixture
def analysis_config() -> AnalysisConfig:
    """Provides a default AnalysisConfig."""
    return AnalysisConfig()

@pytest.fixture
def definition_registry() -> DefinitionRegistry:
    """Provides a DefinitionRegistry instance."""
    return DefinitionRegistry()

# --- Helper ---

def run_code_visitor_on_snippet(
    code_snippet: str,
    module_name: str,
    mock_call_tracker_fixture: MagicMock,
    config_fixture: AnalysisConfig,
    def_reg_fixture: DefinitionRegistry,
    package_name: str = "test_pkg"
):
    """
    Runs analyze_code on a snippet and allows inspection of mock_call_tracker.
    """
    analyze_code(
        code=code_snippet,
        module_name=module_name,
        package_name=package_name,
        source_file=f"{module_name.replace('.', '/')}.py",
        config=config_fixture,
        definition_registry=def_reg_fixture,
        call_tracker=mock_call_tracker_fixture,
        top_level_packages={package_name} 
    )
    return mock_call_tracker_fixture # Return for convenience in tests

# --- Test Cases for Callee FQN Resolution ---

def test_resolve_local_function_call(mock_call_tracker, analysis_config, definition_registry):
    """Test resolving a call to a locally defined function."""
    code = """
def bar(): pass
def foo():
    bar()
"""
    module_name = "test_module_local_func"
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)
    
    expected_caller = f"{module_name}.foo"
    expected_callee = f"{module_name}.bar"
    
    found_call = False
    for c in mock_call_tracker.add_call.call_args_list:
        if c.kwargs.get('caller') == expected_caller and c.kwargs.get('callee') == expected_callee:
            found_call = True
            break
    assert found_call, f"Call from {expected_caller} to {expected_callee} not found."

def test_resolve_imported_function_call(mock_call_tracker, analysis_config, definition_registry):
    """Test resolving 'import x.y; x.y()'."""
    code = """
import other_module.sub_module
def foo():
    other_module.sub_module.baz()
"""
    # Assume other_module.sub_module.baz is defined elsewhere and known via imports for full resolution
    # For this test, we focus on what CodeVisitor can statically determine from the AST.
    # CodeVisitor's _resolve_call_target will construct "other_module.sub_module.baz"
    # based on the AST structure if "other_module.sub_module" is recognized as an import path.
    # Actual existence isn't checked by _resolve_call_target itself, but by how imports are processed.
    
    module_name = "test_module_import_func"
    
    # Mock that 'other_module.sub_module' is an imported name pointing to itself for simplicity in this test
    # In a real run, definition_registry and import processing would establish this.
    # Here, we rely on the internal `self.context.imported_names` that `visit_Import` populates.
    # The analyze_code function runs the visitor, which processes imports.
    
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)

    expected_caller = f"{module_name}.foo"
    # The _resolve_call_target logic for ast.Attribute tries to piece together the base.
    # `other_module.sub_module` becomes the base_name.
    # If `other_module` is in imported_names, it might resolve to `other_module.sub_module.baz`.
    # If `other_module.sub_module` is in imported_names, it resolves to `other_module.sub_module.baz`.
    # The visit_Import for 'import other_module.sub_module' will add:
    # context.imported_names['other_module'] = 'other_module'
    expected_callee = "other_module.sub_module.baz" 

    found_call = False
    for c in mock_call_tracker.add_call.call_args_list:
        if c.kwargs.get('caller') == expected_caller and c.kwargs.get('callee') == expected_callee:
            found_call = True
            break
    assert found_call, f"Call from {expected_caller} to {expected_callee} not found. Calls: {mock_call_tracker.add_call.call_args_list}"


def test_resolve_from_imported_function_call(mock_call_tracker, analysis_config, definition_registry):
    """Test resolving 'from x import y; y()'."""
    code = """
from another_lib import actual_func
def foo():
    actual_func()
"""
    module_name = "test_module_from_import_func"
    # visit_ImportFrom will populate context.imported_names['actual_func'] = 'another_lib.actual_func'
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)
    
    expected_caller = f"{module_name}.foo"
    expected_callee = "another_lib.actual_func"
    
    found_call = False
    for c in mock_call_tracker.add_call.call_args_list:
        if c.kwargs.get('caller') == expected_caller and c.kwargs.get('callee') == expected_callee:
            found_call = True
            break
    assert found_call, f"Call from {expected_caller} to {expected_callee} not found."

def test_resolve_self_method_call(mock_call_tracker, analysis_config, definition_registry):
    """Test resolving 'self.method()'."""
    code = """
class MyClass:
    def method_one(self):
        pass
    def method_two(self):
        self.method_one()
"""
    module_name = "test_module_self_method"
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)
    
    class_fqn = f"{module_name}.MyClass"
    expected_caller = f"{class_fqn}.method_two"
    expected_callee = f"{class_fqn}.method_one"
    
    found_call = False
    for c in mock_call_tracker.add_call.call_args_list:
        if c.kwargs.get('caller') == expected_caller and c.kwargs.get('callee') == expected_callee:
            found_call = True
            break
    assert found_call, f"Call from {expected_caller} to {expected_callee} not found. Calls: {mock_call_tracker.add_call.call_args_list}"

# Add more FQN resolution tests:
# test_resolve_cls_method_call, test_resolve_static_method_call_on_class
# test_unresolved_complex_attribute_call, test_unresolved_call_on_variable

# --- Test Cases for DFA Argument Analysis ---

def get_call_args_for_callee(mock_tracker: MagicMock, callee_name: str) -> dict:
    """Helper to find the call arguments for a specific callee."""
    for c_args in mock_tracker.add_call.call_args_list:
        if c_args.kwargs.get('callee') == callee_name:
            return c_args.kwargs.get('metadata', {})
    return {}

def test_dfa_constant_args(mock_call_tracker, analysis_config, definition_registry):
    """Test DFA for constant arguments: func(1, "s", True, None)."""
    code = """
def target_func(a,b,c,d): pass
def caller_func():
    target_func(1, "hello", True, None)
"""
    module_name = "test_dfa_const"
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)
    
    callee_fqn = f"{module_name}.target_func"
    call_meta = get_call_args_for_callee(mock_call_tracker, callee_fqn)
    
    assert "arguments" in call_meta
    args_data = call_meta["arguments"]
    assert len(args_data) == 4
    assert args_data[0] == {"position": 0, "source_type": "constant", "value": "1"}
    assert args_data[1] == {"position": 1, "source_type": "constant", "value": "'hello'"}
    assert args_data[2] == {"position": 2, "source_type": "constant", "value": "True"}
    assert args_data[3] == {"position": 3, "source_type": "constant", "value": "None"}

def test_dfa_variable_args(mock_call_tracker, analysis_config, definition_registry):
    """Test DFA for variable arguments: a=1; b="s"; func(a, b)."""
    code = """
def target_func(p1, p2): pass
def caller_func():
    a = 10
    b = "world"
    target_func(a, b)
"""
    module_name = "test_dfa_var"
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)
    
    callee_fqn = f"{module_name}.target_func"
    call_meta = get_call_args_for_callee(mock_call_tracker, callee_fqn)
    
    assert "arguments" in call_meta
    args_data = call_meta["arguments"]
    assert len(args_data) == 2
    assert args_data[0] == {"position": 0, "source_type": "variable", "name": "a"}
    assert args_data[1] == {"position": 1, "source_type": "variable", "name": "b"}

def test_dfa_attribute_args(mock_call_tracker, analysis_config, definition_registry):
    """Test DFA for attribute arguments: obj.x = 1; func(obj.x)."""
    code = """
class Data:
    x = 0
def target_func(p1): pass
def caller_func():
    d = Data()
    d.x = 100
    target_func(d.x)
"""
    module_name = "test_dfa_attr"
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)
    
    callee_fqn = f"{module_name}.target_func"
    call_meta = get_call_args_for_callee(mock_call_tracker, callee_fqn)
    
    assert "arguments" in call_meta
    args_data = call_meta["arguments"]
    assert len(args_data) == 1
    # ast.unparse('d.x') would be 'd.x'
    # The fallback logic also produces 'd.x'
    assert args_data[0] == {"position": 0, "source_type": "attribute", "name": "d.x"}


def test_dfa_call_result_args(mock_call_tracker, analysis_config, definition_registry):
    """Test DFA for call result arguments: func(another_func())."""
    code = """
def another_func(): return 5
def target_func(p1): pass
def caller_func():
    target_func(another_func())
"""
    module_name = "test_dfa_call_res"
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)
    
    target_callee_fqn = f"{module_name}.target_func"
    another_func_fqn = f"{module_name}.another_func"
    call_meta = get_call_args_for_callee(mock_call_tracker, target_callee_fqn)
    
    assert "arguments" in call_meta
    args_data = call_meta["arguments"]
    assert len(args_data) == 1
    assert args_data[0] == {"position": 0, "source_type": "call_result", "callee": another_func_fqn}


def test_dfa_keyword_args(mock_call_tracker, analysis_config, definition_registry):
    """Test DFA for keyword arguments."""
    code = """
def target_func(p1, p2): pass
def caller_func():
    val = 42
    target_func(p1="const_str", p2=val)
"""
    module_name = "test_dfa_kwargs"
    run_code_visitor_on_snippet(code, module_name, mock_call_tracker, analysis_config, definition_registry)

    callee_fqn = f"{module_name}.target_func"
    call_meta = get_call_args_for_callee(mock_call_tracker, callee_fqn)

    assert "keyword_arguments" in call_meta
    kwargs_data = call_meta["keyword_arguments"]
    assert len(kwargs_data) == 2
    assert {"name": "p1", "source_type": "constant", "value": "'const_str'"} in kwargs_data
    assert {"name": "p2", "source_type": "variable", "name": "val"} in kwargs_data

# Add more DFA tests:
# test_dfa_expression_args, test_dfa_mixed_args
# test_dfa_star_args_and_kwargs (to see how *args, **kwargs are captured)
