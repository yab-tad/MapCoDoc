"""
Serialization and deserialization utilities for Intermediate Representation (IR) models.

Supports JSON and MessagePack (msgpack) formats.

Note: msgpack is an optional dependency. To use msgpack functions,
ensure the 'msgpack' library is installed (`pip install msgpack`).
"""
import json
import logging
from pathlib import Path
from typing import Optional, Union

# Attempt to import msgpack, but don't fail if it's not installed.
# Functions using it will raise an ImportError if called without msgpack.
try:
    import msgpack
except ImportError:
    msgpack = None # type: ignore

from pydantic import ValidationError

from .models import IRModule # Assuming IRModule is the top-level model to (de)serialize

logger = logging.getLogger(__name__)

# --- JSON Serialization ---

def irmodule_to_json_string(ir_module: IRModule, indent: Optional[int] = 2) -> str:
    """
    Serializes an IRModule instance to a JSON string.

    Args:
        ir_module: The IRModule instance to serialize.
        indent: Indentation level for pretty-printing. None for compact output.

    Returns:
        A JSON string representation of the IRModule.
    """
    try:
        # Pydantic's .model_dump_json() is efficient and handles complex types like datetime
        return ir_module.model_dump_json(indent=indent)
    except Exception as e:
        logger.error(f"Failed to serialize IRModule to JSON string: {e}", exc_info=True)
        raise


def irmodule_from_json_string(json_string: str) -> IRModule:
    """
    Deserializes an IRModule instance from a JSON string.

    Args:
        json_string: The JSON string to deserialize.

    Returns:
        An IRModule instance.

    Raises:
        ValueError: If JSON decoding or Pydantic validation fails.
    """
    try:
        # Pydantic's .model_validate_json() handles parsing and validation
        return IRModule.model_validate_json(json_string)
    except ValidationError as ve:
        logger.error(f"Validation error deserializing IRModule from JSON string: {ve}")
        raise ValueError(f"JSON data failed validation for IRModule: {ve}") from ve
    except json.JSONDecodeError as jde:
        logger.error(f"Invalid JSON format when deserializing IRModule: {jde}")
        raise ValueError(f"Invalid JSON format: {jde}") from jde
    except Exception as e:
        logger.error(f"Failed to deserialize IRModule from JSON string: {e}", exc_info=True)
        raise ValueError(f"An unexpected error occurred during JSON deserialization: {e}") from e


def save_irmodule_to_json_file(
    ir_module: IRModule,
    file_path: Union[str, Path],
    indent: Optional[int] = 2,
    encoding: str = 'utf-8'
) -> None:
    """
    Serializes an IRModule instance and saves it to a JSON file.

    Args:
        ir_module: The IRModule instance.
        file_path: Path to the output JSON file.
        indent: Indentation for the JSON file.
        encoding: File encoding.
    """
    json_string = irmodule_to_json_string(ir_module, indent=indent)
    try:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding=encoding) as f:
            f.write(json_string)
        logger.debug(f"Successfully saved IRModule to JSON file: {file_path}")
    except IOError as e:
        logger.error(f"Failed to write IRModule to JSON file {file_path}: {e}", exc_info=True)
        raise

def load_irmodule_from_json_file(
    file_path: Union[str, Path],
    encoding: str = 'utf-8'
) -> IRModule:
    """
    Loads and deserializes an IRModule instance from a JSON file.

    Args:
        file_path: Path to the input JSON file.
        encoding: File encoding.

    Returns:
        An IRModule instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If JSON decoding or Pydantic validation fails.
    """
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            json_string = f.read()
        logger.debug(f"Successfully read from JSON file: {file_path}")
        return irmodule_from_json_string(json_string)
    except FileNotFoundError:
        logger.error(f"JSON file not found: {file_path}")
        raise
    except IOError as e: # Catches other I/O errors during read
        logger.error(f"Failed to read IRModule from JSON file {file_path}: {e}", exc_info=True)
        raise ValueError(f"Could not read file {file_path}: {e}") from e


# --- MessagePack (msgpack) Serialization ---

def _check_msgpack_availability() -> None:
    """Checks if msgpack is imported and raises ImportError if not."""
    if msgpack is None:
        error_msg = "The 'msgpack' library is not installed. Please install it to use msgpack serialization features (e.g., 'pip install msgpack')."
        logger.error(error_msg)
        raise ImportError(error_msg)

def irmodule_to_msgpack_bytes(ir_module: IRModule) -> bytes:
    """
    Serializes an IRModule instance to msgpack bytes.

    Args:
        ir_module: The IRModule instance to serialize.

    Returns:
        Msgpack bytes representation of the IRModule.

    Raises:
        ImportError: If msgpack library is not installed.
    """
    _check_msgpack_availability()
    try:
        # Pydantic models have .model_dump() to get a dict, which msgpack can handle.
        # We need a custom default handler for datetime objects if not using pydantic_core's C-optimized msgpack.
        # For simplicity with standard msgpack, convert datetime to ISO format string.
        data = ir_module.model_dump(mode='json') # 'json' mode ensures datetime is ISO string
        return msgpack.packb(data, use_bin_type=True)
    except Exception as e:
        logger.error(f"Failed to serialize IRModule to msgpack bytes: {e}", exc_info=True)
        raise

def irmodule_from_msgpack_bytes(msgpack_bytes: bytes) -> IRModule:
    """
    Deserializes an IRModule instance from msgpack bytes.

    Args:
        msgpack_bytes: The msgpack bytes to deserialize.

    Returns:
        An IRModule instance.

    Raises:
        ImportError: If msgpack library is not installed.
        ValueError: If msgpack decoding or Pydantic validation fails.
    """
    _check_msgpack_availability()
    try:
        data = msgpack.unpackb(msgpack_bytes, raw=False)
        # Pydantic's .model_validate() can take a dict
        return IRModule.model_validate(data)
    except ValidationError as ve:
        logger.error(f"Validation error deserializing IRModule from msgpack: {ve}")
        raise ValueError(f"Msgpack data failed validation for IRModule: {ve}") from ve
    except msgpack.ExtraData as ed:
        logger.error(f"Extra data in msgpack stream: {ed}")
        raise ValueError(f"Msgpack stream contains extra data: {ed}") from ed
    except msgpack.UnpackException as ue: # Catches various msgpack errors like UnpackValueError, FormatError
        logger.error(f"Invalid msgpack format when deserializing IRModule: {ue}")
        raise ValueError(f"Invalid msgpack format: {ue}") from ue
    except Exception as e:
        logger.error(f"Failed to deserialize IRModule from msgpack bytes: {e}", exc_info=True)
        raise ValueError(f"An unexpected error occurred during msgpack deserialization: {e}") from e


def save_irmodule_to_msgpack_file(
    ir_module: IRModule,
    file_path: Union[str, Path]
) -> None:
    """
    Serializes an IRModule instance and saves it to a msgpack file.

    Args:
        ir_module: The IRModule instance.
        file_path: Path to the output msgpack file.

    Raises:
        ImportError: If msgpack library is not installed.
    """
    _check_msgpack_availability()
    msgpack_bytes = irmodule_to_msgpack_bytes(ir_module)
    try:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'wb') as f:
            f.write(msgpack_bytes)
        logger.debug(f"Successfully saved IRModule to msgpack file: {file_path}")
    except IOError as e:
        logger.error(f"Failed to write IRModule to msgpack file {file_path}: {e}", exc_info=True)
        raise

def load_irmodule_from_msgpack_file(
    file_path: Union[str, Path]
) -> IRModule:
    """
    Loads and deserializes an IRModule instance from a msgpack file.

    Args:
        file_path: Path to the input msgpack file.

    Returns:
        An IRModule instance.

    Raises:
        ImportError: If msgpack library is not installed.
        FileNotFoundError: If the file does not exist.
        ValueError: If msgpack decoding or Pydantic validation fails.
    """
    _check_msgpack_availability()
    try:
        with open(file_path, 'rb') as f:
            msgpack_bytes = f.read()
        logger.debug(f"Successfully read from msgpack file: {file_path}")
        return irmodule_from_msgpack_bytes(msgpack_bytes)
    except FileNotFoundError:
        logger.error(f"Msgpack file not found: {file_path}")
        raise
    except IOError as e: # Catches other I/O errors during read
        logger.error(f"Failed to read IRModule from msgpack file {file_path}: {e}", exc_info=True)
        raise ValueError(f"Could not read file {file_path}: {e}") from e
