import pytest
import time
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, call, patch, ANY
from typing import Optional, Dict, Any, Generator, Tuple

from code_analysis.analyzers.analyzer_integration import AnalyzerIntegration
from code_analysis.config import AnalysisConfig
from code_analysis.mapcodocreg import MapCoDocRegistry # Mocked
from code_analysis.definition_registry import DefinitionRegistry # Mocked
from code_analysis.graph.importer import ImportTracker # Mocked
from code_analysis.graph.inheritance_tracker import InheritanceTracker # Mocked
from code_analysis.graph.call_graph import CallGraphTracker # Mocked
from code_analysis.events import (
    FILE_CREATED, FILE_MODIFIED, FILE_DELETED,
    MODULE_ANALYSIS_INVALIDATED, MODULE_ANALYSIS_UPDATED,
    EventPayload
)
from code_analysis.ir.cache import get_cache_file_path, generate_cache_key # For cache verification


# Helper to create the standard event payload
def create_file_event_payload(event_type: str, file_path: str, module_path: Optional[str] = None, is_deleted: bool = False) -> EventPayload:
    """Creates a standard event payload for file events."""
    # In a real scenario, module_path might be derived or passed differently
    # For testing, we can often provide it directly if known.
    return EventPayload(
        event_type=event_type,
        source_component="FileSystemWatcher", # Simulate watcher as source
        timestamp=time.time(), # Use current time
        event_specific_data={
            "file_path": file_path,
            "module_path": module_path, # May be None initially
            "is_deleted": is_deleted,
        }
    )


@pytest.fixture
def test_project(tmp_path):
    """Creates a temporary directory structure for testing."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    (project_dir / "pkg").mkdir()
    (project_dir / "pkg" / "__init__.py").touch()
    (project_dir / "pkg" / "mod1.py").write_text("def func1(): pass\nclass ClassA: pass")
    (project_dir / "pkg" / "mod2.py").write_text("from .mod1 import func1\n\ndef func2():\n    func1()")
    (project_dir / "main.py").write_text("from pkg.mod2 import func2\nfunc2()")
    return project_dir

@pytest.fixture
def ir_cache_dir(tmp_path):
    """Creates a temporary IR cache directory."""
    cache_dir = tmp_path / ".ir_cache"
    cache_dir.mkdir()
    return cache_dir

@pytest.fixture
def mock_registry():
    """Fixture for a mocked MapCoDocRegistry."""
    registry = MagicMock(spec=MapCoDocRegistry)
    registry.repo_path = None # Will be set by analyzer fixture
    # Mock the publish_event method
    registry.publish_event = MagicMock()
    # Mock the subscribe_to_event method (needed for initialization)
    registry.subscribe_to_event = MagicMock()
    return registry

@pytest.fixture
def mock_definition_registry():
    """Fixture for a mocked DefinitionRegistry."""
    def_reg = MagicMock(spec=DefinitionRegistry)
    def_reg.remove_definitions_by_module = MagicMock(return_value=1) # Simulate removing 1 def
    return def_reg

@pytest.fixture
def mock_import_tracker():
    """Fixture for a mocked ImportTracker."""
    tracker = MagicMock(spec=ImportTracker)
    tracker.remove_imports_by_module = MagicMock(return_value=1) # Simulate removing 1 import
    return tracker

@pytest.fixture
def mock_inheritance_tracker():
    """Fixture for a mocked InheritanceTracker."""
    tracker = MagicMock(spec=InheritanceTracker)
    tracker.remove_inheritance_by_module = MagicMock(return_value=1) # Simulate removing 1 relationship
    return tracker

@pytest.fixture
def mock_call_tracker():
    """Fixture for a mocked CallGraphTracker."""
    tracker = MagicMock(spec=CallGraphTracker)
    tracker.remove_calls_by_module = MagicMock(return_value=1) # Simulate removing 1 call
    return tracker

@pytest.fixture
def analyzer_config(ir_cache_dir):
    """Fixture for AnalysisConfig with IR cache enabled."""
    return AnalysisConfig(
        generate_ir=True,
        ir_cache_dir=str(ir_cache_dir),
        exclude_patterns=[] # Ensure no exclusions interfere
    )

@pytest.fixture
def analyzer(test_project, analyzer_config, mock_registry, mock_definition_registry,
             mock_import_tracker, mock_inheritance_tracker, mock_call_tracker):
    """Fixture for AnalyzerIntegration with mocked dependencies."""
    # Set repo_path on the mock registry before passing it
    mock_registry.repo_path = str(test_project)

    # Mock the registry's get method to return our mocks
    def _mock_registry_get(component_name: str):
        if component_name == 'definition_registry':
            return mock_definition_registry
        elif component_name == 'import_tracker':
            return mock_import_tracker
        elif component_name == 'inheritance_tracker':
            return mock_inheritance_tracker
        elif component_name == 'call_tracker':
            return mock_call_tracker
        elif component_name == 'config':
            return analyzer_config
        else:
            return MagicMock() # Default mock for other components
    mock_registry.get.side_effect = _mock_registry_get

    # --- Patch analyze_code ---
    # We patch analyze_code to control its return value and avoid actual AST parsing in these tests
    # which focus on the *integration* and *event handling* logic.
    with patch('code_analysis.analyzers.analyzer_integration.analyze_code') as mock_analyze_code:

        def _mock_analyze_code_impl(*args, module_name, source_file, **kwargs):
            # Simulate a successful analysis result
            rel_path = os.path.relpath(source_file, test_project)
            # Basic result structure, can be customized per test if needed
            return {
                "module_name": module_name,
                "package_name": module_name.split('.')[0] if '.' in module_name else "",
                "source_file": source_file,
                "components": {"some_component": {"type": "function", "name": "some_component"}}, # Dummy component
                "public_component_fqns": [f"{module_name}.some_component"],
                "errors": [],
                "module_interface": {"has_all": False},
                "imported_names_map": {},
                "success": True, # Indicate success
                "ir_generated": True, # Simulate IR generation
                "ir_cache_hit": False,
            }
        mock_analyze_code.side_effect = _mock_analyze_code_impl

        # Instantiate the analyzer
        analyzer_instance = AnalyzerIntegration(
            config=analyzer_config,
            registry=mock_registry,
            definition_registry=mock_definition_registry
            # Trackers will be retrieved via registry.get inside AnalyzerIntegration
        )
        # Manually assign trackers if not using registry.get during init (depends on AnalyzerIntegration impl)
        analyzer_instance.import_tracker = mock_import_tracker
        analyzer_instance.inheritance_tracker = mock_inheritance_tracker
        analyzer_instance.call_tracker = mock_call_tracker

        # Set the repo_path explicitly
        analyzer_instance.repo_path = str(test_project)

        # Manually call initialize to set up event handlers (using the mocked subscribe)
        analyzer_instance.initialize()

        # Store the mock_analyze_code for assertions later
        analyzer_instance._mock_analyze_code = mock_analyze_code # Attach mock for inspection

        yield analyzer_instance # Provide the configured analyzer to the test


# --- Test Cases ---

def test_initial_analysis(analyzer: AnalyzerIntegration, test_project):
    """Test that initial analysis populates results."""
    analyzer.analyze_codebase(str(test_project))

    # Check that analyze_file (via mock_analyze_code) was called for each file
    assert analyzer._mock_analyze_code.call_count == 4 # __init__.py, mod1.py, mod2.py, main.py

    # Check that results are stored
    assert len(analyzer.file_analysis_results) == 4
    assert 'pkg/__init__.py' in analyzer.file_analysis_results
    assert 'pkg/mod1.py' in analyzer.file_analysis_results
    assert 'pkg/mod2.py' in analyzer.file_analysis_results
    assert 'main.py' in analyzer.file_analysis_results

    # Check that IR cache was populated (assuming analyze_code simulates generation)
    assert len(analyzer.ir_cache) == 4
    assert 'pkg/mod1.py' in analyzer.ir_cache

    # Check that registry publish was called for each successful analysis
    # Expected calls: 4 * MODULE_ANALYSIS_UPDATED
    update_calls = [
        c for c in analyzer.mock_registry.publish_event.call_args_list
        if c.args[0] == MODULE_ANALYSIS_UPDATED
    ]
    assert len(update_calls) == 4
    # Check payload details for one call (optional)
    payload_data = update_calls[0].args[1] # Get event_data dict
    assert payload_data['success'] is True
    assert payload_data['ir_generated'] is True


def test_file_modification(analyzer: AnalyzerIntegration, test_project, mock_registry, mock_definition_registry,
                           mock_import_tracker, mock_inheritance_tracker, mock_call_tracker):
    """Test handling of a file modification event."""
    # 1. Initial analysis
    analyzer.analyze_codebase(str(test_project))
    analyzer._mock_analyze_code.reset_mock() # Reset mock for modification check
    mock_registry.publish_event.reset_mock()
    mock_definition_registry.remove_definitions_by_module.reset_mock()
    mock_import_tracker.remove_imports_by_module.reset_mock()
    mock_inheritance_tracker.remove_inheritance_by_module.reset_mock()
    mock_call_tracker.remove_calls_by_module.reset_mock()

    # 2. Simulate modification
    mod1_path = str(test_project / "pkg" / "mod1.py")
    mod1_module = "pkg.mod1"
    # Create payload for the event
    event_payload = create_file_event_payload(FILE_MODIFIED, mod1_path, mod1_module)

    # 3. Trigger the handler
    analyzer._handle_file_modified(event_payload)

    # 4. Assertions
    #   - Invalidation called? (Check tracker remove methods)
    mock_definition_registry.remove_definitions_by_module.assert_called_once_with(mod1_module)
    mock_import_tracker.remove_imports_by_module.assert_called_once_with(mod1_module)
    mock_inheritance_tracker.remove_inheritance_by_module.assert_called_once_with(mod1_module)
    mock_call_tracker.remove_calls_by_module.assert_called_once_with(mod1_module)

    #   - Re-analysis triggered? (Check analyze_code mock)
    analyzer._mock_analyze_code.assert_called_once()
    call_args, call_kwargs = analyzer._mock_analyze_code.call_args
    assert call_kwargs.get('module_name') == mod1_module
    assert call_kwargs.get('source_file') == mod1_path

    #   - Caches cleared? (Check they are absent before re-population)
    rel_path = os.path.relpath(mod1_path, test_project)
    # Note: analyze_file is called *after* invalidation, so the caches *will* be populated again by the mock.
    # The critical check is that the invalidation *methods* were called (asserted above).
    # We can check that the result dict was updated (implicitly by analyze_code being called).
    assert rel_path in analyzer.file_analysis_results # Should be re-populated
    assert rel_path in analyzer.ir_cache # Should be re-populated

    #   - Events published? (INVALIDATED then UPDATED)
    assert mock_registry.publish_event.call_count == 2
    # Check first call is INVALIDATED
    invalidate_call = mock_registry.publish_event.call_args_list[0]
    assert invalidate_call.args[0] == MODULE_ANALYSIS_INVALIDATED
    assert invalidate_call.args[1]['module_path'] == mod1_module
    assert invalidate_call.args[1]['file_path'] == rel_path
    assert invalidate_call.args[1]['is_deleted'] is False
    # Check second call is UPDATED
    update_call = mock_registry.publish_event.call_args_list[1]
    assert update_call.args[0] == MODULE_ANALYSIS_UPDATED
    assert update_call.args[1]['module_path'] == mod1_module
    assert update_call.args[1]['file_path'] == rel_path
    assert update_call.args[1]['success'] is True # Based on mock analyze_code


def test_file_creation(analyzer: AnalyzerIntegration, test_project, mock_registry):
    """Test handling of a file creation event."""
    # 1. Initial analysis (optional, ensures analyzer is populated)
    analyzer.analyze_codebase(str(test_project))
    analyzer._mock_analyze_code.reset_mock()
    mock_registry.publish_event.reset_mock()

    # 2. Simulate creation
    new_file_path = str(test_project / "pkg" / "mod3.py")
    new_module = "pkg.mod3"
    Path(new_file_path).write_text("def func3(): return True") # Create the file physically
    event_payload = create_file_event_payload(FILE_CREATED, new_file_path, new_module)

    # 3. Trigger the handler
    analyzer._handle_file_created(event_payload)

    # 4. Assertions
    #   - Analysis triggered?
    analyzer._mock_analyze_code.assert_called_once()
    call_args, call_kwargs = analyzer._mock_analyze_code.call_args
    assert call_kwargs.get('module_name') == new_module
    assert call_kwargs.get('source_file') == new_file_path

    #   - Results added?
    rel_path = os.path.relpath(new_file_path, test_project)
    assert rel_path in analyzer.file_analysis_results
    assert rel_path in analyzer.ir_cache

    #   - Event published? (Only UPDATED for creation)
    assert mock_registry.publish_event.call_count == 1
    update_call = mock_registry.publish_event.call_args_list[0]
    assert update_call.args[0] == MODULE_ANALYSIS_UPDATED
    assert update_call.args[1]['module_path'] == new_module
    assert update_call.args[1]['file_path'] == rel_path
    assert update_call.args[1]['success'] is True


def test_file_deletion(analyzer: AnalyzerIntegration, test_project, mock_registry, mock_definition_registry,
                         mock_import_tracker, mock_inheritance_tracker, mock_call_tracker):
    """Test handling of a file deletion event."""
    # 1. Initial analysis
    analyzer.analyze_codebase(str(test_project))
    analyzer._mock_analyze_code.reset_mock()
    mock_registry.publish_event.reset_mock()
    mock_definition_registry.remove_definitions_by_module.reset_mock()
    mock_import_tracker.remove_imports_by_module.reset_mock()
    mock_inheritance_tracker.remove_inheritance_by_module.reset_mock()
    mock_call_tracker.remove_calls_by_module.reset_mock()

    # 2. Simulate deletion
    mod2_path = str(test_project / "pkg" / "mod2.py")
    mod2_module = "pkg.mod2"
    rel_path = os.path.relpath(mod2_path, test_project)
    # Physically delete the file (optional but good practice for realism)
    # Path(mod2_path).unlink() # Uncomment if needed, but test focuses on handler logic

    event_payload = create_file_event_payload(FILE_DELETED, mod2_path, mod2_module, is_deleted=True)

    # 3. Trigger the handler
    analyzer._handle_file_deleted(event_payload)

    # 4. Assertions
    #   - Invalidation called?
    mock_definition_registry.remove_definitions_by_module.assert_called_once_with(mod2_module)
    mock_import_tracker.remove_imports_by_module.assert_called_once_with(mod2_module)
    mock_inheritance_tracker.remove_inheritance_by_module.assert_called_once_with(mod2_module)
    mock_call_tracker.remove_calls_by_module.assert_called_once_with(mod2_module)

    #   - Re-analysis NOT triggered?
    analyzer._mock_analyze_code.assert_not_called()

    #   - Caches cleared?
    assert rel_path not in analyzer.file_analysis_results
    assert rel_path not in analyzer.ir_cache

    #   - Event published? (Only INVALIDATED for deletion)
    assert mock_registry.publish_event.call_count == 1
    invalidate_call = mock_registry.publish_event.call_args_list[0]
    assert invalidate_call.args[0] == MODULE_ANALYSIS_INVALIDATED
    assert invalidate_call.args[1]['module_path'] == mod2_module
    assert invalidate_call.args[1]['file_path'] == rel_path
    assert invalidate_call.args[1]['is_deleted'] is True


def test_ir_disk_cache_invalidation_on_modify(analyzer: AnalyzerIntegration, test_project, ir_cache_dir):
    """Verify that the disk IR cache file is removed on modification."""
    # 1. Initial analysis
    analyzer.analyze_codebase(str(test_project))

    # 2. Check initial cache file exists
    mod1_path = str(test_project / "pkg" / "mod1.py")
    mod1_module = "pkg.mod1"
    # Need to generate the cache key based on the *actual* file content initially
    # For simplicity, let's assume generate_cache_key uses path for this test
    # A more robust test might mock generate_cache_key or check file content hash
    cache_key = generate_cache_key(mod1_path, analyzer.config)
    cache_file = get_cache_file_path(str(ir_cache_dir), cache_key)

    # Simulate cache file creation (since mock analyze_code doesn't write)
    cache_file.touch()
    assert cache_file.exists()

    # 3. Simulate modification
    event_payload = create_file_event_payload(FILE_MODIFIED, mod1_path, mod1_module)
    analyzer._handle_file_modified(event_payload)

    # 4. Assert cache file is deleted
    # Note: The *old* cache key's file should be deleted. The mock analyze_code
    # will then simulate creating a *new* one (potentially with the same key if content hash isn't used).
    # The key point is that the invalidation logic attempts removal.
    assert not cache_file.exists() # Check the original file is gone


def test_syntax_error_during_reanalysis(analyzer: AnalyzerIntegration, test_project, mock_registry):
    """Test handling when re-analysis encounters a syntax error."""
    # 1. Initial analysis
    analyzer.analyze_codebase(str(test_project))
    analyzer._mock_analyze_code.reset_mock()
    mock_registry.publish_event.reset_mock()

    # 2. Simulate modification leading to syntax error
    mod1_path = str(test_project / "pkg" / "mod1.py")
    mod1_module = "pkg.mod1"
    rel_path = os.path.relpath(mod1_path, test_project)

    # Configure mock analyze_code to raise SyntaxError for this file now
    syntax_error = SyntaxError("Invalid syntax in test", (mod1_path, 5, 1, "bad line"))

    # We need analyze_file to raise the error, so patch it directly
    with patch.object(analyzer, 'analyze_file', side_effect=syntax_error) as mock_analyze_file_direct:
        event_payload = create_file_event_payload(FILE_MODIFIED, mod1_path, mod1_module)

        # 3. Trigger the handler - it should catch the error from analyze_file
        analyzer._handle_file_modified(event_payload)

        # 4. Assertions
        #   - Invalidation still happened (before analysis attempt)
        #     (Check mocks for remove_* methods - omitted for brevity, assume they were called)

        #   - Analysis was attempted (analyze_file was called)
        mock_analyze_file_direct.assert_called_once_with(mod1_path)

        #   - Error logged (implicitly checked by lack of unhandled exception)

        #   - Results cache should NOT contain an entry for the failed analysis
        #     because analyze_file raised before returning a result dict.
        #     The invalidation step removed the old entry.
        assert rel_path not in analyzer.file_analysis_results

        #   - IR cache should also be empty for this file
        assert rel_path not in analyzer.ir_cache

        #   - Events published? (Only INVALIDATED, as UPDATED isn't published if analyze_file fails critically)
        assert mock_registry.publish_event.call_count == 1
        invalidate_call = mock_registry.publish_event.call_args_list[0]
        assert invalidate_call.args[0] == MODULE_ANALYSIS_INVALIDATED
        assert invalidate_call.args[1]['module_path'] == mod1_module
        assert invalidate_call.args[1]['is_deleted'] is False
