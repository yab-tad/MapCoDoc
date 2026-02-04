"""
Configuration module for code repository analysis.
Provides comprehensive configuration management with validation and overrides.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set, Dict, Any, Optional, List
from enum import Enum

from .feature_flags import is_enabled, Feature

logger = logging.getLogger(__name__)

class AnalysisMode(Enum):
    """Analysis mode configuration."""
    STATIC_ONLY = "static"
    DYNAMIC_PREFERRED = "dynamic"
    HYBRID = "hybrid"

class DocumentationStyle(Enum):
    """Documentation style preferences."""
    STANDARD = "standard"
    GOOGLE = "google"
    NUMPY = "numpy"
    SPHINX = "sphinx"

@dataclass
class AnalysisConfig:
    """
    Configuration for repository analysis.
    Includes comprehensive options and validation.
    """
    
    # --- Repository and File System ---
    repo_path: Optional[str] = None # Base repository path for the analysis
    exclude_patterns: List[str] = field(default_factory=lambda: [
    '__pycache__', '.ipynb_checkpoints', 'build', 'dist',
    '.git', '.tox', '.eggs', '-checkpoint'
    ])
    max_file_size: int = 1000000  # 500KB max file size
    # Patterns and files to exclude from analysis
    exclusion_file: Optional[str] = None
    
    #--- Basic analysis options ---
    include_special_members: bool = False
    special_whitelist: Set[str] = field(default_factory=lambda: {
        '__init__', '__new__', '__call__', '__getitem__', '__setitem__', 
        '__len__', '__iter__', '__enter__', '__exit__'
    })
    
    #--- Memory management ---
    max_memory_percentage: float = 70.0
    enable_memory_monitoring: bool = True
    memory_check_interval: float = 2.0 # seconds
    aggressive_memory_cleanup: bool = False
    
    # Analysis mode configuration
    analysis_mode: AnalysisMode = AnalysisMode.HYBRID
    max_analysis_depth: int = 5 # for recursive analysis like import following
    follow_imports: bool = True
    
    #--- Error handling and validation ---
    strict_mode: bool = False # If true, some warnings might become errors
    strict_name_checking: bool = True # For FQN validation
    validation_level: str = "warning"  # error, warning, ignore for various internal validations
        
    #--- Dynamic analysis options ---
    dynamic_all_check: bool = field(default_factory=lambda: is_enabled(Feature.DYNAMIC_ALL_EVALUATION)) #False  # Enable/disable all dynamic analysis
    dynamic_analysis_timeout: int = 30
    # max_dynamic_depth: int = 3
    # fallback_to_static: bool = True
    force_dynamic_check: bool = False # Force analysis even for non-dynamic module
    use_virtual_env: bool = True
    # install_dependencies: bool = True
    
    #--- Dependency handling for dynamic analysis ---
    auto_install_dependencies: bool = True
    dependency_installation_timeout: int = 300  # seconds
    dynamic_stub_external_imports: bool = False
    
    # Whether to dynamically introspect external base classes
    discover_external_methods: bool = True
    
    # --- Compiled extension handling for dynamic analysis ---
    auto_install_target_package: bool = True  # Auto-install target package if compiled extensions detected
    prefer_editable_install: bool = True  # Use 'pip install -e .' when possible
    target_package_install_timeout: int = 600  # seconds (10 min for large packages)
    
    # Runtime Member Discovery
    discover_runtime_members: bool = True  # Discover delegated/metaclass-injected methods
    discover_accessor_chains: bool = True  # Discover accessor pattern methods (Class.accessor.method)
    
    # --- User-provided project metadata (overrides auto-detection) ---
    project_name: Optional[str] = None  # Override auto-detected project/library name
    project_version: Optional[str] = None  # Override auto-detected project version
    pypi_package_name: Optional[str] = None  # PyPI package name if different from project_name (e.g., 'scikit-learn' for 'sklearn')
    
    #--- Decorator handling ---
    unwrap_decorators: bool = True
    decorator_max_depth: int = 3
    preserve_decorator_info: bool = True
    custom_decorators: Set[str] = field(default_factory=set)
    
    #--- Name resolution options ---
    prefer_dynamic_names: bool = True # If dynamic analysis provides a name, prefer it
    include_reexports: bool = True # In API maps / export lists
    track_import_chains: bool = True # For resolving complex re-exports
    resolve_circular_imports: bool = True # Attempt to break cycles during analysis
    
    #--- Documentation options ---
    url_patterns_file: Optional[str] = None # file containing URL segments (base URL + path)
    doc_url_file: Optional[str] = None # file containing crawled URLs for documentation
    doc_style: DocumentationStyle = DocumentationStyle.STANDARD
    verify_doc_urls: bool = True
    
    #--- Performance options ---
    use_caching: bool = True # General caching for various analysis stages
    cache_dir: Optional[str] = ".mapcodoc_cache" # Unified cache directory
    parallel_processing: bool = False # Enable/disable multiprocessing/threading
    max_workers: int = None # Number of workers for parallel processing (None for os.cpu_count())
    batch_size: int = 10  # Process files in batches of 10
    
    #--- Logging options ---
    logging_level: str = 'INFO'
    log_file: Optional[str] = None # Path to log file
    detailed_logging: bool = False
    log_performance: bool = True # Log timing for key operations

    # --- Intermediate Representation (IR) Options ---
    generate_ir: bool = False # Default to off
    ir_cache_dir: Optional[str] = None # Specific directory for caching IR files (can be same as cache_dir or sub-dir)
    validate_ir_after_conversion: bool = True # Validate IR after conversion from analysis results
    
    # --- Watch Mode Options ---
    enable_watch_mode: bool = field(default_factory=lambda: is_enabled(Feature.INCREMENTAL_WATCH_MODE)) #False # Default to off
    watch_debounce_delay: float = 0.5 # Debounce delay in seconds for file system events
   
    # auto_refresh: bool = True         # Deprecated / Automatically refresh when needed
    # force_refresh: bool = False       # Deprecated / Always perform a refresh
    # refresh_timeout: int = 60         # Deprecated / Timeout for refresh operations
   

    def __post_init__(self):
        """Validate configuration after initialization."""
        if self.repo_path: # Ensure repo_path is absolute if provided
            self.repo_path = str(Path(self.repo_path).resolve())
        
        self._validate_config()
        self._setup_paths()
        
        # Setup IR cache path if enabled
        if self.generate_ir:
            if not self.ir_cache_dir:
                # Default IR cache to a sub-directory of the main cache_dir if not specified
                self.ir_cache_dir = str(Path(self.cache_dir or ".mapcodoc_cache") / "ir_cache")
                logger.info(f"IR generation enabled. ir_cache_dir not specified, defaulted to: {Path(self.ir_cache_dir).resolve()}")
            
            ir_cache_path = Path(self.ir_cache_dir)
            try:
                ir_cache_path.mkdir(parents=True, exist_ok=True)
                logger.info(f"IR cache directory ensured at: {ir_cache_path.resolve()}")
            except OSError as e:
                logger.error(f"Could not create IR cache directory {ir_cache_path}: {e}")

        # auto-detect default exlusions file if not set
        if not self.exclusion_file:
            # try relative to repo root; fallback to package default
            repo_default = Path(self.repo_path or ".") / "code_analysis" / "exclusions.json"
            pkg_default = Path(__file__).parent / "exclusions.json"
            if repo_default.exists():
                self.exclusion_file = str(repo_default)
            elif pkg_default.exists():
                self.exclusion_file = str(pkg_default)
        
        if self.exclusion_file:
            self._load_exclusions()
        self._validate_watch_mode_options()
    
    def _load_exclusions(self):
        """Load exclusion patterns from file."""
        try:
            exclusion_path = Path(self.exclusion_file)
            if not exclusion_path.exists():
                logger.warning(f"Exclusion file not found: {self.exclusion_file}")
                return
                
            with open(exclusion_path, 'r') as f:
                exclusions = json.load(f)
            
            # Update exclusion patterns
            if 'exclude_patterns' in exclusions and isinstance(exclusions['exclude_patterns'], list):
                self.exclude_patterns.extend(exclusions['exclude_patterns'])
                logger.info(f"Loaded {len(exclusions['exclude_patterns'])} exclude patterns from {self.exclusion_file}")
            
            # Add file-specific exclusions
            if 'exclude_files' in exclusions and isinstance(exclusions['exclude_files'], list):
                if not hasattr(self, 'exclude_files'):
                    self.exclude_files = []
                self.exclude_files.extend(exclusions['exclude_files'])
                logger.info(f"Loaded {len(exclusions['exclude_files'])} exclude files from {self.exclusion_file}")
                
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in exclusion file: {self.exclusion_file}")
        except Exception as e:
            logger.error(f"Error loading exclusions: {e}")
        
    
    def _validate_config(self):
        """Configuration validation."""
        if self.repo_path and not Path(self.repo_path).exists():
            # This can be a warning or an error depending on when config is loaded vs. when path is needed
            logger.warning(f"Configured repo_path does not exist: {self.repo_path}")
            # raise ValueError(f"repo_path does not exist: {self.repo_path}")
        
        self._validate_analysis_options()
        self._validate_dynamic_options()
        self._validate_documentation_options()
        self._validate_dependency_options()
        self._validate_performance_options()
        
    def _validate_analysis_options(self):
        """Validate analysis-related options."""
        if self.max_analysis_depth <= 0:
            raise ValueError("max_analysis_depth must be positive")
            
        if self.strict_mode and not self.strict_name_checking:
            logger.warning("strict_mode enabled but strict_name_checking is disabled")
            
    def _validate_dynamic_options(self):
        """Validate dynamic analysis options."""
        if self.dynamic_analysis_timeout <= 0:
            raise ValueError("dynamic_analysis_timeout must be positive")
            
        if self.dynamic_all_check and self.analysis_mode == AnalysisMode.STATIC_ONLY:
            raise ValueError("dynamic_all_check cannot be used with STATIC_ONLY mode")
            
    def _validate_documentation_options(self):
        """Validate documentation-related options."""
        if self.url_patterns_file:
            if not Path(self.url_patterns_file).exists():
                raise ValueError(f"URL patterns file not found: {self.url_patterns_file}")
                
        if self.doc_url_file:
            if not Path(self.doc_url_file).exists():
                raise ValueError(f"Documentation URL file not found: {self.doc_url_file}")
                
    def _validate_performance_options(self):
        """Validate performance-related options."""
        if self.max_workers is not None and self.max_workers <= 0: # Allow None
            raise ValueError("max_workers must be positive if set")
            
        if self.use_caching and not self.cache_dir:
            self.cache_dir = ".mapcodoc_cache" # Default main cache directory
            logger.info(f"use_caching is True but cache_dir not set. Defaulting to {self.cache_dir}")
        
        if self.generate_ir and not self.ir_cache_dir: # Already handled in post_init, but good to have validation
            logger.warning("generate_ir is True but ir_cache_dir is not set. Will default in post_init.")
            
    def _validate_dependency_options(self):
        """Validate dependency-related options."""
        if self.dependency_installation_timeout <= 0:
            raise ValueError("dependency_installation_timeout must be positive")
            
    def _validate_watch_mode_options(self):
        """Validate watch mode options."""
        if self.watch_debounce_delay < 0:
            raise ValueError("watch_debounce_delay cannot be negative")

    def _setup_paths(self):
        """Set up necessary paths and directories."""
        if self.cache_dir:
            try:
                Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.error(f"Could not create main cache directory {self.cache_dir}: {e}")
            
        if self.ir_cache_dir : # This will be set if generate_ir is true from __post_init__
            try:
                Path(self.ir_cache_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.error(f"Could not create IR cache directory {self.ir_cache_dir}: {e}")

        if self.log_file:
            try:
                Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.error(f"Could not create parent directory for log file {self.log_file}: {e}")

    @classmethod
    def from_file(cls, config_file: str) -> 'AnalysisConfig':
        """
        Create configuration from JSON file with validation.
        
        Args:
            config_file: Path to JSON configuration file
            
        Returns:
            AnalysisConfig instance
            
        Raises:
            ValueError: If config file is invalid
        """
        try:
            with open(config_file, 'r') as f:
                config_data = json.load(f)
            
            # Convert enums
            if 'analysis_mode' in config_data and isinstance(config_data['analysis_mode'], str):
                config_data['analysis_mode'] = AnalysisMode(config_data['analysis_mode'])
            if 'doc_style' in config_data and isinstance(config_data['doc_style'], str):
                config_data['doc_style'] = DocumentationStyle(config_data['doc_style'])
                
            # Convert sets
            if 'special_whitelist' in config_data:
                config_data['special_whitelist'] = set(config_data['special_whitelist'])
            if 'custom_decorators' in config_data:
                config_data['custom_decorators'] = set(config_data['custom_decorators'])
            
            return cls(**config_data)
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file '{config_file}': {e}")
            raise ValueError(f"Invalid JSON in config file: {e}") from e
        except TypeError as e: # Often happens if a field is missing and has no default, or wrong type for dataclass field
            logger.error(f"Invalid configuration format in '{config_file}' (TypeError): {e}. Check field names and types.")
            raise ValueError(f"Invalid configuration format: {e}") from e
        except Exception as e: # Catch other potential errors like file not found (though resolve should handle it)
            logger.error(f"Error loading configuration from '{config_file}': {e}")
            raise # Re-raise other exceptions
            
    def update_from_dict(self, config_dict: Dict[str, Any]) -> None:
        """
        Update configuration from dictionary.
        
        Args:
            config_dict: Dictionary of configuration updates
        """
        for key, value in config_dict.items():
            if hasattr(self, key):
                setattr(self, key, value)
                
        self._validate_config()
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        config_dict = {
            'repo_path': self.repo_path,
            'project_name': self.project_name,
            'project_version': self.project_version,
            'pypi_package_name': self.pypi_package_name,
            'validate_ir_after_conversion': self.validate_ir_after_conversion,
            'include_special_members': self.include_special_members,
            'special_whitelist': list(self.special_whitelist),
            'analysis_mode': self.analysis_mode.value,
            'max_analysis_depth': self.max_analysis_depth,
            'follow_imports': self.follow_imports,
            'strict_mode': self.strict_mode,
            'strict_name_checking': self.strict_name_checking,
            'validation_level': self.validation_level,
            'dynamic_all_check': self.dynamic_all_check,
            'dynamic_analysis_timeout': self.dynamic_analysis_timeout,
            'force_dynamic_check': self.force_dynamic_check,
            'use_virtual_env': self.use_virtual_env, 
            # 'install_dependencies': self.install_dependencies, 
            'auto_install_dependencies': self.auto_install_dependencies, 
            'dependency_installation_timeout': self.dependency_installation_timeout,
            'dynamic_stub_external_imports':self.dynamic_stub_external_imports, 
            'auto_install_target_package': self.auto_install_target_package,
            'prefer_editable_install': self.prefer_editable_install,
            'target_package_install_timeout': self.target_package_install_timeout,
            'discover_runtime_members': self.discover_runtime_members,
            'discover_accessor_chains': self.discover_accessor_chains,
            'exclude_patterns': self.exclude_patterns, 
            'max_file_size': self.max_file_size, 
            'batch_size': self.batch_size, 
            'max_memory_percentage': self.max_memory_percentage, 
            'enable_memory_monitoring': self.enable_memory_monitoring, 
            'memory_check_interval': self.memory_check_interval, 
            'aggressive_memory_cleanup': self.aggressive_memory_cleanup, 
            'unwrap_decorators': self.unwrap_decorators,
            'decorator_max_depth': self.decorator_max_depth,
            'preserve_decorator_info': self.preserve_decorator_info,
            'custom_decorators': list(self.custom_decorators),
            'prefer_dynamic_names': self.prefer_dynamic_names,
            'include_reexports': self.include_reexports,
            'track_import_chains': self.track_import_chains,
            'resolve_circular_imports': self.resolve_circular_imports,
            'url_patterns_file': self.url_patterns_file,
            'doc_url_file': self.doc_url_file,
            'doc_style': self.doc_style.value,
            'verify_doc_urls': self.verify_doc_urls,
            'use_caching': self.use_caching,
            'cache_dir': self.cache_dir,
            'parallel_processing': self.parallel_processing,
            'max_workers': self.max_workers,
            'logging_level': self.logging_level,
            'log_file': self.log_file,
            'detailed_logging': self.detailed_logging,
            'log_performance': self.log_performance,
            'exclude_patterns': self.exclude_patterns,
            'exclusion_file': self.exclusion_file,
            'exclude_files': getattr(self, 'exclude_files', []),
            'generate_ir': self.generate_ir,
            'ir_cache_dir': self.ir_cache_dir,
            'enable_watch_mode': self.enable_watch_mode,
            'watch_debounce_delay': self.watch_debounce_delay,
        }
        return config_dict

    def save(self, config_file: str) -> None:
        """
        Save configuration to JSON file.
        
        Args:
            config_file: Path to save configuration
        """
        config_data = self.to_dict()
        with open(config_file, 'w') as f:
            json.dump(config_data, f, indent=4)
            
    def get_cache_key(self, data: Any) -> str:
        """
        Generate cache key for data.
        
        Args:
            data: Data to generate key for
            
        Returns:
            Cache key string
        """
        import hashlib
        import pickle
        
        # Create deterministic string representation
        try:
            data_bytes = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
            return hashlib.sha256(data_bytes).hexdigest()
        except Exception as e:
            logger.warning(f"Failed to generate cache key: {e}")
            return ""
            
    def get_cache_path(self, key: str) -> Optional[Path]:
        """
        Get cache file path for key.
        
        Args:
            key: Cache key
            
        Returns:
            Path to cache file or None if caching disabled
        """
        if not self.use_caching or not self.cache_dir:
            return None
            
        return Path(self.cache_dir) / f"{key}.cache"

# Default configuration instance
default_config = AnalysisConfig()