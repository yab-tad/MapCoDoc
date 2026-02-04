"""
Post-analysis inheritance resolution.

Resolves inherited members by propagating methods from parent classes to child classes. 
Works entirely on file_analysis_results without requiring networkx or GraphStore.

Usage:
    resolver = InheritanceResolver(file_analysis_results, top_level_packages)
    resolver.update_analysis_results()
"""

import sys
import json
import logging
import tempfile
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Set, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict, deque
import urllib.request
import urllib.error


logger = logging.getLogger(__name__)


@dataclass
class InheritedMember:
    """Represents an inherited member."""
    name: str
    source_class_fqn: str
    original_fqn: str
    member_type: str  # 'method', 'property', 'class_variable'
    is_external: bool
    signature: Optional[Dict] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dictionary."""
        return {
            "name": self.name,
            "source_class_fqn": self.source_class_fqn,
            "original_fqn": self.original_fqn,
            "member_type": self.member_type,
            "is_external": self.is_external,
            "signature": self.signature,
            # These will be populated by _propagate_api_names_to_inherited_members
            "inherited_api_name": None,
            "inherited_api_names": [],
            "inheriting_class_fqn": None,
            "inheriting_class_api_name": None,
        }


@dataclass
class BaseClassInfo:
    """Information about a base class."""
    name: str                    # Local name used in class definition
    fqn: str                     # Resolved FQN
    is_internal: bool            # True if from internal codebase
    is_local_definition: bool    # True if defined in same module
    import_record: Optional[Dict] = None  # ImportRecord dict if imported


class InheritanceResolver:
    """
    Resolves inherited members after full code analysis.
    
    Works entirely on the file_analysis_results dictionary without requiring networkx or any graph database.
    """
    
    def __init__(
        self, 
        file_analysis_results: Dict[str, Dict[str, Any]],
        top_level_packages: Optional[Set[str]] = None,
    ):
        """
        Args:
            file_analysis_results: Complete analysis results from CodeAnalysisIntegration
            top_level_packages: Known internal packages (for fallback detection)
        """
        self.analysis_results = file_analysis_results
        self.top_level_packages = top_level_packages or set()
        
        # Build lookup caches (no networkx involved)
        self._class_cache: Dict[str, Dict[str, Any]] = {}        # fqn -> class_data
        self._method_cache: Dict[str, Dict[str, Any]] = {}       # fqn -> method_data
        self._module_imports: Dict[str, List[Dict]] = {}         # module_fqn -> import_records
        self._module_components: Dict[str, Set[str]] = {}        # module_fqn -> set of component FQNs
        
        self._build_caches()
    
    def _build_caches(self) -> None:
        """Build lookup caches from analysis results."""
        for file_key, mod_data in self.analysis_results.items():
            if file_key in ("metrics", "errors"):
                continue
            
            module_fqn = mod_data.get("module_name", "")
            if not module_fqn:
                continue
            
            # Cache import records for this module
            self._module_imports[module_fqn] = mod_data.get("import_records", [])
            
            # Cache components
            components = mod_data.get("components", {})
            self._module_components[module_fqn] = set(components.keys())
            
            for fqn, comp_data in components.items():
                comp_type = comp_data.get("component_kind", "")
                
                if comp_type == "class":
                    self._class_cache[fqn] = comp_data
                    # Cache methods from this class
                    for method_data in comp_data.get("methods", []):
                        method_fqn = method_data.get("fully_qualified_name", "")
                        if method_fqn:
                            self._method_cache[method_fqn] = method_data
                            
                elif comp_type == "method":
                    self._method_cache[fqn] = comp_data
        
        logger.debug(f"Built caches: {len(self._class_cache)} classes, "
                    f"{len(self._method_cache)} methods, "
                    f"{len(self._module_imports)} modules with imports")
    
    def _get_module_for_class(self, class_fqn: str) -> Optional[str]:
        """Get the module FQN where a class is defined."""
        class_data = self._class_cache.get(class_fqn, {})
        
        # First check if definition_module_fqn is available
        if class_data.get("definition_module_fqn"):
            return class_data["definition_module_fqn"]
        
        # Fallback: derive from FQN (module.ClassName -> module)
        if class_fqn in self._class_cache:
            parts = class_fqn.rsplit('.', 1)
            if len(parts) > 1:
                potential_module = parts[0]
                if potential_module in self._module_components:
                    return potential_module
        
        return None
    
    def _classify_base_class(
        self, 
        base_name: str, 
        base_fqn: str, 
        defining_module: str
    ) -> BaseClassInfo:
        """
        Classify a base class as internal/external and local/imported.
        
        This is the core framework-agnostic detection logic:
        1. Check if base is defined locally in the same module
        2. Check import records for the module
        3. Fall back to top_level_packages heuristic
        
        Args:
            base_name: The local name used in class definition (e.g., 'ABC', 'BaseClass')
            base_fqn: The resolved FQN of the base class
            defining_module: Module where the inheriting class is defined
        """
        # --- Case 1: Check if base is defined in the same module ---
        local_fqn = f"{defining_module}.{base_name}"
        if local_fqn in self._module_components.get(defining_module, set()):
            return BaseClassInfo(
                name=base_name,
                fqn=local_fqn,
                is_internal=True,
                is_local_definition=True,
                import_record=None
            )
        
        # Also check if base_fqn itself is a local component
        if base_fqn in self._module_components.get(defining_module, set()):
            return BaseClassInfo(
                name=base_name,
                fqn=base_fqn,
                is_internal=True,
                is_local_definition=True,
                import_record=None
            )
        
        # --- Case 2: Check import records for this module ---
        import_records = self._module_imports.get(defining_module, [])
        
        for import_rec in import_records:
            bound_name = import_rec.get("name_bound_in_importer", "")
            
            # Match by the name used in the class definition
            if bound_name == base_name:
                is_internal = import_rec.get("is_source_internal", False)
                
                # Use the resolved FQN from import record if available
                resolved_fqn = import_rec.get("name_bound_points_to_fqn", base_fqn)
                
                return BaseClassInfo(
                    name=base_name,
                    fqn=resolved_fqn,
                    is_internal=is_internal,
                    is_local_definition=False,
                    import_record=import_rec
                )
            
            # Also check if import matches the first part of base_fqn
            # e.g., for `from abc import ABC`, bound_name='ABC'
            if base_name.split('.')[0] == bound_name:
                is_internal = import_rec.get("is_source_internal", False)
                return BaseClassInfo(
                    name=base_name,
                    fqn=import_rec.get("name_bound_points_to_fqn", base_fqn),
                    is_internal=is_internal,
                    is_local_definition=False,
                    import_record=import_rec
                )
        
        # --- Case 3: Fallback - check top_level_packages ---
        # If we can't find an import record, use FQN heuristics
        is_internal = False
        if base_fqn:
            root_package = base_fqn.split('.')[0]
            is_internal = root_package in self.top_level_packages
        
        # Builtins like 'object', 'type', 'Exception' don't have imports
        # and their FQN won't match any internal package
        return BaseClassInfo(
            name=base_name,
            fqn=base_fqn,
            is_internal=is_internal,
            is_local_definition=False,
            import_record=None
        )
    
    def _get_class_methods(self, class_fqn: str) -> List[Dict[str, Any]]:
        """Get all methods for a class from the cache."""
        class_data = self._class_cache.get(class_fqn, {})
        return class_data.get("methods", [])
    
    def _get_parent_fqns(self, class_fqn: str) -> List[str]:
        """Get direct parent FQNs for a class."""
        class_data = self._class_cache.get(class_fqn, {})
        return class_data.get("base_fqns", [])
    
    def _get_base_names(self, class_fqn: str) -> List[str]:
        """Get base class names (as used in code) for a class."""
        class_data = self._class_cache.get(class_fqn, {})
        return class_data.get("bases", [])
    
    def resolve_inherited_members(self, class_fqn: str) -> Tuple[Dict[str, InheritedMember], Set[str]]:
        """
        Resolve all inherited members for a class.
        
        Uses MRO-like traversal (BFS through parent hierarchy).
        
        Args:
            class_fqn: FQN of the class to resolve
            
        Returns:
            Tuple of:
            - Dict of inherited members keyed by member name
            - Set of external base class FQNs
        """
        inherited: Dict[str, InheritedMember] = {}
        external_bases: Set[str] = set()
        visited: Set[str] = set()
        
        # Get methods defined directly in this class (to exclude from inherited)
        own_methods = {m.get("name") for m in self._get_class_methods(class_fqn)}
        
        # Get defining module for this class
        defining_module = self._get_module_for_class(class_fqn)
        
        # Get base names and FQNs
        base_names = self._get_base_names(class_fqn)
        base_fqns = self._get_parent_fqns(class_fqn)
        
        # Build queue with classified base info
        # Queue items: (base_fqn, source_chain) where source_chain tracks inheritance path
        parent_queue: List[Tuple[str, str]] = []  # (fqn, immediate_source_class)
        
        for base_name, base_fqn in zip(base_names, base_fqns):
            if not base_fqn:
                continue
            
            # Classify this base class
            base_info = self._classify_base_class(
                base_name=base_name.split('.')[-1] if '.' in base_name else base_name,
                base_fqn=base_fqn,
                defining_module=defining_module or ""
            )
            
            if not base_info.is_internal:
                external_bases.add(base_fqn)
                continue  # Skip external bases (can't resolve their members)
            
            parent_queue.append((base_fqn, base_fqn))
        
        # BFS through internal parent hierarchy
        while parent_queue:
            parent_fqn, source_class = parent_queue.pop(0)
            
            if parent_fqn in visited:
                continue
            visited.add(parent_fqn)
            
            # Check if this parent is in our internal cache
            if parent_fqn not in self._class_cache:
                # Parent might be external (from a third-party that wasn't analyzed)
                external_bases.add(parent_fqn)
                continue
            
            # Collect methods from internal parent
            parent_methods = self._get_class_methods(parent_fqn)
            
            for method_data in parent_methods:
                method_name = method_data.get("name", "")
                
                # Skip if already in own methods or already inherited (MRO priority)
                if method_name in own_methods or method_name in inherited:
                    continue
                
                # Skip private methods (but keep dunder methods)
                if method_name.startswith('_') and not method_name.startswith('__'):
                    continue
                
                method_fqn = method_data.get("fully_qualified_name", "")
                
                inherited[method_name] = InheritedMember(
                    name=method_name,
                    source_class_fqn=parent_fqn,
                    original_fqn=method_fqn,
                    member_type='method',
                    is_external=False,
                    signature=method_data.get("signature"),
                )
            
            # Add grandparents to queue
            parent_module = self._get_module_for_class(parent_fqn)
            grandparent_names = self._get_base_names(parent_fqn)
            grandparent_fqns = self._get_parent_fqns(parent_fqn)
            
            for gp_name, gp_fqn in zip(grandparent_names, grandparent_fqns):
                if gp_fqn and gp_fqn not in visited:
                    # Classify grandparent
                    gp_info = self._classify_base_class(
                        base_name=gp_name.split('.')[-1] if '.' in gp_name else gp_name,
                        base_fqn=gp_fqn,
                        defining_module=parent_module or ""
                    )
                    
                    if gp_info.is_internal:
                        parent_queue.append((gp_fqn, parent_fqn))
                    else:
                        external_bases.add(gp_fqn)
        
        return inherited, external_bases
    
    def resolve_all_classes(self) -> Dict[str, Dict[str, Any]]:
        """
        Resolve inherited members for ALL classes in the codebase.
        
        Returns:
            Dict mapping class FQN to resolution result:
            {
                "inherited_methods": {name: InheritedMember.to_dict(), ...},
                "external_bases": [list of external base FQNs]
            }
        """
        all_resolved: Dict[str, Dict[str, Any]] = {}
        
        for class_fqn in self._class_cache:
            inherited, external_bases = self.resolve_inherited_members(class_fqn)
            
            if inherited or external_bases:
                all_resolved[class_fqn] = {
                    "inherited_methods": {
                        name: im.to_dict() for name, im in inherited.items()
                    },
                    "external_bases": list(external_bases)
                }
                
                if inherited:
                    logger.debug(f"Class {class_fqn}: {len(inherited)} inherited methods")
        
        logger.info(f"Resolved inheritance for {len(all_resolved)} classes with inherited members")
        return all_resolved
    
    def update_analysis_results(self) -> None:
        """
        Update analysis results in-place with inherited member info.
        
        Adds 'inherited_methods' and 'external_bases' to each class component.
        """
        all_resolved = self.resolve_all_classes()
        
        for file_key, mod_data in self.analysis_results.items():
            if file_key in ("metrics", "errors"):
                continue
            
            components = mod_data.get("components", {})
            for fqn, comp_data in components.items():
                if comp_data.get("component_kind") != "class":
                    continue
                
                resolved = all_resolved.get(fqn)
                if resolved:
                    comp_data["inherited_methods"] = resolved["inherited_methods"]
                    comp_data["external_bases"] = resolved["external_bases"]
                    
                    logger.info(f"Added {len(resolved['inherited_methods'])} inherited methods and {len(resolved['external_bases'])} external bases to {fqn}")



class ExternalIntrospector:
    """
    Safely introspects external libraries in an isolated environment.
    
    Creates a temporary virtual environment, installs required libraries,
    runs introspection via subprocess, and cleans up afterward.
    
    This prevents polluting the main environment with external dependencies.
    """
    
    def __init__(self, cache_dir: Optional[str] = None):
        """
        Args:
            cache_dir: Optional directory to cache introspection results.
                      If provided, skips introspection for already-cached libraries.
        """
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._temp_venv_path: Optional[Path] = None
        self._introspection_cache: Dict[str, Dict] = {}
        self._pypi_name_cache = {}
        
        # Community-maintained import→package mapping (fetched from pipreqs)
        self._community_mapping: Optional[Dict[str, str]] = None
        self._community_mapping_loaded: bool = False
        
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._load_cache()
    
    def _load_cache(self) -> None:
        """Load cached introspection results."""
        if not self.cache_dir:
            return
        
        cache_file = self.cache_dir / "external_introspection_cache.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    self._introspection_cache = json.load(f)
                logger.debug(f"Loaded {len(self._introspection_cache)} cached library introspections")
            except Exception as e:
                logger.warning(f"Could not load introspection cache: {e}")
    
    def _save_cache(self) -> None:
        """Save introspection results to cache."""
        if not self.cache_dir:
            return
        
        cache_file = self.cache_dir / "external_introspection_cache.json"
        try:
            with open(cache_file, 'w') as f:
                json.dump(self._introspection_cache, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save introspection cache: {e}")
    
    def introspect_external_bases(self, external_bases: List[str], already_have: Set[str]) -> Dict[str, Dict[str, Any]]:
        """
        Introspect external base classes and return discovered methods.
        
        Uses caching to avoid repeated introspection of the same libraries.
        Falls back to subprocess-based introspection in isolated venv.
        
        Args:
            external_bases: List of external class FQNs
            already_have: Set of method names to exclude (kept for API compatibility, not used here)
            
        Returns:
            Dict mapping base_fqn -> {method_name: method_info}
        """
        discovered: Dict[str, Dict[str, Any]] = {}  # base_fqn -> {method_name: method_info}
        uncached_bases = []
        
        # Check cache first
        for base_fqn in external_bases:
            if base_fqn in ('object', 'type', 'builtins.object'): continue
            
            cache_key = base_fqn
            if cache_key in self._introspection_cache:
                # Use cached results - preserve structure per base
                discovered[base_fqn] = self._introspection_cache[cache_key].copy()
            else: 
                uncached_bases.append(base_fqn)
        
        # Introspect uncached bases
        if uncached_bases:
            new_results = self._introspect_in_isolation(uncached_bases)
            
            for base_fqn, methods in new_results.items():
                self._introspection_cache[base_fqn] = methods # cache the results
                # Add to discovered (preserve per-base structure)
                discovered[base_fqn] = methods.copy()
            self._save_cache()
        
        return discovered
    
    
    def introspect_dynamic_methods_batch(self, class_introspection_requests: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict]]:
        """
        Batch introspect multiple classes for dynamic methods.
        
        Creates ONE venv with ALL required packages and introspects all classes in a single batch for efficiency.
        
        Args:
            class_introspection_requests: List of dicts with:
                - 'class_fqn': The inheriting class FQN
                - 'external_base_fqns': List of external base FQNs
                - 'already_have': Set of method names to exclude
                
        Returns:
            Dict mapping class_fqn -> {method_name: method_info}
        """
        if not class_introspection_requests:
            return {}
        
        # --- Collect ALL packages needed across ALL requests ---
        all_packages: Set[str] = set()
        requests_to_process: List[Dict[str, Any]] = []
        
        for req in class_introspection_requests:
            class_fqn = req['class_fqn']
            external_bases = req['external_base_fqns']
            
            # Check cache first
            cache_key = (class_fqn, tuple(sorted(external_bases)))
            if cache_key in self._introspection_cache:
                continue  # Will use cached result later
            
            # Collect packages for this class
            inheriting_pkg = class_fqn.split('.')[0]
            pypi_name = self._get_pypi_name(inheriting_pkg)
            if pypi_name:
                all_packages.add(pypi_name)
            
            for base_fqn in external_bases:
                base_pkg = base_fqn.split('.')[0]
                pypi_name = self._get_pypi_name(base_pkg)
                if pypi_name:
                    all_packages.add(pypi_name)
            
            requests_to_process.append(req)
        
        all_results: Dict[str, Dict[str, Dict]] = {}
        
        # --- Single venv with ALL packages, process ALL requests at once ---
        if requests_to_process and all_packages:
            logger.info(f"Batch introspecting {len(requests_to_process)} classes with {len(all_packages)} unique packages: {sorted(all_packages)}")
            all_results = self._introspect_batch_in_venv(list(all_packages), requests_to_process)
        elif requests_to_process:
            logger.warning(f"No valid packages found for {len(requests_to_process)} introspection requests")
        
        # Add cached results
        for req in class_introspection_requests:
            class_fqn = req['class_fqn']
            external_bases = req['external_base_fqns']
            cache_key = (class_fqn, tuple(sorted(external_bases)))
            
            if class_fqn not in all_results and cache_key in self._introspection_cache:
                cached = self._introspection_cache[cache_key]
                already_have = req.get('already_have', set())
                all_results[class_fqn] = {k: v for k, v in cached.items() if k not in already_have}
        
        return all_results
    
    def _introspect_batch_in_venv(
        self,
        packages: List[str],
        requests: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Dict]]:
        """
        Create ONE venv and introspect ALL classes in the batch.
        """
        results: Dict[str, Dict[str, Dict]] = {}
        temp_dir = None
        
        try:
            temp_dir = tempfile.mkdtemp(prefix="mapcodoc_batch_introspect_")
            venv_path = Path(temp_dir) / "venv"
            
            # Create venv
            logger.debug(f"Creating batch venv at {venv_path}")
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                check=True,
                capture_output=True
            )
            
            if sys.platform == "win32":
                pip_path = venv_path / "Scripts" / "pip.exe"
                python_path = venv_path / "Scripts" / "python.exe"
            else:
                pip_path = venv_path / "bin" / "pip"
                python_path = venv_path / "bin" / "python"
            
            # Install packages ONCE - one by one to handle failures
            valid_packages = [p for p in packages if p]
            if valid_packages:
                logger.debug(f"Installing packages individually: {valid_packages}")
                installed_packages = self._install_packages_individually(pip_path, valid_packages)
                
                if not installed_packages:
                    logger.warning("No packages could be installed, skipping batch introspection")
                    return results
                    
                if len(installed_packages) < len(valid_packages):
                    failed = set(valid_packages) - installed_packages
                    logger.info(f"Continuing with {len(installed_packages)} packages installed, {len(failed)} failed: {failed}")
            
            # Generate batch introspection script
            class_data = [
                {
                    'class_fqn': r['class_fqn'], 
                    'external_bases': r['external_base_fqns'],
                    'statically_known_methods': list(r.get('already_have', set()))
                }
                for r in requests
            ]
            script_content = self._generate_batch_introspection_script(class_data)
            script_path = Path(temp_dir) / "introspect_batch.py"
            script_path.write_text(script_content)
            
            # Run introspection
            result = subprocess.run(
                [str(python_path), str(script_path)],
                capture_output=True,
                text=True,
                timeout=300  # Longer timeout for batch
            )
            
            if result.returncode == 0:
                try:
                    batch_results = json.loads(result.stdout)
                    
                    # Process results and update cache
                    for req in requests:
                        class_fqn = req['class_fqn']
                        already_have = req.get('already_have', set())
                        
                        class_methods = batch_results.get(class_fqn, {})
                        
                        # Cache before filtering
                        cache_key = (class_fqn, tuple(sorted(req['external_base_fqns'])))
                        self._introspection_cache[cache_key] = class_methods
                        
                        # Filter for return
                        results[class_fqn] = {k: v for k, v in class_methods.items() if k not in already_have}
                        
                except json.JSONDecodeError:
                    logger.warning("Could not parse batch introspection output")
            else:
                logger.warning(f"Batch introspection failed: {result.stderr[:500]}")
        
        except Exception as e:
            logger.warning(f"Error during batch introspection: {e}")
        
        finally:
            if temp_dir and Path(temp_dir).exists():
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
        
        self._save_cache()
        return results
    
    def _install_packages_individually(
        self, 
        pip_path: Path, 
        packages: List[str],
        timeout_per_pkg: int = 120
    ) -> Set[str]:
        """
        Install packages one by one, with automatic fallback for deprecated packages.
        
        Uses error-driven discovery as a final fallback: if installation fails,
        parses the error message for suggested alternatives.
        
        Args:
            pip_path: Path to pip executable in the venv
            packages: List of package names to install
            timeout_per_pkg: Timeout in seconds for each package
            
        Returns:
            Set of successfully installed package names
        """
        installed = set()
        
        for pkg in packages:
            if not pkg:
                continue
            
            success, actual_pkg = self._try_install_with_fallback(
                pip_path, pkg, timeout_per_pkg
            )
            
            if success:
                installed.add(actual_pkg)
                # Update cache if we discovered a mapping
                if actual_pkg != pkg:
                    # Extract the import name (first part before any version specifiers)
                    import_name = pkg.split('[')[0].split('==')[0].split('>=')[0].strip()
                    self._pypi_name_cache[import_name] = actual_pkg
                    logger.info(f"Learned mapping from install: {import_name} -> {actual_pkg}")
        
        return installed
    
    def _try_install_with_fallback(
        self,
        pip_path: Path,
        package: str,
        timeout: int,
        max_retries: int = 2
    ) -> Tuple[bool, str]:
        """
        Try to install a package, with automatic retry using suggested alternatives.
        
        If installation fails, parses pip's error output for suggestions and retries.
        
        Args:
            pip_path: Path to pip executable
            package: Package name to install
            timeout: Timeout in seconds
            max_retries: Maximum number of retry attempts with alternatives
            
        Returns:
            Tuple of (success: bool, actual_package_name: str)
        """
        current_pkg = package
        tried: Set[str] = set()
        
        for attempt in range(max_retries + 1):
            if current_pkg in tried:
                break
            tried.add(current_pkg)
            
            try:
                result = subprocess.run(
                    [str(pip_path), "install", "--quiet", current_pkg],
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                
                if result.returncode == 0:
                    logger.debug(f"Successfully installed: {current_pkg}")
                    return True, current_pkg
                
                # Parse error for suggested alternative
                suggested = self._extract_package_suggestion(result.stderr)
                if suggested and suggested not in tried:
                    logger.info(f"Package '{current_pkg}' failed, trying suggested: '{suggested}'")
                    current_pkg = suggested
                else:
                    # No suggestion found or already tried, log and give up
                    error_preview = result.stderr[:300].replace('\n', ' ')
                    logger.warning(f"Failed to install '{package}': {error_preview}")
                    break
                    
            except subprocess.TimeoutExpired:
                logger.warning(f"Timeout installing '{current_pkg}' after {timeout}s")
                break
            except Exception as e:
                logger.warning(f"Exception installing '{current_pkg}': {e}")
                break
        
        return False, package
    
    def _introspect_in_isolation(self, base_fqns: List[str]) -> Dict[str, Dict[str, Dict]]:
        """
        Run introspection in an isolated subprocess.
        
        This method:
            1. Groups bases by their top-level package
            2. Creates a temp venv
            3. Installs required packages
            4. Runs introspection script
            5. Cleans up venv
        
        Args:
            base_fqns: List of external class FQNs to introspect
            
        Returns:
            Dict mapping base_fqn -> {method_name: method_info}
        """
        if not base_fqns:
            return {}
        
        # Group by top-level package for efficient installation
        packages_needed = set()
        for fqn in base_fqns:
            top_package = fqn.split('.')[0]
            # Map common package names to PyPI names
            pypi_name = self._get_pypi_name(top_package)
            if pypi_name:
                packages_needed.add(pypi_name)
        
        if not packages_needed:
            return {}
        
        logger.info(f"Introspecting external bases from packages: {packages_needed}")
        
        results = {}
        temp_dir = None
        
        try:
            # Create temporary directory for venv
            temp_dir = tempfile.mkdtemp(prefix="mapcodoc_introspect_")
            venv_path = Path(temp_dir) / "venv"
            
            # Create virtual environment
            logger.debug(f"Creating temporary venv at {venv_path}")
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                check=True,
                capture_output=True
            )
            
            # Determine pip and python paths
            if sys.platform == "win32":
                pip_path = venv_path / "Scripts" / "pip.exe"
                python_path = venv_path / "Scripts" / "python.exe"
            else:
                pip_path = venv_path / "bin" / "pip"
                python_path = venv_path / "bin" / "python"
            
            # Install required packages
            logger.debug(f"Installing packages: {packages_needed}")
            install_result = subprocess.run(
                [str(pip_path), "install", "--quiet"] + list(packages_needed),
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if install_result.returncode != 0:
                logger.warning(f"Package installation failed: {install_result.stderr}")
                return {}
            
            # Create introspection script
            script_path = Path(temp_dir) / "introspect.py"
            script_content = self._generate_introspection_script(base_fqns)
            script_path.write_text(script_content)
            
            # Run introspection
            result = subprocess.run(
                [str(python_path), str(script_path)],
                capture_output=True,
                text=True,
                timeout=120  # 2 minute timeout
            )
            
            if result.returncode == 0:
                try:
                    results = json.loads(result.stdout)
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse introspection output")
            else:
                logger.warning(f"Introspection failed: {result.stderr[:500]}")
        
        except subprocess.TimeoutExpired:
            logger.warning("Introspection timed out")
        except Exception as e:
            logger.warning(f"Error during external introspection: {e}")
        
        finally:
            # Clean up temporary directory
            if temp_dir and Path(temp_dir).exists():
                logger.debug(f"Cleaning up temporary venv at {temp_dir}")
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    logger.warning(f"Could not clean up temp dir: {e}")
        
        return results
    
    def _fetch_community_mapping(self) -> Dict[str, str]:
        """
        Fetch community-maintained import→package mapping from pipreqs.
        
        This mapping is maintained by the pipreqs community and covers common
        cases where import names differ from PyPI package names.
        
        Falls back to empty dict on network failure (other strategies will be used).
        
        Returns:
            Dict mapping import_name -> pypi_package_name
        """
        if self._community_mapping_loaded:
            return self._community_mapping or {}
        
        self._community_mapping_loaded = True
        
        # Check cache first
        if self.cache_dir:
            cache_file = self.cache_dir / "community_mapping_cache.json"
            if cache_file.exists():
                try:
                    # Use cached version if less than 7 days old
                    import time
                    if time.time() - cache_file.stat().st_mtime < 7 * 24 * 3600:
                        with open(cache_file, 'r') as f:
                            self._community_mapping = json.load(f)
                        logger.debug(f"Loaded {len(self._community_mapping)} community mappings from cache")
                        return self._community_mapping
                except Exception as e:
                    logger.debug(f"Could not load community mapping cache: {e}")
        
        # Fetch from pipreqs repository
        url = "https://raw.githubusercontent.com/bndr/pipreqs/master/pipreqs/mapping"
        
        try:
            request = urllib.request.Request(url)
            request.add_header('User-Agent', 'MapCoDoc-ExternalIntrospector/1.0')
            
            with urllib.request.urlopen(request, timeout=10) as response:
                content = response.read().decode('utf-8')
                
                self._community_mapping = {}
                for line in content.strip().split('\n'):
                    line = line.strip()
                    if ':' in line and not line.startswith('#'):
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            import_name = parts[0].strip()
                            package_name = parts[1].strip()
                            if import_name and package_name:
                                self._community_mapping[import_name] = package_name
                
                logger.info(f"Fetched {len(self._community_mapping)} community import→package mappings")
                
                # Cache for future use
                if self.cache_dir:
                    try:
                        cache_file = self.cache_dir / "community_mapping_cache.json"
                        with open(cache_file, 'w') as f:
                            json.dump(self._community_mapping, f)
                    except Exception as e:
                        logger.debug(f"Could not cache community mapping: {e}")
                
                return self._community_mapping
                
        except Exception as e:
            logger.warning(f"Could not fetch community mapping from pipreqs: {e}")
            self._community_mapping = {}
            return {}


    def _check_pypi_deprecation(self, package_name: str) -> Optional[str]:
        """
        Check if a PyPI package is deprecated and extract the replacement.
        
        Queries PyPI JSON API and parses metadata for deprecation signals.
        
        Args:
            package_name: The PyPI package name to check
            
        Returns:
            The replacement package name if deprecated, None otherwise
        """
        import re
        
        url = f"https://pypi.org/pypi/{package_name}/json"
        
        try:
            request = urllib.request.Request(url)
            request.add_header('User-Agent', 'MapCoDoc-ExternalIntrospector/1.0')
            
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                info = data.get('info', {})
                
                # Check classifiers for inactive/deprecated status
                classifiers = info.get('classifiers', [])
                is_inactive = any(
                    'Inactive' in c or ':: 7 -' in c 
                    for c in classifiers
                )
                
                # Combine summary and description for pattern matching
                summary = info.get('summary', '') or ''
                description = info.get('description', '') or ''
                
                # Limit description search to first 2000 chars for efficiency
                text = f"{summary} {description[:2000]}"
                
                # Check for deprecation indicators
                deprecation_indicators = [
                    'deprecated', 'obsolete', 'replaced', 'renamed', 
                    'moved to', 'use .* instead', 'do not use'
                ]
                
                text_lower = text.lower()
                is_deprecated = is_inactive or any(
                    indicator in text_lower 
                    for indicator in ['deprecated', 'obsolete', 'do not use']
                )
                
                if is_deprecated:
                    # Try to extract replacement package name
                    patterns = [
                        r"use ['\"]?([a-zA-Z][a-zA-Z0-9_-]+)['\"]? (?:instead|rather)",
                        r"replaced by ['\"]?([a-zA-Z][a-zA-Z0-9_-]+)['\"]?",
                        r"moved to ['\"]?([a-zA-Z][a-zA-Z0-9_-]+)['\"]?",
                        r"renamed to ['\"]?([a-zA-Z][a-zA-Z0-9_-]+)['\"]?",
                        r"install ['\"]?([a-zA-Z][a-zA-Z0-9_-]+)['\"]? instead",
                        # sklearn-specific pattern
                        r"use ['\"]([^'\"]+)['\"] rather than ['\"][^'\"]+['\"]",
                    ]
                    
                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            replacement = match.group(1).strip()
                            # Validate it's a reasonable package name
                            if replacement and len(replacement) > 1 and replacement != package_name:
                                logger.info(f"Detected deprecated package '{package_name}' -> '{replacement}'")
                                return replacement
                    
                    # Deprecated but no clear replacement found
                    logger.debug(f"Package '{package_name}' appears deprecated but no replacement found")
                
                return None
                
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            logger.debug(f"HTTP error checking deprecation for {package_name}: {e.code}")
            return None
        except Exception as e:
            logger.debug(f"Error checking deprecation for {package_name}: {e}")
            return None


    def _extract_package_suggestion(self, stderr: str) -> Optional[str]:
        """
        Extract suggested package name from pip installation error output.
        
        Parses common pip error patterns to discover the correct package name.
        This is a fallback when other methods fail.
        
        Args:
            stderr: The stderr output from a failed pip install
            
        Returns:
            The suggested package name if found, None otherwise
        """
        import re
        
        patterns = [
            # sklearn deprecation pattern
            r"use ['\"]([^'\"]+)['\"] rather than ['\"][^'\"]+['\"]",
            # pip "did you mean" suggestions
            r"[Dd]id you mean[:\s]+['\"]?([a-zA-Z0-9_-]+)['\"]?",
            # General "install X instead" pattern
            r"install ['\"]?([a-zA-Z0-9_-]+)['\"]? instead",
            # "replaced by" pattern
            r"replaced by ['\"]?([a-zA-Z0-9_-]+)['\"]?",
            # "use X for pip" pattern
            r"use ['\"]([^'\"]+)['\"] for pip",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, stderr, re.IGNORECASE)
            if match:
                suggested = match.group(1).strip()
                if suggested and len(suggested) > 1:
                    logger.debug(f"Extracted package suggestion from pip error: '{suggested}'")
                    return suggested
        
        return None
    
    def _get_pypi_name(self, import_name: str) -> Optional[str]:
        """
        Determine PyPI package name from import name.
        
        Multi-tier strategy:
            1. Skip known problematic/internal imports
            2. Skip stdlib modules
            3. Check cache
            4. Check community-maintained mapping (pipreqs)
            5. Query PyPI API with deprecation detection
            6. Return None if not found
        """
        # 1. Skip known problematic or internal imports
        SKIP_IMPORTS = {
            # Modules that match wrong/broken PyPI packages
            'cli',           # Matches python-cli (broken, uses deprecated use_2to3)
            'git',           # Matches python-git; use GitPython explicitly if needed
            # Internal references that don't exist on PyPI
            'pickle_module',
            'cutlass',       # NVIDIA CUDA library
            # The analysis tool itself
            'mapcodoc',
            # Other false positives
            '__main__', 'test', 'tests', 'conftest', 'setup',
        }
        
        if import_name in SKIP_IMPORTS:
            logger.debug(f"Skipping known problematic import: '{import_name}'")
            self._pypi_name_cache[import_name] = None
            return None
        
        # 2. Skip stdlib
        if self._is_stdlib_module(import_name):
            return None
        
        # 3. Check cache first (fastest path)
        if import_name in self._pypi_name_cache:
            return self._pypi_name_cache[import_name]
        
        # 4. Check community-maintained mapping (pipreqs)
        community_mapping = self._fetch_community_mapping()
        if import_name in community_mapping:
            pkg = community_mapping[import_name]
            logger.info(f"Community mapping: {import_name} -> {pkg}")
            self._pypi_name_cache[import_name] = pkg
            return pkg
        
        # 5. Query PyPI API with deprecation detection
        pypi_name = self._query_pypi_for_package_with_deprecation_check(import_name)
        if pypi_name:
            # Reject known broken packages even if discovered
            BROKEN_PACKAGES = {'python-cli', 'python-git'}
            if pypi_name.lower() in BROKEN_PACKAGES:
                logger.warning(f"Discovered '{pypi_name}' for '{import_name}' but it's known to be broken, skipping")
                self._pypi_name_cache[import_name] = None
                return None
                
            self._pypi_name_cache[import_name] = pypi_name
            logger.info(f"Discovered PyPI package: {import_name} -> {pypi_name}")
            return pypi_name
        
        # 6. Package not found on PyPI - skip it
        logger.warning(f"Could not discover PyPI package for '{import_name}', skipping")
        self._pypi_name_cache[import_name] = None
        return None


    def _query_pypi_for_package_with_deprecation_check(self, import_name: str) -> Optional[str]:
        """
        Query PyPI API to find a package, checking for deprecation.
        
        Generates candidate names, checks each against PyPI, and verifies the package is not deprecated before returning.
        
        Args:
            import_name: The Python import name to look up
            
        Returns:
            The installable PyPI package name if found, None otherwise
        """
        candidates = self._generate_pypi_candidates(import_name)
        
        for candidate in candidates:
            if self._package_exists_on_pypi(candidate):
                # Check if this package is deprecated
                replacement = self._check_pypi_deprecation(candidate)
                if replacement:
                    # Package is deprecated, use the replacement
                    # Verify replacement exists on PyPI
                    if self._package_exists_on_pypi(replacement):
                        return replacement
                    # If replacement doesn't exist, continue to next candidate
                    logger.debug(f"Replacement '{replacement}' for deprecated '{candidate}' not found on PyPI")
                    continue
                else:
                    # Package exists and is not deprecated
                    return candidate
        
        return None


    def _generate_pypi_candidates(self, import_name: str) -> List[str]:
        """
        Generate candidate PyPI package names from an import name.
        
        Applies common naming patterns and transformations used by Python packages.
        Ordered by likelihood (most common patterns first).
        
        Args:
            import_name: The Python import name (e.g., 'sklearn', 'cv2')
            
        Returns:
            List of candidate PyPI package names to try
        """
        candidates = []
        name_lower = import_name.lower()
        
        # 1. Exact match (most packages use same name)
        candidates.append(import_name)
        candidates.append(name_lower)
        
        # 2. Underscore to hyphen (very common: some_package -> some-package)
        if '_' in import_name:
            candidates.append(import_name.replace('_', '-'))
            candidates.append(name_lower.replace('_', '-'))
        
        # 3. scikit-* pattern (sklearn -> scikit-learn, skimage -> scikit-image)
        if import_name.startswith('sk') and len(import_name) > 2:
            rest = import_name[2:]
            candidates.append(f"scikit-{rest}")
            candidates.append(f"scikit-{rest.lower()}")
        
        # 4. Common prefix patterns
        candidates.extend([
            f"python-{import_name}",
            f"python-{name_lower}",
            f"py{import_name}",
            f"py{name_lower}",
            f"Py{import_name.capitalize()}",
        ])
        
        # 5. Capitalization variations
        if import_name != import_name.capitalize():
            candidates.append(import_name.capitalize())
        
        # Remove duplicates while preserving order (first occurrence wins)
        seen = set()
        unique = []
        for c in candidates:
            c_lower = c.lower()
            if c_lower not in seen:
                seen.add(c_lower)
                unique.append(c)
        
        return unique

    def _package_exists_on_pypi(self, package_name: str) -> bool:
        """
        Check if a package exists on PyPI by querying its JSON API.
        
        Uses HTTP HEAD request for efficiency (no body downloaded).
        Handles network errors gracefully with short timeout.
        
        Args:
            package_name: The PyPI package name to check
            
        Returns:
            True if the package exists on PyPI, False otherwise
        """
        
        url = f"https://pypi.org/pypi/{package_name}/json"
        
        try:
            request = urllib.request.Request(url, method='HEAD')
            request.add_header('User-Agent', 'MapCoDoc-ExternalIntrospector/1.0')
            
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status == 200
                
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            # Other HTTP errors (rate limiting, etc.) - log but don't fail
            logger.debug(f"HTTP error checking PyPI for {package_name}: {e.code}")
            return False
            
        except urllib.error.URLError as e:
            # Network unreachable, DNS failure, etc.
            logger.debug(f"Network error checking PyPI for {package_name}: {e.reason}")
            return False
            
        except Exception as e:
            # Timeout or other unexpected errors
            logger.debug(f"Error checking PyPI for {package_name}: {e}")
            return False
    
    def _is_stdlib_module(self, module_name: str) -> bool:
        """
        Check if a module is part of Python's standard library.
        
        Uses sys.stdlib_module_names (Python 3.10+) or fallback list.
        """
        # Python 3.10+ has this built-in
        if hasattr(sys, 'stdlib_module_names'):
            return module_name in sys.stdlib_module_names
        
        # Fallback for older Python versions
        # This is a minimal list - add more as needed
        stdlib = {
            'abc', 'aifc', 'argparse', 'array', 'ast', 'asynchat', 'asyncio',
            'asyncore', 'atexit', 'audioop', 'base64', 'bdb', 'binascii',
            'binhex', 'bisect', 'builtins', 'bz2', 'calendar', 'cgi', 'cgitb',
            'chunk', 'cmath', 'cmd', 'code', 'codecs', 'codeop', 'collections',
            'colorsys', 'compileall', 'concurrent', 'configparser', 'contextlib',
            'contextvars', 'copy', 'copyreg', 'cProfile', 'crypt', 'csv',
            'ctypes', 'curses', 'dataclasses', 'datetime', 'dbm', 'decimal',
            'difflib', 'dis', 'distutils', 'doctest', 'email', 'encodings',
            'enum', 'errno', 'faulthandler', 'fcntl', 'filecmp', 'fileinput',
            'fnmatch', 'fractions', 'ftplib', 'functools', 'gc', 'getopt',
            'getpass', 'gettext', 'glob', 'graphlib', 'grp', 'gzip', 'hashlib',
            'heapq', 'hmac', 'html', 'http', 'imaplib', 'imghdr', 'imp',
            'importlib', 'inspect', 'io', 'ipaddress', 'itertools', 'json',
            'keyword', 'lib2to3', 'linecache', 'locale', 'logging', 'lzma',
            'mailbox', 'mailcap', 'marshal', 'math', 'mimetypes', 'mmap',
            'modulefinder', 'multiprocessing', 'netrc', 'nis', 'nntplib',
            'numbers', 'operator', 'optparse', 'os', 'ossaudiodev', 'pathlib',
            'pdb', 'pickle', 'pickletools', 'pipes', 'pkgutil', 'platform',
            'plistlib', 'poplib', 'posix', 'posixpath', 'pprint', 'profile',
            'pstats', 'pty', 'pwd', 'py_compile', 'pyclbr', 'pydoc', 'queue',
            'quopri', 'random', 're', 'readline', 'reprlib', 'resource',
            'rlcompleter', 'runpy', 'sched', 'secrets', 'select', 'selectors',
            'shelve', 'shlex', 'shutil', 'signal', 'site', 'smtpd', 'smtplib',
            'sndhdr', 'socket', 'socketserver', 'spwd', 'sqlite3', 'ssl',
            'stat', 'statistics', 'string', 'stringprep', 'struct', 'subprocess',
            'sunau', 'symtable', 'sys', 'sysconfig', 'syslog', 'tabnanny',
            'tarfile', 'telnetlib', 'tempfile', 'termios', 'test', 'textwrap',
            'threading', 'time', 'timeit', 'tkinter', 'token', 'tokenize',
            'trace', 'traceback', 'tracemalloc', 'tty', 'turtle', 'turtledemo',
            'types', 'typing', 'unicodedata', 'unittest', 'urllib', 'uu',
            'uuid', 'venv', 'warnings', 'wave', 'weakref', 'webbrowser',
            'winreg', 'winsound', 'wsgiref', 'xdrlib', 'xml', 'xmlrpc',
            'zipapp', 'zipfile', 'zipimport', 'zlib', '_thread'
        }
        return module_name in stdlib
    
    def _generate_introspection_script(self, base_fqns: List[str]) -> str:
        """Generate Python script to introspect classes."""
        return '''import json
import inspect
import importlib
import sys
import dataclasses

def introspect_class(class_fqn):
    """Introspect a class and return its methods."""
    parts = class_fqn.rsplit('.', 1)
    if len(parts) != 2:
        return {}
    
    module_path, class_name = parts
    
    try:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name, None)
        
        if cls is None or not isinstance(cls, type):
            return {}
        
        methods = {}
        documented_dunders = {
            '__init__', '__call__', '__len__', '__iter__',
            '__getitem__', '__setitem__', '__contains__',
            '__enter__', '__exit__', '__repr__', '__str__'
        }
        
        def is_property_in_mro(klass, name):
            """Check if name is a property anywhere in klass's MRO."""
            for base in klass.__mro__:
                if name in vars(base) and isinstance(vars(base)[name], property):
                    return True, base
            return False, None
        
        # Check for dataclass fields
        if dataclasses.is_dataclass(cls):
            for field_name, field_obj in cls.__dataclass_fields__.items():
                if field_name.startswith('_'):
                    continue
                methods[field_name] = {
                    "name": field_name,
                    "source_class_fqn": class_fqn,
                    "original_fqn": f"{class_fqn}.{field_name}",
                    "member_type": "field",
                    "is_external": True,
                    "signature": {"full": field_name},
                    "docstring": ""
                }
        
        for attr_name in dir(cls):
            # Skip private (keep documented dunders)
            if attr_name.startswith('_'):
                if attr_name not in documented_dunders: continue
            
            try:
                # Check if it's a property via MRO traversal FIRST
                is_prop, defining_class = is_property_in_mro(cls, attr_name)
                if is_prop:
                    prop = vars(defining_class)[attr_name]
                    docstring = getattr(prop, '__doc__', None) or ""
                    source_fqn = f"{defining_class.__module__}.{defining_class.__name__}"
                    methods[attr_name] = {
                        "name": attr_name,
                        "source_class_fqn": source_fqn,
                        "original_fqn": f"{source_fqn}.{attr_name}",
                        "member_type": "property",
                        "is_external": True,
                        "signature": {"full": f"{attr_name}"},
                        "docstring": docstring
                    }
                    continue
                
                # If not captured as property or callable, check if it's a documented attribute (visible in dir but not a type, property, or method)
                attr = getattr(cls, attr_name, None)
                if attr is None: continue
                if isinstance(attr, type): continue
                
                # Already handled properties above
                # If it's not callable, it might be a data attribute/descriptor
                if not callable(attr):
                    # Capture as an attribute (e.g., uid)
                    methods[attr_name] = {
                        "name": attr_name,
                        "source_class_fqn": class_fqn,
                        "original_fqn": f"{class_fqn}.{attr_name}",
                        "member_type": "attribute",
                        "is_external": True,
                        "signature": {"full": f"{attr_name}"},
                        "docstring": ""
                    }
                    continue
                
                # Get signature and docstring
                try:
                    sig = str(inspect.signature(attr))
                    full_sig = f"{attr_name}{sig}"
                    docstring = getattr(attr, '__doc__', None) or ""
                except (ValueError, TypeError):
                    full_sig = f"{attr_name}(...)"
                    docstring = ""
                    
                methods[attr_name] = {
                    "name": attr_name,
                    "source_class_fqn": class_fqn,
                    "original_fqn": f"{class_fqn}.{attr_name}",
                    "member_type": "method",
                    "is_external": True,
                    "signature": {"full": full_sig},
                    "docstring": docstring
                }
                
            except Exception:
                continue
        
        return methods
        
    except Exception as e:
        return {}

# Classes to introspect
base_fqns = ''' + json.dumps(base_fqns) + '''

results = {}
for fqn in base_fqns:
    results[fqn] = introspect_class(fqn)

print(json.dumps(results))
'''

    def _generate_batch_introspection_script(self, class_data: List[Dict]) -> str:
        """
        Generate script to introspect MULTIPLE classes at once.
        
        This script identifies dynamically-generated methods by comparing
        what exists at runtime vs what was captured by static AST analysis.
        
        The class_data should include 'statically_known_methods' which contains
        method names from MapCoDoc's static analysis.
        """
        return f'''import json
import inspect
import importlib
import dataclasses

def get_class(fqn):
    """Import and return a class by FQN."""
    parts = fqn.rsplit('.', 1)
    if len(parts) != 2:
        return None
    module_path, class_name = parts
    try:
        module = importlib.import_module(module_path)
        return getattr(module, class_name, None)
    except Exception:
        return None

def introspect_class(inheriting_fqn, external_base_fqns, statically_known_methods):
    """
    Discover methods that exist at runtime but were NOT captured by static analysis.
    
    This is framework-agnostic: we find any method that:
    1. Exists on the inheriting class at runtime
    2. Was NOT captured by MapCoDoc's static AST analysis
    3. Appears to come from an external package
    
    Key insight: Dynamically-injected methods (like sklearn's set_*_request) 
    ARE in vars(cls) but were NOT in static AST analysis. We must capture them!
    """
    inheriting_cls = get_class(inheriting_fqn)
    if not inheriting_cls:
        return {{}}
    
    # Documented dunder methods worth capturing
    documented_dunders = {{
        '__init__', '__call__', '__len__', '__iter__',
        '__getitem__', '__setitem__', '__contains__',
        '__enter__', '__exit__', '__repr__', '__str__'
    }}
    
    # External packages to check against
    external_packages = set()
    for base_fqn in external_base_fqns:
        pkg = base_fqn.split('.')[0]
        if pkg not in ('builtins', 'object', 'type'):
            external_packages.add(pkg)
    
    if not external_packages: return {{}}
    
    methods = {{}}
    
    # Helper to find properties via MRO
    def is_property_in_mro(klass, name):
        for base in klass.__mro__:
            if name in vars(base) and isinstance(vars(base)[name], property):
                return True, base
        return False, None
    
    # Check for dataclass fields on the inheriting class
    if dataclasses.is_dataclass(inheriting_cls):
        for field_name, field_obj in inheriting_cls.__dataclass_fields__.items():
            if field_name.startswith('_'):
                continue
            if field_name in statically_known_methods:
                continue
            methods[field_name] = {{
                "name": field_name,
                "source_class_fqn": inheriting_fqn,
                "original_fqn": f"{{inheriting_fqn}}.{{field_name}}",
                "member_type": "field",
                "is_external": False,
                "is_dynamic": False,
                "signature": {{"full": field_name}},
                "docstring": ""
            }}
    
    # Check ALL attributes on the class
    for attr_name in dir(inheriting_cls):
        # Skip private methods unless documented dunders
        if attr_name.startswith('_') and attr_name not in documented_dunders:
            continue
        
        # KEY CHECK: Skip if this method was already captured by static analysis
        # This is the ONLY filter - we don't skip based on own_vars
        if attr_name in statically_known_methods:
            continue
        
        try:
            # First check if it's a property via MRO traversal
            is_prop, defining_class = is_property_in_mro(inheriting_cls, attr_name)
            if is_prop:
                defining_module = getattr(defining_class, '__module__', '')
                # Check if defining class is from an external package
                is_external_prop = any(defining_module.startswith(pkg) for pkg in external_packages)
                if is_external_prop:
                    prop = vars(defining_class)[attr_name]
                    docstring = getattr(prop, '__doc__', None) or ""
                    source_fqn = f"{{defining_module}}.{{defining_class.__name__}}"
                    methods[attr_name] = {{
                        "name": attr_name,
                        "source_class_fqn": source_fqn,
                        "original_fqn": f"{{source_fqn}}.{{attr_name}}",
                        "member_type": "property",
                        "is_external": True,
                        "is_dynamic": False,
                        "signature": {{"full": f"{{attr_name}}"}},
                        "docstring": docstring
                    }}
                continue
            
            attr = getattr(inheriting_cls, attr_name, None)
            if attr is None: continue
            
            # Skip types (nested classes)
            if isinstance(attr, type): continue
            
            # Capture callables, properties, and descriptors (like RequestMethod)
            is_descriptor = hasattr(type(attr), '__get__')
            
            # If not callable and not a descriptor, it might be a data attribute (like uid)
            if not callable(attr) and not is_descriptor:
                # Try to find source from MRO
                attr_source = None
                for base in inheriting_cls.__mro__[1:]:
                    if base is object:
                        continue
                    base_module = getattr(base, '__module__', '')
                    for ext_pkg in external_packages:
                        if base_module.startswith(ext_pkg):
                            attr_source = f"{{base_module}}.{{base.__name__}}"
                            break
                    if attr_source:
                        break
                
                if attr_source:
                    methods[attr_name] = {{
                        "name": attr_name,
                        "source_class_fqn": attr_source,
                        "original_fqn": f"{{attr_source}}.{{attr_name}}",
                        "member_type": "attribute",
                        "is_external": True,
                        "is_dynamic": False,
                        "signature": {{"full": f"{{attr_name}}"}},
                        "docstring": ""
                    }}
                continue
            
            # Determine the source of this method
            source_class = None
            method_module = getattr(attr, '__module__', None)
            
            # Strategy 1: Check if method's __module__ belongs to external package
            if method_module:
                for ext_pkg in external_packages:
                    if method_module.startswith(ext_pkg):
                        source_class = f"{{method_module}}.dynamic"
                        break
            
            # Strategy 2: Walk MRO to find which external class has this attribute
            if not source_class:
                for base in inheriting_cls.__mro__[1:]:
                    if base is object:
                        continue
                    base_module = getattr(base, '__module__', '')
                    for ext_pkg in external_packages:
                        if base_module.startswith(ext_pkg) and attr_name in vars(base):
                            source_class = f"{{base_module}}.{{base.__name__}}"
                            break
                    if source_class:
                        break
            
            # Strategy 3: Check if it's in the class's own vars but with external module
            if not source_class and attr_name in vars(inheriting_cls):
                # This is dynamically injected - try to find source
                if method_module:
                    for ext_pkg in external_packages:
                        if method_module.startswith(ext_pkg):
                            source_class = f"{{method_module}}.dynamically_injected"
                            break
                # If still no source but we know it's external injection, mark as such
                if not source_class and external_base_fqns:
                    # Heuristic: if it looks like a generated method, attribute to first external base
                    source_class = f"{{external_base_fqns[0].split('.')[0]}}.dynamically_generated"
            
            # Strategy 4: Last resort for methods reachable via MRO
            if not source_class:
                for base in inheriting_cls.__mro__[1:]:
                    if base is object:
                        continue
                    if hasattr(base, attr_name):
                        base_module = getattr(base, '__module__', '')
                        for ext_pkg in external_packages:
                            if base_module.startswith(ext_pkg):
                                source_class = f"{{base_module}}.{{base.__name__}}"
                                break
                        if source_class:
                            break
            
            if not source_class:
                continue
            
            # Get signature and docstring
            try:
                sig = str(inspect.signature(attr))
                full_sig = f"{{attr_name}}{{sig}}"
            except (ValueError, TypeError):
                full_sig = f"{{attr_name}}(...)"
            
            docstring = getattr(attr, '__doc__', None) or ""
            
            methods[attr_name] = {{
                "name": attr_name,
                "source_class_fqn": source_class,
                "original_fqn": f"{{source_class}}.{{attr_name}}",
                "member_type": "method",
                "is_external": True,
                "is_dynamic": True,
                "signature": {{"full": full_sig}},
                "docstring": docstring if docstring else ""
            }}
            
        except Exception:
            continue
    
    return methods

# Batch data
class_data = json.loads({repr(json.dumps(class_data))})

results = {{}}
for item in class_data:
    class_fqn = item['class_fqn']
    external_bases = item.get('external_bases', [])
    statically_known = set(item.get('statically_known_methods', []))
    results[class_fqn] = introspect_class(class_fqn, external_bases, statically_known)

print(json.dumps(results))
'''
