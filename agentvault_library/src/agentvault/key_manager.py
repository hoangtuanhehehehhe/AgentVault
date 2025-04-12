"""
Manages loading and retrieving API keys securely from various sources.

Priority Order for Loading Keys (highest to lowest):
1. Key File (.env or .json)
2. Environment Variables
3. OS Keyring (if enabled and key explicitly requested via get_key)
"""

import os
import json
import logging
import pathlib
from typing import Dict, Optional, Union, Any

# Import dotenv for .env file support
from dotenv import dotenv_values

# Import custom exceptions
from .exceptions import KeyManagementError

# Set up logging
logger = logging.getLogger(__name__)

# Optional keyring import handling
try:
    import keyring
    _keyring_installed = True
except ImportError:
    keyring = None # type: ignore
    _keyring_installed = False


class KeyManager:
    """
    Handles loading and accessing API keys from environment variables,
    files (.env or .json), and optionally the OS keyring.
    """

    def __init__(
        self,
        key_file_path: Optional[Union[str, pathlib.Path]] = None,
        use_env_vars: bool = True,
        use_keyring: bool = False,
        env_prefix: str = "AGENTVAULT_KEY_"
    ):
        """
        Initializes the KeyManager.

        Args:
            key_file_path: Optional path to a key file (.env or .json).
            use_env_vars: Whether to load keys from environment variables.
            use_keyring: Whether to attempt loading/saving keys from the OS keyring.
                         Requires the 'keyring' package to be installed.
            env_prefix: The prefix for environment variables holding keys
                        (e.g., AGENTVAULT_KEY_OPENAI).
        """
        self.key_file_path: Optional[pathlib.Path] = None
        if key_file_path:
            self.key_file_path = pathlib.Path(key_file_path).resolve()

        self.use_env_vars = use_env_vars
        self.use_keyring = use_keyring and _keyring_installed # Only use if installed
        self.env_prefix = env_prefix

        self._keys: Dict[str, str] = {}
        self._key_sources: Dict[str, str] = {} # Stores source like 'env', 'file', 'keyring'

        if use_keyring and not _keyring_installed:
            logger.warning(
                "Keyring usage requested, but 'keyring' package is not installed. "
                "Install with 'pip install agentvault[os_keyring]' or 'poetry install --extras os_keyring'."
            )

        # Load keys on initialization based on priority
        self._load_keys()

    def _load_keys(self) -> None:
        """Loads keys from configured sources in priority order."""
        # Priority: File > Env. Keyring is loaded on demand by get_key.
        if self.key_file_path:
            self._load_from_file()
        if self.use_env_vars:
            self._load_from_env()
        # Keyring is loaded lazily in get_key

    def _load_from_file(self) -> None:
        """
        Loads keys from the specified key file (.env or .json).

        Keys loaded from the file have the highest priority and will overwrite
        any keys previously loaded from environment variables.
        """
        if not self.key_file_path:
            logger.debug("No key file path specified, skipping file loading.")
            return

        logger.debug(f"Attempting to load keys from file: {self.key_file_path}")

        if not self.key_file_path.exists():
            logger.warning(f"Key file specified but not found: {self.key_file_path}")
            return
        if not self.key_file_path.is_file():
            logger.warning(f"Key file path specified but is not a file: {self.key_file_path}")
            return

        file_suffix = self.key_file_path.suffix.lower()
        loaded_count = 0

        try:
            if file_suffix == ".env":
                logger.debug("Processing key file as .env format.")
                # Use stream=None to prevent warnings if file doesn't exist (already checked)
                env_values = dotenv_values(self.key_file_path, stream=None)
                for key, value in env_values.items():
                    # --- FIX: Check if value is not None AND not empty string ---
                    if value:
                        normalized_id = key.lower()
                        self._keys[normalized_id] = value
                        self._key_sources[normalized_id] = 'file'
                        logger.info(f"Loaded key for service '{normalized_id}' from file.")
                        loaded_count += 1
                    else:
                         logger.warning(f"Skipping empty value for key '{key}' in file '{self.key_file_path}'.")

            elif file_suffix == ".json":
                logger.debug("Processing key file as .json format.")
                raw_content = self.key_file_path.read_text(encoding='utf-8')
                data = json.loads(raw_content)

                if not isinstance(data, dict):
                    logger.error(f"Invalid format in JSON key file '{self.key_file_path}': Root element must be an object (dictionary).")
                    return

                for key, value in data.items():
                    normalized_id = key.lower()
                    if isinstance(value, str):
                        # --- FIX: Check if value is not empty string ---
                        if value:
                            self._keys[normalized_id] = value
                            self._key_sources[normalized_id] = 'file'
                            logger.info(f"Loaded key for service '{normalized_id}' from file.")
                            loaded_count += 1
                        else:
                            logger.warning(f"Skipping empty string value for key '{key}' in JSON file '{self.key_file_path}'.")
                    else:
                        logger.warning(f"Skipping non-string value for key '{key}' in JSON file '{self.key_file_path}'. Value type: {type(value)}")

            else:
                logger.warning(f"Unsupported key file extension '{file_suffix}' for file: {self.key_file_path}. Only '.env' and '.json' are supported.")
                return

            logger.debug(f"Finished loading from file. Loaded {loaded_count} keys.")

        except IOError as e:
            logger.error(f"Error reading key file '{self.key_file_path}': {e}", exc_info=True)
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from key file '{self.key_file_path}': {e}", exc_info=True)
        except Exception as e:
            # Catch potential errors from dotenv or other issues
            logger.error(f"An unexpected error occurred loading key file '{self.key_file_path}': {e}", exc_info=True)


    def _load_from_env(self) -> None:
        """
        Loads keys from environment variables based on the prefix.

        Keys loaded from environment variables will only be added if a key
        for the same service ID hasn't already been loaded from a file.
        """
        logger.debug(f"Attempting to load keys from environment variables with prefix '{self.env_prefix}'...")
        prefix_len = len(self.env_prefix)
        loaded_count = 0
        for env_var, value in os.environ.items():
            if env_var.startswith(self.env_prefix):
                service_id_part = env_var[prefix_len:]
                if not service_id_part:
                    logger.warning(f"Skipping environment variable '{env_var}' with empty service ID part.")
                    continue

                normalized_id = service_id_part.lower()

                if normalized_id not in self._keys:
                    if value:
                        self._keys[normalized_id] = value
                        self._key_sources[normalized_id] = 'env'
                        logger.info(f"Loaded key for service '{normalized_id}' from environment variable.")
                        loaded_count += 1
                    else:
                        logger.warning(f"Environment variable '{env_var}' found but has an empty value. Skipping.")
                else:
                    logger.debug(f"Key for service '{normalized_id}' already loaded from '{self._key_sources.get(normalized_id)}'. Skipping environment variable.")

        logger.debug(f"Finished loading from environment variables. Loaded {loaded_count} new keys.")


    def _load_from_keyring(self, service_id: str) -> Optional[str]:
        """
        Attempts to load a specific key from the OS keyring.
        Internal method called by get_key if keyring is enabled and the key
        was not found in file or environment variables.
        """
        if not self.use_keyring:
            logger.debug("Keyring usage is disabled, skipping keyring load.")
            return None
        if not _keyring_installed or keyring is None:
            logger.debug("Keyring package not available, skipping keyring load.")
            return None

        normalized_id = service_id.lower()
        keyring_service_name = f"agentvault:{normalized_id}"

        try:
            logger.debug(f"Attempting to load key for service '{normalized_id}' from keyring (service name: '{keyring_service_name}').")
            key_value = keyring.get_password(keyring_service_name, normalized_id)

            if key_value is not None:
                logger.info(f"Loaded key for service '{normalized_id}' from OS keyring.")
                return key_value
            else:
                logger.debug(f"Key for service '{normalized_id}' not found in OS keyring.")
                return None
        except Exception as e:
            logger.error(f"Failed to get key for service '{normalized_id}' from keyring: {e}", exc_info=True)
            return None


    def get_key(self, service_id: str) -> Optional[str]:
        """
        Retrieves a key for the given service ID.

        Checks loaded keys first (File > Env). If not found and keyring is
        enabled, attempts to load from the OS keyring and caches the result.

        Args:
            service_id: The identifier for the service key (case-insensitive).

        Returns:
            The API key string, or None if not found.
        """
        normalized_id = service_id.lower()

        # 1. Check cache (File > Env)
        if normalized_id in self._keys:
            source = self._key_sources.get(normalized_id, 'cache')
            logger.debug(f"Returning cached key for '{normalized_id}' (loaded from {source}).")
            return self._keys[normalized_id]

        # 2. Try loading from keyring if enabled
        if self.use_keyring:
            logger.debug(f"Key for '{normalized_id}' not in cache, attempting keyring load.")
            key_value = self._load_from_keyring(normalized_id)
            if key_value is not None:
                # Cache the result from keyring
                self._keys[normalized_id] = key_value
                self._key_sources[normalized_id] = 'keyring'
                return key_value

        # 3. Not found anywhere
        logger.debug(f"Key for service '{normalized_id}' not found in any configured source.")
        return None

    def get_key_source(self, service_id: str) -> Optional[str]:
        """
        Returns the source from which the key for the given service ID was loaded.

        Note: This only returns the source if the key has been accessed via `get_key`
              at least once (to potentially load from keyring and update source).

        Args:
            service_id: The identifier for the service key (case-insensitive).

        Returns:
            A string indicating the source ('file', 'env', 'keyring'),
            or None if the key was not found or its source isn't tracked.
        """
        normalized_id = service_id.lower()
        # Check the source map directly. get_key updates this map after keyring load.
        return self._key_sources.get(normalized_id)

    def set_key_in_keyring(self, service_id: str, key_value: str) -> None:
        """
        Stores or updates a key in the OS keyring.

        Requires the 'keyring' package to be installed and `use_keyring`
        to be True during KeyManager initialization.

        Args:
            service_id: The identifier for the service key.
            key_value: The API key string to store.

        Raises:
            KeyManagementError: If keyring is not enabled or not installed,
                                or if storing the key fails.
        """
        if not self.use_keyring:
            raise KeyManagementError("Keyring support is not enabled for this KeyManager instance.")
        if not _keyring_installed or keyring is None:
             raise KeyManagementError("The 'keyring' package is not installed. Cannot set key.")

        normalized_id = service_id.lower()
        keyring_service_name = f"agentvault:{normalized_id}"

        try:
            logger.info(f"Setting key for service '{normalized_id}' in OS keyring under service name '{keyring_service_name}'.")
            keyring.set_password(keyring_service_name, normalized_id, key_value)
        except Exception as e:
            logger.error(f"Failed to set key for service '{normalized_id}' in keyring: {e}", exc_info=True)
            raise KeyManagementError(f"Failed to set key in keyring for service '{normalized_id}': {e}") from e

#
