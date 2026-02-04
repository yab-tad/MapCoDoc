import os
import pytest
from pathlib import Path

# Keep necessary imports
from code_analysis.config import AnalysisConfig
from code_analysis.analyzers.analyzer_integration import AnalyzerIntegration
from code_analysis.relationship_types import (
    NODE_TYPE_PACKAGE, NODE_TYPE_MODULE, REL_TYPE_CONTAINS,
    REL_TYPE_IMPORTS, REL_TYPE_INHERITS, REL_TYPE_CALLS
)

# Helper function to create files
def create_file(path: Path, content: str):
    """Creates a file with the given content, ensuring parent directories exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

@pytest.fixture(scope="module")
def sample_project(tmp_path_factory):
    """Creates a sample project structure in a temporary directory."""
    base_path = tmp_path_factory.mktemp("sample_project_root")
    project_path = base_path / "my_project"

    # Create files with content
    create_file(project_path / "__init__.py", "# Root package")
    create_file(project_path / "utils" / "__init__.py", "# Utils package")
    create_file(project_path / "utils" / "helpers.py", "def helper_func():\n    pass")
    create_file(project_path / "models" / "__init__.py", "class BaseModel:\n    pass")
    create_file(
        project_path / "models" / "user.py",
        """
from my_project.utils.helpers import helper_func
from . import BaseModel # Relative import

class User(BaseModel):
    def __init__(self, name):
        self.name = name
        helper_func() # Call

    def get_name(self):
        return self.name
"""
    )
    create_file(project_path / "services" / "__init__.py", "from my_project.models.user import User")
    create_file(
        project_path / "services" / "auth.py",
        """
from my_project.models.user import User

def authenticate_user(username):
    user_obj = User(username) # Call (__init__)
    name = user_obj.get_name() # Call
    print(f"Authenticating {name}")
    return True
"""
    )
    # Add a setup.py to mark the root for AnalyzerIntegration
    create_file(base_path / "setup.py", "# Empty setup.py")

    return base_path


@pytest.fixture(scope="module")
def analyzer(sample_project):
    """Initializes AnalyzerIntegration and analyzes the sample project."""
    config = AnalysisConfig()
    # Ensure repo_path is correctly set for module name resolution
    integration = AnalyzerIntegration(config=config)
    integration.repo_path = str(sample_project) # Explicitly set repo_path
    integration.analyze_codebase(str(sample_project))
    return integration

# --- Test Cases ---

def test_package_module_structure(analyzer: AnalyzerIntegration):
    """Verify package and module nodes and CONTAINS relationships."""
    store = analyzer.store
    graph = store.graph

    # Expected Nodes (Type: FQN)
    expected_nodes = {
        NODE_TYPE_PACKAGE: {"my_project", "my_project.utils", "my_project.models", "my_project.services"},
        NODE_TYPE_MODULE: {"my_project.utils.helpers", "my_project.models.user", "my_project.services.auth"}
    }

    # Check nodes and their types
    for node_type, fqns in expected_nodes.items():
        for fqn in fqns:
            assert fqn in graph.nodes, f"Node {fqn} not found in graph"
            assert graph.nodes[fqn].get('node_type') == node_type, f"Node {fqn} has incorrect type"

    # Expected CONTAINS Edges (Parent -> Child)
    expected_contains = [
        ("my_project", "my_project.utils"),
        ("my_project", "my_project.models"),
        ("my_project", "my_project.services"),
        ("my_project.utils", "my_project.utils.helpers"),
        ("my_project.models", "my_project.models.user"),
        ("my_project.services", "my_project.services.auth"),
    ]

    # Check CONTAINS edges
    contains_edges = {(u, v) for u, v, data in graph.edges(data=True) if data.get('edge_type') == REL_TYPE_CONTAINS}
    for edge in expected_contains:
        assert edge in contains_edges, f"CONTAINS edge {edge} not found"

def test_import_relationships(analyzer: AnalyzerIntegration):
    """Verify IMPORTS relationships."""
    store = analyzer.store
    graph = store.graph
    import_edges = {(u, v, data.get('imported_name')) for u, v, data in graph.edges(data=True) if data.get('edge_type') == REL_TYPE_IMPORTS}

    # Expected IMPORTS Edges (Importer -> Imported Module, Imported Name)
    expected_imports = [
        ("my_project.models.user", "my_project.utils.helpers", "helper_func"),
        ("my_project.models.user", "my_project.models", "BaseModel"), # Resolved relative import
        ("my_project.services", "my_project.models.user", "User"), # From services/__init__.py
        ("my_project.services.auth", "my_project.models.user", "User"),
    ]

    for importer, imported_module, name in expected_imports:
        # Check if an edge exists matching the criteria
        found = False
        for u, v, imp_name in import_edges:
            # Note: The store might store the target as the specific item FQN or the module FQN depending on tracker logic.
            # Let's check if the target *starts with* the expected module.
            # And check if the imported_name matches.
            if u == importer and v.startswith(imported_module) and imp_name == name:
                found = True
                break
        assert found, f"Import edge ({importer} -> {imported_module} [{name}]) not found or stored differently"


def test_inheritance_relationships(analyzer: AnalyzerIntegration):
    """Verify INHERITS relationships."""
    store = analyzer.store
    graph = store.graph
    inheritance_edges = {(u, v) for u, v, data in graph.edges(data=True) if data.get('edge_type') == REL_TYPE_INHERITS}

    # Expected INHERITS Edge (Child -> Parent)
    expected_inheritance = ("my_project.models.user.User", "my_project.models.BaseModel")

    assert expected_inheritance in inheritance_edges, f"Inheritance edge {expected_inheritance} not found"

def test_call_relationships(analyzer: AnalyzerIntegration):
    """Verify CALLS relationships."""
    store = analyzer.store
    graph = store.graph
    call_edges = {(u, v) for u, v, data in graph.edges(data=True) if data.get('edge_type') == REL_TYPE_CALLS}

    # Expected CALLS Edges (Caller -> Callee)
    expected_calls = [
        ("my_project.models.user.User.__init__", "my_project.utils.helpers.helper_func"),
        # Call to User() implicitly calls __init__ or __new__
        ("my_project.services.auth.authenticate_user", "my_project.models.user.User"), # Simplified target for class instantiation
        ("my_project.services.auth.authenticate_user", "my_project.models.user.User.get_name"),
    ]

    # Check calls - allow for slight variations in how class instantiation is represented
    found_calls = set()
    for caller, callee in expected_calls:
        found = False
        for u, v in call_edges:
            if u == caller:
                # For the User() call, accept User or User.__init__ as target
                if callee == "my_project.models.user.User" and (v == callee or v == "my_project.models.user.User.__init__"):
                    found = True
                    found_calls.add((caller, v)) # Add the actual found edge
                    break
                elif v == callee:
                    found = True
                    found_calls.add((caller, v))
                break
        assert found, f"Call edge ({caller} -> {callee}) not found"

    # Optional: Assert the exact number of call edges found if needed
    # assert len(call_edges) == len(expected_calls)


def test_data_access_methods(analyzer: AnalyzerIntegration):
    """Verify that data access methods retrieve correct information."""
    # Test get_imports
    user_imports = analyzer.get_imports(module_name="my_project.models.user")
    assert len(user_imports) >= 2 # helpers and BaseModel
    assert any(imp['target'].startswith("my_project.utils.helpers") for imp in user_imports)
    assert any(imp['target'].startswith("my_project.models") for imp in user_imports)

    # Test get_inheritance
    user_inheritance = analyzer.get_inheritance(class_name="my_project.models.user.User")
    assert len(user_inheritance) >= 1 # Should have one parent
    assert any(inh['parent'] == "my_project.models.BaseModel" for inh in user_inheritance)

    # Test get_calls (example: calls made by authenticate_user)
    auth_calls = analyzer.get_calls(function_name="my_project.services.auth.authenticate_user")
    assert len(auth_calls) >= 2 # Calls User() and get_name()
    assert any(call['callee'].startswith("my_project.models.user.User") for call in auth_calls) # User or User.__init__
    assert any(call['callee'] == "my_project.models.user.User.get_name" for call in auth_calls)

    # Test get_all_relationships
    all_rels = analyzer.get_all_relationships()
    assert len(all_rels) > 5 # Should have CONTAINS, IMPORTS, INHERITS, CALLS
    rel_types = {rel['relationship_type'] for rel in all_rels}
    assert REL_TYPE_CONTAINS in rel_types
    assert REL_TYPE_IMPORTS in rel_types
    assert REL_TYPE_INHERITS in rel_types
    assert REL_TYPE_CALLS in rel_types