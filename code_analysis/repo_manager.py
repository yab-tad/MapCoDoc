"""
Repository Manager for MapCoDoc.

Handles validation and preparation of repository paths, including:
- Detecting if input is a local path or remote URL
- Cloning remote repositories to temporary directories
- Cleanup of temporary clones after analysis
"""

import os
import re
import shutil
import tempfile
import logging
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

class RepoManager:
    """
    Manages repository preparation (local validation or remote cloning).
    """
    
    def __init__(self, cache_dir: Optional[str] = None):
        """
        Initialize the repository manager.
        
        Args:
            cache_dir: Optional directory to clone repositories (instead of temp).
                       Useful for persistent caching across runs.
        """
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.temp_dirs = []  # Track temp dirs for cleanup
        
    def prepare_repository(self, repo_input: str) -> Tuple[str, bool]:
        """
        Prepares a repository for analysis.
        
        If the input is a local path, validates and returns it.
        If the input is a URL, clones to a temporary directory.
        
        Args:
            repo_input: Local path or remote URL.
            
        Returns:
            Tuple of (local_path, is_temp) where is_temp indicates if cleanup is needed.
        """
        if self._is_url(repo_input):
            logger.info(f"Detected remote URL: {repo_input}")
            return self._clone_repository(repo_input), True
        else:
            logger.info(f"Detected local path: {repo_input}")
            if not os.path.exists(repo_input):
                raise ValueError(f"Local repository path does not exist: {repo_input}")
            return os.path.abspath(repo_input), False
    
    def _is_url(self, path: str) -> bool:
        """Check if the input string is a URL."""
        try:
            result = urlparse(path)
            # Must have scheme (http/https) and netloc (domain)
            return all([result.scheme in ('http', 'https', 'git'), result.netloc])
        except Exception:
            return False
    
    def _parse_versioned_url(self, url: str) -> Tuple[str, Optional[str]]:
        """
        Parse a repository URL and extract optional version/tag/branch.
        
        Supported formats:
        - https://github.com/user/repo.git@v1.2.3  (version suffix)
        - https://github.com/user/repo@v1.2.3     (version suffix, no .git)
        - https://github.com/user/repo/tree/v1.2.3 (GitHub tree URL)
        - https://github.com/user/repo.git        (no version - uses default branch)
        
        Args:
            url: Repository URL, optionally with version suffix.
            
        Returns:
            Tuple of (clean_clone_url, version_or_branch_or_None)
        """
        # Format: repo.git@tag or repo@tag
        if '@' in url:
            # Split on last @ to handle URLs that might have @ in username
            at_idx = url.rfind('@')
            base_url = url[:at_idx]
            ref = url[at_idx + 1:]
            
            # Ensure base URL ends with .git for cloning
            if not base_url.endswith('.git'):
                base_url += '.git'
            
            logger.info(f"Parsed versioned URL: {base_url} @ {ref}")
            return base_url, ref
        
        # Format: /tree/tag or /tree/branch (GitHub web URL)
        tree_match = re.match(r'(https://github\.com/[^/]+/[^/]+)/tree/(.+)', url)
        if tree_match:
            repo_path = tree_match.group(1)
            ref = tree_match.group(2)
            clone_url = f"{repo_path}.git"
            logger.info(f"Parsed GitHub tree URL: {clone_url} @ {ref}")
            return clone_url, ref
        
        # Format: archive/refs/tags/tag.zip (GitHub archive download URL)
        archive_match = re.match(
            r'(https://github\.com/[^/]+/[^/]+)/archive/refs/tags/([^/]+)\.(zip|tar\.gz)', 
            url
        )
        if archive_match:
            repo_path = archive_match.group(1)
            ref = archive_match.group(2)
            clone_url = f"{repo_path}.git"
            logger.info(f"Parsed GitHub archive URL: {clone_url} @ {ref}")
            return clone_url, ref
        
        # Plain URL - no version specified
        return url, None
    
    def _clone_repository(self, url: str) -> str:
        """
        Clone a remote repository to a local directory.
        
        Supports versioned URLs:
        - https://github.com/user/repo.git@v1.2.3
        - https://github.com/user/repo/tree/v1.2.3
        
        Args:
            url: Git repository URL, optionally with version suffix.
            
        Returns:
            Local path to the cloned repository.
        """
        try:
            import git
        except ImportError:
            raise ImportError(
                "GitPython is required for cloning remote repositories. "
                "Install it with: pip install gitpython"
            )
        
        # Parse URL to extract optional version/tag
        clone_url, ref = self._parse_versioned_url(url)
        
        # Determine clone location (use very short path on Windows to avoid length issues)
        import hashlib, platform
        # Include ref in hash so different versions get different directories
        hash_input = f"{clone_url}@{ref}" if ref else clone_url
        url_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
        
        if self.cache_dir:
            clone_dir = self.cache_dir / f"r_{url_hash}"
            clone_dir.mkdir(parents=True, exist_ok=True)
            target_path = str(clone_dir)
            
            if (clone_dir / ".git").exists():
                logger.info(f"Repository already cloned at {target_path}.")
                # If a specific ref was requested, ensure we're on it
                if ref:
                    try:
                        repo = git.Repo(target_path)
                        repo.git.checkout(ref)
                        logger.info(f"Checked out {ref}")
                    except Exception as e:
                        logger.warning(f"Checkout of {ref} failed: {e}. Using existing state.")
                return target_path
        else:
            # Use a VERY short path on Windows to avoid 260-char limit
            if platform.system() == "Windows":
                # Try C:\Temp\mc\ first (much shorter than AppData\Local\Temp)
                short_base = Path("C:/Temp/mc")
                try:
                    short_base.mkdir(parents=True, exist_ok=True)
                    target_path = str(short_base / f"{url_hash}")
                except (PermissionError, OSError):
                    # Fallback to temp if C:\Temp\mc isn't writable
                    target_path = tempfile.mkdtemp(prefix="mc_")
            else:
                target_path = tempfile.mkdtemp(prefix="mapcodoc_repo_")
            
            self.temp_dirs.append(target_path)
        
        # Build clone options
        clone_options = ['--config core.longpaths=true']
        if ref:
            # Clone specific branch/tag
            clone_options.append(f'--branch {ref}')
            logger.info(f"Cloning {clone_url} (ref: {ref}) to {target_path}...")
        else:
            clone_options.append('--tags')
            logger.info(f"Cloning {clone_url} to {target_path}...")
        
        try:
            repo = git.Repo.clone_from(
                clone_url, 
                target_path, 
                depth=1,
                multi_options=clone_options,
                allow_unsafe_options=True
            )
            
            # Fetch tags if no specific ref (for version detection)
            if not ref:
                try:
                    repo.git.fetch('--tags', '--force')
                    logger.debug("Fetched tags successfully")
                except Exception as e:
                    logger.debug(f"Could not fetch tags: {e}")
            
            # # Initialize submodules (important for PyTorch, TensorFlow, etc.)
            # try:
            #     logger.info("Initializing submodules...")
            #     repo.git.submodule('update', '--init', '--recursive')
            #     logger.debug("Submodules initialized successfully")
            # except Exception as e:
            #     logger.debug(f"Submodule initialization skipped or failed: {e}")
            
            logger.info(f"Clone successful: {target_path}" + (f" (ref: {ref})" if ref else ""))
            return target_path
            
        except git.GitCommandError as e:
            # Check if it's a long-path error
            if "Filename too long" in str(e):
                logger.error(
                    f"Git clone failed due to Windows path length limits.\n"
                    f"To fix this permanently, run as Administrator:\n"
                    f"  New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' "
                    f"-Name 'LongPathsEnabled' -Value 1 -PropertyType DWORD -Force\n"
                    f"Then restart your terminal."
                )
            raise RuntimeError(f"Git clone failed for {clone_url}: {e}")
        except Exception as e:
            logger.error(f"Failed to clone repository: {e}")
            raise RuntimeError(f"Git clone failed for {clone_url}: {e}")
    
    def cleanup(self):
        """Remove all temporary clone directories."""
        import time
        import stat
        
        def handle_remove_readonly(func, path, exc):
            """
            Error handler for Windows readonly file issues.
            Clears the readonly bit and retries deletion.
            """
            if not os.access(path, os.W_OK):
                # Make file writable
                os.chmod(path, stat.S_IWRITE)
                func(path)
            else:
                raise
        
        for temp_dir in self.temp_dirs:
            if not os.path.exists(temp_dir):
                continue
                
            logger.info(f"Cleaning up temporary clone: {temp_dir}")
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    shutil.rmtree(temp_dir, onerror=handle_remove_readonly)
                    logger.info(f"Successfully removed: {temp_dir}")
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Cleanup attempt {attempt + 1} failed: {e}. Retrying in 1s...")
                        time.sleep(1)
                    else:
                        logger.warning(f"Failed to remove temp dir {temp_dir} after {max_retries} attempts: {e}")
                        logger.info(f"The directory will be left for OS cleanup: {temp_dir}")
        
        self.temp_dirs.clear()
        
    def clear_cache(self):
        """Remove all cached repositories (persistent clones in cache_dir)."""
        if not self.cache_dir or not self.cache_dir.exists():
            logger.info("No cache directory to clear.")
            return
        
        logger.info(f"Clearing cache directory: {self.cache_dir}")
        try:
            shutil.rmtree(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Cache cleared successfully.")
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
