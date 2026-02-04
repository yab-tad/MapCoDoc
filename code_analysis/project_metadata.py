"""
Project metadata discovery module.

Discovers project/library name and version from Python repositories using a
collect-then-score strategy that handles multiple packaging ecosystems:
- PEP 621 (pyproject.toml)
- Poetry, Hatch, Flit, PDM build backends
- Legacy setup.cfg and setup.py
- Package __version__ attributes
- Version files (__about__.py, _version.py)
- Git tags as fallback
"""

from __future__ import annotations

import configparser
import re
import subprocess
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_VERSION_ATTR_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")
_VER_ATTR_RE = re.compile(r"\bver\s*=\s*['\"]([^'\"]+)['\"]")
_TITLE_ATTR_RE = re.compile(r"__title__\s*=\s*['\"]([^'\"]+)['\"]")
_PACKAGE_ATTR_RE = re.compile(r"__package__\s*=\s*['\"]([^'\"]+)['\"]")
# --------------------------SETUP_NAME patterns--------------------------
_SETUP_NAME_ASSIGN_RE = re.compile(r'(?:^|\b)name\s*=\s*[\'"]([a-zA-Z][a-zA-Z0-9_-]*)[\'"]', re.MULTILINE) # Pattern 1: name = "value" or name="value" (assignment)
_SETUP_NAME_DICT_RE = re.compile(r'[\'"]name[\'"]\s*:\s*[\'"]([a-zA-Z][a-zA-Z0-9_-]*)[\'"]', re.MULTILINE) # Pattern 2: "name": "value" (dict literal)
_SETUP_NAME_CALL_RE = re.compile(r'setup\s*\([^)]*\bname\s*=\s*[\'"]([a-zA-Z][a-zA-Z0-9_-]*)[\'"]', re.MULTILINE | re.DOTALL) # Pattern 3: setup(name="value" - specifically in setup() call
# ---------------------------------------------------------------------------
_SETUP_VERSION_RE = re.compile(r"version\s*=\s*['\"]([^'\"]+)['\"]")
_SETUP_VERSION_DICT_RE = re.compile(r'[\'"]version[\'"]\s*:\s*[\'"]([^\'\"]+)[\'"]')
_ATTR_TEMPLATE = r"{attr}\s*=\s*['\"]([^'\"]+)['\"]"

# Version validation pattern - must look like a semantic version
# Matches: 1.0, 1.0.0, 2.3.4a1, 1.0.0rc2, 1.0.0.dev123, etc.
_VALID_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*(?:[-._]?(?:a|alpha|b|beta|c|rc|dev|post|pre|rev|r)\d*)*(?:[+.].*)?$", re.IGNORECASE)

def _is_valid_version(version: str) -> bool:
    """
    Check if a string looks like a valid Python version.
    
    Filters out:
    - Pure hex strings (git commit hashes)
    - Placeholder strings like 'VERSION', 'dev', 'unknown'
    - Empty or whitespace-only strings
    """
    if not version or not version.strip():
        return False
    
    version = version.strip()
    
    # Must start with a digit
    if not version[0].isdigit():
        return False
    
    # Filter out pure hex strings (likely git hashes)
    if re.fullmatch(r"[0-9a-fA-F]+", version) and len(version) >= 7:
        return False
    
    # Check against valid version pattern
    return bool(_VALID_VERSION_RE.match(version))

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """A metadata candidate with confidence score and source."""
    value: str
    confidence: float
    source: str


@dataclass
class MetadataResult:
    """Collected metadata with all candidates for transparency."""
    name: Optional[str] = None
    version: Optional[str] = None
    name_source: Optional[str] = None
    version_source: Optional[str] = None
    name_candidates: List[Candidate] = field(default_factory=list)
    version_candidates: List[Candidate] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Optional[str]]:
        """Return simplified dict for backward compatibility."""
        sources = []
        if self.name_source:
            sources.append(f"name:{self.name_source}")
        if self.version_source:
            sources.append(f"version:{self.version_source}")
        return {
            "name": self.name,
            "version": self.version,
            "source": ", ".join(sources) if sources else None,
        }


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def extract_project_metadata(repo_path: str) -> Dict[str, Optional[str]]:
    """
    Discover project/library name and version using common packaging metadata.

    Uses a collect-then-score strategy:
      1. Discover actual Python packages in the repository
      2. Collect name/version candidates from all sources with confidence scores
      3. Select the highest-confidence candidate for each field

    Supported sources (in rough confidence order):
      - pyproject.toml (PEP 621, Poetry, Hatch, Flit, PDM)
      - setup.cfg
      - setup.py (regex heuristics)
      - Package __version__ / __about__.py / _version.py
      - Root VERSION/RELEASE files
      - Git describe tags

    Returns:
        Dict with keys: name, version, source
    """
    result = extract_project_metadata_detailed(repo_path)
    return result.to_dict()


# ---------------------------------------------------------------------------
# Python Package Root Detection
# ---------------------------------------------------------------------------

def find_python_package_roots(repo: Path, project_name: Optional[str] = None) -> List[Path]:
    """
    Find Python package roots using structural signals.
    
    Logic:
    1. If repo root has packaging files (setup.py, pyproject.toml) AND packages → repo is root
    2. If a subdirectory has packaging files → that subdir is a root
    3. If a subdirectory contains packages but IS NOT a package itself → it's a root
    4. If a subdirectory IS a package (has __init__.py) → repo root is the root
    
    Args:
        repo: Repository path
        project_name: Optional project name to help distinguish main package from source dirs
    
    Returns:
        List of Python package roots
    """
    logger.info(f"=== Finding Python package roots in {repo} ===")
    
    roots: List[Path] = []
    
    packaging_files = ["pyproject.toml", "setup.py", "setup.cfg"]
    skip_dirs = {
        "docs", "doc", "tests", "test", "testing",
        "examples", "example", "build", "dist",
        "buildconfig", "config", "configs",
        ".git", ".github", "node_modules", "__pycache__",
        "scripts", "tools", "utils", "ci", "assets", "data",
    }
    
    def has_packaging_files(path: Path) -> bool:
        return any((path / f).is_file() for f in packaging_files)
    
    def is_package(path: Path) -> bool:
        """A directory is a package if it has __init__.py"""
        return (path / "__init__.py").is_file()
    
    def contains_packages(path: Path) -> bool:
        """Check if directory contains Python packages (subdirs with __init__.py)"""
        try:
            for item in path.iterdir():
                if item.is_dir() and item.name.isidentifier() and not item.name.startswith((".", "_")):
                    if item.name.lower() not in skip_dirs and is_package(item):
                        return True
        except Exception:
            pass
        return False
    
    def contains_python_modules(path: Path) -> bool:
        """Check if directory contains Python module files"""
        try:
            py_files = [f for f in path.iterdir() 
                       if f.suffix == ".py" and f.name not in ("setup.py", "conftest.py")]
            return len(py_files) >= 2
        except Exception:
            return False
    
    # Check repo root first
    repo_has_packaging = has_packaging_files(repo)
    repo_has_packages = contains_packages(repo)
    
    # Scan subdirectories
    try:
        for item in sorted(repo.iterdir(), key=lambda p: p.name):
            if not item.is_dir():
                continue
            if item.name.startswith((".", "_")):
                continue
            if item.name.lower() in skip_dirs:
                continue
            
            # Note: If this directory IS a package, it's NOT a root.
            # The repo root (or its parent) is the root.
            if is_package(item):
                # If it matches the project name, it's the main package - skip
                if project_name and item.name == project_name:
                    logger.debug(f"'{item.name}' is the main package (matches project name) - repo root is the Python root")
                    continue
                
                # Otherwise, if it contains packages/modules, treat it as a source directory
                if contains_packages(item) or contains_python_modules(item):
                    roots.append(item)
                    logger.debug(f"'{item.name}' is a source directory with __init__.py - treating as root")
                    continue
                else:
                    # Pure package with no substructure, skip
                    logger.debug(f"'{item.name}' is a simple package - repo root is the Python root")
                    continue
            
            # Check if this subdirectory should be a Python root
            is_python_root = False
            
            # Signal 1: Has its own packaging files
            if has_packaging_files(item):
                is_python_root = True
                logger.debug(f"Detected Python root via packaging files: {item}")
            
            # Signal 2: Contains packages (but is not a package itself)
            elif contains_packages(item):
                is_python_root = True
                logger.debug(f"Detected Python root via contained packages: {item}")
            
            # Signal 3: Contains multiple Python modules (flat layout)
            elif contains_python_modules(item):
                is_python_root = True
                logger.debug(f"Detected Python root via Python modules: {item}")
            
            if is_python_root:
                roots.append(item)
                
    except Exception as e:
        logger.warning(f"Error during Python root detection: {e}")
    
    # If we found packages at repo root and no subdirectory roots, repo is the root
    if not roots and (repo_has_packages or repo_has_packaging):
        roots.append(repo)
        logger.debug(f"Using repo root as Python root (has packages or packaging files)")
    
    # Ultimate fallback
    if not roots:
        roots.append(repo)
        logger.debug(f"Using repo root as fallback")
    
    logger.info(f"Final roots: {roots}")
    return roots


def extract_project_metadata_detailed(repo_path: str) -> MetadataResult:
    """
    Detailed version that returns all candidates for inspection/debugging.
    """
    repo = Path(repo_path).resolve()
    result = MetadataResult()

    # Phase 0: Find Python package roots (handles multi-language repos)
    python_roots = find_python_package_roots(repo)
    
    # Phase 1: Discover package structure from ALL roots
    all_packages: List[Tuple[str, Path]] = []
    for root in python_roots:
        packages = _discover_packages(root)
        all_packages.extend(packages)
    
    # Deduplicate by package name, preferring subdirectory roots
    seen_names = set()
    unique_packages = []
    for pkg in all_packages:
        if pkg[0] not in seen_names:
            seen_names.add(pkg[0])
            unique_packages.append(pkg)
    
    # Phase 2: Collect candidates from all Python roots AND repo root
    metadata_roots = list(python_roots)
    if repo not in metadata_roots:
        metadata_roots.append(repo)  # Always check repo root for setup.py/pyproject.toml
    
    for root in metadata_roots:
        root_label = str(root.relative_to(repo)) if root != repo else "."
        _collect_from_pyproject(root, unique_packages, result, root_label)
        _collect_from_setup_cfg(root, result, root_label)
        _collect_from_setup_py(root, result, root_label)
    
    # Version from package files (using discovered packages)
    _collect_from_version_files(repo, unique_packages, result)
    _collect_from_root_version_files(repo, result)
    _collect_from_git_describe(repo, result)

    # Add package discovery as name candidate (if not already found)
    if unique_packages:
        primary_pkg = unique_packages[0][0]
        existing_names = {c.value.lower() for c in result.name_candidates}
        if primary_pkg.lower() not in existing_names:
            result.name_candidates.append(
                Candidate(primary_pkg, 0.5, "package_discovery")
            )

    # Fallback: repo directory name
    result.name_candidates.append(
        Candidate(repo.name, 0.1, "repo_directory")
    )

    # Phase 3: Select best candidates
    if result.name_candidates:
        result.name_candidates.sort(key=lambda c: -c.confidence)
        best = result.name_candidates[0]
        result.name = best.value
        result.name_source = best.source

    if result.version_candidates:
        result.version_candidates.sort(key=lambda c: -c.confidence)
        best = result.version_candidates[0]
        result.version = best.value
        result.version_source = best.source

    return result


# ---------------------------------------------------------------------------
# Package Discovery
# ---------------------------------------------------------------------------

def _discover_packages(repo: Path) -> List[Tuple[str, Path]]:
    """
    Find top-level Python packages in the repository.

    Searches flat layout, src layout, AND common Python subdirectories.
    Excludes test directories, docs, examples, and hidden/private directories.

    Returns:
        List of (package_name, path_to_init) tuples, sorted by likelihood.
    """
    logger.info(f"=== Discovering packages in {repo} ===")
    
    packages: List[Tuple[str, Path]] = []
    skip_names = {
        "test", "tests", "testing",
        "doc", "docs", "documentation",
        "example", "examples",
        "script", "scripts",
        "bin", "build", "dist",
        "venv", "env", ".venv", ".env",
        "node_modules", "__pycache__",
        "buildconfig", "config", "configs", "setup"
    }
    
    # Known Python subdirectory patterns (in priority order)
    python_subdirs = ["src", "src_py", "python-package", "python", "py", "lib"]
    
    # Build list of roots to search (in priority order)
    search_roots = []
    for subdir in python_subdirs:
        candidate = repo / subdir
        if candidate.is_dir():
            search_roots.append(candidate)
    search_roots.append(repo)  # Always include repo root last
    
    for root in search_roots:
        if not root.exists():
            continue

        for child in root.iterdir():
            if not child.is_dir():
                # Check for module files (flat layout like pygame.py)
                if child.suffix == ".py" and child.stem.isidentifier():
                    name = child.stem
                    if not name.startswith((".", "_")) and name.lower() not in skip_names:
                        packages.append((name, child))  # Use .py file as path
                continue

            name = child.name
            if not name.isidentifier():
                continue
            if name.startswith((".", "_")) or name.lower() in skip_names:
                continue

            init_file = child / "__init__.py"
            if init_file.is_file():
                packages.append((name, init_file))
                
    logger.info(f"Found packages before sort: {[(p[0], str(p[1])) for p in packages]}")

    # Sort: prioritize packages from Python subdirectories, then alphabetically
    def sort_key(item: Tuple[str, Path]) -> Tuple[int, str]:
        name, path = item
        path_str = str(path).lower()
        # Prioritize packages from known Python subdirectories
        for i, subdir in enumerate(python_subdirs):
            if f"/{subdir}/" in path_str or f"\\{subdir}\\" in path_str:
                return (i, name.lower())  # Earlier subdirs get higher priority
        return (len(python_subdirs), name.lower())  # Repo root is lowest priority

    packages.sort(key=sort_key)
    
    # Deduplicate by name, keeping highest priority (first occurrence)
    seen = set()
    unique = []
    for pkg in packages:
        if pkg[0] not in seen:
            seen.add(pkg[0])
            unique.append(pkg)
    
    logger.info(f"Final packages: {[(p[0], str(p[1])) for p in unique]}")
    
    return unique


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def _collect_from_pyproject(
    repo: Path, 
    packages: List[Tuple[str, Path]], 
    result: MetadataResult,
    root_label: str = ".",
) -> None:
    """Collect metadata from pyproject.toml (PEP 621 + build backends)."""
    file = repo / "pyproject.toml"
    if not file.is_file():
        return

    source_prefix = f"{root_label}/" if root_label != "." else ""
    
    try:
        data = tomllib.load(file.open("rb"))
    except Exception:
        return

    # PEP 621 [project] table
    project = data.get("project") or {}

    if name := _clean_name(project.get("name")):
        result.name_candidates.append(Candidate(name, 0.95, f"{source_prefix}pyproject.toml[project]"))

    if version := project.get("version"):
        if resolved := _resolve_version_token(repo, version):
            if _is_valid_version(resolved):  # Add validation
                result.version_candidates.append(
                    Candidate(resolved, 0.95, f"{source_prefix}pyproject.toml[project.version]")
                )

    # Dynamic version handling
    dynamic = project.get("dynamic", [])
    if "version" in dynamic:
        if version := _resolve_dynamic_version(repo, data, packages):
            result.version_candidates.append(
                Candidate(version, 0.90, "pyproject.toml[dynamic]")
            )

    # Poetry
    poetry = data.get("tool", {}).get("poetry", {})
    if name := _clean_name(poetry.get("name")):
        result.name_candidates.append(Candidate(name, 0.90, "pyproject.toml[tool.poetry]"))
    if version := poetry.get("version"):
        if resolved := _resolve_version_token(repo, version):
            result.version_candidates.append(
                Candidate(resolved, 0.90, "pyproject.toml[tool.poetry]")
            )


def _collect_from_setup_cfg(repo: Path, result: MetadataResult, root_label: str = ".") -> None:
    """Collect metadata from setup.cfg."""
    file = repo / "setup.cfg"
    if not file.is_file():
        return
    
    source_prefix = f"{root_label}/" if root_label != "." else ""
    
    parser = configparser.ConfigParser()
    try:
        parser.read(file, encoding="utf-8")
    except Exception:
        return

    if not parser.has_section("metadata"):
        return

    if raw_name := parser.get("metadata", "name", fallback=None):
        if name := _clean_name(raw_name):
            result.name_candidates.append(Candidate(name, 0.85, f"{source_prefix}setup.cfg"))

    if raw_version := parser.get("metadata", "version", fallback=None):
        if version := _resolve_version_token(repo, raw_version):
            result.version_candidates.append(Candidate(version, 0.85, f"{source_prefix}setup.cfg"))


def _collect_from_setup_py(repo: Path, result: MetadataResult, root_label: str = ".") -> None:
    """Collect metadata from setup.py using regex (low confidence)."""
    file = repo / "setup.py"
    if not file.is_file():
        logger.debug(f"No setup.py found at {repo}") 
        return
    
    source_prefix = f"{root_label}/" if root_label != "." else ""
    
    try:
        text = file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return

    logger.debug(f"Checking setup.py at {file}")
    
    # Try multiple patterns for name extraction
    name_patterns = [
        (_SETUP_NAME_CALL_RE, "setup() call"),      # Most reliable - inside setup()
        (_SETUP_NAME_ASSIGN_RE, "assignment"),       # name = "value"
        (_SETUP_NAME_DICT_RE, "dict literal"),       # "name": "value"
    ]
    
    for pattern, pattern_name in name_patterns:
        if match := pattern.search(text):
            raw_name = match.group(1)
            logger.debug(f"Found name via {pattern_name}: {raw_name}")
            if name := _clean_name(raw_name):
                result.name_candidates.append(Candidate(name, 0.6, f"{source_prefix}setup.py"))
                break
        else:
            logger.debug(f"No name match in setup.py for {pattern_name}")

    version_patterns = [
        (_SETUP_VERSION_RE, "assignment"),        # version = "2.6.1"
        (_SETUP_VERSION_DICT_RE, "dict literal"), # "version": "2.6.1"
    ]
    
    for pattern, pattern_name in version_patterns:
        if match := pattern.search(text):
            version = match.group(1).strip()
            logger.debug(f"Found version via {pattern_name}: {version}")
            if version and not version.startswith(("attr:", "file:")) and _is_valid_version(version):
                result.version_candidates.append(Candidate(version, 0.6, f"{source_prefix}setup.py"))
                break
        else:
            logger.debug(f"No version match in setup.py for {pattern_name}")


def _collect_from_version_files(repo: Path, packages: List[Tuple[str, Path]], result: MetadataResult) -> None:
    """
    Collect version from package __init__.py, __about__.py, _version.py, etc.
    Also looks for __title__ as a name candidate.
    """
    version_files = ["__about__.py", "_version.py", "version.py", "__version__.py", "__init__.py"]

    for pkg_name, init_path in packages:
        pkg_dir = init_path.parent

        for filename in version_files:
            target = pkg_dir / filename
            if not target.is_file():
                continue

            try:
                text = target.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Version
            for pattern in [_VERSION_ATTR_RE, _VER_ATTR_RE]:
                if match := pattern.search(text):
                    source = f"{pkg_name}/{filename}"
                    # __about__.py and _version.py are more intentional
                    if filename in ("__about__.py", "_version.py"):
                        confidence = 0.55  # Slightly below setup.py
                    else:
                        confidence = 0.50  # __init__.py is lowest
                    result.version_candidates.append(
                        Candidate(match.group(1), confidence, source)
                    )
                    break

            # Title as name (from __about__.py pattern)
            if match := _TITLE_ATTR_RE.search(text):
                result.name_candidates.append(
                    Candidate(match.group(1), 0.70, f"{pkg_name}/{filename}")
                )
            elif match := _PACKAGE_ATTR_RE.search(text):
                result.name_candidates.append(
                    Candidate(match.group(1), 0.65, f"{pkg_name}/{filename}")
                )


def _collect_from_root_version_files(repo: Path, result: MetadataResult) -> None:
    """Collect version from root-level VERSION, RELEASE files."""
    candidates = [
        "VERSION",
        "VERSION.txt",
        "version.txt",
        "RELEASE",
        "release.txt",
    ]

    for name in candidates:
        path = repo / name
        if not path.is_file():
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue

        if content:
            # Take first line only
            version = content.splitlines()[0].strip()
            if version:
                result.version_candidates.append(
                    Candidate(version, 0.50, name)
                )
                return  # Only use first found


def _collect_from_git_describe(repo: Path, result: MetadataResult) -> None:
    """Collect version from git tags using multiple strategies."""
    if not (repo / ".git").exists():
        logger.debug(f"No .git directory found at {repo}")
        return

    version = None
    
    # Strategy 1: git describe (works if HEAD is at or after a tag)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "describe", "--tags", "--abbrev=0"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        logger.debug(f"git describe returned: {proc.returncode}, stdout: '{proc.stdout.strip()}'")
        
        if proc.returncode == 0 and proc.stdout.strip():
            version = _normalize_git_tag(proc.stdout.strip())
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        pass
    
    # Strategy 2: Get the latest tag directly (works on shallow clones with --tags)
    if not version:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "tag", "-l", "--sort=-version:refname"],
                check=False, capture_output=True, text=True, timeout=5,
            )
            logger.debug(f"git tag -l returned: {proc.returncode}, first few tags: {proc.stdout.strip()[:200]}")
            
            if proc.returncode == 0:
                for line in proc.stdout.strip().splitlines():
                    tag = line.strip()
                    if tag:
                        normalized = _normalize_git_tag(tag)
                        if _is_valid_version(normalized):
                            version = normalized
                            break
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
            pass
    
    if version and _is_valid_version(version):
        logger.debug(f"Valid Git version detected: {version}")
        result.version_candidates.append(Candidate(version, 0.30, "git tag"))
    elif version:
        logger.debug(f"Invalid Git version detected: {version}")
    else:
        logger.debug(f"No Git version detected")


# ---------------------------------------------------------------------------
# Dynamic Version Resolution
# ---------------------------------------------------------------------------

def _resolve_dynamic_version(repo: Path, data: dict, packages: List[Tuple[str, Path]]) -> Optional[str]:
    """
    Resolve dynamic version from various build backends.
    """
    tool = data.get("tool", {})

    # Setuptools dynamic
    setuptools_dynamic = tool.get("setuptools", {}).get("dynamic", {})
    if isinstance(setuptools_dynamic, dict):
        version_cfg = setuptools_dynamic.get("version")
        if resolved := _resolve_version_token(repo, version_cfg):
            return resolved

    # Hatch
    hatch_version = tool.get("hatch", {}).get("version", {})
    if isinstance(hatch_version, dict):
        if path := hatch_version.get("path"):
            if resolved := _resolve_file_content(repo, path):
                return resolved
        if source := hatch_version.get("source"):
            if source == "vcs":
                return _git_describe_version(repo)

    # Flit - uses __version__ from the module
    if "flit" in tool or "flit_core" in tool:
        # Flit reads from the main module's __version__
        for pkg_name, init_path in packages:
            if version := _extract_version_attr(init_path):
                return version

    # PDM
    pdm_version = tool.get("pdm", {}).get("version", {})
    if isinstance(pdm_version, dict):
        source = pdm_version.get("source")
        if source == "scm":
            return _git_describe_version(repo)
        if source == "file":
            if path := pdm_version.get("path"):
                if resolved := _resolve_file_content(repo, path):
                    return resolved

    # setuptools-scm - typically writes to _version.py
    if "setuptools_scm" in tool:
        for pkg_name, init_path in packages:
            version_file = init_path.parent / "_version.py"
            if version := _extract_version_attr(version_file):
                return version

    # Final fallback: scan package __init__.py files
    for pkg_name, init_path in packages:
        if version := _extract_version_attr(init_path):
            return version

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_name(value: Optional[str]) -> Optional[str]:
    """Clean and validate a package name."""
    if not value:
        return None
    value = value.strip()
    # Skip attr: and file: indirection for names
    if value.lower().startswith(("attr:", "file:")):
        return None
    
    # Strip trailing dots (from matches like 'pygame.')
    value = value.rstrip('.')
    
    # Handle hyphens: pygame-ce -> pygame (or keep as-is depending on policy)
    # For now, take the first part before hyphen as the canonical name
    if '-' in value:
        value = value.split('-')[0]
    
    # Validate: must be a valid Python identifier (no dots for top-level name)
    # Or a dotted path where each part is an identifier
    if '.' in value:
        # Likely matched a subpackage; take just the first part
        parts = value.split('.')
        if parts[0].isidentifier():
            value = parts[0]
        else:
            return None
    elif not value.isidentifier():
        return None
    
    return value or None


def _resolve_version_token(repo: Path, token: Union[str, Sequence[str], dict, None]) -> Optional[str]:
    """Resolve version from various token formats."""
    if token is None:
        return None

    if isinstance(token, str):
        token = token.strip()
        if token.lower().startswith("attr:"):
            return _resolve_attr(repo, token.split(":", 1)[1].strip())
        if token.lower().startswith("file:"):
            return _resolve_file_content(repo, token.split(":", 1)[1].strip())
        return token or None

    if isinstance(token, Sequence) and not isinstance(token, str):
        for part in token:
            if resolved := _resolve_version_token(repo, part):
                return resolved

    if isinstance(token, dict):
        if "attr" in token:
            return _resolve_attr(repo, str(token["attr"]).strip())
        if "file" in token:
            return _resolve_file_content(repo, str(token["file"]).strip())

    return None


def _resolve_attr(repo: Path, dotted: str) -> Optional[str]:
    """Resolve attr:package.module.__version__ style references."""
    dotted = dotted.strip()
    if not dotted:
        return None

    # Handle setuptools-style "package:attr" format
    if ":" in dotted:
        dotted = dotted.split(":", 1)[1].strip()

    module_path, _, attr = dotted.rpartition(".")
    if not module_path or not attr:
        return None

    for file_path in _candidate_module_files(repo, module_path):
        if value := _search_attr_in_file(file_path, attr):
            return value

    return None


def _resolve_file_content(repo: Path, filename: str) -> Optional[str]:
    """Resolve file:path/to/file style references."""
    filename = filename.strip()
    if not filename:
        return None

    candidates = [
        repo / filename,
        repo / "src" / filename,
    ]

    for candidate in candidates:
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8", errors="ignore").strip()
                if content:
                    # First non-empty line
                    for line in content.splitlines():
                        line = line.strip()
                        if line:
                            # Try to extract __version__ if it's a .py file
                            if candidate.suffix == ".py":
                                if match := _VERSION_ATTR_RE.search(content):
                                    return match.group(1)
                            return line
            except Exception:
                continue

    return None


def _extract_version_attr(file_path: Path) -> Optional[str]:
    """Extract __version__ from a Python file."""
    if not file_path.is_file():
        return None
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if match := _VERSION_ATTR_RE.search(text):
            return match.group(1)
    except Exception:
        pass
    return None


def _search_attr_in_file(file_path: Path, attr: str) -> Optional[str]:
    """Search for a specific attribute assignment in a file."""
    pattern = re.compile(_ATTR_TEMPLATE.format(attr=re.escape(attr)))
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    if match := pattern.search(text):
        return match.group(1)
    return None


def _candidate_module_files(repo: Path, module_path: str) -> Sequence[Path]:
    """Generate candidate file paths for a dotted module path."""
    parts = module_path.split(".")
    candidates: List[Path] = []

    # Direct layouts
    pkg_init = repo.joinpath(*parts, "__init__.py")
    if pkg_init.is_file():
        candidates.append(pkg_init)

    if len(parts) > 1:
        module_py = repo.joinpath(*parts[:-1], f"{parts[-1]}.py")
        if module_py.is_file():
            candidates.append(module_py)

    # src/ layout
    src_pkg_init = repo / "src" / Path(*parts) / "__init__.py"
    if src_pkg_init.is_file():
        candidates.append(src_pkg_init)

    if len(parts) > 1:
        src_module_py = repo / "src" / Path(*parts[:-1]) / f"{parts[-1]}.py"
        if src_module_py.is_file():
            candidates.append(src_module_py)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _git_describe_version(repo: Path) -> Optional[str]:
    """Get version from git describe."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "describe", "--tags", "--abbrev=0"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return _normalize_git_tag(proc.stdout.strip())
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        pass
    return None


def _normalize_git_tag(tag: str) -> str:
    """Strip common prefixes from git tags to get version."""
    # Order matters - check longer prefixes first
    prefixes = [
        "release_", "release-", "release/",
        "version-", "version_", "version/",
        "ver-", "ver_", "ver/",
        "rel-", "rel_", "rel/",
        "v", "V",
    ]
    for prefix in prefixes:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return tag
