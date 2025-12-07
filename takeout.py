#!/usr/bin/env python3
"""
Google Takeout Bulk Downloader - Unified Script
================================================
A comprehensive tool for downloading Google Takeout archives.

Usage:
    python takeout.py                    # CLI mode (default)
    python takeout.py --web              # Web interface
    python takeout.py --gui              # Desktop GUI
    python takeout.py --web --port 8080  # Web on custom port

Features:
    - Resume partial downloads
    - ZIP integrity verification
    - Automatic auth expiry detection
    - Speed limiting / bandwidth management
    - Webhook/email notifications
    - Auto-extract and organize files
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import zipfile
import smtplib
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

VERSION = "2.0.0"
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
DEFAULT_PARALLEL = 6
DEFAULT_FILE_COUNT = 100
DEFAULT_OUTPUT_DIR = "./downloads"
AUTH_WARNING_MINUTES = 45  # Warn when session is this old
AUTH_EXPIRY_MINUTES = 60   # Typical Google session expiry

# Partial download extension
PARTIAL_EXT = ".partial"
PROGRESS_EXT = ".progress"

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class DownloadProgress:
    """Track progress of a single file download."""
    filename: str
    url: str
    output_path: Path
    total_bytes: int = 0
    downloaded_bytes: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: str = "pending"  # pending, downloading, complete, failed, paused
    error: Optional[str] = None
    is_auth_failure: bool = False
    
    def to_dict(self) -> dict:
        return {
            'filename': self.filename,
            'total_bytes': self.total_bytes,
            'downloaded_bytes': self.downloaded_bytes,
            'status': self.status,
            'error': self.error,
            'percent': self.percent,
        }
    
    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 0
        return (self.downloaded_bytes / self.total_bytes) * 100
    
    def save_progress(self):
        """Save progress to file for resume support."""
        progress_file = Path(str(self.output_path) + PROGRESS_EXT)
        data = {
            'url': self.url,
            'total_bytes': self.total_bytes,
            'downloaded_bytes': self.downloaded_bytes,
            'started_at': self.started_at.isoformat() if self.started_at else None,
        }
        with open(progress_file, 'w') as f:
            json.dump(data, f)
    
    @classmethod
    def load_progress(cls, output_path: Path) -> Optional['DownloadProgress']:
        """Load progress from file."""
        progress_file = Path(str(output_path) + PROGRESS_EXT)
        if not progress_file.exists():
            return None
        try:
            with open(progress_file) as f:
                data = json.load(f)
            return cls(
                filename=output_path.name,
                url=data['url'],
                output_path=output_path,
                total_bytes=data['total_bytes'],
                downloaded_bytes=data['downloaded_bytes'],
                started_at=datetime.fromisoformat(data['started_at']) if data['started_at'] else None,
                status='paused',
            )
        except (json.JSONDecodeError, KeyError):
            return None
    
    def clear_progress(self):
        """Remove progress file."""
        progress_file = Path(str(self.output_path) + PROGRESS_EXT)
        if progress_file.exists():
            progress_file.unlink()


@dataclass
class DownloadStats:
    """Overall download statistics."""
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    bytes_downloaded: int = 0
    start_time: Optional[datetime] = None
    auth_start_time: Optional[datetime] = None
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @property
    def elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0
        return (datetime.now() - self.start_time).total_seconds()
    
    @property
    def auth_elapsed_minutes(self) -> float:
        if not self.auth_start_time:
            return 0
        return (datetime.now() - self.auth_start_time).total_seconds() / 60
    
    @property
    def speed_mbps(self) -> float:
        elapsed = self.elapsed_seconds
        if elapsed == 0:
            return 0
        return (self.bytes_downloaded / elapsed) / (1024 * 1024)
    
    def get_eta(self, total_bytes: int) -> str:
        """Calculate estimated time remaining."""
        if self.speed_mbps == 0:
            return "calculating..."
        remaining_bytes = total_bytes - self.bytes_downloaded
        remaining_seconds = remaining_bytes / (self.speed_mbps * 1024 * 1024)
        if remaining_seconds < 60:
            return f"{int(remaining_seconds)}s"
        elif remaining_seconds < 3600:
            return f"{int(remaining_seconds / 60)}m"
        else:
            hours = int(remaining_seconds / 3600)
            mins = int((remaining_seconds % 3600) / 60)
            return f"{hours}h {mins}m"


@dataclass
class NotificationConfig:
    """Notification settings."""
    # Webhook
    webhook_url: Optional[str] = None
    webhook_events: List[str] = field(default_factory=lambda: ['complete', 'auth_expired', 'error'])
    
    # Email
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None
    
    # Desktop notifications
    desktop_enabled: bool = True
    sound_enabled: bool = True


@dataclass 
class DownloadConfig:
    """Download configuration."""
    cookie: str = ""
    url: str = ""
    output_dir: str = DEFAULT_OUTPUT_DIR
    file_count: int = DEFAULT_FILE_COUNT
    parallel: int = DEFAULT_PARALLEL
    
    # Speed limiting (0 = unlimited)
    speed_limit_mbps: float = 0
    
    # Resume support
    resume_enabled: bool = True
    
    # Verification
    verify_zip: bool = True
    
    # Auto-extract
    auto_extract: bool = False
    extract_dir: Optional[str] = None
    organize_by_type: bool = False
    delete_after_extract: bool = False
    
    # Notifications
    notifications: NotificationConfig = field(default_factory=NotificationConfig)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def load_env_file(env_path: Path = None):
    """Load environment variables from .env file."""
    if env_path is None:
        env_path = Path(__file__).parent / '.env'
    
    if not env_path.exists():
        return
    
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)


def extract_url_parts(url: str) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str], str]:
    """Extract URL parts for Google Takeout pattern.
    
    Returns: (base_url, batch_num, file_num, extension, query_string)
    """
    if '?' in url:
        url_path, query_string = url.split('?', 1)
    else:
        url_path, query_string = url, ''
    
    match = re.search(r'(.*takeout-[^-]+-)(\d+)-(\d+)(\.\w+)$', url_path)
    if not match:
        return None, None, None, None, ''
    
    base = match.group(1)
    batch_num = int(match.group(2))
    file_num = int(match.group(3))
    ext = match.group(4)
    
    return base, batch_num, file_num, ext, query_string


def extract_cookie_from_curl(curl_text: str) -> str:
    """Extract cookie value from a cURL command or raw cookie string."""
    if 'curl' in curl_text.lower() or "-H 'Cookie:" in curl_text or '-H "Cookie:' in curl_text:
        match = re.search(r"-H\s*['\"]Cookie:\s*([^'\"]+)['\"]", curl_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    if curl_text.lower().startswith('cookie:'):
        return curl_text[7:].strip()
    
    cookie = curl_text.strip()
    if (cookie.startswith("'") and cookie.endswith("'")) or \
       (cookie.startswith('"') and cookie.endswith('"')):
        cookie = cookie[1:-1]
    
    return cookie


def extract_url_from_curl(curl_text: str) -> Optional[str]:
    """Extract the download URL from a cURL command."""
    match = re.search(r"curl\s+['\"]?(https?://[^'\"\s]+)['\"]?", curl_text, re.IGNORECASE)
    if match:
        url = match.group(1)
        if 'takeout' in url.lower():
            return url
    return None


def verify_zip_file(file_path: Path) -> Tuple[bool, str]:
    """Verify a ZIP file is valid and not corrupted.
    
    Returns: (is_valid, message)
    """
    if not file_path.exists():
        return False, "File does not exist"
    
    # Check file size
    if file_path.stat().st_size < 1000:
        return False, "File too small to be valid"
    
    # Check magic bytes
    with open(file_path, 'rb') as f:
        magic = f.read(4)
        if magic[:2] != b'PK':
            return False, "Invalid ZIP magic bytes"
    
    # Try to open and test the ZIP
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Test CRC of all files
            bad_file = zf.testzip()
            if bad_file:
                return False, f"Corrupted file in archive: {bad_file}"
        return True, "ZIP file is valid"
    except zipfile.BadZipFile as e:
        return False, f"Bad ZIP file: {e}"
    except Exception as e:
        return False, f"Error verifying ZIP: {e}"


def extract_zip_file(zip_path: Path, extract_dir: Path, organize: bool = False) -> Tuple[bool, str, List[Path]]:
    """Extract a ZIP file, optionally organizing by type.
    
    Returns: (success, message, extracted_files)
    """
    extracted_files = []
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if organize:
                # Extract and organize by Google service type
                for member in zf.namelist():
                    # Determine category from path
                    category = "Other"
                    member_lower = member.lower()
                    
                    if 'photos' in member_lower or 'google photos' in member_lower:
                        category = "Photos"
                    elif 'drive' in member_lower or 'my drive' in member_lower:
                        category = "Drive"
                    elif 'mail' in member_lower or 'gmail' in member_lower:
                        category = "Mail"
                    elif 'calendar' in member_lower:
                        category = "Calendar"
                    elif 'contacts' in member_lower:
                        category = "Contacts"
                    elif 'youtube' in member_lower:
                        category = "YouTube"
                    elif 'maps' in member_lower or 'location' in member_lower:
                        category = "Maps"
                    elif 'chrome' in member_lower:
                        category = "Chrome"
                    elif 'keep' in member_lower:
                        category = "Keep"
                    elif 'fit' in member_lower:
                        category = "Fit"
                    
                    # Extract to category folder
                    target_dir = extract_dir / category
                    target_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Get just the filename, not the full path
                    filename = Path(member).name
                    if filename:  # Skip directories
                        target_path = target_dir / filename
                        with zf.open(member) as src, open(target_path, 'wb') as dst:
                            dst.write(src.read())
                        extracted_files.append(target_path)
            else:
                # Simple extraction
                zf.extractall(extract_dir)
                extracted_files = [extract_dir / name for name in zf.namelist()]
        
        return True, f"Extracted {len(extracted_files)} files", extracted_files
    except Exception as e:
        return False, f"Extraction failed: {e}", []


# =============================================================================
# NOTIFICATION SYSTEM
# =============================================================================

class NotificationManager:
    """Handle all notification types."""
    
    def __init__(self, config: NotificationConfig):
        self.config = config
    
    def send(self, event: str, title: str, message: str, data: dict = None):
        """Send notification via all configured channels."""
        if self.config.desktop_enabled:
            self._send_desktop(title, message)
        
        if self.config.webhook_url and event in self.config.webhook_events:
            self._send_webhook(event, title, message, data)
        
        if self.config.smtp_host and self.config.smtp_to:
            self._send_email(title, message)
    
    def _send_desktop(self, title: str, message: str):
        """Send desktop notification."""
        try:
            if sys.platform == 'darwin':
                subprocess.run([
                    'osascript', '-e',
                    f'display notification "{message}" with title "{title}"'
                ], capture_output=True)
            elif sys.platform == 'linux':
                subprocess.run(['notify-send', title, message], capture_output=True)
            elif sys.platform == 'win32':
                # Windows toast notification via PowerShell
                ps_script = f'''
                [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
                $template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
                $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
                $xml.GetElementsByTagName("text")[0].AppendChild($xml.CreateTextNode("{title}"))
                $xml.GetElementsByTagName("text")[1].AppendChild($xml.CreateTextNode("{message}"))
                $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
                [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Google Takeout Downloader").Show($toast)
                '''
                subprocess.run(['powershell', '-Command', ps_script], capture_output=True)
        except Exception:
            pass  # Silently fail for notifications
    
    def _send_webhook(self, event: str, title: str, message: str, data: dict = None):
        """Send webhook notification."""
        try:
            payload = {
                'event': event,
                'title': title,
                'message': message,
                'timestamp': datetime.now().isoformat(),
                'data': data or {},
            }
            requests.post(
                self.config.webhook_url,
                json=payload,
                timeout=10,
                headers={'Content-Type': 'application/json'}
            )
        except Exception:
            pass  # Silently fail for webhooks
    
    def _send_email(self, title: str, message: str):
        """Send email notification."""
        try:
            msg = MIMEMultipart()
            msg['From'] = self.config.smtp_from or self.config.smtp_user
            msg['To'] = self.config.smtp_to
            msg['Subject'] = f"[Takeout Downloader] {title}"
            
            body = f"""
Google Takeout Downloader Notification
======================================

{message}

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
            msg.attach(MIMEText(body, 'plain'))
            
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port) as server:
                server.starttls()
                if self.config.smtp_user and self.config.smtp_password:
                    server.login(self.config.smtp_user, self.config.smtp_password)
                server.send_message(msg)
        except Exception:
            pass  # Silently fail for email
    
    def play_sound(self, sound_type: str = 'complete'):
        """Play notification sound."""
        if not self.config.sound_enabled:
            return
        
        try:
            if sys.platform == 'darwin':
                sound = 'Glass' if sound_type == 'complete' else 'Basso'
                subprocess.run(['afplay', f'/System/Library/Sounds/{sound}.aiff'], capture_output=True)
            elif sys.platform == 'linux':
                subprocess.run(['paplay', '/usr/share/sounds/freedesktop/stereo/complete.oga'], capture_output=True)
            elif sys.platform == 'win32':
                import winsound
                if sound_type == 'complete':
                    winsound.MessageBeep(winsound.MB_OK)
                else:
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


# =============================================================================
# DOWNLOAD ENGINE (Core)
# =============================================================================

class DownloadEngine:
    """Core download engine with all features."""
    
    def __init__(self, config: DownloadConfig, 
                 on_progress: Callable = None,
                 on_log: Callable = None,
                 on_auth_expired: Callable = None):
        self.config = config
        self.stats = DownloadStats()
        self.downloads: Dict[str, DownloadProgress] = {}
        self.should_stop = False
        self.is_running = False
        
        # Callbacks
        self.on_progress = on_progress or (lambda *args: None)
        self.on_log = on_log or (lambda msg, level: print(f"[{level.upper()}] {msg}"))
        self.on_auth_expired = on_auth_expired
        
        # Thread safety
        self._lock = threading.Lock()
        self._cookie_lock = threading.Lock()
        self._current_cookie = config.cookie
        
        # Notifications
        self.notifier = NotificationManager(config.notifications)
        
        # Speed limiting
        self._speed_limiter = SpeedLimiter(config.speed_limit_mbps)
    
    def log(self, message: str, level: str = 'info'):
        """Log a message."""
        self.on_log(message, level)
    
    def get_cookie(self) -> str:
        """Get current cookie (thread-safe)."""
        with self._cookie_lock:
            return self._current_cookie
    
    def set_cookie(self, cookie: str):
        """Set current cookie (thread-safe)."""
        with self._cookie_lock:
            self._current_cookie = cookie
            self.stats.auth_start_time = datetime.now()
    
    def create_session(self) -> requests.Session:
        """Create an optimized requests session."""
        session = requests.Session()
        
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        )
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive',
            'Cookie': self.get_cookie(),
        })
        return session
    
    def check_auth_expiry(self) -> Tuple[bool, int]:
        """Check if auth might expire soon.
        
        Returns: (is_warning, minutes_remaining)
        """
        elapsed = self.stats.auth_elapsed_minutes
        remaining = AUTH_EXPIRY_MINUTES - elapsed
        is_warning = elapsed >= AUTH_WARNING_MINUTES
        return is_warning, int(remaining)
    
    def build_download_list(self) -> List[DownloadProgress]:
        """Build list of files to download."""
        base_url, batch_num, start_file, extension, query_string = extract_url_parts(self.config.url)
        
        if base_url is None:
            self.log("Invalid URL format", 'error')
            return []
        
        output_path = Path(self.config.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        downloads = []
        skipped = 0
        
        for i in range(1, self.config.file_count + 1):
            filename = f"takeout-{batch_num}-{i:03d}{extension}"
            file_path = output_path / filename
            
            # Check if already complete
            if file_path.exists():
                # Verify if enabled
                if self.config.verify_zip:
                    is_valid, _ = verify_zip_file(file_path)
                    if is_valid:
                        skipped += 1
                        continue
                    else:
                        # Invalid file, will re-download
                        file_path.unlink()
                else:
                    skipped += 1
                    continue
            
            # Build URL
            current_url = f"{base_url}{batch_num}-{i:03d}{extension}"
            if query_string:
                current_url += f"?{query_string}"
            
            # Check for partial download
            partial_path = Path(str(file_path) + PARTIAL_EXT)
            progress = None
            
            if self.config.resume_enabled and partial_path.exists():
                progress = DownloadProgress.load_progress(file_path)
                if progress:
                    progress.url = current_url
                    self.log(f"Resuming {filename} from {progress.downloaded_bytes / (1024*1024):.1f} MB", 'info')
            
            if not progress:
                progress = DownloadProgress(
                    filename=filename,
                    url=current_url,
                    output_path=file_path,
                )
            
            downloads.append(progress)
        
        self.stats.skipped_files = skipped
        self.stats.total_files = len(downloads)
        
        self.log(f"Found {len(downloads)} files to download ({skipped} skipped)", 'info')
        return downloads
    
    def download_file(self, progress: DownloadProgress) -> DownloadProgress:
        """Download a single file with resume support."""
        session = self.create_session()
        filename = progress.filename
        output_path = progress.output_path
        partial_path = Path(str(output_path) + PARTIAL_EXT)
        
        # Prepare headers for resume
        headers = {}
        start_byte = 0
        
        if self.config.resume_enabled and partial_path.exists() and progress.downloaded_bytes > 0:
            start_byte = progress.downloaded_bytes
            headers['Range'] = f'bytes={start_byte}-'
            self.log(f"[{filename}] Resuming from byte {start_byte}", 'info')
        
        try:
            with session.get(progress.url, stream=True, allow_redirects=True, 
                           timeout=(10, 300), headers=headers) as r:
                
                # Handle 416 Range Not Satisfiable (file already complete)
                if r.status_code == 416:
                    if partial_path.exists():
                        partial_path.rename(output_path)
                    progress.status = 'complete'
                    return progress
                
                r.raise_for_status()
                
                # Check content type
                content_type = r.headers.get('content-type', '')
                if 'text/html' in content_type:
                    preview = r.content[:500].decode('utf-8', errors='ignore')
                    if 'signin' in preview.lower() or 'login' in preview.lower():
                        progress.status = 'failed'
                        progress.error = "Auth failed - cookies invalid/expired"
                        progress.is_auth_failure = True
                        return progress
                    progress.status = 'failed'
                    progress.error = "Got HTML instead of ZIP"
                    progress.is_auth_failure = True
                    return progress
                
                # Get total size
                if r.status_code == 206:  # Partial content (resume)
                    content_range = r.headers.get('content-range', '')
                    if '/' in content_range:
                        progress.total_bytes = int(content_range.split('/')[-1])
                else:
                    progress.total_bytes = int(r.headers.get('content-length', 0)) + start_byte
                
                # Validate size
                if progress.total_bytes < 1000000:
                    progress.status = 'failed'
                    progress.error = f"File too small ({progress.total_bytes} bytes) - likely auth failure"
                    progress.is_auth_failure = True
                    return progress
                
                # Start download
                progress.status = 'downloading'
                progress.started_at = progress.started_at or datetime.now()
                
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Open file in append mode for resume
                mode = 'ab' if start_byte > 0 else 'wb'
                first_chunk = (start_byte == 0)
                
                with open(partial_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if self.should_stop:
                            progress.status = 'paused'
                            progress.save_progress()
                            return progress
                        
                        # Validate first chunk is a ZIP file
                        if first_chunk and chunk:
                            first_chunk = False
                            if chunk[:2] != b'PK':
                                preview = chunk[:500].decode('utf-8', errors='ignore').lower()
                                if 'signin' in preview or 'login' in preview or 'accounts.google' in preview:
                                    progress.error = "Auth failed - redirected to login"
                                else:
                                    progress.error = "Not a valid ZIP file (wrong magic bytes)"
                                progress.status = 'failed'
                                progress.is_auth_failure = True
                                if partial_path.exists():
                                    partial_path.unlink()
                                return progress
                        
                        if chunk:
                            # Apply speed limiting
                            self._speed_limiter.limit(len(chunk))
                            
                            f.write(chunk)
                            chunk_len = len(chunk)
                            progress.downloaded_bytes += chunk_len
                            
                            with self._lock:
                                self.stats.bytes_downloaded += chunk_len
                            
                            # Save progress periodically (every 10MB)
                            if self.config.resume_enabled and progress.downloaded_bytes % (10 * 1024 * 1024) < CHUNK_SIZE:
                                progress.save_progress()
                            
                            # Callback
                            self.on_progress(progress)
                
                # Download complete - rename partial to final
                partial_path.rename(output_path)
                progress.clear_progress()
                progress.status = 'complete'
                progress.completed_at = datetime.now()
                
                # Verify ZIP if enabled
                if self.config.verify_zip:
                    is_valid, msg = verify_zip_file(output_path)
                    if not is_valid:
                        self.log(f"[{filename}] Verification failed: {msg}", 'warning')
                        output_path.unlink()
                        progress.status = 'failed'
                        progress.error = f"Verification failed: {msg}"
                        return progress
                    self.log(f"[{filename}] Verified OK", 'success')
                
                # Auto-extract if enabled
                if self.config.auto_extract:
                    extract_dir = Path(self.config.extract_dir or self.config.output_dir) / "extracted"
                    success, msg, files = extract_zip_file(
                        output_path, extract_dir, 
                        organize=self.config.organize_by_type
                    )
                    if success:
                        self.log(f"[{filename}] {msg}", 'success')
                        if self.config.delete_after_extract:
                            output_path.unlink()
                    else:
                        self.log(f"[{filename}] {msg}", 'warning')
                
                return progress
                
        except requests.exceptions.RequestException as e:
            progress.status = 'failed'
            progress.error = str(e)
            if partial_path.exists() and not self.config.resume_enabled:
                partial_path.unlink()
            return progress
    
    def run(self) -> DownloadStats:
        """Run the download process."""
        self.is_running = True
        self.should_stop = False
        self.stats = DownloadStats(
            start_time=datetime.now(),
            auth_start_time=datetime.now(),
        )
        
        # Build download list
        downloads = self.build_download_list()
        if not downloads:
            self.log("No files to download", 'info')
            self.is_running = False
            return self.stats
        
        self.log(f"Starting download of {len(downloads)} files with {self.config.parallel} parallel connections", 'info')
        
        # Track remaining downloads for retry on auth failure
        remaining = downloads.copy()
        auth_warning_shown = False
        
        while remaining and not self.should_stop:
            auth_failed = False
            completed_this_round = []
            
            with ThreadPoolExecutor(max_workers=self.config.parallel) as executor:
                futures = {
                    executor.submit(self.download_file, dl): dl
                    for dl in remaining
                }
                
                for future in as_completed(futures):
                    if self.should_stop:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    
                    progress = future.result()
                    
                    if progress.status == 'complete':
                        with self._lock:
                            self.stats.completed_files += 1
                        self.log(f"✓ {progress.filename} complete", 'success')
                        completed_this_round.append(progress)
                        
                        # Check auth expiry warning
                        if not auth_warning_shown:
                            is_warning, mins_remaining = self.check_auth_expiry()
                            if is_warning:
                                auth_warning_shown = True
                                self.log(f"⚠️ Auth may expire in ~{mins_remaining} minutes", 'warning')
                                self.notifier.send('auth_warning', 'Auth Expiring Soon',
                                    f'Session may expire in ~{mins_remaining} minutes')
                    
                    elif progress.status == 'failed':
                        if progress.is_auth_failure:
                            auth_failed = True
                            self.log(f"✗ {progress.filename}: {progress.error}", 'error')
                            # Cancel remaining
                            for f in futures:
                                f.cancel()
                            break
                        else:
                            with self._lock:
                                self.stats.failed_files += 1
                            self.log(f"✗ {progress.filename}: {progress.error}", 'error')
                            completed_this_round.append(progress)
                    
                    elif progress.status == 'paused':
                        self.log(f"⏸ {progress.filename} paused", 'info')
            
            # Remove completed from remaining
            remaining = [dl for dl in remaining if dl not in completed_this_round]
            
            if auth_failed:
                self.log("🔐 Authentication expired", 'error')
                self.notifier.send('auth_expired', 'Authentication Expired',
                    'Google session has expired. Please provide a new cookie.')
                self.notifier.play_sound('alert')
                
                if self.on_auth_expired:
                    new_cookie = self.on_auth_expired()
                    if new_cookie:
                        self.set_cookie(new_cookie)
                        auth_warning_shown = False
                        self.log("Cookie updated, resuming...", 'info')
                        # Rebuild remaining list (files not yet downloaded)
                        remaining = [dl for dl in downloads if not dl.output_path.exists()]
                        continue
                    else:
                        self.log("No new cookie provided, stopping", 'warning')
                        break
                else:
                    break
            else:
                # All done
                break
        
        self.is_running = False
        
        # Final notification
        if self.stats.completed_files > 0:
            self.notifier.send('complete', 'Downloads Complete',
                f'{self.stats.completed_files} files downloaded, {self.stats.failed_files} failed')
            self.notifier.play_sound('complete')
        
        return self.stats
    
    def stop(self):
        """Stop the download process."""
        self.should_stop = True
        self.log("Stopping downloads...", 'info')


class SpeedLimiter:
    """Limit download speed."""
    
    def __init__(self, limit_mbps: float = 0):
        self.limit_mbps = limit_mbps
        self._last_time = time.time()
        self._bytes_this_second = 0
        self._lock = threading.Lock()
    
    def limit(self, bytes_downloaded: int):
        """Apply speed limiting. Call after each chunk download."""
        if self.limit_mbps <= 0:
            return
        
        limit_bytes_per_second = self.limit_mbps * 1024 * 1024
        
        with self._lock:
            now = time.time()
            elapsed = now - self._last_time
            
            if elapsed >= 1.0:
                # Reset counter every second
                self._last_time = now
                self._bytes_this_second = bytes_downloaded
            else:
                self._bytes_this_second += bytes_downloaded
                
                # If we've exceeded the limit, sleep
                if self._bytes_this_second >= limit_bytes_per_second:
                    sleep_time = 1.0 - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    self._last_time = time.time()
                    self._bytes_this_second = 0


# =============================================================================
# CLI MODE
# =============================================================================

def run_cli():
    """Run in CLI mode."""
    import argparse
    
    load_env_file()
    
    parser = argparse.ArgumentParser(
        description='Google Takeout Bulk Downloader',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --cookie "YOUR_COOKIE" --url "https://..."
  %(prog)s --web                    # Start web interface
  %(prog)s --gui                    # Start desktop GUI
  %(prog)s --speed-limit 10         # Limit to 10 MB/s
  %(prog)s --auto-extract --organize # Extract and organize by type
        """
    )
    
    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--web', action='store_true', help='Start web interface')
    mode_group.add_argument('--gui', action='store_true', help='Start desktop GUI')
    
    # Basic options
    parser.add_argument('--cookie', default=os.environ.get('GOOGLE_COOKIE'),
                       help='Google cookie (or set GOOGLE_COOKIE env var)')
    parser.add_argument('--url', default=os.environ.get('TAKEOUT_URL'),
                       help='First download URL (or set TAKEOUT_URL env var)')
    parser.add_argument('--output', '-o', default=os.environ.get('OUTPUT_DIR', DEFAULT_OUTPUT_DIR),
                       help=f'Output directory (default: {DEFAULT_OUTPUT_DIR})')
    parser.add_argument('--count', '-n', type=int, 
                       default=int(os.environ.get('FILE_COUNT', str(DEFAULT_FILE_COUNT))),
                       help=f'Max files to download (default: {DEFAULT_FILE_COUNT})')
    parser.add_argument('--parallel', '-p', type=int,
                       default=int(os.environ.get('PARALLEL_DOWNLOADS', str(DEFAULT_PARALLEL))),
                       help=f'Parallel downloads (default: {DEFAULT_PARALLEL})')
    
    # Advanced options
    parser.add_argument('--speed-limit', type=float, default=0,
                       help='Speed limit in MB/s (0 = unlimited)')
    parser.add_argument('--no-resume', action='store_true',
                       help='Disable resume support')
    parser.add_argument('--no-verify', action='store_true',
                       help='Disable ZIP verification')
    
    # Extract options
    parser.add_argument('--auto-extract', action='store_true',
                       help='Auto-extract downloaded ZIPs')
    parser.add_argument('--extract-dir', help='Directory for extracted files')
    parser.add_argument('--organize', action='store_true',
                       help='Organize extracted files by type')
    parser.add_argument('--delete-after-extract', action='store_true',
                       help='Delete ZIP after extraction')
    
    # Notification options
    parser.add_argument('--webhook', help='Webhook URL for notifications')
    parser.add_argument('--email', help='Email address for notifications')
    parser.add_argument('--smtp-host', help='SMTP server host')
    parser.add_argument('--smtp-port', type=int, default=587, help='SMTP server port')
    parser.add_argument('--smtp-user', help='SMTP username')
    parser.add_argument('--smtp-password', help='SMTP password')
    
    # Web options
    parser.add_argument('--port', type=int, default=5000, help='Web server port')
    parser.add_argument('--host', default='0.0.0.0', help='Web server host')
    
    parser.add_argument('--version', action='version', version=f'%(prog)s {VERSION}')
    
    args = parser.parse_args()
    
    # Handle mode selection
    if args.web:
        run_web(args)
        return
    
    if args.gui:
        run_gui()
        return
    
    # CLI mode - validate required args
    if not args.cookie:
        parser.error('Cookie is required. Set --cookie or GOOGLE_COOKIE env var')
    
    # Try to extract URL from cookie if it's a cURL command
    if not args.url and args.cookie:
        extracted_url = extract_url_from_curl(args.cookie)
        if extracted_url:
            args.url = extracted_url
            print("✓ Auto-extracted URL from cURL command")
    
    if not args.url:
        parser.error('URL is required. Set --url or TAKEOUT_URL env var')
    
    # Extract actual cookie if full cURL was provided
    args.cookie = extract_cookie_from_curl(args.cookie)
    
    # Build config
    config = DownloadConfig(
        cookie=args.cookie,
        url=args.url,
        output_dir=args.output,
        file_count=args.count,
        parallel=args.parallel,
        speed_limit_mbps=args.speed_limit,
        resume_enabled=not args.no_resume,
        verify_zip=not args.no_verify,
        auto_extract=args.auto_extract,
        extract_dir=args.extract_dir,
        organize_by_type=args.organize,
        delete_after_extract=args.delete_after_extract,
        notifications=NotificationConfig(
            webhook_url=args.webhook,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            smtp_user=args.smtp_user,
            smtp_password=args.smtp_password,
            smtp_to=args.email,
        ),
    )
    
    # Progress display
    last_update = [0]
    
    def on_progress(progress: DownloadProgress):
        now = time.time()
        if now - last_update[0] >= 1:  # Update every second
            last_update[0] = now
            print(f"\r[{progress.filename}] {progress.percent:.1f}% ", end='', flush=True)
    
    def on_log(msg: str, level: str):
        print(f"\n[{level.upper()}] {msg}")
    
    def on_auth_expired() -> Optional[str]:
        """Prompt for new cookie."""
        print("\n" + "=" * 60)
        print("🔐 AUTHENTICATION EXPIRED")
        print("=" * 60)
        print("\nTo get a new cookie:")
        print("1. Open Chrome DevTools (F12) on Google Takeout")
        print("2. Go to Network tab")
        print("3. Click a download link")
        print("4. Right-click the request -> Copy -> Copy as cURL")
        print("\nPaste the cURL command below (or 'q' to quit):")
        print("-" * 60)
        
        try:
            lines = []
            while True:
                line = input()
                if line.strip().lower() == 'q':
                    return None
                lines.append(line)
                if not line.rstrip().endswith('\\'):
                    break
            
            full_text = ' '.join(lines)
            if not full_text.strip():
                return None
            
            cookie = extract_cookie_from_curl(full_text)
            if cookie:
                print(f"\n✓ Extracted cookie ({len(cookie)} chars)")
                return cookie
            else:
                print("\n✗ Couldn't extract cookie from input")
                return None
        except (EOFError, KeyboardInterrupt):
            return None
    
    # Create and run engine
    engine = DownloadEngine(
        config,
        on_progress=on_progress,
        on_log=on_log,
        on_auth_expired=on_auth_expired,
    )
    
    print(f"\nGoogle Takeout Downloader v{VERSION}")
    print(f"Output: {config.output_dir}")
    print(f"Parallel: {config.parallel}")
    if config.speed_limit_mbps > 0:
        print(f"Speed limit: {config.speed_limit_mbps} MB/s")
    print("-" * 60)
    
    try:
        stats = engine.run()
        print("\n" + "=" * 60)
        print(f"✅ Complete! {stats.completed_files} succeeded, {stats.failed_files} failed")
        print(f"Downloaded: {stats.bytes_downloaded / (1024*1024*1024):.2f} GB")
        print(f"Time: {stats.elapsed_seconds / 60:.1f} minutes")
        print(f"Average speed: {stats.speed_mbps:.1f} MB/s")
    except KeyboardInterrupt:
        print("\n\nInterrupted!")
        engine.stop()


# =============================================================================
# WEB MODE
# =============================================================================

def run_web(args):
    """Run web interface."""
    try:
        from flask import Flask, render_template_string, request, jsonify
        from flask_socketio import SocketIO, emit
    except ImportError:
        print("Web mode requires Flask and Flask-SocketIO.")
        print("Install with: pip install flask flask-socketio")
        sys.exit(1)
    
    # Import the web module
    from google_takeout_web import app, socketio
    
    print(f"\n🌐 Starting web interface on http://{args.host}:{args.port}")
    print("Open this URL in your browser to access the downloader.")
    print("-" * 60)
    
    socketio.run(app, host=args.host, port=args.port, debug=False)


# =============================================================================
# GUI MODE
# =============================================================================

def run_gui():
    """Run desktop GUI."""
    try:
        import tkinter as tk
    except ImportError:
        print("GUI mode requires tkinter.")
        print("On Ubuntu/Debian: sudo apt install python3-tk")
        print("On Fedora: sudo dnf install python3-tkinter")
        sys.exit(1)
    
    # Import and run the GUI module
    from google_takeout_gui import TakeoutDownloaderGUI
    
    root = tk.Tk()
    app = TakeoutDownloaderGUI(root)
    root.mainloop()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    run_cli()
