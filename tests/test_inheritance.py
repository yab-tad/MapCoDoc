"""
This test module acts as an integration test. It verifies that the entire analysis pipeline (AnalyzerIntegration, which uses CodeVisitor and populates the trackers) correctly identifies and stores inheritance relationships from actual Python code files.

Methodology:
- Creates a sample Python project with various inheritance structures (inheritance_project fixture).
- Runs AnalyzerIntegration over this project (analyzer fixture).
- Asserts the correctness of the discovered relationships by querying the InheritanceTracker instance within the AnalyzerIntegration object (analyzer.inheritance_tracker). It tests methods like get_direct_parents, get_direct_children, is_subclass_of, etc., based on the results of the full analysis.
"""

import pytest
from pathlib import Path
from typing import Generator, Dict, Any, List

# Ensure imports work correctly for sibling modules
# (Adjust based on your project structure if needed)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from code_analysis.analyzers.analyzer_integration import AnalyzerIntegration
from code_analysis.graph.store import GraphStore
from code_analysis.graph.inheritance_tracker import InheritanceTracker
from code_analysis.relationship_types import REL_TYPE_INHERITS, NODE_TYPE_CLASS
from code_analysis.config import AnalysisConfig



# Helper to create files
def create_file(path: Path, content: str):
    """Creates a file with the given content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)



@pytest.fixture(scope="module")
def inheritance_project(tmp_path_factory) -> Generator[Path, None, None]:
    """Creates a temporary sample project with inheritance structures."""
    project_dir = tmp_path_factory.mktemp("inheritance_project")

    # Base class
    create_file(project_dir / "base.py", """
class BaseClass:
    pass
""")

    # Single inheritance
    create_file(project_dir / "single.py", """
from base import BaseClass

class SingleDerived(BaseClass):
    pass
""")

    # Multiple inheritance
    create_file(project_dir / "multiple.py", """
from base import BaseClass
from single import SingleDerived

class MultipleDerived(BaseClass, SingleDerived):
    pass
""")

    # Nested inheritance
    create_file(project_dir / "nested.py", """
from base import BaseClass

class Outer:
    class Inner(BaseClass):
        pass
""")

    # Another derived class
    create_file(project_dir / "sibling.py", """
from base import BaseClass

class SiblingDerived(BaseClass):
    pass
""")

    # Unrelated class
    create_file(project_dir / "unrelated.py", """
class UnrelatedClass:
    pass
""")

    # __init__.py to make it a package (optional but good practice)
    create_file(project_dir / "__init__.py", "")

    yield project_dir



@pytest.fixture(scope="module")
def analyzer(inheritance_project: Path) -> AnalyzerIntegration:
    """Analyzes the inheritance_project."""
    config = AnalysisConfig()
    # No registry needed for this specific test focus if we directly access tracker
    analyzer_instance = AnalyzerIntegration(config=config)
    analyzer_instance.analyze_codebase(str(inheritance_project))
    return analyzer_instance

# --- Test Cases ---

def test_inheritance_relationships_added(analyzer: AnalyzerIntegration):
    """Verify that INHERITS relationships were added to the graph store."""
    # Use the public method that relies on the tracker
    all_inheritance = analyzer.get_inheritance() # Gets all inheritance relationships

    # Expected relationships (Child FQN -> Parent FQN)
    expected = {
        ("single.SingleDerived", "base.BaseClass"),
        ("multiple.MultipleDerived", "base.BaseClass"),
        ("multiple.MultipleDerived", "single.SingleDerived"),
        ("nested.Outer.Inner", "base.BaseClass"),
        ("sibling.SiblingDerived", "base.BaseClass"),
    }

    found = set()
    for rel in all_inheritance:
        # Assuming get_inheritance returns dicts like {'child': fqn, 'parent': fqn, ...}
        # or directly from the store {'source': child_fqn, 'target': parent_fqn, 'relationship_type': REL_TYPE_INHERITS}
        child = rel.get('child') or rel.get('source')
        parent = rel.get('parent') or rel.get('target')
        rel_type = rel.get('relationship_type')

        if rel_type == REL_TYPE_INHERITS:
             found.add((child, parent))

    assert found == expected, f"Expected {expected}, but found {found}"
    assert len(found) == 5 # Ensure no extra relationships were found

def test_get_direct_parents(analyzer: AnalyzerIntegration):
    """Test retrieving direct parents."""
    tracker = analyzer.inheritance_tracker

    assert set(tracker.get_direct_parents("single.SingleDerived")) == {"base.BaseClass"}
    assert set(tracker.get_direct_parents("multiple.MultipleDerived")) == {"base.BaseClass", "single.SingleDerived"}
    assert set(tracker.get_direct_parents("nested.Outer.Inner")) == {"base.BaseClass"}
    assert set(tracker.get_direct_parents("sibling.SiblingDerived")) == {"base.BaseClass"}
    assert set(tracker.get_direct_parents("base.BaseClass")) == set()
    assert set(tracker.get_direct_parents("unrelated.UnrelatedClass")) == set()
    assert set(tracker.get_direct_parents("nested.Outer")) == set() # Outer class has no explicit base

def test_get_direct_children(analyzer: AnalyzerIntegration):
    """Test retrieving direct children."""
    tracker = analyzer.inheritance_tracker

    assert set(tracker.get_direct_children("base.BaseClass")) == {
        "single.SingleDerived",
        "multiple.MultipleDerived",
        "nested.Outer.Inner",
        "sibling.SiblingDerived",
    }
    assert set(tracker.get_direct_children("single.SingleDerived")) == {"multiple.MultipleDerived"}
    assert set(tracker.get_direct_children("multiple.MultipleDerived")) == set()
    assert set(tracker.get_direct_children("nested.Outer.Inner")) == set()
    assert set(tracker.get_direct_children("sibling.SiblingDerived")) == set()
    assert set(tracker.get_direct_children("unrelated.UnrelatedClass")) == set()
    assert set(tracker.get_direct_children("nested.Outer")) == set() # Outer has no children in this structure

def test_is_subclass_of(analyzer: AnalyzerIntegration):
    """Test the is_subclass_of check."""
    tracker = analyzer.inheritance_tracker

    # Direct inheritance
    assert tracker.is_subclass_of("single.SingleDerived", "base.BaseClass") is True
    assert tracker.is_subclass_of("nested.Outer.Inner", "base.BaseClass") is True

    # Transitive inheritance
    assert tracker.is_subclass_of("multiple.MultipleDerived", "base.BaseClass") is True
    assert tracker.is_subclass_of("multiple.MultipleDerived", "single.SingleDerived") is True

    # Negative cases
    assert tracker.is_subclass_of("base.BaseClass", "single.SingleDerived") is False
    assert tracker.is_subclass_of("unrelated.UnrelatedClass", "base.BaseClass") is False
    assert tracker.is_subclass_of("single.SingleDerived", "unrelated.UnrelatedClass") is False
    assert tracker.is_subclass_of("nested.Outer.Inner", "nested.Outer") is False # Inner inherits BaseClass, not Outer
    assert tracker.is_subclass_of("nested.Outer", "base.BaseClass") is False

    # Self-check
    assert tracker.is_subclass_of("single.SingleDerived", "single.SingleDerived") is False # Should not consider self a subclass

def test_get_all_ancestors(analyzer: AnalyzerIntegration):
    """Test retrieving all ancestors (superclasses)."""
    tracker = analyzer.inheritance_tracker

    assert tracker.get_all_ancestors("multiple.MultipleDerived") == {"base.BaseClass", "single.SingleDerived"}
    assert tracker.get_all_ancestors("single.SingleDerived") == {"base.BaseClass"}
    assert tracker.get_all_ancestors("nested.Outer.Inner") == {"base.BaseClass"}
    assert tracker.get_all_ancestors("base.BaseClass") == set()
    assert tracker.get_all_ancestors("unrelated.UnrelatedClass") == set()

def test_get_all_descendants(analyzer: AnalyzerIntegration):
    """Test retrieving all descendants (subclasses)."""
    tracker = analyzer.inheritance_tracker

    assert tracker.get_all_descendants("base.BaseClass") == {
        "single.SingleDerived",
        "multiple.MultipleDerived",
        "nested.Outer.Inner",
        "sibling.SiblingDerived",
    }
    assert tracker.get_all_descendants("single.SingleDerived") == {"multiple.MultipleDerived"}
    assert tracker.get_all_descendants("multiple.MultipleDerived") == set()
    assert tracker.get_all_descendants("unrelated.UnrelatedClass") == set()
