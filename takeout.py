#!/usr/bin/env python3
"""
Google Takeout Bulk Downloader
==============================
Downloads Google Takeout archives. Simple and robust.

Usage:
    python takeout.py                    # Launch the TUI (only interface)

Captures come from the browser extension ("Copy as JSON") or a pasted cURL
command, entered directly in the TUI.

Features:
    - Parallel downloads (configurable 1-20)
    - Auto-retry on failure with exponential backoff (up to MAX_RETRIES)
    - Track file sizes to detect incomplete downloads
    - Resume from last good file
    - ZIP integrity verification (end-of-central-directory check)
    - Path validation (output directory restricted to allowed prefixes)
    - aria2c integration support
    - Thread-safe size history with locking
"""

import os
import re
import sys
import json
import time
import secrets
import threading
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Callable
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load .env file if present (for standalone use; Docker uses environment vars directly)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

VERSION = "6.0.0"
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
DEFAULT_PARALLEL = 1
MAX_PARALLEL = 20
DEFAULT_FILE_COUNT = 100
DEFAULT_OUTPUT_DIR = "./downloads"
SIZE_HISTORY_FILE = ".takeout_sizes.json"
MAX_FILE_COUNT = 1000
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0

# Allowed directories for output (resolved paths)
_ALLOWED_DIRS: Optional[Tuple[Path, ...]] = None

def _get_allowed_dirs() -> Tuple[Path, ...]:
    """Lazily compute allowed directory prefixes (resolved, absolute)."""
    global _ALLOWED_DIRS
    if _ALLOWED_DIRS is None:
        _ALLOWED_DIRS = (
            Path.cwd().resolve(),
            Path.home().resolve(),
            Path("/opt/").resolve(),
            Path("/downloads/").resolve(),
            Path("/tmp/").resolve(),
        )
    return _ALLOWED_DIRS


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class DownloadStats:
    """Simple download statistics."""
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    bytes_downloaded: int = 0
    start_time: Optional[datetime] = None


# =============================================================================
# SIZE HISTORY - Track known file sizes for detecting incomplete downloads
# =============================================================================

class SizeHistory:
    """Track known file sizes to detect incomplete downloads."""

    def __init__(self, output_dir: str):
        self.path = Path(output_dir) / SIZE_HISTORY_FILE
        self.sizes: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self):
        """Load size history from file."""
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self.sizes = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.sizes = {}

    def save(self):
        """Save size history to file."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'w') as f:
                json.dump(self.sizes, f, indent=2)

    def get_expected_size(self, filename: str) -> Optional[int]:
        """Get expected size for a file, if known."""
        return self.sizes.get(filename)

    def record_size(self, filename: str, size: int):
        """Record a successful download size."""
        self.sizes[filename] = size
        self.save()


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def validate_output_dir(output_dir: str) -> Path:
    """Validate that output_dir is within allowed directories.

    Allowed directories: current working directory (resolved),
    user home directory, /opt/, /downloads/, /tmp/.

    Returns the resolved Path if valid.
    Raises ValueError if the path escapes allowed directories.
    """
    resolved = Path(output_dir).resolve()
    allowed = _get_allowed_dirs()
    for prefix in allowed:
        try:
            resolved.relative_to(prefix)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"Output directory '{resolved}' is outside allowed directories: "
        f"{', '.join(str(p) for p in allowed)}"
    )


def generate_secret_key() -> str:
    """Generate a 32-byte hex string suitable for Flask secret_key fallback."""
    return secrets.token_hex(32)


def extract_url_parts(url: str) -> Tuple[Optional[str], Optional[int], Optional[str], str]:
    """Extract URL parts for Google Takeout pattern.

    Pattern: takeout-TIMESTAMP-BATCH-FILENUM.zip
    Example: takeout-20251207T071725Z-3-003.zip

    Returns: (base_url_with_batch, file_num, extension, query_string)
    """
    if '?' in url:
        url_path, query_string = url.split('?', 1)
    else:
        url_path, query_string = url, ''

    # Match pattern: takeout-TIMESTAMP-BATCH-FILENUM.zip
    # Example: takeout-20251207T071725Z-3-003.zip
    #          base = everything up to and including "3-"
    #          file_num = 003
    match = re.search(r'(.*takeout-\d{8}T\d{6}Z-\d+-)(\d{3})(\.\w+)$', url_path)
    if not match:
        # Try pattern with timestamp but no batch number: takeout-TIMESTAMP-FILENUM.zip
        # Example: takeout-20260613T071725Z-001.zip
        match = re.search(r'(.*takeout-\d{8}T\d{6}Z-)(\d{3})(\.\w+)$', url_path)
        if not match:
            # Try alternate pattern without timestamp
            match = re.search(r'(.*takeout-[^-]+-\d+-)(\d{3})(\.\w+)$', url_path)
            if not match:
                return None, None, None, ''

    base = match.group(1)
    file_num = int(match.group(2))
    ext = match.group(3)

    return base, file_num, ext, query_string


def extract_cookies_from_powershell(ps_text: str) -> str:
    """Extract cookies from PowerShell Invoke-WebRequest format.

    Parses lines like:
    $session.Cookies.Add((New-Object System.Net.Cookie("NAME", "VALUE", "/", "domain")))

    Returns a cookie string in the format: NAME=VALUE; NAME2=VALUE2
    """
    cookies = []
    # Match: New-Object System.Net.Cookie("NAME", "VALUE", ...)
    # The cookie name and value are the first two string arguments
    pattern = r'New-Object\s+System\.Net\.Cookie\s*\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']*)["\']'

    for match in re.finditer(pattern, ps_text, re.IGNORECASE):
        name = match.group(1)
        value = match.group(2)
        cookies.append(f"{name}={value}")

    return "; ".join(cookies)


def extract_url_from_powershell(ps_text: str) -> Optional[str]:
    """Extract the download URL from a PowerShell Invoke-WebRequest command.

    Looks for: Invoke-WebRequest ... -Uri "URL"
    """
    # Match -Uri "URL" or -Uri 'URL'
    match = re.search(r'-Uri\s+["\']?(https?://[^"\'\'\s`]+)["\']?', ps_text, re.IGNORECASE)
    if match:
        url = match.group(1)
        if 'takeout' in url.lower():
            return url
    return None


def is_powershell_format(text: str) -> bool:
    """Check if the input appears to be PowerShell format."""
    ps_indicators = [
        'Invoke-WebRequest',
        'New-Object System.Net.Cookie',
        '$session',
        'WebRequestSession',
        '-WebSession',
    ]
    return any(indicator in text for indicator in ps_indicators)


def extract_cookie_from_curl(curl_text: str) -> str:
    """Extract cookie value from a cURL command, PowerShell command, or raw cookie string."""
    # Check if this is PowerShell format
    if is_powershell_format(curl_text):
        return extract_cookies_from_powershell(curl_text)

    # Try to find Cookie header in cURL command
    match = re.search(r"-H\s*['\"]Cookie:\s*([^'\"]+)['\"]", curl_text, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Handle "Cookie: value" format
    if curl_text.lower().startswith('cookie:'):
        return curl_text[7:].strip()

    # Just return as-is (might be raw cookie)
    cookie = curl_text.strip()
    if (cookie.startswith("'") and cookie.endswith("'")) or \
       (cookie.startswith('"') and cookie.endswith('"')):
        cookie = cookie[1:-1]

    return cookie


def extract_url_from_curl(curl_text: str) -> Optional[str]:
    """Extract the download URL from a cURL or PowerShell command."""
    # Check if this is PowerShell format
    if is_powershell_format(curl_text):
        return extract_url_from_powershell(curl_text)

    # Standard cURL format
    match = re.search(r"curl\s+['\"]?(https?://[^'\"\s]+)['\"]?", curl_text, re.IGNORECASE)
    if match:
        url = match.group(1)
        if 'takeout' in url.lower():
            return url
    return None


# =============================================================================
# DOWNLOAD ENGINE - Simple and Robust
# =============================================================================

class TakeoutDownloader:
    """
    Simple downloader that:
    1. Keeps trying to download
    2. On failure, prompts for new cURL
    3. Tracks known sizes to detect incomplete downloads
    4. Cleans up bad zips and resumes from last good
    5. Verifies ZIP integrity after download
    6. Retries transient errors with exponential backoff
    """

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR, parallel: int = DEFAULT_PARALLEL):
        self.output_dir = validate_output_dir(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.size_history = SizeHistory(str(self.output_dir))
        self.cookie = ""
        self.base_url = ""
        self.query_string = ""
        self.extension = ".zip"
        self.file_count = DEFAULT_FILE_COUNT
        self.parallel = min(max(1, parallel), MAX_PARALLEL)  # Clamp to 1-20
        self.should_stop = False
        self.auth_failed = False  # Flag for parallel downloads
        self.stats = DownloadStats()
        self._lock = threading.Lock()  # For thread-safe stats updates

    def set_curl(self, curl_text: str) -> bool:
        """Set cookie and URL from cURL command."""
        # Extract cookie
        self.cookie = extract_cookie_from_curl(curl_text)
        if not self.cookie:
            print("✗ Could not extract cookie from cURL")
            return False

        # Extract URL
        url = extract_url_from_curl(curl_text)
        if not url:
            print("✗ Could not extract URL from cURL")
            return False

        # Parse URL parts
        base, file_num, ext, query = extract_url_parts(url)
        if not base:
            print("✗ Could not parse URL pattern")
            return False

        self.base_url = base
        self.extension = ext
        self.query_string = query

        print(f"✓ Cookie: {len(self.cookie)} chars")
        print(f"✓ URL pattern: {base}XXX{ext}")
        return True

    def get_filename(self, num: int) -> str:
        """Get filename for file number."""
        return f"{self.base_url.split('/')[-1]}{num:03d}{self.extension}"

    def get_url(self, num: int) -> str:
        """Get URL for file number."""
        url = f"{self.base_url}{num:03d}{self.extension}"
        if self.query_string:
            url += f"?{self.query_string}"
        return url

    def get_filepath(self, num: int) -> Path:
        """Get local file path for file number."""
        return self.output_dir / self.get_filename(num)

    def cleanup_bad_files(self) -> int:
        """
        Clean up zero-sized and incomplete files.
        Returns the first file number that needs downloading.

        Note: .downloading files are preserved for resume support.
        """
        first_missing = None

        for num in range(1, self.file_count + 1):
            filepath = self.get_filepath(num)
            temp_path = filepath.with_suffix('.downloading')

            # Check if there's a partial download to resume
            if temp_path.exists():
                partial_size = temp_path.stat().st_size
                if partial_size > 0:
                    print(f"  Found partial: {filepath.name} ({partial_size/(1024*1024):.1f}MB to resume)")
                    if first_missing is None:
                        first_missing = num
                    continue
                else:
                    # Zero-sized partial, delete it
                    temp_path.unlink()

            if not filepath.exists():
                if first_missing is None:
                    first_missing = num
                continue

            size = filepath.stat().st_size

            # Zero-sized = definitely bad
            if size == 0:
                print(f"  Deleting zero-sized: {filepath.name}")
                filepath.unlink()
                if first_missing is None:
                    first_missing = num
                continue

            # Check against known size
            expected = self.size_history.get_expected_size(filepath.name)
            if expected and size < expected:
                print(f"  Deleting incomplete: {filepath.name} ({size} < {expected})")
                filepath.unlink()
                if first_missing is None:
                    first_missing = num
                continue

            # File looks good, record its size
            if not expected:
                self.size_history.record_size(filepath.name, size)

        return first_missing if first_missing is not None else 1

    @staticmethod
    def _verify_zip_integrity(filepath: Path) -> bool:
        """Verify that a file has a valid ZIP end-of-central-directory record.

        Checks that the ZIP EOCD signature (b'PK\\x05\\x06') appears
        somewhere in the last 1024 bytes of the file.
        """
        if not filepath.exists() or filepath.stat().st_size < 22:
            return False
        try:
            with open(filepath, 'rb') as f:
                # Seek to 1024 bytes from end (or start if file is smaller)
                seek_pos = max(0, filepath.stat().st_size - 1024)
                f.seek(seek_pos)
                tail = f.read()
                return b'PK\x05\x06' in tail
        except OSError:
            return False

    def download_file(self, num: int) -> Tuple[bool, str]:
        """
        Download a single file with resume support and internal retry loop.

        Returns: (success, error_message)

        Resume logic:
        - If .downloading file exists, resume from that byte offset
        - Uses HTTP Range header for partial content
        - Preserves partial file on auth failure for later resume
        - Retries transient network errors up to MAX_RETRIES with exponential backoff
        """
        filepath = self.get_filepath(num)
        url = self.get_url(num)

        # Skip if already exists and looks complete
        if filepath.exists():
            size = filepath.stat().st_size
            expected = self.size_history.get_expected_size(filepath.name)
            if size > 0 and (not expected or size >= expected):
                return True, "already exists"

        temp_path = filepath.with_suffix('.downloading')
        resume_from = 0

        # Check for existing partial download to resume
        if temp_path.exists():
            resume_from = temp_path.stat().st_size
            if resume_from > 0:
                print(f"  [{filepath.name}] Resuming from {resume_from/(1024*1024):.1f}MB")

        for attempt in range(MAX_RETRIES):
            try:
                headers = {
                    'Cookie': self.cookie,
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                }

                # Add Range header for resume
                if resume_from > 0:
                    headers['Range'] = f'bytes={resume_from}-'

                response = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(10, 300),
                )

                # Check for auth failure via status
                if response.status_code in (401, 403):
                    # Keep partial file for resume
                    return False, "AUTH_FAILED"

                # Check for redirect to login.
                # requests follows redirects by default, so status 302 is never
                # seen here — instead we detect redirect via the final URL.
                if 'accounts.google' in response.url:
                    return False, "AUTH_FAILED"

                # 416 = Range Not Satisfiable (file might be complete or server doesn't support range)
                if response.status_code == 416:
                    # Try without range header - file might be complete
                    if resume_from > 0:
                        print(f"  [{filepath.name}] Range not satisfiable, checking if complete...")
                        # Verify with a fresh request to get content-length
                        head_resp = requests.head(url, headers={'Cookie': self.cookie, 'User-Agent': headers['User-Agent']}, timeout=10)
                        if head_resp.status_code == 200:
                            expected_size = int(head_resp.headers.get('content-length', 0))
                            if expected_size > 0 and resume_from >= expected_size:
                                # File is complete, rename it
                                temp_path.rename(filepath)
                                self.size_history.record_size(filepath.name, resume_from)
                                return True, "resumed-complete"
                    # Otherwise restart from scratch — reset resume and retry in loop
                    resume_from = 0
                    temp_path.unlink(missing_ok=True)
                    if attempt > 0:
                        time.sleep(RETRY_BACKOFF_BASE ** attempt)
                    continue

                response.raise_for_status()

                # Check content type
                content_type = response.headers.get('content-type', '')
                if 'text/html' in content_type:
                    return False, "AUTH_FAILED"

                # Get total size - for 206 Partial Content, content-length is remaining bytes
                content_length = int(response.headers.get('content-length', 0))

                # For resumed downloads (206), total = resume_from + content_length
                # For fresh downloads (200), total = content_length
                if response.status_code == 206:
                    total_size = resume_from + content_length
                else:
                    total_size = content_length
                    # Fresh download - reset resume_from
                    if resume_from > 0:
                        print(f"  [{filepath.name}] Server doesn't support resume, starting fresh")
                        resume_from = 0

                if total_size < 1000 and resume_from == 0:
                    return False, "AUTH_FAILED"  # Too small, probably auth page

                # Open file in append mode for resume, write mode for fresh
                file_mode = 'ab' if resume_from > 0 and response.status_code == 206 else 'wb'
                downloaded = resume_from

                filepath.parent.mkdir(parents=True, exist_ok=True)

                with open(temp_path, file_mode) as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if self.should_stop:
                            # Keep partial file for resume on stop
                            return False, "stopped"

                        if chunk:
                            # Check first chunk for ZIP magic (only on fresh downloads)
                            if downloaded == 0 and chunk[:2] != b'PK':
                                temp_path.unlink()
                                return False, "AUTH_FAILED"

                            f.write(chunk)
                            downloaded += len(chunk)

                            # Progress
                            pct = (downloaded / total_size * 100) if total_size else 0
                            print(f"\r  [{filepath.name}] {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB ({pct:.0f}%)", end='', flush=True)

                print()  # Newline after progress

                # Verify ZIP integrity before renaming
                if not self._verify_zip_integrity(temp_path):
                    temp_path.unlink(missing_ok=True)
                    if attempt < MAX_RETRIES - 1:
                        wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                        print(f"  [{filepath.name}] ZIP integrity check failed, retrying in {wait:.1f}s...")
                        time.sleep(wait)
                        resume_from = 0
                        continue
                    return False, "INTEGRITY_FAILED"

                # Rename to final
                temp_path.rename(filepath)

                # Record size for future reference
                self.size_history.record_size(filepath.name, downloaded)

                return True, ""

            except requests.exceptions.HTTPError as e:
                if e.response and e.response.status_code == 404:
                    return False, "NOT_FOUND"
                # Transient HTTP error — retry if attempts remain
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                    print(f"  [{filepath.name}] HTTP error (attempt {attempt+1}/{MAX_RETRIES}), retrying in {wait:.1f}s: {e}")
                    time.sleep(wait)
                    continue
                return False, f"HTTP error: {e}"
            except requests.exceptions.RequestException as e:
                # Transient network error — retry if attempts remain
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                    print(f"  [{filepath.name}] Network error (attempt {attempt+1}/{MAX_RETRIES}), retrying in {wait:.1f}s: {e}")
                    time.sleep(wait)
                    continue
                return False, f"Network error: {e}"

        # Exhausted all retries
        return False, "MAX_RETRIES_EXCEEDED"

    def download_file_with_retry(self, num: int) -> Tuple[bool, str]:
        """Wrapper around download_file that adds an outer retry loop for transient errors.

        Used by both sequential and parallel download paths.
        Transient errors (not AUTH_FAILED, NOT_FOUND, or 'already exists') are
        retried up to MAX_RETRIES times with exponential backoff.
        """
        outer_attempts = 0
        while outer_attempts < MAX_RETRIES:
            success, error = self.download_file(num)

            if success:
                return True, error

            # Non-retryable errors — return immediately
            if error in ("AUTH_FAILED", "NOT_FOUND", "already exists", "stopped", "INTEGRITY_FAILED"):
                return False, error

            # Retryable transient error
            outer_attempts += 1
            if outer_attempts < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE ** outer_attempts
                print(f"  [{self.get_filename(num)}] Outer retry {outer_attempts}/{MAX_RETRIES} after error '{error}', waiting {wait:.1f}s...")
                time.sleep(wait)

        return False, error

    def prompt_new_curl(self) -> bool:
        """Prompt user for new cURL command. Returns True if successful."""
        print("\n" + "=" * 60)
        print("🔐 AUTHENTICATION NEEDED")
        print("=" * 60)
        print("\nTo get a new cURL command:")
        print("1. Go to takeout.google.com in your browser")
        print("2. Open DevTools (F12) -> Network tab")
        print("3. Click any download link")
        print("4. Right-click the request -> Copy -> Copy as cURL")
        print("   (PowerShell format also supported)")
        print("\nPaste the cURL command (or 'q' to quit):")
        print("-" * 60)

        try:
            lines = []
            while True:
                line = input()
                if line.strip().lower() == 'q':
                    return False
                lines.append(line)
                # cURL commands can span multiple lines with backslash
                if not line.rstrip().endswith('\\'):
                    break

            curl_text = ' '.join(lines)
            if not curl_text.strip():
                return False

            return self.set_curl(curl_text)

        except (EOFError, KeyboardInterrupt):
            return False

    def run(self, file_count: int = DEFAULT_FILE_COUNT) -> DownloadStats:
        """
        Main download loop.
        Keeps trying until all files downloaded or user quits.
        Supports parallel downloads with simple auth retry.
        """
        self.file_count = min(max(1, file_count), MAX_FILE_COUNT)
        self.stats = DownloadStats(start_time=datetime.now())
        self.should_stop = False
        self.auth_failed = False

        print(f"\nGoogle Takeout Downloader v{VERSION}")
        print(f"Output: {self.output_dir}")
        print(f"Max files: {self.file_count}")
        print(f"Parallel: {self.parallel}")
        print("-" * 60)

        # Initial cURL if not set
        if not self.cookie or not self.base_url:
            if not self.prompt_new_curl():
                print("No cURL provided, exiting.")
                return self.stats

        while not self.should_stop:
            # Clean up any bad files first
            print(f"\nChecking for incomplete downloads...")
            first_needed = self.cleanup_bad_files()

            # Build list of files to download
            to_download = []
            for num in range(first_needed, self.file_count + 1):
                filepath = self.get_filepath(num)
                if filepath.exists() and filepath.stat().st_size > 0:
                    expected = self.size_history.get_expected_size(filepath.name)
                    if not expected or filepath.stat().st_size >= expected:
                        continue  # Skip existing good files
                to_download.append(num)

            if not to_download:
                print("\nAll files downloaded!")
                break

            print(f"\nDownloading {len(to_download)} files starting from {to_download[0]}...")

            # Reset auth flag
            self.auth_failed = False
            consecutive_404 = 0

            if self.parallel == 1:
                # Sequential mode (simpler) — uses download_file_with_retry
                for num in to_download:
                    if self.should_stop or self.auth_failed:
                        break

                    filepath = self.get_filepath(num)
                    success, error = self.download_file_with_retry(num)

                    if success:
                        print(f"✓ {filepath.name}")
                        with self._lock:
                            self.stats.completed_files += 1
                        consecutive_404 = 0

                    elif error == "AUTH_FAILED":
                        print(f"\n✗ Auth failed on file {num}")
                        self.auth_failed = True
                        break

                    elif error == "NOT_FOUND":
                        consecutive_404 += 1
                        print(f"✗ {filepath.name} not found (404)")
                        if consecutive_404 >= 3:
                            print(f"\n3 consecutive 404s - assuming done")
                            self.should_stop = True
                            break

                    else:
                        print(f"✗ {filepath.name}: {error}")
                        with self._lock:
                            self.stats.failed_files += 1
                        consecutive_404 = 0
            else:
                # Parallel mode — download_file has internal retries;
                # download_file_with_retry adds the outer retry layer
                with ThreadPoolExecutor(max_workers=self.parallel) as executor:
                    futures = {executor.submit(self.download_file_with_retry, num): num for num in to_download}

                    for future in as_completed(futures):
                        if self.should_stop or self.auth_failed:
                            # Cancel remaining futures
                            for f in futures:
                                f.cancel()
                            break

                        num = futures[future]
                        filepath = self.get_filepath(num)

                        try:
                            success, error = future.result()

                            if success:
                                print(f"✓ {filepath.name}")
                                with self._lock:
                                    self.stats.completed_files += 1

                            elif error == "AUTH_FAILED":
                                print(f"\n✗ Auth failed on file {num}")
                                self.auth_failed = True

                            elif error == "NOT_FOUND":
                                print(f"✗ {filepath.name} not found (404)")
                                # Don't track consecutive 404s in parallel mode

                            else:
                                print(f"✗ {filepath.name}: {error}")
                                with self._lock:
                                    self.stats.failed_files += 1

                        except Exception as e:
                            print(f"✗ {filepath.name}: {e}")
                            with self._lock:
                                self.stats.failed_files += 1

            # Handle auth failure - prompt for new cURL and retry
            if self.auth_failed:
                if not self.prompt_new_curl():
                    print("No new cURL provided, stopping.")
                    break
                # Loop will continue and retry remaining files
            else:
                # No auth failure - we're done
                break

        # Summary
        print("\n" + "=" * 60)
        print(f"✅ Done!")
        print(f"   Completed: {self.stats.completed_files}")
        print(f"   Skipped:   {self.stats.skipped_files}")
        print(f"   Failed:    {self.stats.failed_files}")
        print("=" * 60)

        return self.stats

    def stop(self):
        """Stop downloading."""
        self.should_stop = True


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main():
    """Main entry point. The TUI is the only interface.

    Captures come from the browser extension ("Copy as JSON") and are pasted
    into the TUI. cURL paste is kept as a fallback for browsers that can't
    run the extension.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description='Google Takeout Bulk Downloader (TUI)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Launch the TUI

Paste a JSON payload (from the browser extension's "Copy as JSON" button)
or a cURL command into the TUI to start downloading.
        """
    )

    parser.add_argument('--version', '-v', action='version',
                       version=f'%(prog)s {VERSION}')

    parser.parse_args()
    run_tui()


def run_tui():
    """Run the terminal UI."""
    try:
        from google_takeout_tui import TakeoutTUI
        app = TakeoutTUI()
        app.run()
    except ImportError as e:
        print(f"TUI mode requires textual: {e}")
        print("Install with: pip install textual rich requests")
        sys.exit(1)


if __name__ == '__main__':
    main()
