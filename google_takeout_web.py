#!/usr/bin/env python3
"""
Google Takeout Bulk Downloader - Web Version
A web interface for downloading Google Takeout archives.

This module can be run standalone or launched via: python takeout.py --web
"""

import os
import secrets
import sys
import threading
import time
from functools import wraps
from pathlib import Path

# Load .env file if present (for standalone use; Docker uses environment vars directly)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, rely on OS environment
from datetime import datetime
from typing import Optional

from flask import Flask, render_template_string, request, jsonify, Response
from flask_socketio import SocketIO, emit

# Import shared core
from takeout import (
    TakeoutDownloader, SizeHistory, DownloadStats,
    extract_url_parts, extract_cookie_from_curl, extract_url_from_curl,
    validate_output_dir, generate_secret_key,
    VERSION, CHUNK_SIZE, MAX_FILE_COUNT, MAX_RETRIES
)
import requests

# ============================================================================
# AUTHENTICATION
# ============================================================================

AUTH_USER = os.environ.get('AUTH_USER', '')
AUTH_PASS = os.environ.get('AUTH_PASS', '')

def check_auth():
    """Decorator that enforces HTTP Basic Auth when AUTH_USER and AUTH_PASS are set.
    If both env vars are empty/missing, allow unauthenticated access (local dev)."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if AUTH_USER and AUTH_PASS:
                auth = request.authorization
                if not auth or not (secrets.compare_digest(auth.username, AUTH_USER)
                                    and secrets.compare_digest(auth.password, AUTH_PASS)):
                    return Response(
                        'Authentication required', 401,
                        {'WWW-Authenticate': 'Basic realm="Google Takeout Downloader"'})
            return f(*args, **kwargs)
        return wrapper
    return decorator


def check_socketio_auth():
    """Check auth for SocketIO events. Returns True if auth passes or not required."""
    if not (AUTH_USER and AUTH_PASS):
        return True
    # Flask-SocketIO request context has the same request object
    auth = request.authorization
    if not auth or not (secrets.compare_digest(auth.username, AUTH_USER)
                        and secrets.compare_digest(auth.password, AUTH_PASS)):
        return False
    return True


# ============================================================================
# CORS CONFIGURATION
# ============================================================================

CORS_ORIGINS = [origin.strip() for origin in os.environ.get('CORS_ORIGINS', 'http://localhost:5000').split(',') if origin.strip()]

# ============================================================================
# RATE LIMITING
# ============================================================================

_rate_limit_store = {}  # endpoint -> last_request_time
_rate_limit_lock = threading.Lock()

def rate_limit(seconds: int):
    """Simple in-memory rate limiter: max 1 request per `seconds` per endpoint."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = f.__name__
            with _rate_limit_lock:
                last = _rate_limit_store.get(key, 0)
                now = time.time()
                if now - last < seconds:
                    return jsonify({'error': f'Rate limited. Try again in {seconds - int(now - last)}s.'}), 429
                _rate_limit_store[key] = now
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ============================================================================
# CONFIGURATION
# ============================================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', generate_secret_key())
socketio = SocketIO(app, cors_allowed_origins=CORS_ORIGINS, async_mode='threading')

# Default settings
DEFAULT_OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '/opt/takeout')
DEFAULT_PARALLEL = int(os.environ.get('PARALLEL_DOWNLOADS', '6'))
DEFAULT_FILE_COUNT = int(os.environ.get('FILE_COUNT', '100'))

# Download state
download_state = {
    'is_running': False,
    'should_stop': False,  # Flag to stop downloads on auth failure
    'cookie': '',
    'url': '',
    'output_dir': DEFAULT_OUTPUT_DIR,
    'parallel': DEFAULT_PARALLEL,
    'file_count': DEFAULT_FILE_COUNT,
    'files': [],
    'stats': {
        'total_files': 0,
        'completed_files': 0,
        'failed_files': 0,
        'skipped_files': 0,
        'bytes_downloaded': 0,
        'start_time': None,
    },
    'log': [],  # Preserve log messages for reconnecting clients
}
state_lock = threading.Lock()
MAX_LOG_ENTRIES = 500  # Limit log buffer size


# ============================================================================
# SANITIZATION HELPERS
# ============================================================================

def sanitize_state(state: dict) -> dict:
    """Return a copy of state dict with cookie removed for API responses."""
    sanitized = dict(state)
    sanitized.pop('cookie', None)
    if 'stats' in sanitized:
        sanitized['stats'] = dict(sanitized['stats'])
    return sanitized


# ============================================================================
# DOWNLOAD ENGINE - Uses shared core from takeout.py
# ============================================================================

def emit_status(event: str, data: dict):
    """Emit status update to all connected clients."""
    socketio.emit(event, data)

def add_log(message: str, log_type: str = 'info'):
    """Add a log entry to the buffer and emit to clients."""
    entry = {
        'time': datetime.now().strftime('%H:%M:%S'),
        'message': message,
        'type': log_type,
    }
    with state_lock:
        download_state['log'].append(entry)
        # Keep log buffer bounded
        if len(download_state['log']) > MAX_LOG_ENTRIES:
            download_state['log'] = download_state['log'][-MAX_LOG_ENTRIES:]
    socketio.emit('log_entry', entry)

def download_file(url: str, output_path: Path, file_index: int, cookie: str, size_history: SizeHistory) -> dict:
    """Download a single file with progress tracking and resume support."""
    filename = output_path.name
    result = {
        'index': file_index,
        'filename': filename,
        'success': False,
        'message': '',
        'auth_failed': False,
        'size': 0,
    }

    temp_path = output_path.with_suffix('.downloading')

    # Retry loop instead of recursion for 416 handling
    for _attempt in range(MAX_RETRIES):
        resume_from = 0

        # Check for existing partial download to resume
        if temp_path.exists():
            resume_from = temp_path.stat().st_size

        try:
            headers = {
                'Cookie': cookie,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }

            # Add Range header for resume
            if resume_from > 0:
                headers['Range'] = f'bytes={resume_from}-'
                add_log(f'Resuming {filename} from {resume_from/(1024*1024):.1f}MB', 'info')

            response = requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=(10, 300),
            )

            # Check for auth failure
            if response.status_code in (401, 403):
                result['message'] = 'Auth failed'
                result['auth_failed'] = True
                return result

            if 'accounts.google' in response.url:
                result['message'] = 'Auth failed - redirected to login'
                result['auth_failed'] = True
                return result

            # 416 = Range Not Satisfiable (file might be complete)
            if response.status_code == 416:
                if resume_from > 0:
                    # Verify with HEAD request
                    head_resp = requests.head(url, headers={'Cookie': cookie, 'User-Agent': headers['User-Agent']}, timeout=10)
                    if head_resp.status_code == 200:
                        expected_size = int(head_resp.headers.get('content-length', 0))
                        if expected_size > 0 and resume_from >= expected_size:
                            # Verify ZIP integrity before finalizing
                            try:
                                with open(temp_path, 'rb') as vf:
                                    vf.seek(max(0, resume_from - 1024))
                                    vtail = vf.read()
                                    if b'PK\x05\x06' not in vtail:
                                        temp_path.unlink(missing_ok=True)
                                        result['message'] = 'Integrity check failed on resumed file'
                                        return result
                            except OSError:
                                pass
                            temp_path.rename(output_path)
                            size_history.record_size(filename, resume_from)
                            result['success'] = True
                            result['message'] = 'Resumed complete'
                            result['size'] = resume_from
                            return result
                temp_path.unlink(missing_ok=True)
                # Retry without resume (loop iteration, not recursion)
                continue

            response.raise_for_status()

            content_type = response.headers.get('content-type', '')
            if 'text/html' in content_type:
                result['message'] = 'Auth failed - got HTML'
                result['auth_failed'] = True
                return result

            # Get total size - for 206 Partial Content, content-length is remaining bytes
            content_length = int(response.headers.get('content-length', 0))

            if response.status_code == 206:
                total_size = resume_from + content_length
            else:
                total_size = content_length
                if resume_from > 0:
                    # Server doesn't support resume, start fresh
                    resume_from = 0

            if total_size < 1000 and resume_from == 0:
                result['message'] = 'Auth failed - file too small'
                result['auth_failed'] = True
                return result

            output_path.parent.mkdir(parents=True, exist_ok=True)

            emit_status('file_start', {
                'index': file_index,
                'filename': filename,
                'size': total_size,
                'resumed_from': resume_from,
            })

            # Open file in append mode for resume, write mode for fresh
            file_mode = 'ab' if resume_from > 0 and response.status_code == 206 else 'wb'
            downloaded = resume_from
            last_emit_time = time.time()

            with open(temp_path, file_mode) as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        # Check first chunk for ZIP magic (only on fresh downloads)
                        if downloaded == 0 and chunk[:2] != b'PK':
                            temp_path.unlink()
                            result['message'] = 'Auth failed - not a ZIP'
                            result['auth_failed'] = True
                            return result

                        f.write(chunk)
                        downloaded += len(chunk)

                        with state_lock:
                            download_state['stats']['bytes_downloaded'] += len(chunk)

                        # Emit progress every 500ms
                        now = time.time()
                        if now - last_emit_time >= 0.5:
                            percent = int((downloaded / total_size) * 100) if total_size > 0 else 0
                            emit_status('file_progress', {
                                'index': file_index,
                                'filename': filename,
                                'downloaded': downloaded,
                                'total': total_size,
                                'percent': percent,
                            })
                            last_emit_time = now

            # Verify ZIP integrity before finalizing
            with open(temp_path, 'rb') as f:
                f.seek(max(0, downloaded - 1024))
                tail = f.read()
                if b'PK\x05\x06' not in tail:
                    temp_path.unlink(missing_ok=True)
                    result['message'] = 'Integrity check failed - not a valid ZIP'
                    return result

            # Rename to final
            temp_path.rename(output_path)
            size_history.record_size(filename, downloaded)

            result['success'] = True
            result['message'] = 'Complete'
            result['size'] = downloaded
            return result

        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 404:
                result['message'] = 'File not found (404)'
                return result
            # Keep partial file for resume
            result['message'] = f'HTTP error: {e}'
            return result
        except requests.exceptions.RequestException as e:
            # Keep partial file for resume
            result['message'] = f'Network error: {e}'
            return result

    # If we exhausted retries for 416
    result['message'] = 'Failed after retries (416)'
    return result


def run_downloads(cookie: str, url: str, output_dir: str, parallel: int, file_count: int):
    """Run the download process with parallel downloads."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    global download_state

    with state_lock:
        download_state['is_running'] = True
        download_state['should_stop'] = False
        download_state['cookie'] = cookie
        download_state['stats'] = {
            'total_files': file_count,
            'completed_files': 0,
            'failed_files': 0,
            'skipped_files': 0,
            'bytes_downloaded': 0,
            'start_time': datetime.now().isoformat(),
        }
        download_state['files'] = []
        download_state['log'] = []

    add_log(f'Starting downloads (parallel: {parallel})...', 'info')
    emit_status('download_started', {'message': 'Starting downloads...'})

    # Parse URL
    base_url, file_num, extension, query_string = extract_url_parts(url)

    if base_url is None:
        add_log('Invalid URL format', 'error')
        emit_status('error', {'message': 'Invalid URL format'})
        with state_lock:
            download_state['is_running'] = False
        return

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    size_history = SizeHistory(output_dir)

    add_log(f'Output: {output_dir}', 'info')
    add_log(f'URL pattern: {base_url}XXX{extension}', 'info')

    auth_failed = False

    while not auth_failed:
        with state_lock:
            if download_state['should_stop']:
                break
            current_cookie = download_state['cookie']

        # Build list of files to download
        to_download = []
        for num in range(1, file_count + 1):
            filename = f"{base_url.split('/')[-1]}{num:03d}{extension}"
            filepath = output_path / filename

            # Skip existing files
            if filepath.exists() and filepath.stat().st_size > 0:
                expected = size_history.get_expected_size(filename)
                if not expected or filepath.stat().st_size >= expected:
                    with state_lock:
                        download_state['stats']['skipped_files'] += 1
                    continue

            file_url = f"{base_url}{num:03d}{extension}"
            if query_string:
                file_url += f"?{query_string}"
            to_download.append((num, file_url, filepath, filename))

        if not to_download:
            add_log('All files already downloaded!', 'success')
            break

        add_log(f'Downloading {len(to_download)} files...', 'info')
        emit_status('stats_update', sanitize_state(download_state)['stats'])

        # Parallel downloads
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(download_file, url, path, idx, current_cookie, size_history): (idx, name)
                for idx, url, path, name in to_download
            }

            for future in as_completed(futures):
                with state_lock:
                    if download_state['should_stop']:
                        break

                idx, filename = futures[future]

                try:
                    result = future.result()

                    if result['success']:
                        add_log(f"✓ {filename} ({result['size']/(1024*1024):.1f} MB)", 'success')
                        with state_lock:
                            download_state['stats']['completed_files'] += 1

                    elif result['auth_failed']:
                        add_log(f"✗ {filename}: {result['message']}", 'error')
                        auth_failed = True
                        # Cancel remaining futures
                        for f in futures:
                            f.cancel()
                        break

                    elif 'not found' in result['message'].lower() or '404' in result['message']:
                        add_log(f"⊘ {filename}: not found", 'warning')

                    else:
                        add_log(f"✗ {filename}: {result['message']}", 'error')
                        with state_lock:
                            download_state['stats']['failed_files'] += 1

                except Exception as e:
                    add_log(f"✗ {filename}: {e}", 'error')
                    with state_lock:
                        download_state['stats']['failed_files'] += 1

                emit_status('stats_update', sanitize_state(download_state)['stats'])

        if auth_failed:
            add_log('⚠️ Authentication needed - provide new cURL', 'warning')
            emit_status('auth_required', {'message': 'Auth expired. Provide new cURL.'})
            with state_lock:
                download_state['is_running'] = False
            return

        # Done with this batch
        break

    with state_lock:
        stats = download_state['stats']
        add_log(f"🎉 Done! {stats['completed_files']} completed, {stats['skipped_files']} skipped, {stats['failed_files']} failed", 'success')
        emit_status('download_complete', {'message': 'Done!', 'stats': stats})
        download_state['is_running'] = False


# ============================================================================
# WEB ROUTES
# ============================================================================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Google Takeout Downloader</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        :root {
            --bg-dark: #1e1e2e;
            --bg-medium: #2d2d3f;
            --bg-light: #3d3d5c;
            --accent: #7c3aed;
            --accent-hover: #8b5cf6;
            --success: #22c55e;
            --warning: #f59e0b;
            --error: #ef4444;
            --text: #f8fafc;
            --text-dim: #94a3b8;
            --border: #4b5563;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: var(--bg-dark);
            color: var(--text);
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
        }

        header {
            text-align: center;
            margin-bottom: 30px;
        }

        h1 {
            font-size: 2rem;
            margin-bottom: 8px;
            background: linear-gradient(135deg, var(--accent), #a855f7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .subtitle {
            color: var(--text-dim);
            font-size: 0.95rem;
        }

        .card {
            background: var(--bg-medium);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            border: 1px solid var(--border);
        }

        .card h2 {
            font-size: 1.1rem;
            margin-bottom: 16px;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .form-group {
            margin-bottom: 16px;
        }

        label {
            display: block;
            margin-bottom: 6px;
            color: var(--text-dim);
            font-size: 0.9rem;
        }

        input, textarea {
            width: 100%;
            padding: 12px;
            background: var(--bg-dark);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text);
            font-size: 0.95rem;
            transition: border-color 0.2s;
        }

        input:focus, textarea:focus {
            outline: none;
            border-color: var(--accent);
        }

        textarea {
            min-height: 100px;
            resize: vertical;
            font-family: monospace;
        }

        .row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
        }

        button {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-primary {
            background: var(--accent);
            color: white;
        }

        .btn-primary:hover:not(:disabled) {
            background: var(--accent-hover);
            transform: translateY(-1px);
        }

        .btn-primary:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .btn-danger {
            background: var(--error);
            color: white;
        }

        .btn-danger:hover {
            background: #dc2626;
        }

        .btn-secondary {
            background: var(--bg-light);
            color: var(--text);
            border: 1px solid var(--border);
        }

        .btn-secondary:hover {
            background: var(--accent);
            color: white;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
        }

        .stat-box {
            background: var(--bg-dark);
            padding: 16px;
            border-radius: 8px;
            text-align: center;
        }

        .stat-value {
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--accent);
        }

        .stat-label {
            color: var(--text-dim);
            font-size: 0.85rem;
            margin-top: 4px;
        }

        .stat-box.success .stat-value { color: var(--success); }
        .stat-box.error .stat-value { color: var(--error); }
        .stat-box.warning .stat-value { color: var(--warning); }

        .log-container {
            background: var(--bg-dark);
            border-radius: 8px;
            padding: 16px;
            max-height: 400px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 0.85rem;
        }

        .log-entry {
            padding: 4px 0;
            border-bottom: 1px solid var(--border);
        }

        .log-entry:last-child {
            border-bottom: none;
        }

        .log-entry.success { color: var(--success); }
        .log-entry.error { color: var(--error); }
        .log-entry.info { color: var(--text-dim); }
        .log-entry.warning { color: var(--warning); }

        .file-list {
            max-height: 300px;
            overflow-y: auto;
        }

        .file-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 12px;
            background: var(--bg-dark);
            border-radius: 6px;
            margin-bottom: 8px;
        }

        .file-name {
            font-family: monospace;
            font-size: 0.9rem;
        }

        .file-status {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: 600;
        }

        .file-status.pending { background: var(--bg-light); color: var(--text-dim); }
        .file-status.downloading { background: var(--accent); color: white; }
        .file-status.complete { background: var(--success); color: white; }
        .file-status.failed { background: var(--error); color: white; }

        .progress-bar {
            width: 100%;
            height: 8px;
            background: var(--bg-dark);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 8px;
        }

        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--accent), #a855f7);
            transition: width 0.3s ease;
        }

        .hidden { display: none; }

        .alert {
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 16px;
        }

        .alert-error {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid var(--error);
            color: var(--error);
        }

        .alert-success {
            background: rgba(34, 197, 94, 0.2);
            border: 1px solid var(--success);
            color: var(--success);
        }

        .help-text {
            font-size: 0.85rem;
            color: var(--text-dim);
            margin-top: 6px;
        }

        /* Login form styles */
        .login-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.85);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }

        .login-card {
            background: var(--bg-medium);
            border-radius: 12px;
            padding: 32px;
            max-width: 380px;
            width: 90%;
            border: 1px solid var(--border);
        }

        .login-card h2 {
            margin-bottom: 20px;
            text-align: center;
        }

        .login-card .form-group {
            margin-bottom: 14px;
        }

        /* Update cookie modal */
        .modal-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1001;
        }

        .modal-card {
            background: var(--bg-medium);
            border-radius: 12px;
            padding: 28px;
            max-width: 500px;
            width: 90%;
            border: 1px solid var(--border);
        }

        .modal-card h2 {
            margin-bottom: 16px;
        }

        .modal-card .btn-row {
            display: flex;
            gap: 12px;
            margin-top: 16px;
        }

        /* Helper tools */
        .helper-section {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }

        .bookmarklet-code {
            background: var(--bg-dark);
            border-radius: 6px;
            padding: 12px;
            font-family: monospace;
            font-size: 0.8rem;
            word-break: break-all;
            max-height: 80px;
            overflow-y: auto;
            margin-top: 8px;
            display: none;
        }

        .bookmarklet-code.visible {
            display: block;
        }

        /* aria2c indicator */
        .aria2c-indicator {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 0.85rem;
            padding: 4px 10px;
            border-radius: 4px;
            background: var(--bg-dark);
        }

        .aria2c-indicator .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }

        .aria2c-indicator .dot.active { background: var(--success); }
        .aria2c-indicator .dot.inactive { background: var(--error); }

        @media (max-width: 600px) {
            body { padding: 12px; }
            h1 { font-size: 1.5rem; }
            .card { padding: 16px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Login Overlay (hidden unless auth required and not authenticated) -->
        <div class="login-overlay hidden" id="login-overlay">
            <div class="login-card">
                <h2>🔒 Authentication Required</h2>
                <div class="form-group">
                    <label for="login-user">Username</label>
                    <input type="text" id="login-user" autocomplete="username">
                </div>
                <div class="form-group">
                    <label for="login-pass">Password</label>
                    <input type="password" id="login-pass" autocomplete="current-password">
                </div>
                <button class="btn-primary" style="width:100%;" onclick="doLogin()">Login</button>
                <p id="login-error" style="color:var(--error);margin-top:12px;font-size:0.9rem;" class="hidden"></p>
            </div>
        </div>

        <!-- Update Cookie Modal (hidden by default) -->
        <div class="modal-overlay hidden" id="cookie-modal">
            <div class="modal-card">
                <h2>🔄 Update Cookie</h2>
                <p class="help-text">Paste a new cURL command to update the cookie without restarting the download.</p>
                <div class="form-group">
                    <label for="update-curl-input">New cURL Command or Cookie</label>
                    <textarea id="update-curl-input" placeholder="Paste the full cURL command from Chrome DevTools..."></textarea>
                </div>
                <div class="form-group">
                    <label for="update-url-input">Download URL (optional)</label>
                    <input type="text" id="update-url-input" placeholder="Leave blank to keep current URL">
                </div>
                <div class="btn-row">
                    <button class="btn-primary" onclick="submitCookieUpdate()">Update Cookie</button>
                    <button class="btn-secondary" onclick="closeCookieModal()">Cancel</button>
                </div>
                <p id="update-cookie-error" style="color:var(--error);margin-top:12px;font-size:0.9rem;" class="hidden"></p>
            </div>
        </div>

        <header>
            <h1>📦 Google Takeout Downloader</h1>
            <p class="subtitle">Web interface for headless environments</p>
        </header>

        <div id="alert-container"></div>

        <!-- Helper Tools Card -->
        <div class="card">
            <h2>🛠️ Helper Tools</h2>
            <div class="helper-section">
                <span class="aria2c-indicator" id="aria2c-status">
                    <span class="dot inactive" id="aria2c-dot"></span>
                    <span id="aria2c-text">aria2c: checking...</span>
                </span>
                <button class="btn-secondary" onclick="toggleBookmarklet()" style="font-size:0.85rem;padding:6px 14px;">🔖 Bookmarklet</button>
                <a href="/static/takeout-helper.user.js" target="_blank" style="color:var(--accent);font-size:0.85rem;">📝 Userscript</a>
            </div>
            <div class="bookmarklet-code" id="bookmarklet-code">
                javascript:void((function(){var%20c=prompt('Paste%20cURL%20command%20or%20cookie:');if(c){var%20u=window.location.href;fetch('/api/update-cookie',{method:'POST',headers:{'Content-Type':'application/json','Authorization':document.querySelector('meta[name="auth-token"]')?.content||''},body:JSON.stringify({curl_input:c,url:u})}).then(r=>r.json()).then(d=>alert(d.success?'Cookie%20updated!':d.error||'Failed')).catch(e=>alert('Error:'+e))}})())
            </div>
        </div>

        <!-- Configuration Card -->
        <div class="card" id="config-card">
            <h2>⚙️ Configuration</h2>

            <div class="form-group">
                <label for="curl-input">cURL Command or Cookie</label>
                <textarea id="curl-input" placeholder="Paste the full cURL command from Chrome DevTools Network tab → Copy → Copy as cURL (bash) or Copy as PowerShell"></textarea>
                <p class="help-text">Right-click a download request in DevTools Network tab → Copy → Copy as cURL (bash) or Copy as PowerShell</p>
            </div>

            <div class="form-group">
                <label for="url-input">Download URL (optional if using cURL)</label>
                <input type="text" id="url-input" placeholder="https://storage.cloud.google.com/takeout-...">
            </div>

            <div class="row">
                <div class="form-group">
                    <label for="output-dir">Output Directory</label>
                    <input type="text" id="output-dir" value="{{ output_dir }}">
                </div>
                <div class="form-group">
                    <label for="parallel">Parallel Downloads</label>
                    <input type="number" id="parallel" value="{{ parallel }}" min="1" max="20">
                </div>
                <div class="form-group">
                    <label for="file-count">Parts</label>
                    <input type="number" id="file-count" value="{{ file_count }}" min="1" max="1000">
                </div>
            </div>

            <button class="btn-primary" id="start-btn" onclick="startDownload()">
                🚀 Start Download
            </button>
            <button class="btn-danger hidden" id="stop-btn" onclick="stopDownload()">
                ⏹ Stop
            </button>
        </div>

        <!-- Progress Card -->
        <div class="card hidden" id="progress-card">
            <h2>📊 Download Progress</h2>

            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value" id="stat-total">0</div>
                    <div class="stat-label">Total Files</div>
                </div>
                <div class="stat-box success">
                    <div class="stat-value" id="stat-complete">0</div>
                    <div class="stat-label">Completed</div>
                </div>
                <div class="stat-box error">
                    <div class="stat-value" id="stat-failed">0</div>
                    <div class="stat-label">Failed</div>
                </div>
                <div class="stat-box warning">
                    <div class="stat-value" id="stat-skipped">0</div>
                    <div class="stat-label">Skipped</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" id="stat-size">0 GB</div>
                    <div class="stat-label">Downloaded</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" id="stat-speed">0 MB/s</div>
                    <div class="stat-label">Speed</div>
                </div>
            </div>

            <div class="progress-bar" style="margin-top: 20px;">
                <div class="progress-fill" id="overall-progress" style="width: 0%"></div>
            </div>
        </div>

        <!-- Files Card -->
        <div class="card hidden" id="files-card">
            <h2>📁 Files</h2>
            <div class="file-list" id="file-list"></div>
        </div>

        <!-- Log Card -->
        <div class="card">
            <h2>📝 Activity Log</h2>
            <div class="log-container" id="log-container">
                <div class="log-entry info">Ready to start...</div>
            </div>
        </div>
    </div>

    <script>
        // Auth state
        let authRequired = false;
        let authCredentials = null;  // {user, pass} for Basic Auth

        const socket = io();
        let downloadStartTime = null;
        let lastBytesDownloaded = 0;
        let lastSpeedUpdate = Date.now();

        // Check if auth is required on page load
        fetch('/api/auth-check')
            .then(res => {
                if (res.status === 401) {
                    authRequired = true;
                    document.getElementById('login-overlay').classList.remove('hidden');
                }
                return res.json();
            })
            .then(data => {
                if (data && data.authenticated) {
                    authRequired = false;
                }
            })
            .catch(() => {});

        // Check aria2c availability
        fetch('/api/aria2c-check')
            .then(res => res.json())
            .then(data => {
                const dot = document.getElementById('aria2c-dot');
                const txt = document.getElementById('aria2c-text');
                if (data.available) {
                    dot.className = 'dot active';
                    txt.textContent = 'aria2c: active';
                } else {
                    dot.className = 'dot inactive';
                    txt.textContent = 'aria2c: not available';
                }
            })
            .catch(() => {
                document.getElementById('aria2c-dot').className = 'dot inactive';
                document.getElementById('aria2c-text').textContent = 'aria2c: not available';
            });

        function getAuthHeader() {
            if (authCredentials) {
                return 'Basic ' + btoa(authCredentials.user + ':' + authCredentials.pass);
            }
            return null;
        }

        function authedFetch(url, options) {
            options = options || {};
            const hdr = getAuthHeader();
            if (hdr) {
                options.headers = options.headers || {};
                options.headers['Authorization'] = hdr;
            }
            return fetch(url, options);
        }

        function doLogin() {
            const user = document.getElementById('login-user').value.trim();
            const pass = document.getElementById('login-pass').value;
            if (!user || !pass) {
                const errEl = document.getElementById('login-error');
                errEl.textContent = 'Please enter both username and password.';
                errEl.classList.remove('hidden');
                return;
            }
            authCredentials = {user, pass};
            // Verify with server
            authedFetch('/api/auth-check')
                .then(res => {
                    if (res.status === 401) {
                        authCredentials = null;
                        const errEl = document.getElementById('login-error');
                        errEl.textContent = 'Invalid credentials.';
                        errEl.classList.remove('hidden');
                    } else {
                        return res.json();
                    }
                })
                .then(data => {
                    if (data && data.authenticated) {
                        authRequired = false;
                        document.getElementById('login-overlay').classList.add('hidden');
                    }
                })
                .catch(() => {
                    authCredentials = null;
                });
        }

        function log(message, type) {
            type = type || 'info';
            const container = document.getElementById('log-container');
            const entry = document.createElement('div');
            entry.className = 'log-entry ' + type;
            const time = new Date().toLocaleTimeString();
            entry.textContent = '[' + time + '] ' + message;
            container.appendChild(entry);
            container.scrollTop = container.scrollHeight;
        }

        function showAlert(message, type) {
            type = type || 'error';
            const container = document.getElementById('alert-container');
            const alertDiv = document.createElement('div');
            alertDiv.className = 'alert alert-' + type;
            alertDiv.textContent = message;
            container.appendChild(alertDiv);
            setTimeout(function() {
                if (alertDiv.parentNode) alertDiv.parentNode.removeChild(alertDiv);
            }, 5000);
        }

        function formatBytes(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
            return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
        }

        function startDownload() {
            var curlInput = document.getElementById('curl-input').value.trim();
            var urlInput = document.getElementById('url-input').value.trim();
            var outputDir = document.getElementById('output-dir').value.trim();
            var parallel = parseInt(document.getElementById('parallel').value);
            var fileCount = parseInt(document.getElementById('file-count').value);

            if (!curlInput && !urlInput) {
                showAlert('Please provide a cURL command or cookie and URL');
                return;
            }

            document.getElementById('start-btn').disabled = true;
            document.getElementById('progress-card').classList.remove('hidden');
            document.getElementById('files-card').classList.remove('hidden');

            downloadStartTime = Date.now();
            lastBytesDownloaded = 0;

            log('Starting download...', 'info');

            var headers = {'Content-Type': 'application/json'};
            var authHdr = getAuthHeader();
            if (authHdr) headers['Authorization'] = authHdr;

            fetch('/api/start', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify({
                    curl_input: curlInput,
                    url: urlInput,
                    output_dir: outputDir,
                    parallel: parallel,
                    file_count: fileCount,
                })
            })
            .then(function(res) {
                if (res.status === 401) {
                    authRequired = true;
                    document.getElementById('login-overlay').classList.remove('hidden');
                    document.getElementById('start-btn').disabled = false;
                    return {error: 'Authentication required'};
                }
                if (res.status === 429) {
                    return res.json();
                }
                return res.json();
            })
            .then(function(data) {
                if (data.error) {
                    showAlert(data.error);
                    document.getElementById('start-btn').disabled = false;
                }
            })
            .catch(function(err) {
                showAlert('Failed to start download: ' + err);
                document.getElementById('start-btn').disabled = false;
            });
        }

        function stopDownload() {
            var headers = {'Content-Type': 'application/json'};
            var authHdr = getAuthHeader();
            if (authHdr) headers['Authorization'] = authHdr;

            fetch('/api/stop', {
                method: 'POST',
                headers: headers,
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                log('Stop requested', 'warning');
                document.getElementById('stop-btn').classList.add('hidden');
            })
            .catch(function(err) {
                showAlert('Failed to stop: ' + err);
            });
        }

        // Update cookie modal
        function openCookieModal() {
            document.getElementById('cookie-modal').classList.remove('hidden');
            document.getElementById('update-cookie-error').classList.add('hidden');
        }

        function closeCookieModal() {
            document.getElementById('cookie-modal').classList.add('hidden');
        }

        function submitCookieUpdate() {
            var curlInput = document.getElementById('update-curl-input').value.trim();
            var urlInput = document.getElementById('update-url-input').value.trim();

            if (!curlInput) {
                var errEl = document.getElementById('update-cookie-error');
                errEl.textContent = 'Please provide a cURL command or cookie.';
                errEl.classList.remove('hidden');
                return;
            }

            var headers = {'Content-Type': 'application/json'};
            var authHdr = getAuthHeader();
            if (authHdr) headers['Authorization'] = authHdr;

            fetch('/api/update-cookie', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify({curl_input: curlInput, url: urlInput || ''})
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.success) {
                    log('Cookie updated successfully', 'success');
                    closeCookieModal();
                } else {
                    var errEl = document.getElementById('update-cookie-error');
                    errEl.textContent = data.error || 'Failed to update cookie';
                    errEl.classList.remove('hidden');
                }
            })
            .catch(function(err) {
                var errEl = document.getElementById('update-cookie-error');
                errEl.textContent = 'Error: ' + err;
                errEl.classList.remove('hidden');
            });
        }

        // Bookmarklet toggle
        function toggleBookmarklet() {
            var codeEl = document.getElementById('bookmarklet-code');
            codeEl.classList.toggle('visible');
        }

        // Socket event handlers

        // Handle state restoration on reconnect/refresh
        socket.on('restore_state', function(state) {
            // Show progress cards if there's an active or completed session
            if (state.is_running || state.stats.total_files > 0) {
                document.getElementById('progress-card').classList.remove('hidden');
                document.getElementById('files-card').classList.remove('hidden');
                document.getElementById('start-btn').disabled = state.is_running;
            }

            // Restore stats
            document.getElementById('stat-total').textContent = state.stats.total_files;
            document.getElementById('stat-complete').textContent = state.stats.completed_files;
            document.getElementById('stat-failed').textContent = state.stats.failed_files;
            document.getElementById('stat-skipped').textContent = state.stats.skipped_files;
            document.getElementById('stat-size').textContent = formatBytes(state.stats.bytes_downloaded);
            lastBytesDownloaded = state.stats.bytes_downloaded;

            // Update progress bar (include skipped in progress)
            var total = state.stats.total_files || 1;
            var completed = state.stats.completed_files + state.stats.failed_files + state.stats.skipped_files;
            var percent = Math.min((completed / total) * 100, 100);
            document.getElementById('overall-progress').style.width = percent + '%';

            // Restore file list - SAFE: use createElement + textContent instead of innerHTML
            var fileList = document.getElementById('file-list');
            fileList.textContent = '';
            state.files.forEach(function(file, index) {
                updateFileStatus(index, file.filename, file.status);
            });

            // Restore log - SAFE: use createElement + textContent
            var logContainer = document.getElementById('log-container');
            logContainer.textContent = '';
            state.log.forEach(function(entry) {
                var div = document.createElement('div');
                div.className = 'log-entry ' + entry.type;
                div.textContent = '[' + entry.time + '] ' + entry.message;
                logContainer.appendChild(div);
            });
            logContainer.scrollTop = logContainer.scrollHeight;

            if (state.is_running) {
                log('Reconnected to active download session', 'info');
            }
        });

        // Handle individual log entries
        socket.on('log_entry', function(entry) {
            var container = document.getElementById('log-container');
            var div = document.createElement('div');
            div.className = 'log-entry ' + entry.type;
            div.textContent = '[' + entry.time + '] ' + entry.message;
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
        });

        socket.on('download_started', function(data) {
            log(data.message, 'info');
        });

        socket.on('download_info', function(data) {
            document.getElementById('stat-total').textContent = data.total;
            document.getElementById('stat-skipped').textContent = data.skipped;
            log('Found ' + data.total + ' files to download (' + data.skipped + ' skipped)', 'info');
        });

        socket.on('file_start', function(data) {
            log('Starting: ' + data.filename + ' (' + formatBytes(data.size) + ')', 'info');
            updateFileStatus(data.index, data.filename, 'downloading');
        });

        socket.on('file_progress', function(data) {
            updateFileProgress(data.index, data.percent);
        });

        socket.on('file_complete', function(data) {
            if (data.success) {
                log('✓ ' + data.filename + ' complete (' + formatBytes(data.size) + ')', 'success');
                updateFileStatus(data.index, data.filename, 'complete');
            } else {
                log('✗ ' + data.filename + ': ' + data.message, 'error');
                updateFileStatus(data.index, data.filename, 'failed');
            }
        });

        socket.on('stats_update', function(stats) {
            document.getElementById('stat-complete').textContent = stats.completed_files;
            document.getElementById('stat-failed').textContent = stats.failed_files;
            document.getElementById('stat-skipped').textContent = stats.skipped_files;
            document.getElementById('stat-size').textContent = formatBytes(stats.bytes_downloaded);

            // Calculate speed - update every 0.5 seconds for faster feedback
            var now = Date.now();
            var elapsed = (now - lastSpeedUpdate) / 1000;
            if (elapsed >= 0.5) {
                var bytesDelta = stats.bytes_downloaded - lastBytesDownloaded;
                var speed = bytesDelta / elapsed;
                if (speed > 0) {
                    document.getElementById('stat-speed').textContent = formatBytes(speed) + '/s';
                }
                lastBytesDownloaded = stats.bytes_downloaded;
                lastSpeedUpdate = now;
            }

            // Update progress bar
            var total = parseInt(document.getElementById('stat-total').textContent) || 1;
            var completed = stats.completed_files + stats.failed_files + stats.skipped_files;
            var percent = (completed / total) * 100;
            document.getElementById('overall-progress').style.width = percent + '%';
        });

        socket.on('auth_required', function(data) {
            log('⚠️ ' + data.message, 'warning');
            showAlert(data.message + ' Update cookie or restart.', 'error');
            document.getElementById('start-btn').disabled = false;
            // Show update cookie button
            openCookieModal();
        });

        socket.on('download_complete', function(data) {
            log('🎉 ' + data.message, 'success');
            showAlert(data.message, 'success');
            document.getElementById('start-btn').disabled = false;
        });

        socket.on('error', function(data) {
            log('Error: ' + data.message, 'error');
            showAlert(data.message);
            document.getElementById('start-btn').disabled = false;
        });

        // SAFE: updateFileStatus uses createElement + textContent instead of innerHTML
        function updateFileStatus(index, filename, status) {
            var list = document.getElementById('file-list');
            var item = document.getElementById('file-' + index);

            if (!item) {
                item = document.createElement('div');
                item.id = 'file-' + index;
                item.className = 'file-item';

                var nameSpan = document.createElement('span');
                nameSpan.className = 'file-name';
                nameSpan.textContent = filename;

                var statusSpan = document.createElement('span');
                statusSpan.className = 'file-status ' + status;
                statusSpan.textContent = status;

                item.appendChild(nameSpan);
                item.appendChild(statusSpan);
                list.appendChild(item);
            } else {
                var statusEl = item.querySelector('.file-status');
                statusEl.className = 'file-status ' + status;
                statusEl.textContent = status;
            }
        }

        function updateFileProgress(index, percent) {
            var item = document.getElementById('file-' + index);
            if (item) {
                var statusEl = item.querySelector('.file-status');
                statusEl.textContent = percent + '%';
            }
        }

        // Fetch state on page load (backup in case socket is slow)
        window.addEventListener('load', function() {
            authedFetch('/api/status')
                .then(function(res) { return res.json(); })
                .then(function(state) {
                    if (state.is_running || state.stats.total_files > 0) {
                        // Trigger restore via the same handler
                        socket.emit('request_state');
                    }
                })
                .catch(function(err) { console.log('Could not fetch initial state:', err); });
        });
    </script>
</body>
</html>
'''


# ============================================================================
# CONTENT SECURITY POLICY MIDDLEWARE
# ============================================================================

@app.after_request
def add_csp_headers(response):
    """Add Content-Security-Policy header to prevent inline script injection from external sources."""
    # Allow inline scripts (our template uses them) but restrict everything else
    # We allow 'self' for most things, specific CDN for socket.io, and inline scripts/styles
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' wss: ws:; "
        "img-src 'self'; "
        "font-src 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response


# ============================================================================
# ROUTES
# ============================================================================

@app.route('/')
@check_auth()
def index():
    return render_template_string(
        HTML_TEMPLATE,
        output_dir=DEFAULT_OUTPUT_DIR,
        parallel=DEFAULT_PARALLEL,
        file_count=DEFAULT_FILE_COUNT,
    )


@app.route('/api/auth-check')
@check_auth()
def api_auth_check():
    """Check if authentication is configured and credentials are valid.
    Returns {authenticated: true} if auth is configured and valid.
    If auth is not configured, always returns {authenticated: true}.
    If auth is configured but invalid, returns 401."""
    return jsonify({'authenticated': True})


@app.route('/api/aria2c-check')
@check_auth()
def api_aria2c_check():
    """Check if aria2c is available on the system."""
    import shutil
    available = shutil.which('aria2c') is not None
    return jsonify({'available': available})


@app.route('/api/start', methods=['POST'])
@check_auth()
@rate_limit(5)
def api_start():
    data = request.json

    with state_lock:
        if download_state['is_running']:
            return jsonify({'error': 'Download already in progress'})

    curl_input = data.get('curl_input', '')
    url = data.get('url', '')
    output_dir = data.get('output_dir', DEFAULT_OUTPUT_DIR)
    parallel = data.get('parallel', DEFAULT_PARALLEL)
    file_count = data.get('file_count', DEFAULT_FILE_COUNT)

    # Clamp values to safe ranges
    parallel = max(1, min(int(parallel), 20))
    file_count = max(1, min(int(file_count), MAX_FILE_COUNT))

    # Validate output directory (path traversal protection)
    try:
        validate_output_dir(output_dir)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Extract cookie from cURL
    cookie = extract_cookie_from_curl(curl_input) if curl_input else ''

    if not cookie:
        return jsonify({'error': 'Could not extract cookie from input'})

    # Try to extract URL from cURL if not provided
    if not url and curl_input:
        url = extract_url_from_curl(curl_input)

    if not url:
        return jsonify({'error': 'No download URL provided'})

    # Start download in background thread
    thread = threading.Thread(
        target=run_downloads,
        args=(cookie, url, output_dir, parallel, file_count),
        daemon=True
    )
    thread.start()

    return jsonify({'status': 'started'})


@app.route('/api/stop', methods=['POST'])
@check_auth()
def api_stop():
    """Stop the current download."""
    with state_lock:
        if download_state['is_running']:
            download_state['should_stop'] = True
            return jsonify({'status': 'stopping'})
        return jsonify({'status': 'not running'})


@app.route('/api/update-cookie', methods=['POST'])
@check_auth()
def api_update_cookie():
    """Update the cookie mid-download without restarting.
    Accepts {curl_input, url} and updates the cookie in the running state."""
    data = request.json
    curl_input = data.get('curl_input', '')
    url = data.get('url', '')

    if not curl_input:
        return jsonify({'error': 'No curl_input provided'}), 400

    new_cookie = extract_cookie_from_curl(curl_input) if curl_input else ''

    if not new_cookie:
        return jsonify({'error': 'Could not extract cookie from input'}), 400

    with state_lock:
        download_state['cookie'] = new_cookie
        if url:
            download_state['url'] = url
        # If stopped due to auth failure, allow resuming
        download_state['should_stop'] = False

    add_log('Cookie updated via API', 'success')
    emit_status('cookie_updated', {'message': 'Cookie updated'})

    # If download was stopped due to auth failure and is not running, restart it
    with state_lock:
        if not download_state['is_running'] and download_state['stats'].get('total_files', 0) > 0:
            # Restart with current settings
            cookie = download_state['cookie']
            dl_url = download_state['url']
            output_dir = download_state['output_dir']
            parallel = download_state['parallel']
            file_count = download_state['file_count']

            thread = threading.Thread(
                target=run_downloads,
                args=(cookie, dl_url, output_dir, parallel, file_count),
                daemon=True
            )
            thread.start()
            add_log('Download restarted with new cookie', 'info')

    return jsonify({'success': True})


@app.route('/api/status')
@check_auth()
def api_status():
    """Get full current state for page refresh/reconnection.
    Cookie is stripped from response to prevent credential leaking."""
    with state_lock:
        sanitized = sanitize_state(download_state)
    return jsonify({
        'is_running': sanitized['is_running'],
        'stats': sanitized['stats'],
        'files': sanitized['files'],
        'log': sanitized['log'],
    })


# ============================================================================
# SOCKETIO EVENT HANDLERS
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Send current state to newly connected/reconnected clients.
    Requires auth if configured."""
    if not check_socketio_auth():
        # Disconnect unauthenticated clients
        emit('auth_required', {'message': 'Authentication required for WebSocket'})
        return False  # Returning False disconnects the client

    with state_lock:
        if download_state['is_running'] or download_state['stats']['total_files'] > 0:
            # Send current state to the reconnecting client (cookie stripped)
            sanitized = sanitize_state(download_state)
            emit('restore_state', {
                'is_running': sanitized['is_running'],
                'stats': sanitized['stats'],
                'files': sanitized['files'],
                'log': sanitized['log'],
            })


@socketio.on('request_state')
def handle_request_state():
    """Handle explicit state request from client.
    Requires auth if configured."""
    if not check_socketio_auth():
        emit('auth_required', {'message': 'Authentication required'})
        return

    with state_lock:
        sanitized = sanitize_state(download_state)
        emit('restore_state', {
            'is_running': sanitized['is_running'],
            'stats': sanitized['stats'],
            'files': sanitized['files'],
            'log': sanitized['log'],
        })


# ============================================================================
# APP FACTORY
# ============================================================================

def create_app():
    """Factory function to create the Flask app and SocketIO instance."""
    return app, socketio


# ============================================================================
# MAIN (for standalone use)
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description='Google Takeout Downloader - Web Interface')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=5000, help='Port to bind to (default: 5000)')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')

    args = parser.parse_args()

    # Security warnings
    if not (AUTH_USER and AUTH_PASS):
        print("\n⚠️  WARNING: AUTH_USER and AUTH_PASS not set. Running without authentication!")
        print("   Set both environment variables to enable HTTP Basic Auth.\n")

    if not os.environ.get('SECRET_KEY'):
        print("⚠️  WARNING: SECRET_KEY not set. Using auto-generated key (changes on restart).")
        print("   Set SECRET_KEY environment variable for persistent sessions.\n")

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         Google Takeout Downloader - Web Interface            ║
╠══════════════════════════════════════════════════════════════╣
║  Open your browser to: http://{args.host}:{args.port:<5}                    ║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
""")

    # The development werkzeug server requires allow_unsafe_werkzeug=True.
    # For production, use gunicorn with gevent/eventlet workers instead.
    # Docker container uses the development server for simplicity.
    socketio.run(app, host=args.host, port=args.port, debug=args.debug, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
