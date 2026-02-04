"""
Utilities for caching Intermediate Representation (IR) objects to disk.
Relies on `code_analysis.ir.serialization` for the actual object-to-format conversion.
"""

import os
import json # Still needed for config serialization in cache key
import hashlib
import logging
from pathlib import Path
from typing import Optional, Dict, Any

from .models import IRModule
from .serialization import (
    load_irmodule_from_json_file,
    save_irmodule_to_json_file,
    # Potentially import msgpack functions if we want to support them here later
    # load_irmodule_from_msgpack_file,
    # save_irmodule_to_msgpack_file
)
from .validation import fully_validate_irmodule # Optional: for validating on load
from code_analysis.config import AnalysisConfig

logger = logging.getLogger(__name__)

# --- Cache Configuration ---
# Could be expanded, e.g., to select serialization format
CACHE_SERIALIZATION_FORMAT = "json" # Or "msgpack"
CACHE_FILE_EXTENSION = f".ir.{CACHE_SERIALIZATION_FORMAT}"
VALIDATE_ON_LOAD = True # Configuration option to validate IR when loading from cache

# --- Cache Key Generation ---

def generate_cache_key(file_path: str, config: AnalysisConfig) -> str:
    """
    Generates a cache key based on file content and relevant config options.

    Args:
        file_path: Absolute path to the source file.
        config: The analysis configuration used.

    Returns:
        A unique SHA256 hash string representing the cache key.
    """
    hasher = hashlib.sha256()

    # 1. Include file content hash
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
    except OSError as e:
        logger.warning(f"Could not read file {file_path} for cache key generation: {e}")
        # Fallback: use file path and mtime if content read fails
        try:
            mtime = os.path.getmtime(file_path)
            hasher.update(file_path.encode())
            hasher.update(str(mtime).encode())
        except OSError:
             # Last resort: just use file path
             hasher.update(file_path.encode())


    # 2. Include relevant configuration options
    # Select options that affect AST parsing or IR conversion
    relevant_config_dict = {
        "include_special_members": config.include_special_members,
        "unwrap_decorators": config.unwrap_decorators,
        "decorator_max_depth": config.decorator_max_depth,
        # Add other config options if they influence IR generation
        # e.g., feature flags affecting IR structure
    }
    # Also include the chosen cache serialization format in the key if it can vary, to prevent trying to load a msgpack file as json.
    # relevant_config_dict["cache_serialization_format"] = CACHE_SERIALIZATION_FORMAT
    
    config_bytes = json.dumps(relevant_config_dict, sort_keys=True).encode()
    hasher.update(config_bytes)

    return hasher.hexdigest()

# --- Cache File Operations ---

def get_cache_file_path(cache_dir: str, cache_key: str) -> Path:
    """
    Constructs the full path for a cache file using the configured extension.
    """
    return Path(cache_dir) / f"{cache_key}{CACHE_FILE_EXTENSION}"

def read_from_cache(cache_file_path: Path) -> Optional[IRModule]:
    """
    Reads and deserializes an IRModule from a cache file using the configured format.
    Optionally validates the loaded IRModule.

    Args:
        cache_file_path: Path to the cache file.

    Returns:
        The deserialized IRModule, or None if reading, deserialization,
        or validation fails, or if the file is not found.
    """
    if not cache_file_path.exists():
        logger.debug(f"Cache file not found: {cache_file_path}")
        return None

    try:
        logger.debug(f"Attempting to read IR from cache: {cache_file_path} (Format: {CACHE_SERIALIZATION_FORMAT})")
        
        ir_module: Optional[IRModule] = None
        if CACHE_SERIALIZATION_FORMAT == "json":
            ir_module = load_irmodule_from_json_file(cache_file_path)
        # elif CACHE_SERIALIZATION_FORMAT == "msgpack":
        #     ir_module = load_irmodule_from_msgpack_file(cache_file_path)
        else:
            logger.error(f"Unsupported cache serialization format: {CACHE_SERIALIZATION_FORMAT}")
            return None

        if ir_module and VALIDATE_ON_LOAD:
            logger.debug(f"Performing validation for IRModule loaded from cache: {cache_file_path}")
            validation_issues = fully_validate_irmodule(ir_module, attempt_schema_revalidation=True)
            if validation_issues:
                logger.warning(
                    f"IRModule from cache {cache_file_path} failed validation with {len(validation_issues)} issues. Ignoring cache."
                )
                # Optionally remove or mark the corrupted cache file
                # try:
                #     os.remove(cache_file_path)
                #     logger.info(f"Removed corrupted cache file: {cache_file_path}")
                # except OSError as e_remove:
                #     logger.warning(f"Could not remove corrupted cache file {cache_file_path}: {e_remove}")
                return None
            logger.debug(f"Successfully validated IRModule from cache: {cache_file_path}")
        
        logger.info(f"Successfully loaded IR from cache: {cache_file_path}")
        return ir_module

    except FileNotFoundError: # Should be caught by the initial exists() check, but as a safeguard.
        logger.warning(f"Cache file disappeared before read: {cache_file_path}")
        return None
    except ValueError as ve: # Catches JSON/Msgpack decoding errors and Pydantic validation from serialization module
        logger.warning(f"Failed to deserialize or validate IR from cache file {cache_file_path}: {ve}. Ignoring cache.")
        return None
    except ImportError as ie: # If msgpack is chosen but not installed
        logger.error(f"ImportError while reading cache for {cache_file_path}: {ie}. Check dependencies.")
        return None
    except Exception as e:
        logger.error(f"Unexpected error loading IR from cache file {cache_file_path}: {e}", exc_info=True)
        return None


def write_to_cache(cache_file_path: Path, ir_module: IRModule) -> bool:
    """
    Serializes and writes an IRModule to a cache file using the configured format.

    Args:
        cache_file_path: Path to the cache file.
        ir_module: The IRModule object to cache.

    Returns:
        True if writing was successful, False otherwise.
    """
    try:
        logger.debug(f"Writing IR to cache: {cache_file_path} (Format: {CACHE_SERIALIZATION_FORMAT})")
        
        if CACHE_SERIALIZATION_FORMAT == "json":
            save_irmodule_to_json_file(ir_module, cache_file_path, indent=2)
        # elif CACHE_SERIALIZATION_FORMAT == "msgpack":
        #     save_irmodule_to_msgpack_file(ir_module, cache_file_path)
        else:
            logger.error(f"Unsupported cache serialization format: {CACHE_SERIALIZATION_FORMAT}")
            return False
            
        logger.info(f"Successfully wrote IR to cache: {cache_file_path}")
        return True
    except ImportError as ie: # If msgpack is chosen but not installed
        logger.error(f"ImportError while writing cache for {cache_file_path}: {ie}. Check dependencies.")
        return False
    except Exception as e: # Catches IOErrors from serialization module and other unexpected errors
        logger.error(f"Failed to write IR to cache file {cache_file_path}: {e}", exc_info=True)
        return False
