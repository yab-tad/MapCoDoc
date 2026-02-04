"""
Dynamic analysis module for Python code.

This module provides capabilities for dynamically executing a Python module in an isolated virtual environment. Its primary goal is to determine the module's
runtime exports (considering `__all__` or public names from `dir()`) and, for each exported item, identify if it's a re-export, its original FQN,
and its defining module. This information helps in understanding the module's public API as it would appear at runtime.

Key features:
1.  Isolated execution using temporary virtual environments.
2.  Leverages static analysis information (imports, local definitions) passed from the caller to aid in dynamic introspection.
3.  Focuses on identifying exported names and their origins.
"""

import os
import re
import ast
import sys
import json
import toml
import venv
import shutil
import logging
import tempfile
import subprocess
import uuid # For unique namespacing in the dynamic script
from pathlib import Path
import threading
from typing import Dict, Set, Optional, Any, Tuple, List
from collections.abc import Iterable

from .config import AnalysisConfig
from .utils import DynamicAnalysisError, ErrorContext, Timer, get_platform_info # ProcessLimiter, ControlledMemoryManager removed as not used in this version
from .feature_flags import Feature, is_enabled # For DYNAMIC_ALL_EVALUATION flag


logger = logging.getLogger(__name__)


# Global to potentially reuse a single venv across multiple DynamicAnalyzer instances or calls within the same process, if configured.
_SHARED_VENV_DETAILS: Optional[Dict[str, Any]] = None
_SHARED_VENV_LOCK = threading.Lock() # ensure thread-safe access to _SHARED_VENV_DETAILS


class VirtualEnvironment:
    """
    Manages a Python virtual environment for isolated module execution.
    """
    
    def __init__(self, 
                 project_root_path: Path, 
                 python_executable: Optional[str] = None, 
                 reuse_shared_env: bool = True,
                 python_package_roots: Optional[List[Path]] = None):
        """
        Initializes the virtual environment manager.

        Args:
            project_root_path: The root path of the project being analyzed.
                               This is added to PYTHONPATH for the dynamic script.
            python_executable: Optional path to the Python interpreter to create the venv with.
                               If None, uses the current Python interpreter.
            reuse_shared_env: If True, attempts to reuse a single venv across the application's lifetime.
            python_package_roots: Optional list of paths to Python package roots (for src layout support).
                                  If None, defaults to [project_root_path].
        """
        self.project_root_path = project_root_path.resolve()
        self.python_executable = python_executable or sys.executable
        self.reuse_shared_env = reuse_shared_env
        # Store Python package roots for PYTHONPATH setup (supports src layout)
        self.python_package_roots = python_package_roots if python_package_roots else [self.project_root_path]
        
        self.venv_path: Optional[Path] = None
        self.bin_path: Optional[Path] = None # Path to 'Scripts' (Win) or 'bin' (Unix)
        self.effective_python_executable: Optional[str] = None # Python executable *inside* the venv
        
        self.platform = get_platform_info().get('system', 'Unknown')
        self._setup_completed = False
        self._dependencies_installed_in_shared_env: Set[str] = set()

    
    def _determine_paths(self, venv_root: Path) -> None:
        """Determines bin and Python executable paths based on platform."""
        self.venv_path = venv_root
        if self.platform == "Windows":
            self.bin_path = self.venv_path / "Scripts"
            self.effective_python_executable = str(self.bin_path / "python.exe")
        else:
            self.bin_path = self.venv_path / "bin"
            self.effective_python_executable = str(self.bin_path / "python")


    def setup(self) -> bool:
        """
        Sets up the virtual environment. Creates a new one or reuses a shared one.

        Returns:
            True if setup was successful and the environment is usable, False otherwise.
        """
        global _SHARED_VENV_DETAILS
        if self._setup_completed:
            return True

        with _SHARED_VENV_LOCK: # Ensure thread-safe venv creation/reuse check
            if self.reuse_shared_env and _SHARED_VENV_DETAILS:
                venv_p = Path(_SHARED_VENV_DETAILS["venv_path"])
                if venv_p.exists() and (venv_p / _SHARED_VENV_DETAILS["python_rel_path"]).exists():
                    self._determine_paths(venv_p)
                    self._dependencies_installed_in_shared_env = _SHARED_VENV_DETAILS.get("installed_packages", set())
                    self._setup_completed = True
                    logger.info(f"Reusing shared virtual environment at {self.venv_path}")
                    return True
                else:
                    logger.warning("Shared venv path or Python executable within it not found. Creating new.")
                    _SHARED_VENV_DETAILS = None # Invalidate shared

            try:
                # Create in a temporary directory
                # The temp directory itself will be cleaned up by OS eventually if process crashes, but we also try to clean up self.venv_path.
                temp_dir_base = Path(tempfile.gettempdir()) / f"mapcodoc_venvs_{os.getpid()}"
                temp_dir_base.mkdir(parents=True, exist_ok=True)
                prospective_venv_path = temp_dir_base / f"dyn_env_{uuid.uuid4().hex[:8]}"
                
                logger.info(f"Creating new virtual environment at {prospective_venv_path} using {self.python_executable}")
                venv.create(
                    str(prospective_venv_path), 
                    system_site_packages=False, 
                    clear=True, 
                    with_pip=True, 
                    prompt="mapcodoc_dyn"
                )
                self._determine_paths(prospective_venv_path)

                if not self.effective_python_executable or not Path(self.effective_python_executable).exists():
                    raise RuntimeError(f"Python executable not found in venv: {self.effective_python_executable}")

                if self.reuse_shared_env:
                    python_rel_path = Path(self.effective_python_executable).relative_to(self.venv_path)
                    _SHARED_VENV_DETAILS = {
                        "venv_path": str(self.venv_path), 
                        "python_rel_path": str(python_rel_path),
                        "installed_packages": set() # Initialize for new shared env
                    }
                    self._dependencies_installed_in_shared_env = set()
                self._setup_completed = True
                return True

            except Exception as e:
                logger.error(f"Failed to create/setup virtual environment: {e}", exc_info=True)
                if hasattr(self, 'venv_path') and self.venv_path and self.venv_path.exists():
                    shutil.rmtree(self.venv_path, ignore_errors=True) # Attempt cleanup
                self.venv_path = None
                self._setup_completed = False
                return False
    
    
    def install_dependencies(self, dependencies: Dict[str, str], fail_fast: bool = False) -> bool:
        """
        Installs dependencies into the virtual environment.

        Args:
            dependencies: Dictionary of dependencies to install (name -> spec).
            fail_fast: If True, return immediately on first failure.
                    If False (default), try to install each individually on batch failure.

        Returns:
            True if all dependencies were installed successfully, False if any failed.
        """
        global _SHARED_VENV_DETAILS
        if not self.bin_path or not self.effective_python_executable:
            logger.error("Virtual environment not properly set up for dependency installation.")
            return False
            
        pip_cmd = str(self.bin_path / ("pip.exe" if self.platform == "Windows" else "pip"))
        if not Path(pip_cmd).exists():
            logger.error(f"pip not found at {pip_cmd}")
            return False

        deps_to_install: Dict[str, str] = {}  # name -> spec

        for name, spec in dependencies.items():
            if self.reuse_shared_env and name in self._dependencies_installed_in_shared_env:
                logger.debug(f"Dependency '{name}' already marked as installed in shared venv.")
                continue
            
            # Validate and sanitize spec - strip any remaining comments
            sanitized_spec = spec.split('#')[0].strip()
            if not sanitized_spec:
                logger.debug(f"Skipping empty spec for '{name}' after sanitization")
                continue
                
            deps_to_install[name] = sanitized_spec

        if not deps_to_install:
            logger.info("All specified dependencies already considered installed in this venv session.")
            return True
        
        specs_list = list(deps_to_install.values())
        logger.info(f"Attempting to install/upgrade {len(specs_list)} dependencies: {', '.join(specs_list[:5])}...")
        
        try:
            # Upgrade pip first silently
            subprocess.run([pip_cmd, "install", "--upgrade", "pip", "setuptools", "wheel"], check=False, capture_output=True, timeout=120)
            
            # Try batch install first (faster)
            cmd = [pip_cmd, "install"] + specs_list
            result = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
            
            if result.returncode == 0:
                logger.info(f"Successfully installed/updated {len(specs_list)} dependencies.")
                self._mark_installed(deps_to_install.keys())
                return True
            else:
                logger.warning(f"Batch dependency installation failed. Trying individual installs...")
                
                if fail_fast:
                    logger.warning(f"Pip stderr: {result.stderr[:500]}")
                    return False
                
                # Fallback: install one by one
                return self._install_dependencies_individually(pip_cmd, deps_to_install)
                
        except subprocess.TimeoutExpired:
            logger.error("Timeout during dependency installation.")
            return False
        except Exception as e:
            logger.error(f"Exception during dependency installation: {e}", exc_info=True)
            return False


    def _install_dependencies_individually(self, pip_cmd: str, deps: Dict[str, str]) -> bool:
        """
        Install dependencies one by one, continuing on failures.
        
        Returns True if at least some dependencies were installed.
        """
        installed = set()
        failed = set()
        
        for name, spec in deps.items():
            try:
                result = subprocess.run(
                    [pip_cmd, "install", "--quiet", spec],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if result.returncode == 0:
                    installed.add(name)
                    logger.debug(f"Installed: {spec}")
                else:
                    failed.add(name)
                    logger.warning(f"Failed to install '{spec}': {result.stderr[:150]}")
            except subprocess.TimeoutExpired:
                failed.add(name)
                logger.warning(f"Timeout installing '{spec}'")
            except Exception as e:
                failed.add(name)
                logger.warning(f"Exception installing '{spec}': {e}")
        
        if installed:
            self._mark_installed(installed)
            logger.info(f"Installed {len(installed)}/{len(deps)} dependencies individually")
        
        if failed:
            logger.warning(f"Failed to install {len(failed)} dependencies: {failed}")
        
        # Return True if we installed anything
        return len(installed) > 0

    def _mark_installed(self, names: Iterable[str]) -> None:
        """Mark packages as installed in the shared venv tracking."""
        global _SHARED_VENV_DETAILS
        if self.reuse_shared_env and _SHARED_VENV_DETAILS:
            _SHARED_VENV_DETAILS.setdefault("installed_packages", set()).update(names)
            self._dependencies_installed_in_shared_env.update(names)
    
    def install_local_package(self, package_path: Path, package_name: str, editable: bool = True, timeout: int = 600) -> bool:
        """
        Installs a local package (from cloned/local repository) into the virtual environment.
        
        This enables dynamic analysis of packages with compiled extensions by installing
        the package itself, making compiled modules (e.g., torch._C) available.
        
        Strategy:
        1. First install from PyPI to get all dependencies (handles dynamic deps)
        2. Then install editable from source to use local code
        
        Args:
            package_path: Path to the package root (containing setup.py/pyproject.toml)
            package_name: Name of the package (for tracking installation status)
            editable: If True, uses 'pip install -e .' for editable install
            timeout: Installation timeout in seconds (default 10 minutes)
            
        Returns:
            True if installation succeeded, False otherwise
        """
        global _SHARED_VENV_DETAILS
        
        if not self.bin_path or not self.effective_python_executable:
            logger.error("Virtual environment not properly set up for local package installation.")
            return False
        
        # Check if already installed in shared venv
        if self.reuse_shared_env and package_name in self._dependencies_installed_in_shared_env:
            logger.info(f"Target package '{package_name}' already installed in shared venv.")
            return True
        
        pip_cmd = str(self.bin_path / ("pip.exe" if self.platform == "Windows" else "pip"))
        if not Path(pip_cmd).exists():
            logger.error(f"pip not found at {pip_cmd}")
            return False
        
        # Verify package is installable
        has_pyproject = (package_path / "pyproject.toml").exists()
        has_setup_py = (package_path / "setup.py").exists()
        has_setup_cfg = (package_path / "setup.cfg").exists()
        
        if not (has_pyproject or has_setup_py or has_setup_cfg):
            logger.warning(f"Package at {package_path} has no build configuration. Cannot install.")
            return False
        
        try:
            # Step 1: Install from PyPI first to get all dependencies
            # This handles packages with dynamic dependencies (e.g., requests)
            logger.info(f"Installing '{package_name}' dependencies from PyPI...")
            deps_cmd = [pip_cmd, "install", package_name, "--quiet"]
            deps_result = subprocess.run(
                deps_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300
            )
            if deps_result.returncode == 0:
                logger.debug(f"Successfully installed dependencies for '{package_name}' from PyPI")
            else:
                # Not fatal - package might not be on PyPI or might have different name
                logger.debug(f"PyPI install for dependencies returned code {deps_result.returncode}. "
                           f"Continuing with local install...")
            
            # Step 2: Install from local source (editable or regular)
            logger.info(f"Installing local package '{package_name}' from {package_path} "
                       f"(editable={editable}, timeout={timeout}s)")
            
            if editable:
                cmd = [pip_cmd, "install", "-e", str(package_path)]
            else:
                cmd = [pip_cmd, "install", str(package_path)]
            
            # Add flag for quieter output
            cmd.append("--quiet")
            
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                cwd=str(package_path)
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully installed local package '{package_name}'")
                
                # Mark as installed in shared venv
                if self.reuse_shared_env and _SHARED_VENV_DETAILS:
                    _SHARED_VENV_DETAILS.setdefault("installed_packages", set()).add(package_name)
                    _SHARED_VENV_DETAILS["target_package_installed"] = True
                    _SHARED_VENV_DETAILS["target_package_name"] = package_name
                self._dependencies_installed_in_shared_env.add(package_name)
                
                return True
            else:
                logger.warning(f"Local package installation failed. Return code: {result.returncode}")
                logger.warning(f"Pip stderr: {result.stderr[:2000]}")
                
                # Try non-editable install as fallback if editable failed
                if editable:
                    logger.info("Retrying with non-editable install...")
                    return self.install_local_package(
                        package_path, package_name, 
                        editable=False, timeout=timeout
                    )
                return False
                
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout ({timeout}s) during local package installation. "
                        f"Package '{package_name}' may require longer build time.")
            return False
        except Exception as e:
            logger.error(f"Error during local package installation: {e}")
            return False
    
    def run_script(self,
                   script_path: str, 
                   target_module_abs_path: str,
                   static_info_json_path: str,
                   timeout: int = 30, 
                   custom_env: Optional[Dict[str, str]] = None,
                   exclude_project_from_pythonpath: bool = False) -> subprocess.CompletedProcess:
        """
        Runs a Python script within the virtual environment.

        Args:
            script_path: Absolute path to the Python script to execute.
            target_module_abs_path: Absolute path of the module being analyzed.
            static_info_json_path: Path to the temp JSON file containing static analysis info.
            timeout: Timeout in seconds for the script execution.
            custom_env: Optional dictionary of additional environment variables.
            exclude_project_from_pythonpath: If True, does NOT add project_root to PYTHONPATH.
                Use when the module imports compiled extensions from installed package.

        Returns:
            subprocess.CompletedProcess object containing the execution results.
        """
        if not self._setup_completed or not self.effective_python_executable:
            raise RuntimeError("Virtual environment is not properly set up. Cannot run script.")

        cmd = [self.effective_python_executable, "-S", script_path, target_module_abs_path, static_info_json_path]
        
        proc_env = os.environ.copy()
        
        # Conditionally build PYTHONPATH
        if exclude_project_from_pythonpath:
            # Don't add project root - use installed package via site-packages
            logger.debug(f"PYTHONPATH: Excluding project root (compiled extension mode)")
            # Add venv's site-packages so installed packages are accessible
            if self.bin_path:
                if self.platform == "Windows":
                    site_packages = self.venv_path / "Lib" / "site-packages"
                else:
                    # Find python version for Linux/Mac
                    site_packages = self.venv_path / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
                python_path_parts = [str(site_packages)]
            else:
                python_path_parts = []
        else:
            # Normal mode: use python_package_roots for source file access (supports src layout)
            python_path_parts = [str(root) for root in self.python_package_roots]
            # Also add project root if not already included (fallback for imports)
            if str(self.project_root_path) not in python_path_parts:
                python_path_parts.append(str(self.project_root_path))
            
            # Add site-packages so installed dependencies are accessible (needed with -S flag)
            if self.venv_path:
                if self.platform == "Windows":
                    site_packages = self.venv_path / "Lib" / "site-packages"
                else:
                    site_packages = self.venv_path / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
                if site_packages.exists():
                    python_path_parts.append(str(site_packages))
        
        existing_python_path = proc_env.get("PYTHONPATH")
        if existing_python_path and not exclude_project_from_pythonpath:
            python_path_parts.append(existing_python_path)
        
        if python_path_parts:
            proc_env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
        elif "PYTHONPATH" in proc_env:
            del proc_env["PYTHONPATH"]
        
        proc_env["PYTHONDONTWRITEBYTECODE"] = "1"
        if custom_env:
            proc_env.update(custom_env)

        logger.debug(f"Running dynamic script: {' '.join(cmd)}")
        logger.debug(f"  PYTHONPATH for script: {proc_env.get('PYTHONPATH', '(not set)')}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                env=proc_env,
                cwd=self.project_root_path
            )
            if result.returncode != 0:
                logger.warning(f"Dynamic script for {Path(target_module_abs_path).name} exited with code {result.returncode}. Stderr: {result.stderr[:500]}")
            return result
        except subprocess.TimeoutExpired:
            logger.warning(f"Dynamic script for {Path(target_module_abs_path).name} timed out after {timeout}s.")
            return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=f"Timeout ({timeout}s)")
        except Exception as e:
            logger.error(f"Exception running dynamic script for {Path(target_module_abs_path).name}: {e}", exc_info=True)
            return subprocess.CompletedProcess(cmd, returncode=-2, stdout="", stderr=f"Exception: {str(e)}")

    def cleanup(self) -> None:
        """Cleans up the virtual environment if it's not a shared one or if shared one is invalidated."""
        global _SHARED_VENV_DETAILS
        with _SHARED_VENV_LOCK:
            if self.reuse_shared_env and _SHARED_VENV_DETAILS and Path(_SHARED_VENV_DETAILS["venv_path"]) == self.venv_path:
                logger.debug(f"Not cleaning up shared venv: {self.venv_path}")
                return # Don't clean up the active shared venv

            if self.venv_path and self.venv_path.exists():
                logger.info(f"Cleaning up virtual environment: {self.venv_path}")
                try:
                    shutil.rmtree(self.venv_path, ignore_errors=True)
                except Exception as e:
                    logger.warning(f"Error during venv cleanup {self.venv_path}: {e}")
            
            # If this was the shared one and it got cleaned (e.g. due to earlier error), clear the global
            if _SHARED_VENV_DETAILS and Path(_SHARED_VENV_DETAILS["venv_path"]) == self.venv_path:
                _SHARED_VENV_DETAILS = None
        
        self._setup_completed = False
        self.venv_path = None


class DynamicAnalyzer:
    """
    Performs dynamic analysis of Python modules by executing them in an isolated environment to determine runtime exports and their origins.
    """
    
    def __init__(self, repo_path: str, config: Optional[AnalysisConfig] = None, python_package_roots: Optional[List[Path]] = None):
        """
        Args:
            repo_path: Absolute path to the root of the repository being analyzed.
            config: Analysis configuration.
            python_package_roots: Optional list of paths to Python package roots (for src layout support).
                                  If None, defaults to [repo_path].
        """
        self.repo_path = Path(repo_path).resolve()
        self.config = config or AnalysisConfig()
        self.python_package_roots = python_package_roots if python_package_roots else [self.repo_path]
        self.venv: Optional[VirtualEnvironment] = None
        self._setup_attempted = False # To avoid repeated setup failures in a session
        self._setup_successful = False
        
        logger.info(f"{self.__class__.__name__} initialized for repo: {self.repo_path}")
    
    def _parse_pyproject_toml(self, toml_file_path: Path) -> Dict[str, str]:
        """
        Parses pyproject.toml to extract project dependencies.
        Focuses on [project.dependencies] and [project.optional-dependencies].
        """
        dependencies: Dict[str, str] = {}
        if not toml_file_path.exists():
            return dependencies

        logger.debug(f"Parsing dependencies from {toml_file_path}")
        try:
            data = toml.load(toml_file_path)
            
            project_table = data.get("project", {})
            
            # Core dependencies
            if "dependencies" in project_table and isinstance(project_table["dependencies"], list):
                for dep_spec in project_table["dependencies"]:
                    if isinstance(dep_spec, str):
                        # Strip inline comments (e.g., "package>=1.0  # comment")
                        dep_spec = dep_spec.split('#')[0].strip()
                        if not dep_spec:
                            continue
                        
                        # Simple name extraction for the key, full spec as value
                        match = re.match(r"^([a-zA-Z0-9_.-]+)", dep_spec)
                        name = match.group(1) if match else dep_spec.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].strip()
                        if name: dependencies[name] = dep_spec
            
            # Optional dependencies - we might want to install all for comprehensive analysis
            # Or allow configuration to specify which extras to install. For now, grab all.
            if "optional-dependencies" in project_table and isinstance(project_table["optional-dependencies"], dict):
                for group_name, group_deps in project_table["optional-dependencies"].items():
                    if isinstance(group_deps, list):
                        logger.debug(f"Adding optional dependency group: {group_name}")
                        for dep_spec in group_deps:
                            if isinstance(dep_spec, str):
                                # Strip inline comments
                                dep_spec = dep_spec.split('#')[0].strip()
                                if not dep_spec:
                                    continue
                                
                                match = re.match(r"^([a-zA-Z0-9_.-]+)", dep_spec)
                                name = match.group(1) if match else dep_spec.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].strip()
                                if name and name not in dependencies:
                                    dependencies[name] = dep_spec
                                elif name and name in dependencies:
                                    logger.debug(f"Optional dependency '{name}' already in core dependencies. Keeping core spec: '{dependencies[name]}'")


            # Build system dependencies (less common for runtime, but might be needed for sdist/wheel build before analysis)
            # build_system_table = data.get("build-system", {})
            # if "requires" in build_system_table and isinstance(build_system_table["requires"], list):
            #     for dep_spec in build_system_table["requires"]:
            #         if isinstance(dep_spec, str) and "setuptools" not in dep_spec and "wheel" not in dep_spec: # Avoid common build tools if already handled
            #             name = dep_spec.split("==")[0].split(">=")[0].strip()
            #             if name and name not in dependencies: dependencies[name] = dep_spec
                        
        except ImportError:
            logger.warning("`toml` library not installed. Cannot parse pyproject.toml for dependencies. Please install with `pip install toml`.")
        except Exception as e:
            logger.warning(f"Error parsing {toml_file_path}: {e}")
        return dependencies


    def _parse_setup_py(self, setup_py_path: Path) -> Dict[str, str]:
        """
        Parses setup.py using AST to extract `install_requires` dependencies.
        Avoids executing the setup.py file.
        """
        dependencies: Dict[str, str] = {}
        if not setup_py_path.exists():
            return dependencies

        logger.debug(f"Attempting to parse dependencies from {setup_py_path} using AST.")
        try:
            with open(setup_py_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            tree = ast.parse(content, filename=str(setup_py_path))
            
            # Look for `setup(install_requires=[...])` or `install_requires = [...]`
            found_install_requires = None

            for node in ast.walk(tree):
                # Case 1: Direct assignment `install_requires = [...]`
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "install_requires":
                            if isinstance(node.value, (ast.List, ast.Tuple)):
                                found_install_requires = node.value
                                break
                    if found_install_requires:
                        break
                
                # Case 2: Call to setup(..., install_requires=[...], ...)
                if isinstance(node, ast.Call):
                    is_setup_call = False
                    if isinstance(node.func, ast.Name) and node.func.id == "setup":
                        is_setup_call = True
                    # Could also be setuptools.setup() but requires resolving 'setuptools' import
                    elif isinstance(node.func, ast.Attribute) and \
                         isinstance(node.func.value, ast.Name) and \
                         node.func.value.id == "setuptools" and node.func.attr == "setup":
                        is_setup_call = True # Handle setuptools.setup()

                    if is_setup_call:
                        for kw in node.keywords:
                            if kw.arg == "install_requires":
                                if isinstance(kw.value, (ast.List, ast.Tuple)):
                                    found_install_requires = kw.value
                                    break
                        if found_install_requires:
                            break
            
            if found_install_requires:
                for elt in found_install_requires.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str): # Python 3.8+
                        dep_spec = elt.value
                        match = re.match(r"^([a-zA-Z0-9_.-]+)", dep_spec)
                        name = match.group(1) if match else dep_spec.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].strip()
                        if name: dependencies[name] = dep_spec
                    elif isinstance(elt, ast.Str): # Python < 3.8
                        dep_spec = elt.s
                        match = re.match(r"^([a-zA-Z0-9_.-]+)", dep_spec)
                        name = match.group(1) if match else dep_spec.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].strip()
                        if name: dependencies[name] = dep_spec
        except SyntaxError:
            logger.warning(f"Syntax error parsing {setup_py_path}. Cannot extract dependencies.")
        except Exception as e:
            logger.warning(f"Error parsing {setup_py_path} with AST: {e}")
        return dependencies
    
    def _collect_build_dependencies(self) -> Dict[str, str]:
        """
        Collects build-system dependencies from pyproject.toml.
        
        These are needed to build packages with compiled extensions from source.
        Common build deps include: setuptools, wheel, cmake, ninja, pyyaml, etc.
        
        Returns:
            Dictionary mapping package name to install spec
        """
        build_deps: Dict[str, str] = {}
        
        pyproject_path = self.repo_path / "pyproject.toml"
        if not pyproject_path.exists():
            return build_deps
        
        logger.debug(f"Collecting build dependencies from {pyproject_path}")
        try:
            data = toml.load(pyproject_path)
            
            build_system_table = data.get("build-system", {})
            if "requires" in build_system_table and isinstance(build_system_table["requires"], list):
                for dep_spec in build_system_table["requires"]:
                    if isinstance(dep_spec, str):
                        # Strip inline comments (e.g., "cmake>=3.18 # for CUDA support")
                        dep_spec = dep_spec.split('#')[0].strip()
                        if not dep_spec: continue
                        
                        match = re.match(r"^([a-zA-Z0-9_.-]+)", dep_spec)
                        name = match.group(1) if match else dep_spec.split("==")[0].split(">=")[0].strip()
                        if name:
                            build_deps[name] = dep_spec
                            
            logger.info(f"Collected {len(build_deps)} build dependencies: {list(build_deps.keys())[:10]}...")
            
        except Exception as e:
            logger.warning(f"Error collecting build dependencies: {e}")
        
        return build_deps
    
    def _collect_project_dependencies(self) -> Dict[str, str]:
        """
        Collects project dependencies from requirements.txt, pyproject.toml, and setup.py.
        """
        all_dependencies: Dict[str, str] = {}

        # 1. requirements.txt (highest precedence for explicit deps for an env)
        req_file = self.repo_path / 'requirements.txt'
        if req_file.exists():
            logger.info(f"Parsing dependencies from {req_file}")
            try:
                with open(req_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and not line.startswith('-'):
                            # Strip inline comments
                            line = line.split('#')[0].strip()
                            if not line:
                                continue
                            
                            match = re.match(r"^([a-zA-Z0-9_.-]+)", line)
                            name = match.group(1) if match else line.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].strip()
                            if name: 
                                if name in all_dependencies:
                                    logger.debug(f"Dependency '{name}' from requirements.txt overriding previous.")
                                all_dependencies[name] = line
            except Exception as e:
                logger.warning(f"Error parsing requirements.txt {req_file}: {e}")

        # 2. pyproject.toml
        pyproject_file = self.repo_path / 'pyproject.toml'
        toml_deps = self._parse_pyproject_toml(pyproject_file)
        for name, spec in toml_deps.items():
            if name not in all_dependencies: # requirements.txt takes precedence
                all_dependencies[name] = spec
            else:
                logger.debug(f"Dependency '{name}' from pyproject.toml already found from requirements.txt. Keeping requirements.txt version.")


        # 3. setup.py (lowest precedence for install_requires)
        setup_py_file = self.repo_path / 'setup.py'
        setup_deps = self._parse_setup_py(setup_py_file)
        for name, spec in setup_deps.items():
            if name not in all_dependencies: # Other files take precedence
                all_dependencies[name] = spec
            else:
                logger.debug(f"Dependency '{name}' from setup.py already found from other sources. Keeping other version.")

        logger.info(f"Collected {len(all_dependencies)} unique project dependencies: {list(all_dependencies.keys())}")
        return all_dependencies


    def _ensure_environment(self) -> bool:
        """Ensures the virtual environment for dynamic analysis exists and is set up. If it doesn't exist, it creates it and installs dependencies."""
        
        if self._setup_successful:
            return True
        if self._setup_attempted: # Already tried and failed
            return False 
        
        self._setup_attempted = True
        self.venv = VirtualEnvironment(
            project_root_path=self.repo_path, 
            reuse_shared_env=True,
            python_package_roots=self.python_package_roots
        )
        if not self.venv.setup():
            self.venv = None
            return False

        base_deps = {"pip": "pip>=20.0", "setuptools": "setuptools", "wheel": "wheel", "typing_extensions": "typing_extensions"}
        if not self.venv.install_dependencies(base_deps):
            logger.warning("Failed to install/verify base venv deps. Dynamic analysis may be unstable.")
        
        # quick sanity check: import inside the venv
        try:
            subprocess.run(
                [self.venv.effective_python_executable, "-c", "import typing_extensions"],
                check=True, capture_output=True, text=True, timeout=30
            )
            logger.debug("typing_extensions imported successfully inside the venv.")
        except Exception:
            logger.warning("typing_extensions still missing after installation attempt.")
        
        if self.config.auto_install_dependencies:
            project_deps = self._collect_project_dependencies()
            if project_deps:
                logger.info(f"Attempting to install {len(project_deps)} project dependencies for dynamic analysis.")
                if not self.venv.install_dependencies(project_deps):
                    logger.warning("Failed to install all project-specific dependencies. Module imports might fail.")
        
        if self.venv and self.venv.effective_python_executable and Path(self.venv.effective_python_executable).exists():
            self._setup_successful = True
            return True
        else:
            if self.venv:
                self.venv.cleanup()
                self.venv = None
            return False

    def install_target_package(self, package_path: Path, package_name: str, package_version: Optional[str] = None) -> bool:
        """
        Installs the target package being analyzed into the dynamic analysis venv.
        
        Strategy:
        1. Install build dependencies
        2. Install runtime dependencies  
        3. Try local source install (pip install -e .)
        4. If local fails, fall back to PyPI with matching version
        
        Args:
            package_path: Root path of the package repository
            package_name: Name of the package (from project metadata)
            package_version: Version from project metadata (for PyPI fallback)
            
        Returns:
            True if installation succeeded, False otherwise
        """
        if not self._ensure_environment():
            logger.error("Cannot install target package: venv setup failed")
            return False
        
        if not self.venv:
            logger.error("Cannot install target package: no venv available")
            return False
        
        # Step 1: Install build dependencies first
        build_deps = self._collect_build_dependencies()
        if build_deps:
            logger.info(f"Installing {len(build_deps)} build dependencies...")
            if not self.venv.install_dependencies(build_deps):
                logger.warning("Some build dependencies failed to install. Continuing...")
        
        # Step 2: Install runtime dependencies
        runtime_deps = self._collect_project_dependencies()
        if runtime_deps:
            logger.info(f"Installing {len(runtime_deps)} runtime dependencies...")
            if not self.venv.install_dependencies(runtime_deps):
                logger.warning("Some runtime dependencies failed to install. Continuing...")
        
        # Step 3: Try local source install
        logger.info(f"Attempting to install '{package_name}' from local source...")
        success = self.venv.install_local_package(
            package_path=package_path,
            package_name=package_name,
            editable=getattr(self.config, 'prefer_editable_install', True),
            timeout=getattr(self.config, 'target_package_install_timeout', 600)
        )
        
        if success:
            return True
        
        # Step 4: Fall back to PyPI with version matching
        logger.info(f"Local source install failed. Falling back to PyPI...")
        return self._install_from_pypi(package_name, package_version)
    
    
    def _install_from_pypi(self, package_name: str, package_version: Optional[str] = None) -> bool:
        """
        Installs a package from PyPI as a fallback when local build fails.
        
        Uses the version extracted from the repository to get a matching release.
        Falls back to latest if version not found on PyPI.
        
        Args:
            package_name: Package name
            package_version: Target version (from repo metadata)
            
        Returns:
            True if installation succeeded
        """
        if not self.venv:
            return False
        
        # Try exact version first
        if package_version:
            # Clean version string (remove dev/local parts that PyPI won't have)
            # e.g., "2.11.0a0+git51bdb12" -> try "2.11.0" or closest
            clean_version = self._normalize_version_for_pypi(package_version)
            
            if clean_version:
                logger.info(f"Trying PyPI install: {package_name}=={clean_version}")
                spec = f"{package_name}=={clean_version}"
                if self.venv.install_dependencies({package_name: spec}):
                    logger.info(f"Successfully installed {spec} from PyPI")
                    return True
                
                # Try without patch version (e.g., 2.11.0 -> 2.11)
                major_minor = '.'.join(clean_version.split('.')[:2])
                if major_minor != clean_version:
                    logger.info(f"Exact version not found. Trying: {package_name}>={major_minor},<{int(major_minor.split('.')[0])+1}.0")
                    spec = f"{package_name}>={major_minor},<{int(major_minor.split('.')[0])+1}.0"
                    if self.venv.install_dependencies({package_name: spec}):
                        logger.info(f"Successfully installed {package_name} (version range) from PyPI")
                        return True
        
        # Fall back to latest version
        logger.info(f"Trying latest version: {package_name}")
        if self.venv.install_dependencies({package_name: package_name}):
            logger.info(f"Successfully installed latest {package_name} from PyPI")
            return True
        
        logger.error(f"Failed to install {package_name} from PyPI")
        return False
    
    
    def _normalize_version_for_pypi(self, version: str) -> Optional[str]:
        """
        Normalizes a version string for PyPI lookup.
        
        Strips dev/local identifiers that won't exist on PyPI.
        E.g., "2.11.0a0+git51bdb12" -> "2.11.0"
             "1.0.0.dev123" -> "1.0.0"
        
        Args:
            version: Raw version string from repository
            
        Returns:
            Normalized version suitable for PyPI, or None if unparseable
        """
        if not version:
            return None
        
        import re
        
        # Remove local version identifier (+...)
        version = re.sub(r'\+.*$', '', version)
        
        # Remove dev/alpha/beta suffixes for base version
        # But keep if it's an actual release (e.g., 2.0.0a1 might exist on PyPI)
        base_match = re.match(r'^(\d+\.\d+(?:\.\d+)?)', version)
        if base_match:
            return base_match.group(1)
        
        return None
    
    def evaluate_module_exports(self, 
                                module_abs_path: str,
                                static_info: Dict[str, Any],
                                has_compiled_extension_imports: bool = False) -> Optional[Dict[str, Any]]:
        """
        Dynamically executes a module to determine its runtime exports.

        Args:
            module_abs_path: Absolute path to the Python module file.
            static_info: Dictionary containing static analysis info for the module, including:
                         - "module_fqn": FQN of the module.
                         - "import_records": List of ImportRecord dicts from static analysis.
                         - "local_definitions": List of FQNs defined locally in the module.
            has_compiled_extension_imports: If True, exclude project root from PYTHONPATH
                so installed package (with compiled extensions) is used.

        Returns:
            A dictionary with "module_fqn", "discovered_exports" (list of export observation dicts),
            and "dynamic_execution_error" (if any), or None if dynamic analysis is disabled/fails early.
        """
        
        if not is_enabled(Feature.DYNAMIC_ALL_EVALUATION):
            logger.debug(f"Dynamic export evaluation disabled for {module_abs_path}")
            return None
        
        if os.path.basename(module_abs_path) == "setup.py":
            logger.info("Skipping dynamic analysis for setup.py")
            return {"module_fqn": static_info.get("module_fqn"), "discovered_exports": [], "dynamic_execution_error": "Skipped dynamic analysis for setup.py"}
        
        if not self._ensure_environment() or not self.venv: # Check self.venv also
            return {"module_fqn": static_info.get("module_fqn"), "discovered_exports": [], "dynamic_execution_error": "Dynamic environment setup failed."}
        
        # The calling context (AnalyzerIntegration) MUST now provide top_level_packages
        if "top_level_packages" not in static_info:
            logger.warning("`top_level_packages` not found in static_info for dynamic analysis. Runtime import filtering will be disabled.")
            static_info["top_level_packages"] = []
        
        static_info_json_str = json.dumps(static_info)
        static_info_file: Optional[Path] = None # create a temporary file for the static info JSON
        dynamic_script_file: Optional[Path] = None
        
        try:
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding='utf-8') as tmp_f:
                tmp_f.write(static_info_json_str)
                static_info_file = Path(tmp_f.name)
        except Exception as e:
            logger.error(f"Failed to write static info to temp file for dynamic analysis: {e}")
            return {"dynamic_execution_error": "Failed to prepare static info for script."}

        # Create the dynamic analysis script on the fly
        script_content = self._get_dynamic_analysis_script_content()
        
        try:
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix="_mapcodoc_dyn_script.py", encoding='utf-8') as tmp_script:
                tmp_script.write(script_content)
                dynamic_script_file = Path(tmp_script.name)

            # Pass ANALYSIS_REPO_ROOT to the script's environment for robust pathing inside script
            custom_env = {"ANALYSIS_REPO_ROOT": str(self.repo_path)}

            # If compiled extensions detected, signal script to not modify sys.path
            if has_compiled_extension_imports:
                custom_env["USE_INSTALLED_PACKAGE"] = "true"
            
            process_result = self.venv.run_script(
                script_path=str(dynamic_script_file),
                target_module_abs_path=module_abs_path,
                static_info_json_path=str(static_info_file),
                timeout=self.config.dynamic_analysis_timeout,
                custom_env=custom_env,
                exclude_project_from_pythonpath=has_compiled_extension_imports
            )

            if process_result.returncode != 0 or not process_result.stdout.strip():
                error_detail = process_result.stderr[:1000] if process_result.stderr else "No stderr output."
                logger.warning(f"Dynamic script for {Path(module_abs_path).name} failed (code {process_result.returncode}) or produced no output. Stderr: {error_detail}")
                return {"module_fqn": static_info.get("module_fqn"), 
                        "discovered_exports": [],
                        "dynamic_execution_error": f"Script execution failed (code {process_result.returncode}): {error_detail}"}
            
            try:
                dynamic_data = json.loads(process_result.stdout)
                return dynamic_data
            except json.JSONDecodeError as json_e:
                logger.error(f"Failed to parse JSON output from dynamic script for {module_abs_path}: {json_e}. Output: {process_result.stdout[:1000]}")
                return {"module_fqn": static_info.get("module_fqn"),
                        "discovered_exports": [],
                        "dynamic_execution_error": f"JSON parsing failed: {json_e}"}

        except Exception as e:
            logger.error(f"Exception during dynamic export evaluation for {module_abs_path}: {e}", exc_info=True)
            return {"module_fqn": static_info.get("module_fqn"),
                    "discovered_exports": [],
                    "dynamic_execution_error": f"Outer exception: {str(e)}"}
        finally:
            if dynamic_script_file and dynamic_script_file.exists():
                try: os.unlink(dynamic_script_file)
                except OSError: pass
            if static_info_file and static_info_file.exists():
                try: os.unlink(static_info_file)
                except OSError: pass
    
    
    def _get_dynamic_analysis_script_content(self) -> str:
        """
        Generates the complete Python script that runs in an isolated environment.

        This enhanced script does the following:
        1.  Sets up an interception mechanism (monkey-patching) for `importlib.import_module`.
        2.  Executes the target module's code, capturing any dynamic imports that occur.
        3.  After execution, introspects the module's final namespace (`__all__` or `dir()`).
        4.  For each exported name, it determines its origin by checking:
            a. Statically known local definitions.
            b. Statically known direct imports.
            c. The new list of intercepted dynamic imports.
            d. Final fallback to runtime introspection (`obj.__module__`).
            e. Tags its import source as 'static' or 'dynamic'.
        5.  Prints the final, comprehensive export information as a JSON object to stdout.
        """
        # All helper functions are defined within this script string.
        # Using ''' for the script string. Internal ''' are escaped as \'\'\'.
        # Ensure all necessary standard library imports for the script are at the top.
        
        script_content = """
import os
import sys
import json
import types
import inspect
import importlib.util
import traceback
import uuid # For unique module name during import
import logging
from pathlib import Path

# --- Script-level Setup ---
# Minimal logger for the dynamic script itself to avoid "no handler" messages if the main application uses logging but the script doesn't configure it.
_dyn_script_logger = logging.getLogger("__mapcodoc_dynamic_script__")
if not _dyn_script_logger.handlers:
    # Add a NullHandler to prevent "No handlers could be found" errors if the script itself tries to log something (e.g. via a library it uses) and the calling environment hasn't configured logging for this name.
    _dyn_script_logger.addHandler(logging.NullHandler())
_dyn_script_logger.propagate = False # Don't pass to root logger from here

# --- Monkey-Patching Infrastructure for Dynamic Import Interception ---
_original_import_module = importlib.import_module
_DYNAMICALLY_LOADED_MODULES = set()  # Stores FQNs of modules imported via import_module
_PROJECT_PREFIXES = tuple() # set later from static info
_STUB_EXTERNAL = False  # default: disabled

def _wrapped_import_module(name, package=None):
    \'\'\'A wrapper around importlib.import_module to intercept and log dynamic imports.\'\'\'
    global _DYNAMICALLY_LOADED_MODULES, _PROJECT_PREFIXES
    try:
        # Resolve relative imports to absolute FQNs if package is provided
        absolute_name = importlib.util.resolve_name(name, package) if package and name.startswith('.') else name
        imported_module = _original_import_module(name, package)
        # Store the resolved absolute name for tracking
        _DYNAMICALLY_LOADED_MODULES.add(absolute_name)
        _dyn_script_logger.debug(f"[DYNAMIC_TRACE] Imported: '{name}' resolved to '{absolute_name}'")
        return imported_module
    except ModuleNotFoundError as e:
        # Stub only if explicitly enabled and clearly not part of the analyzed project
        if _STUB_EXTERNAL and (not _PROJECT_PREFIXES or not any(absolute_name.startswith(p) for p in _PROJECT_PREFIXES)):
            stub = types.ModuleType(absolute_name)
            sys.modules[absolute_name] = stub
            _dyn_script_logger.debug(f"[DYNAMIC_TRACE] Stubbed missing external module: '{absolute_name}'")
            return stub
        # Re-raise the exception so the target module's behavior is not altered
        raise
# --- 

def _get_actual_defining_module_and_name(obj, default_module_name_for_obj, obj_name_in_current_scope):
    \'\'\'
    Tries to get the FQN prefix (defining module) and actual simple name of an object.
    Helps differentiate between an object and an alias pointing to it.
    \'\'\'
    obj_defining_module_name = None
    obj_actual_name = obj_name_in_current_scope # Default to how it's known in the current scope

    try:
        # For module objects themselves (e.g. if a module is exported)
        # Note: type(sys) is types.ModuleType, ensure types is imported if this check is made more specific
        if 'types' in sys.modules and isinstance(obj, sys.modules['types'].ModuleType):
            obj_defining_module_name = obj.__name__
            obj_actual_name = obj.__name__.split('.')[-1]
            return obj_defining_module_name, obj_actual_name
        if hasattr(obj, '__module__'):
            obj_defining_module_name = obj.__module__
        # For other callables (functions, classes, methods)
        if hasattr(obj, '__name__'): 
            obj_actual_name = obj.__name__
        # If __module__ is None (e.g. for some builtins or C extensions) or doesn't make sense, try inspect.getmodule as a fallback.
        if not obj_defining_module_name or obj_defining_module_name == 'builtins':
            inspected_module = inspect.getmodule(obj)
            if inspected_module and hasattr(inspected_module, '__name__'):
                obj_defining_module_name = inspected_module.__name__
            # If inspect.getmodule returns None (e.g. for a builtin method bound to a type), obj_defining_module_name remains as it was (None or 'builtins').
    except Exception as e:
        _dyn_script_logger.debug(f"Dynamic script: Error introspecting {obj_name_in_current_scope}: {e}")
        # Fallback if introspection fails; obj_defining_module_name might still be None or from getattr
        if obj_defining_module_name is None and hasattr(obj, '__module__'): # Try __module__ one last time
            obj_defining_module_name = obj.__module__

    # If, after all attempts, the defining module is still unclear or points to builtins, and it's not clearly a module object itself, attribute it to the default.
    if obj_defining_module_name is None:
        obj_defining_module_name = default_module_name_for_obj if getattr(obj, '__module__', None) != 'builtins' else 'builtins'
    return obj_defining_module_name, obj_actual_name


def main_dynamic_analysis(target_module_abs_path_str, static_info_json_path_str):
    results = {"module_fqn": "unknown_module", "discovered_exports": [], "runtime_imports": [], "dynamic_execution_error": None, "traceback_info": None, "debug_info": {}}
    static_info = {}
    
    # --- Load Static Info ---
    try:
        with open(static_info_json_path_str, 'r', encoding='utf-8') as f_static:
            static_info = json.load(f_static)
        static_imports_map = {rec["name_bound_in_importer"]: rec for rec in static_info.get("import_records", [])}
        static_local_definition_fqns = set(static_info.get("local_definition_fqns", []))
        target_module_fqn_from_static = static_info.get("module_fqn", "unknown_module_from_static")
        results["module_fqn"] = target_module_fqn_from_static # Use FQN from static info
    except Exception as e:
        results["dynamic_execution_error"] = "Failed to load static_info_json -> {}: {}".format(type(e).__name__, str(e))
        results["traceback_info"] = traceback.format_exc()
        print(json.dumps(results))
        return

    # Ensure project root is in sys.path for the target module's own imports ONLY if not using installed package
    use_installed_pkg = os.environ.get("USE_INSTALLED_PACKAGE", "").lower() == "true"
    if not use_installed_pkg:
        repo_root_env = os.environ.get("ANALYSIS_REPO_ROOT")
        if repo_root_env and Path(repo_root_env).is_dir():
            if repo_root_env not in sys.path:
                sys.path.insert(0, str(repo_root_env))
            repo_parent = str(Path(repo_root_env).parent)
            if repo_parent not in sys.path:
                sys.path.insert(0, repo_parent)
    else:
        _dyn_script_logger.debug("Using installed package mode - not adding repo root to sys.path")
    results["debug_info"]["initial_sys_path_head"] = sys.path[:5]
    results["debug_info"]["use_installed_package"] = use_installed_pkg
    
    # Configure stubbing from caller (defaults to False)
    global _STUB_EXTERNAL
    _STUB_EXTERNAL = bool(static_info.get("stub_external_imports", False))
    # Set project prefixes for stubbing and activate import hook now
    global _PROJECT_PREFIXES
    _PROJECT_PREFIXES = tuple(static_info.get("top_level_packages", []))
    importlib.import_module = _wrapped_import_module
    
    # Prepare parent package chain in sys.modules (namespace-safe)
    # But skip this in installed package mode and let Python use the installed package's structure
    if not use_installed_pkg:
        try:
            parts = target_module_fqn_from_static.split('.')
            for i in range(1, len(parts)):
                pkg = ".".join(parts[:i])
                if pkg not in sys.modules:
                    mod = types.ModuleType(pkg)
                    mod.__path__ = [str(Path(target_module_abs_path_str).parents[len(parts) - i])]
                    sys.modules[pkg] = mod
        except Exception as e:
            _dyn_script_logger.debug(f"Could not pre-create package chain for {target_module_fqn_from_static}: {e}")

    # Take a snapshot of sys.modules before executing the target module
    initial_modules = set(sys.modules.keys())

    # --- Execute Target Module ---
    dynamic_module_spec_name = f"__mapcodoc_dyn_target_{uuid.uuid4().hex[:6]}__"
    module_being_analyzed = None

    try:
        if use_installed_pkg:
            # For compiled extension modules, import from INSTALLED package
            # This ensures torch._C, torch.utils, etc. are all from the same installed source
            _dyn_script_logger.debug(f"Importing {target_module_fqn_from_static} from installed package")
            module_being_analyzed = importlib.import_module(target_module_fqn_from_static)
            sys.modules[dynamic_module_spec_name] = module_being_analyzed
        else:
            # For pure Python analysis, execute source file directly
            spec = importlib.util.spec_from_file_location(target_module_fqn_from_static, target_module_abs_path_str)
            if not spec or not spec.loader:
                raise ImportError(f"Could not create module spec for {target_module_abs_path_str}")
            
            module_being_analyzed = importlib.util.module_from_spec(spec)
            sys.modules[target_module_fqn_from_static] = module_being_analyzed 
            sys.modules[dynamic_module_spec_name] = module_being_analyzed 
            
            try:
                spec.loader.exec_module(module_being_analyzed)
            except ModuleNotFoundError as e:
                # Relative imports may fail because sibling modules aren't loaded in this subprocess
                # This is expected for some modules - skip dynamic analysis gracefully
                _dyn_script_logger.info(f"Skipping dynamic analysis due to import dependency: {e}")
                # Clean up
                sys.modules.pop(target_module_fqn_from_static, None)
                sys.modules.pop(dynamic_module_spec_name, None)
                # Return skip result
                results["dynamic_execution_skipped"] = True
                results["skip_reason"] = f"Module has unresolved import dependency: {e}"
                print(json.dumps(results))
                return

        # --- Analyze Final Namespace ---
        effective_exports = []
        is_explicit_from_all = False
        has_all_attribute = hasattr(module_being_analyzed, "__all__")
        
        if has_all_attribute:
            module_all_attr = module_being_analyzed.__all__
            if isinstance(module_all_attr, (list, tuple)):
                # Convert all items to strings - __all__ may contain non-string items that evaluate to strings at runtime (e.g., variables)
                effective_exports = []
                for x in module_all_attr:
                    if isinstance(x, str):
                        effective_exports.append(x)
                    else:
                        # Try to convert non-string items to string (handles dynamic values)
                        try:
                            effective_exports.append(str(x))
                        except Exception:
                            _dyn_script_logger.debug(f"Could not convert __all__ item to string: {x!r}")
                is_explicit_from_all = True
            else:
                _dyn_script_logger.warning(f"Module {target_module_fqn_from_static} __all__ is not list/tuple (type: {type(module_all_attr)}). Using dir() fallback.")
        
        # Only fall back to dir() if there's NO __all__ attribute at all
        # If __all__ exists but is empty, that's intentional - don't override with dir()
        if not has_all_attribute:
            effective_exports = [name for name in dir(module_being_analyzed) if not name.startswith('_')]
            is_explicit_from_all = False
            
        results["module_has_explicit_all"] = is_explicit_from_all
        # Use consistent key name for both cases
        results["module_all_values"] = list(effective_exports)
        results["resolved_all_values"] = list(effective_exports)  # For backward compatibility

        for exported_name_in_scope in effective_exports:
            if not hasattr(module_being_analyzed, exported_name_in_scope):
                _dyn_script_logger.warning(f"Name '{exported_name_in_scope}' in effective exports of {target_module_fqn_from_static} but not found as an attribute.")
                continue
            
            obj = getattr(module_being_analyzed, exported_name_in_scope)
            
            export_info = {
                "exported_name": exported_name_in_scope,
                "is_explicit_export": is_explicit_from_all,
                "is_reexport": False, # Default
                "target_item_fqn": None, # FQN of the item being pointed to
                "source_module": None, # FQN of module from which item was imported into current module
                "defining_module_fqn": None, # FQN of module where the item is actually defined
                "source_import_type": "static" # Default to static
            }
            
            # Determine runtime component kind (member/module/package)
            try:
                _is_module_obj = isinstance(obj, types.ModuleType)
            except Exception:
                _is_module_obj = False
            if _is_module_obj:
                export_info["component_kind"] = "package" if hasattr(obj, "__path__") else "module"
            else: export_info["component_kind"] = "member"

            # --- Determine the object's true origin using static info and runtime introspection ---
            static_import_record = static_imports_map.get(exported_name_in_scope)
            potential_local_fqn = f"{target_module_fqn_from_static}.{exported_name_in_scope}"

            if static_import_record:
                # Case 1: It's a statically known import.
                export_info["is_reexport"] = True
                export_info["source_module"] = static_import_record.get("source_module_fqn")
                export_info["target_item_fqn"] = static_import_record.get("name_bound_points_to_fqn")
                # Introspect the object to find its actual defining module
                obj_def_mod, _ = _get_actual_defining_module_and_name(obj, target_module_fqn_from_static, exported_name_in_scope)
                export_info["defining_module_fqn"] = obj_def_mod
            
            elif potential_local_fqn in static_local_definition_fqns:
                # Case 2: It's a statically known local definition.
                export_info["is_reexport"] = False
                export_info["defining_module_fqn"] = target_module_fqn_from_static
                export_info["target_item_fqn"] = potential_local_fqn 
            
            else:
                # Case 3: Fallback using runtime introspection for dynamically created names or wildcard imports.
                obj_def_mod, obj_actual_name = _get_actual_defining_module_and_name(obj, target_module_fqn_from_static, exported_name_in_scope)
                if obj_def_mod and obj_def_mod != target_module_fqn_from_static and obj_def_mod != 'builtins':
                    export_info["is_reexport"] = True
                    export_info["source_module"] = obj_def_mod
                    export_info["defining_module_fqn"] = obj_def_mod
                    export_info["target_item_fqn"] = f"{obj_def_mod}.{obj_actual_name}"
                else:
                    export_info["is_reexport"] = False
                    export_info["defining_module_fqn"] = target_module_fqn_from_static
                    export_info["target_item_fqn"] = f"{target_module_fqn_from_static}.{exported_name_in_scope}"
            
            # After determining the origin, check if the source was loaded dynamically
            if export_info["is_reexport"] and export_info["source_module"] in _DYNAMICALLY_LOADED_MODULES:
                export_info["source_import_type"] = "dynamic"
            
            results["discovered_exports"].append(export_info)
            
        # --- Analyze Final sys.modules for Imports ---
        final_modules = set(sys.modules.keys())
        newly_loaded_modules = final_modules - initial_modules
        
        # We only care about modules that are part of the project being analyzed.
        # We use the top_level_packages list passed from the static analysis to filter.
        if _PROJECT_PREFIXES:
            runtime_project_imports = [mod_name for mod_name in newly_loaded_modules if any(mod_name.startswith(p) for p in _PROJECT_PREFIXES)]
            results["runtime_imports"] = sorted(list(set(runtime_project_imports)))

    except (ModuleNotFoundError, ImportError) as e:
        error_msg = str(e)
        results["dynamic_execution_error"] = f"{type(e).__name__}: {error_msg}"
        results["traceback_info"] = traceback.format_exc()
    
    except AttributeError as e:
        error_msg = str(e)
        results["dynamic_execution_error"] = f"{type(e).__name__}: {error_msg}"
        results["traceback_info"] = traceback.format_exc()
    
    except Exception as e:
        results["dynamic_execution_error"] = f"{type(e).__name__}: {str(e)}"
        results["traceback_info"] = traceback.format_exc()

    finally:
        # CRITICAL: Restore the original importlib.import_module
        importlib.import_module = _original_import_module
        # Clean up: remove the dynamically loaded module from sys.modules to prevent interference
        if dynamic_module_spec_name in sys.modules:
            del sys.modules[dynamic_module_spec_name]
        
    print(json.dumps(results))

if __name__ == "__main__":
    # Script expects: <script_name> <target_module_abs_path> <static_info_json_path>
    if len(sys.argv) == 3:
        main_dynamic_analysis(sys.argv[1], sys.argv[2])
    else:
        # Print error to stderr, and JSON error to stdout for parent process to catch
        err_msg = "Dynamic analysis script called with incorrect number of arguments."
        sys.stderr.write(err_msg + "\\n")
        print(json.dumps({"module_fqn": "unknown_script_call_error", "discovered_exports": [], "dynamic_execution_error": err_msg}))

"""
        return script_content.strip()


    def cleanup(self):
        """Cleans up the virtual environment if it was created and not designated as shared,
        or if the shared one needs to be reset/cleaned by this instance."""
        if self.venv:
            self.venv.cleanup() # VirtualEnvironment's cleanup handles shared logic
            self.venv = None
        self._setup_attempted = False
        self._setup_successful = False
        logger.info(f"{self.__class__.__name__} environment resources cleaned up.")

