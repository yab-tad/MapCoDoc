"""
Utilities module providing error handling, logging, performance tracking,
and common operations.

Features:
1. Comprehensive error handling
2. Advanced logging configuration
3. Performance monitoring
4. File operations
5. Caching utilities
"""

import gc
import time
import logging
import json
import hashlib
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Union, List, Set, Tuple
from dataclasses import dataclass, field
import traceback
import subprocess
import sys
import tempfile
import contextlib
import psutil
from concurrent.futures import ThreadPoolExecutor
import platform

# Platform-specific imports
try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False


# Configure default logger
logger = logging.getLogger(__name__)



#----------------------------------------------------------------------------------------#
#                               Resource Management                                      #
#----------------------------------------------------------------------------------------#


class ControlledMemoryManager:
    """Memory manager that minimizes disruption to shared data structures."""
    
    def __init__(self, gc_threshold: float = 70.0, force_interval: int = 10):
        """
        Initialize memory manager.
        
        Args:
            gc_threshold: Memory percentage threshold to trigger GC
            force_interval: Force GC every N operations
        """
        self.gc_threshold = gc_threshold
        self.force_interval = force_interval
        self.operations_since_gc = 0
        
        try:
            import psutil
            self.process = psutil.Process()
            self.has_psutil = True
        except ImportError:
            self.has_psutil = False
    
    def collect_if_needed(self) -> bool:
        """
        Run garbage collection only if needed based on memory pressure.
        
        Returns:
            True if collection was performed
        """
        self.operations_since_gc += 1
        
        should_collect = False
        
        # Check memory pressure
        if self.has_psutil:
            try:
                memory_percent = self.process.memory_percent()
                if memory_percent > self.gc_threshold:
                    should_collect = True
                    logger.debug(f"Memory pressure high ({memory_percent:.1f}%), triggering GC")
            except Exception:
                pass
        
        # Also collect at forced intervals
        if self.operations_since_gc >= self.force_interval:
            should_collect = True
            logger.debug(f"Force GC after {self.force_interval} operations")
        
        if should_collect:
            gc.collect()
            self.operations_since_gc = 0
            return True
        
        return False


class AdaptiveMemoryManager:
    """Manages memory with adaptive batch sizing and resource monitoring."""
    
    def __init__(self, initial_batch_size=50, min_batch_size=10, max_batch_size=200):
        self.batch_size = initial_batch_size
        self.min_batch_size = min_batch_size
        self.max_batch_size = max_batch_size
        self.memory_threshold = 70.0  # 70% memory usage triggers adjustment
        
        # self.last_adjustment_time = time.time()
        # self.adjustment_interval = 10.0  # seconds between adjustments
        
        # Initialize memory monitoring
        try:
            import psutil
            self.process = psutil.Process()
            self.has_psutil = True
        except ImportError:
            self.has_psutil = False
    
    def adjust_batch_size(self):
        """Dynamically adjust batch size based on memory pressure."""
        
        if not self.has_psutil:
            return self.batch_size
        
        # now = time.time()
        # if now - self.last_adjustment_time < self.adjustment_interval:
        #     return self.batch_size
            
        # self.last_adjustment_time = now
        
        # if not self.has_psutil:
        #     return self.batch_size
            
        # Get current memory usage
        try:
            memory_percent = self.process.memory_percent()
            
            # Adjust batch size based on memory pressure
            if memory_percent > self.memory_threshold:
                # Reduce batch size when memory pressure is high
                new_size = max(self.min_batch_size, int(self.batch_size * 0.75))
                if new_size != self.batch_size:
                    logger.info(f"Memory pressure high ({memory_percent:.1f}%), reducing batch size: {self.batch_size} -> {new_size}")
                    self.batch_size = new_size
            elif memory_percent < self.memory_threshold * 0.7:  # Only increase if well below threshold
                # Increase batch size when memory pressure is low
                new_size = min(self.max_batch_size, int(self.batch_size * 1.25))
                if new_size != self.batch_size:
                    logger.info(f"Memory pressure low ({memory_percent:.1f}%), increasing batch size: {self.batch_size} -> {new_size}")
                    self.batch_size = new_size
        except Exception as e:
            logger.warning(f"Error adjusting batch size: {e}")
            
        return self.batch_size
    
    def create_batches(self, items: List[Any]) -> List[List[Any]]:
        """Create batches of items with adaptive sizing."""
        self.adjust_batch_size()
        return [items[i:i + self.batch_size] for i in range(0, len(items), self.batch_size)]
    
    def clear_memory(self):
        """Force garbage collection and memory cleanup."""
        gc.collect()



class ResourceManager:
    """
    Manages computational resources for analysis.
    
    This class tracks and limits memory usage and CPU utilization
    to prevent the system from running out of resources.
    """
    
    def __init__(self,
                 max_memory_percentage: float = 70.0,
                 max_cpu_percentage: Optional[float] = None,
                 check_interval: float = 1.0):
        """
        Initialize resource manager.
        
        Args:
            max_memory_percentage: Max memory usage as percentage of system memory
            max_cpu_percentage: Max CPU usage as percentage (optional)
            check_interval: Interval between checks in seconds
        """
        self.max_memory_percentage = max_memory_percentage
        self.max_cpu_percentage = max_cpu_percentage
        self.check_interval = check_interval
        
        try:
            import psutil
            self.process = psutil.Process()
            # Calculate max memory in bytes
            system_memory = psutil.virtual_memory().total
            self.max_memory_bytes = int(system_memory * max_memory_percentage / 100.0)
            self.has_psutil = True
        except ImportError:
            # Default to 1GB if psutil not available
            self.max_memory_bytes = 1000 * 1024 * 1024
            self.has_psutil = False
            logger.warning("psutil not available - resource monitoring will be limited")
        
        # Tracking
        self.current_memory_usage = 0
        self.peak_memory_usage = 0
        self.last_check_time = time.time()
        
        # Initialize
        self.update_usage()
        
        logger.info(f"Resource Manager initialized. "
                  f"Max memory: {self.max_memory_bytes / (1024*1024):.1f}MB "
                  f"({max_memory_percentage:.1f}% of system memory)")
    
    def update_usage(self):
        """Update current resource usage metrics."""
        if not self.has_psutil:
            return
            
        try:
            # Limit checking frequency
            now = time.time()
            if now - self.last_check_time < self.check_interval:
                return
                
            self.last_check_time = now
            
            # Get memory usage
            memory_info = self.process.memory_info()
            self.current_memory_usage = memory_info.rss
            self.peak_memory_usage = max(self.peak_memory_usage, self.current_memory_usage)
            
            # Get CPU usage if needed
            if self.max_cpu_percentage is not None:
                self.current_cpu_percentage = self.process.cpu_percent(interval=None)
                
        except Exception as e:
            logger.warning(f"Failed to update resource usage: {e}")
    
    def is_memory_critical(self) -> bool:
        """Check if memory usage is critical."""
        self.update_usage()
        
        if not self.has_psutil:
            # Without psutil, we can use Python's memory_info as a fallback
            # Force garbage collection first
            gc.collect()
            
            # Get size of objects tracked by GC
            total_size = sum(sys.getsizeof(obj) for obj in gc.get_objects())
            critical = total_size > self.max_memory_bytes
        else:
            critical = self.current_memory_usage > self.max_memory_bytes
            
        if critical:
            # Log warning with details
            logger.warning(
                f"Memory usage critical: {self.current_memory_usage / (1024*1024):.1f}MB "
                f"exceeds limit of {self.max_memory_bytes / (1024*1024):.1f}MB "
                f"({self.max_memory_percentage:.1f}% of system memory)"
            )
            
        return critical
    
    def is_cpu_critical(self) -> bool:
        """Check if CPU usage is critical."""
        if not self.has_psutil or self.max_cpu_percentage is None:
            return False
            
        self.update_usage()
        return self.current_cpu_percentage > self.max_cpu_percentage
    
    def clear_memory(self):
        """Attempt to clear memory."""
        gc.collect()
        
        # Log current usage after collection
        self.update_usage()
        logger.debug(
            f"Memory usage after gc.collect(): {self.current_memory_usage / (1024*1024):.1f}MB"
        )
        
    def get_status(self) -> Dict[str, Any]:
        """Get resource status."""
        self.update_usage()
        return {
            'current_memory_mb': self.current_memory_usage / (1024*1024),
            'peak_memory_mb': self.peak_memory_usage / (1024*1024),
            'max_memory_mb': self.max_memory_bytes / (1024*1024),
            'memory_percentage': (self.current_memory_usage / self.max_memory_bytes) * 100,
            'is_memory_critical': self.is_memory_critical(),
            'is_cpu_critical': self.is_cpu_critical() if self.max_cpu_percentage else False
        }


#----------------------------------------------------------------------------------------#
#                                 Error Management                                       #
#----------------------------------------------------------------------------------------#


@dataclass
class ErrorContext:
    """Context information for errors."""
    source: Optional[str] = None
    line_number: Optional[int] = None
    code_snippet: Optional[str] = None
    component_type: Optional[str] = None
    component_name: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


class AnalysisError(Exception):
    """
    Base class for analysis errors with context.
    
    Attributes:
        message: Error description
        context: Error context information
        details: Additional error details
        traceback: Stack trace if available
    """
    def __init__(self,
                 message: str,
                 context: Optional[ErrorContext] = None,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.context = context or ErrorContext()
        self.details = details or {}
        # self.traceback = traceback.extract_stack()
        
        # Store serializable traceback information
        tb_frames = []
        for frame in traceback.extract_stack():
            tb_frames.append({
                'filename': str(frame.filename),
                'lineno': frame.lineno,
                'name': str(frame.name),
                'line': str(frame.line) if hasattr(frame, 'line') else ''
            })
        self.traceback = tb_frames
        
    def __str__(self) -> str:
        """Format error message with context."""
        parts = [self.message]
        
        if self.context.source:
            parts.append(f"Source: {self.context.source}")
            
        if self.context.line_number:
            parts.append(f"Line: {self.context.line_number}")
            
        if self.context.component_name:
            parts.append(f"Component: {self.context.component_type} {self.context.component_name}")
            
        if self.details:
            parts.append(f"Details: {json.dumps(self.details, indent=2)}")
            
        return " | ".join(parts)
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        
        details = {}
        # Ensure all values in details are serializable
        if self.details:
            for k, v in self.details.items():
                try:
                    # Test JSON serialization
                    json.dumps({k: v})
                    details[k] = v
                except (TypeError, OverflowError):
                    # If value can't be serialized, convert to string
                    details[k] = str(v)
        
        return {
            'message': self.message,
            'type': self.__class__.__name__,
            'source': self.context.source,
            'line_number': self.context.line_number,
            'code_snippet': self.context.code_snippet,
            'component_type': self.context.component_type,
            'component_name': self.context.component_name,
            'details': self.details, #details,
            'traceback': self.traceback #tb_info
        }
 
 
class APIResolutionError(AnalysisError):
    """
    Error in API path resolution with context.
    
    This error provides detailed information about what went wrong during
    the API path resolution process, making it easier to diagnose issues
    with import chains and re-exports.
    
    Attributes:
        message: Error description
        resolution_stage: Stage in resolution process where error occurred
        file_path: The file path being resolved
        component_name: The component name being resolved
        package_name: Package name involved (if applicable)
        module_name: Module name involved (if applicable)
        resolution_chain: Path resolution chain at time of error
    """
    
    def __init__(self, 
                message: str,
                resolution_stage: str,
                context: Optional[ErrorContext] = None,
                file_path: Optional[str] = None,
                component_name: Optional[str] = None,
                package_name: Optional[str] = None,
                module_name: Optional[str] = None,
                resolution_chain: Optional[List[str]] = None):
        """
        Initialize API resolution error.
        
        Args:
            message: Error description
            resolution_stage: Stage in resolution process where error occurred
            context: Provides context with resolution-specific details
            file_path: The file path being resolved
            component_name: The component name being resolved
            package_name: Package name involved (if applicable)
            module_name: Module name involved (if applicable)
            resolution_chain: Path resolution chain at time of error
        """
        # Create context with resolution-specific details
        if context is None:
            context = ErrorContext(
                source=file_path,
                component_name=component_name,
                details={
                    "resolution_stage": resolution_stage,
                    "package_name": package_name,
                    "module_name": module_name,
                    "resolution_chain": resolution_chain or []
                }
            )
        
        super().__init__(message=message, context=context)
        
        # Store resolution-specific attributes
        self.message = message
        self.resolution_stage = resolution_stage
        self.file_path = file_path
        self.component_name = component_name
        self.package_name = package_name
        self.module_name = module_name
        self.resolution_chain = resolution_chain or []
    
    def __str__(self) -> str:
        """Format error message with API resolution context."""
        parts = [f"API Resolution Error in {self.resolution_stage}: {self.message}"]
        
        if self.file_path:
            parts.append(f"File Path: {self.file_path}")
        
        if self.component_name:
            parts.append(f"Component: {self.component_name}")
            
        if self.package_name:
            parts.append(f"Package: {self.package_name}")
            
        if self.module_name:
            parts.append(f"Module: {self.module_name}")
            
        if self.resolution_chain:
            parts.append(f"Resolution Chain: {' -> '.join(self.resolution_chain)}")
            
        return " | ".join(parts)
    
    @classmethod
    def module_export_error(cls, module_name: str, component_name: str, message: str) -> 'APIResolutionError':
        """Create error for module export issues."""
        return cls(
            message=message,
            resolution_stage="module_export_resolution",
            component_name=component_name,
            module_name=module_name
        )
    
    @classmethod
    def package_export_error(cls, package_name: str, component_name: str, message: str) -> 'APIResolutionError':
        """Create error for package export issues."""
        return cls(
            message=message,
            resolution_stage="package_export_resolution",
            component_name=component_name,
            package_name=package_name
        )
    
    @classmethod
    def path_normalization_error(cls, file_path: str, component_name: str, message: str) -> 'APIResolutionError':
        """Create error for path normalization issues."""
        return cls(
            message=message,
            resolution_stage="path_normalization",
            file_path=file_path,
            component_name=component_name
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            'message': self.message,
            'type': self.__class__.__name__,
            'resolution_stage': self.resolution_stage,
            'file_path': self.file_path,
            'component_name': self.component_name,
            'package_name': self.package_name,
            'module_name': self.module_name,
            'resolution_chain': self.resolution_chain
        }


class ModuleAnalysisError(AnalysisError):
    """Error analyzing a module's structure."""
    pass

class PackageAnalysisError(AnalysisError):
    """Error analyzing a package's structure."""
    pass

class DynamicAnalysisError(AnalysisError):
    """Error in dynamic analysis of a module."""
    pass

class ParseError(AnalysisError):
    """Error in parsing Python code."""
    def __init__(self, message: str, context: Optional[ErrorContext] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, context, details)

class ComponentError(AnalysisError):
    """Error in component processing."""
    def __init__(self, message: str, context: Optional[ErrorContext] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, context, details)

class ResourceError(AnalysisError):
    """Error in resource management."""
    def __init__(self, message: str, context: Optional[ErrorContext] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, context, details)

class DocumentationError(AnalysisError):
    """Base class for documentation related errors."""
    def __init__(self, message: str, context: Optional[ErrorContext] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, context, details)

class AllTrackingError(AnalysisError):
    """Error in __all__ analysis."""
    def __init__(self, message: str, source: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.source = source
        self.details = details or {}
        
    def __str__(self) -> str:
        parts = [self.message]
        if self.source:
            parts.append(f"Source: {self.source}")
        if self.details:
            parts.append("Details: " + ", ".join(f"{k}={v}" for k, v in self.details.items()))
        return " | ".join(parts)


@dataclass
class PerformanceMetrics:
    """Tracks performance metrics."""
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    peak_memory: float = 0.0
    cpu_percent: float = 0.0
    io_counters: Optional[Dict[str, int]] = None
    
    @property
    def duration(self) -> float:
        """Get operation duration in seconds."""
        if self.end_time:
            return self.end_time - self.start_time
        return time.time() - self.start_time
        
    def update(self):
        """Update current metrics."""
        process = psutil.Process()
        self.peak_memory = process.memory_info().rss / 1024 / 1024  # MB
        self.cpu_percent = process.cpu_percent()
        self.io_counters = process.io_counters()._asdict()
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'duration': self.duration,
            'peak_memory_mb': self.peak_memory,
            'cpu_percent': self.cpu_percent,
            'io_counters': self.io_counters
        }


class Timer:
    """
    Context manager for timing operations with performance tracking.
    
    Usage:
        with Timer("Operation name", logger) as timer:
            # Code to time
            timer.metrics.update()  # Optional manual updates
    """
    
    def __init__(self,
                 description: str = "",
                 logger: Optional[logging.Logger] = None,
                 track_performance: bool = True):
        self.description = description
        self.logger = logger or logging.getLogger(__name__)
        self.track_performance = track_performance
        self.metrics = PerformanceMetrics()
        
    def __enter__(self):
        """Start timing."""
        self.metrics.start_time = time.time()
        if self.track_performance:
            self.metrics.update()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """End timing and log results."""
        self.metrics.end_time = time.time()
        if self.track_performance:
            self.metrics.update()
            
        message = f"{self.description} took {self.metrics.duration:.2f} seconds"
        if self.track_performance:
            message += f" (Memory: {self.metrics.peak_memory:.1f}MB, CPU: {self.metrics.cpu_percent:.1f}%)"
            
        self.logger.debug(message)


# class ResourceMonitor:
#     """
#     Cross-platform resource monitoring and limiting.
    
#     Usage:
#         with ResourceMonitor(max_memory_mb=1000, max_cpu_percent=80):
#             # Resource-intensive code
#     """
    
#     def __init__(self,
#                  max_memory_mb: Optional[int] = None,
#                  max_cpu_percent: Optional[float] = None,
#                  check_interval: float = 1.0):
#         self.max_memory_mb = max_memory_mb
#         self.max_cpu_percent = max_cpu_percent
#         self.check_interval = check_interval
#         self.process = psutil.Process()
#         self._initial_memory = None
        
#     def __enter__(self):
#         """Start monitoring."""
#         self._initial_memory = self.process.memory_info().rss
        
#         if self.max_memory_mb and HAS_RESOURCE:
#             try:
#                 # On Unix systems, try to set hard memory limit
#                 resource.setrlimit(
#                     resource.RLIMIT_AS,
#                     (self.max_memory_mb * 1024 * 1024, resource.RLIM_INFINITY)
#                 )
#             except Exception as e:
#                 logger.warning(f"Failed to set memory limit: {e}")
        
#         return self
        
#     def __exit__(self, exc_type, exc_val, exc_tb):
#         """Stop monitoring."""
#         pass
        
#     def check_resources(self):
#         """Check current resource usage."""
#         try:
#             if self.max_memory_mb:
#                 memory_info = self.process.memory_info()
#                 current_memory_mb = memory_info.rss / (1024 * 1024)
#                 if current_memory_mb > self.max_memory_mb:
#                     raise ResourceError(
#                         message="Memory limit exceeded",
#                         context=ErrorContext(
#                             details={
#                                 "current_mb": current_memory_mb,
#                                 "limit_mb": self.max_memory_mb
#                             }
#                         )
#                     )
                    
#             if self.max_cpu_percent:
#                 cpu_percent = self.process.cpu_percent()
#                 if cpu_percent > self.max_cpu_percent:
#                     raise ResourceError(
#                         message="CPU usage too high",
#                         context=ErrorContext(
#                             details={
#                                 "cpu_percent": cpu_percent,
#                                 "limit_percent": self.max_cpu_percent
#                             }
#                         )
#                     )
                    
#         except psutil.Error as e:
#             logger.warning(f"Resource monitoring error: {e}")


#---------------------------------------------------------------------------------#
#                               PLATFORM INFORMATION                              #
#---------------------------------------------------------------------------------#

def get_platform_info() -> Dict[str, Any]:
    """Get platform-specific information."""
    return {
        'system': platform.system(),
        'python_version': platform.python_version(),
        'has_resource': HAS_RESOURCE,
        'machine': platform.machine(),
        'processor': platform.processor()
    }

#---------------------------------------------------------------------------------#
#                                     Caching                                     #
#---------------------------------------------------------------------------------#

class Cache:
    """
    Simple file-based cache with TTL support.
    
    Usage:
        cache = Cache(".cache")
        value = cache.get("key")
        cache.set("key", "value", ttl=3600)
    """
    
    def __init__(self, cache_dir: Union[str, Path], ttl: int = 3600):
        self.cache_dir = Path(cache_dir)
        self.ttl = ttl
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
    def _get_path(self, key: str) -> Path:
        """Get cache file path for key."""
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / f"{key_hash}.cache"
        
    def get(self, key: str) -> Optional[Any]:
        """Get cached value."""
        path = self._get_path(key)
        if not path.exists():
            return None
            
        try:
            data = json.loads(path.read_text())
            if time.time() - data['timestamp'] > self.ttl:
                path.unlink()
                return None
            return data['value']
        except Exception:
            return None
            
    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Set cache value."""
        path = self._get_path(key)
        data = {
            'timestamp': time.time(),
            'value': value
        }
        path.write_text(json.dumps(data))
        
    def clear(self):
        """Clear all cached values."""
        for path in self.cache_dir.glob("*.cache"):
            path.unlink()


def safe_read_file(path: Union[str, Path],
                   encoding: str = 'utf-8',
                   errors: str = 'strict') -> str:
    """
    Safely read a file with enhanced error handling.
    
    Args:
        path: Path to file
        encoding: File encoding
        errors: Error handling mode
        
    Returns:
        File contents
        
    Raises:
        ParseError: For encoding issues
        AnalysisError: For other read errors
    """
    path = Path(path)
    try:
        with open(path, 'r', encoding=encoding, errors=errors) as f:
            return f.read()
    except UnicodeDecodeError as e:
        raise ParseError(
            message="File encoding error",
            context=ErrorContext(
                source=str(path),
                details={"error": str(e)}
            )
        )
    except IOError as e:
        raise AnalysisError(
            message="File read error",
            context=ErrorContext(
                source=str(path),
                details={"error": str(e)}
            )
        )


def safe_write_file(path: Union[str, Path],
                    content: str,
                    encoding: str = 'utf-8',
                    errors: str = 'strict'):
    """Safely write a file with error handling."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding=encoding, errors=errors) as f:
            f.write(content)
    except Exception as e:
        raise AnalysisError(
            message="File write error",
            context=ErrorContext(
                source=str(path),
                details={"error": str(e)}
            )
        )


# @contextlib.contextmanager
# def temp_path(suffix: Optional[str] = None,
#              prefix: Optional[str] = None,
#              dir: Optional[Union[str, Path]] = None):
#     """
#     Context manager for temporary path handling.
    
#     Usage:
#         with temp_path(suffix='.py') as path:
#             # Use temporary path
#     """
#     path = Path(tempfile.mktemp(suffix=suffix, prefix=prefix, dir=dir))
#     try:
#         yield path
#     finally:
#         if path.exists():
#             path.unlink()


def format_error(error: Exception) -> Dict[str, Any]:
    """
    Format exception info for consistent reporting with robust traceback handling.
    
    Args:
        error: Exception to format
        
    Returns:
        Dictionary with error details in a JSON-serializable format
    """
    try:
        # Handle AnalysisError objects
        if isinstance(error, AnalysisError):
            error_dict = {}
            try:
                error_dict = {
                    'message': error.message,
                    'type': error.__class__.__name__,
                    'source': getattr(error.context, 'source', None),
                    'line_number': getattr(error.context, 'line_number', None),
                    'code_snippet': getattr(error.context, 'code_snippet', None),
                    'component_type': getattr(error.context, 'component_type', None),
                    'component_name': getattr(error.context, 'component_name', None),
                    'details': getattr(error, 'details', {}) or {},
                }
                
                # Extract traceback safely
                tb_info = []
                try:
                    # Extract frames from traceback safely
                    if hasattr(error, 'traceback'):
                        tb = error.traceback
                        if hasattr(tb, '__iter__'):  # It's a StackSummary
                            for frame in tb:
                                tb_info.append({
                                    'filename': str(getattr(frame, 'filename', '')),
                                    'lineno': getattr(frame, 'lineno', 0),
                                    'name': str(getattr(frame, 'name', '')),
                                    'line': str(getattr(frame, 'line', ''))
                                })
                        else:
                            frames = traceback.extract_tb(tb)
                            for frame in frames:
                                tb_info.append({
                                    'filename': str(frame.filename),
                                    'lineno': frame.lineno,
                                    'name': str(frame.name),
                                    'line': str(frame.line)
                                })
                except Exception as tb_error:
                    tb_info = [f"Error extracting traceback: {str(tb_error)}"]
                    
                error_dict['traceback'] = tb_info
                return error_dict
                
            except Exception as e:
                return {
                    'message': str(error),
                    'type': error.__class__.__name__,
                    'error_in_formatting': str(e)
                }
    
        # For standard exceptions
        tb_info = []
        try:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            if exc_traceback:
                frames = traceback.extract_tb(exc_traceback)
                for frame in frames:
                    tb_info.append({
                        'filename': str(frame.filename),
                        'lineno': frame.lineno,
                        'name': str(frame.name),
                        'line': str(getattr(frame, 'line', ''))
                    })
        except Exception as tb_error:
            tb_info = [f"Error extracting traceback: {str(tb_error)}"]
        
        return {
            'message': str(error),
            'type': error.__class__.__name__,
            'traceback': tb_info
        }
        
    except Exception as outer_error:
        # Absolute fallback - ensure we never crash during error formatting
        return {
            'message': str(error) if error else "Unknown error",
            'type': error.__class__.__name__ if error else "UnknownError",
            'error_in_error_formatting': str(outer_error)
        }

def configure_logging(level=logging.INFO, log_file: Optional[str] = None, include_thread_name: bool = False):
    """
    Configures basic logging for the application.
    Allows specifying a level and optionally a file to log to.

    Args:
        level: The logging level (e.g., logging.INFO, logging.DEBUG).
        log_file: Optional path to a file where logs should be written.
                  If None, logs go to stdout.
        include_thread_name: If True, includes thread name in log format.
    """
    handlers = []
    if log_file:
        try:
            # Ensure directory for log file exists
            import os
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8') # 'a' for append
            handlers.append(file_handler)
        except Exception as e:
            # Fallback to console if file handler fails
            print(f"Warning: Could not set up file logger at {log_file}: {e}", file=sys.stderr)
            console_handler = logging.StreamHandler(sys.stdout)
            handlers.append(console_handler)
    else:
        console_handler = logging.StreamHandler(sys.stdout)
        handlers.append(console_handler)

    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    if include_thread_name:
        log_format = '%(asctime)s - %(name)s:%(threadName)s - %(levelname)s - %(message)s'

    logging.basicConfig(
        level=level,
        format=log_format,
        handlers=handlers,
        force=True # Recommended for re-configuring root logger if called multiple times
    )
    logger = logging.getLogger("mapcodoc") # Get a root logger for the app
    logger.info(f"Logging configured. Level: {logging.getLevelName(level)}. Output: {'File (' + log_file + ')' if log_file else 'Console'}")


def get_logger(name: str = 'code_analysis') -> logging.Logger:
    """Get configured logger instance."""
    logger = logging.getLogger(name)
    if not logger.handlers and not logging.getLogger().handlers:
        raise RuntimeError("Logger not configured. Call configure_logging first.")
    return logger


def validate_python_path(path: Union[str, Path]) -> Path:
    """
    Validate and normalize Python file/directory path.
    
    Args:
        path: Path to validate
        
    Returns:
        Normalized Path object
        
    Raises:
        ValueError: If path is invalid
    """
    try:
        path = Path(path).resolve()
        if path.is_file() and path.suffix != '.py':
            raise ValueError("Not a Python file")
        if not path.exists():
            raise ValueError("Path does not exist")
        return path
    except Exception as e:
        raise ValueError(f"Invalid path: {e}")


class ThreadPoolManager:
    """
    Manages thread pools for parallel operations.
    
    Usage:
        with ThreadPoolManager(max_workers=4) as pool:
            futures = [pool.submit(func, *args) for args in arg_list]
    """
    
    def __init__(self, max_workers: int = None):
        self.max_workers = max_workers
        self.executor = None
        
    def __enter__(self):
        """Start thread pool."""
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        return self.executor
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Shut down thread pool."""
        if self.executor:
            self.executor.shutdown(wait=True)


class ProcessLimiter:
    """
    Limits subprocess creation and resource usage.
    
    Usage:
        with ProcessLimiter(max_processes=5, timeout=30):
            subprocess.run(...)
    """
    
    def __init__(self,
                 max_processes: int = 5,
                 timeout: int = 30,
                 kill_on_timeout: bool = True):
        self.max_processes = max_processes
        self.timeout = timeout
        self.kill_on_timeout = kill_on_timeout
        self.processes: Set[subprocess.Popen] = set()
        
    def __enter__(self):
        """Initialize process tracking."""
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up processes."""
        self.kill_all()
        
    def run(self, *args, **kwargs) -> subprocess.CompletedProcess:
        """Run subprocess with limits."""
        if len(self.processes) >= self.max_processes:
            raise ResourceError(
                f"Process limit exceeded: {len(self.processes)} >= {self.max_processes}"
            )
            
        kwargs['timeout'] = self.timeout
        process = subprocess.Popen(*args, **kwargs)
        self.processes.add(process)
        
        try:
            process.wait(timeout=self.timeout)
            return subprocess.CompletedProcess(
                args=process.args,
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr
            )
        except subprocess.TimeoutExpired:
            if self.kill_on_timeout:
                process.kill()
            raise
        finally:
            self.processes.remove(process)
            
    def kill_all(self):
        """Kill all tracked processes."""
        for process in self.processes:
            try:
                process.kill()
            except:
                pass
        self.processes.clear()


def run_with_timeout(func: callable,
                    args: tuple = (),
                    kwargs: dict = None,
                    timeout: int = 30) -> Any:
    """
    Run function with timeout.
    
    Args:
        func: Function to run
        args: Function arguments
        kwargs: Function keyword arguments
        timeout: Timeout in seconds
        
    Returns:
        Function result
        
    Raises:
        TimeoutError: If function exceeds timeout
    """
    kwargs = kwargs or {}
    
    def wrapper():
        return func(*args, **kwargs)
        
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wrapper)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            raise TimeoutError(f"Function {func.__name__} timed out after {timeout} seconds")


class MemoryCache:
    """
    Thread-safe in-memory cache with size limits.
    
    Usage:
        cache = MemoryCache(max_size=1000)
        value = cache.get(key)
        cache.set(key, value)
    """
    
    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        
    def get(self, key: str) -> Optional[Any]:
        """Get cached value."""
        with self._lock:
            return self._cache.get(key)
            
    def set(self, key: str, value: Any):
        """Set cache value."""
        with self._lock:
            if len(self._cache) >= self.max_size:
                # Simple LRU: remove random items
                remove_count = len(self._cache) // 4
                for _ in range(remove_count):
                    self._cache.popitem()
                    
            self._cache[key] = value
            
    def clear(self):
        """Clear cache."""
        with self._lock:
            self._cache.clear()


def get_component_property(component, property_name, default=None):
    """Safely get a property from a component regardless of its type."""
    try:
        if isinstance(component, dict):
            return component.get(property_name, default)
        else:
            return getattr(component, property_name, default)
    except Exception:
        return default

class AnalysisAborted(AnalysisError):
    """Raised to abort a long‑running analysis pass early (fatal but expected)."""
    pass