#!/usr/bin/env python3
"""
Google Takeout Downloader — server-side CLI
=============================================

A self-contained Python CLI that accepts a JSON payload from the browser
extension (cookies, headers, download URL) and downloads all export files
concurrently with an internal threaded downloader, resume, and ZIP validation.

Designed for remote servers over SSH/tmux where a mouse-grabbing TUI is
not usable. Plain prompts, plain ANSI progress display, paste-friendly.

URL semantics (confirmed empirically 2026-06-14):

  https://takeout-download.usercontent.google.com/download/<FILENAME>
      ?j=<JOB>&i=<EXPORT_INDEX>&user=<UID>&authuser=<AUTHUSER>

  - `i` selects the export/file, NOT a part within a file.
  - The `-NNN` filename component is cosmetic; the server returns the
    same file for any filename when `i` is held constant.
  - The authoritative filename is in the `Content-Disposition` header.
  - Discovery sweeps `i = 0, 1, 2, ...` until the server returns an
    invalid response (e.g. HTTP 500), then downloads every valid export.

Usage:

  python takeout_dl.py

  Or via Docker:

  docker build -t takeout-dl .
  docker run --rm -it -v /srv/storage/google-takeout/my-takeout:/downloads takeout-dl

Environment variables:

  OUTPUT_DIR          default output directory
  PARALLEL_DOWNLOADS  concurrent downloads (default 4)
  MAX_EXPORTS         safety cap on export discovery (default 50)
  MAX_AUTH_REPROMPTS  how many times to re-prompt for a fresh cookie
                      before giving up (default 5)
  NO_COLOR            disable ANSI colors
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse, urlencode, urlunparse

import requests

# -----------------------------------------------------------------------------
# UTF-8 on Windows so banner chars and progress bars render.
# -----------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = "/srv/storage/google-takeout/my-takeout"
DEFAULT_PARALLEL = int(os.environ.get("PARALLEL_DOWNLOADS", "4"))
DEFAULT_MAX_EXPORTS = int(os.environ.get("MAX_EXPORTS", "50"))
DEFAULT_MAX_AUTH_REPROMPTS = int(os.environ.get("MAX_AUTH_REPROMPTS", "5"))

ALLOWED_DIR_PREFIXES = [
    str(Path.home()),
    str(Path.cwd()),
    "/opt",
    "/downloads",
    "/tmp",
]
for extra in (os.environ.get("ALLOWED_DIRS") or "").split(os.pathsep):
    if extra.strip():
        ALLOWED_DIR_PREFIXES.append(Path(extra.strip()).resolve().as_posix())

REQUIRED_COOKIE_MARKERS = (
    "__Secure-1PSID",
    "__Secure-3PSID",
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
)

# -----------------------------------------------------------------------------
# Terminal helpers
# -----------------------------------------------------------------------------
_IS_TTY = sys.stdout.isatty()
_USE_COLOR = _IS_TTY and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def bold(text: str) -> str:
    return _c("1", text)


def dim(text: str) -> str:
    return _c("2", text)


def green(text: str) -> str:
    return _c("32", text)


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def cyan(text: str) -> str:
    return _c("36", text)


def clear_screen_region(rows: int) -> None:
    if not _IS_TTY:
        return
    out = ["\x1b[s"]  # save cursor
    for _ in range(rows + 2):
        out.append("\x1b[1A\x1b[K")
    out.append("\x1b[u")  # restore cursor
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def human_size(n: int) -> str:
    if n < 0:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if isinstance(n, float) else f"{n} {unit}"
        n /= 1024.0
    return f"{n:.1f} PiB"


def human_speed(bps: float) -> str:
    return f"{human_size(int(bps))}/s"


def progress_bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    filled = max(0, min(width, filled))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# -----------------------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------------------
log = logging.getLogger("takeout_dl")


def setup_logging(log_path: Optional[Path] = None, level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
        log.setLevel(level)


# -----------------------------------------------------------------------------
# Payload parsing
# -----------------------------------------------------------------------------
@dataclass
class ExportEntry:
    url: str
    filename: str
    size: int
    part_index: int = 0


@dataclass
class Payload:
    url: str
    cookie: str
    headers: dict = field(default_factory=dict)
    method: str = "GET"
    source: str = "extension"
    captured_at: str = ""
    exports: list[ExportEntry] = field(default_factory=list)


def parse_payload(text: str) -> Payload:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty payload")
    if not text.startswith("{"):
        raise ValueError("Payload must be JSON from the extension (Copy as JSON)")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")

    cookie = data.get("cookie") or ""
    url = data.get("url") or ""

    # Multi-payload: URLs live inside the exports array.
    exports_raw = data.get("exports") if data.get("multi") else None
    if not url and isinstance(exports_raw, list) and exports_raw:
        url = exports_raw[0].get("url", "")

    if not url or not cookie:
        raise ValueError("Payload missing url or cookie")

    # Normalize headers: keep only the ones Google actually validates.
    headers = {}
    for k, v in (data.get("headers") or {}).items():
        kl = k.lower()
        if kl == "cookie":
            continue
        if kl in ("user-agent", "accept", "accept-language", "referer", "origin"):
            headers[k] = v

    # Fallback defaults if the extension didn't capture them.
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    headers.setdefault("Accept", "*/*")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Referer", "https://takeout.google.com/")

    # Multi-payload: extension has already enumerated all part URLs.
    exports = []
    if data.get("multi") and isinstance(data.get("exports"), list):
        for idx, entry in enumerate(data["exports"]):
            exports.append(ExportEntry(
                url=entry.get("url", ""),
                filename=entry.get("filename") or f"part-{idx:03d}",
                size=int(entry.get("size") or 0),
                part_index=int(entry.get("partIndex", idx)),
            ))

    return Payload(
        url=url,
        cookie=cookie,
        headers=headers,
        method=data.get("method", "GET"),
        source=data.get("source", "extension"),
        captured_at=data.get("captured_at") or datetime.now(timezone.utc).isoformat(),
        exports=exports,
    )


def validate_cookie(cookie: str) -> tuple[bool, str]:
    if not any(marker in cookie for marker in REQUIRED_COOKIE_MARKERS):
        return False, (
            "Cookie is missing Google session markers (e.g. __Secure-1PSID, SID). "
            "Re-capture from a running download on takeout.google.com."
        )
    return True, ""


# -----------------------------------------------------------------------------
# Output directory safety
# -----------------------------------------------------------------------------
def validate_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    rp = resolved.as_posix()
    allowed = [Path(p).resolve().as_posix() for p in ALLOWED_DIR_PREFIXES]
    if not any(rp.startswith(a) for a in allowed):
        raise ValueError(
            f"Output directory {path} is not under an allowed prefix. "
            f"Set ALLOWED_DIRS or use one of: {', '.join(ALLOWED_DIR_PREFIXES[:5])}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


# -----------------------------------------------------------------------------
# URL handling
# -----------------------------------------------------------------------------
def make_export_url(base_url: str, export_index: int) -> str:
    """Return a URL with `i` set to export_index, keeping everything else."""
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["i"] = [str(export_index)]
    # Keep the first value for everything else.
    new_query = urlencode({k: v[0] for k, v in qs.items()}, doseq=False)
    return urlunparse(parsed._replace(query=new_query))


# -----------------------------------------------------------------------------
# Auth + discovery
# -----------------------------------------------------------------------------
class AuthError(Exception):
    pass


def probe_export(url: str, headers: dict, cookie: str, timeout: tuple = (10, 30)) -> tuple[int, int, str]:
    """
    Make a 1-byte Range probe. Returns (status, total_size, content_disposition_filename).
    Raises AuthError if the cookie has expired or the server redirects to signin.
    """
    h = dict(headers)
    h["Cookie"] = cookie
    h["Range"] = "bytes=0-0"
    resp = requests.get(url, headers=h, stream=True, timeout=timeout, allow_redirects=True)
    try:
        final_host = resp.url.split("://")[1].split("/")[0] if "://" in resp.url else ""
        ctype = resp.headers.get("content-type", "")
        if final_host.endswith("accounts.google.com") or "accounts.google.com" in resp.url:
            raise AuthError(f"Redirected to Google sign-in ({final_host})")
        if "text/html" in ctype:
            raise AuthError(f"Server returned HTML (content-type={ctype})")
        if resp.status_code in (401, 403):
            raise AuthError(f"HTTP {resp.status_code}")

        cd = resp.headers.get("content-disposition", "")
        filename = ""
        m = re.search(r'filename="?([^"\;]+)"?', cd)
        if m:
            filename = m.group(1).strip().strip('"')

        total = 0
        cr = resp.headers.get("content-range", "")
        mm = re.search(r"/(\d+)\s*$", cr)
        if mm:
            total = int(mm.group(1))
        elif resp.status_code == 200:
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit():
                total = int(cl)

        return resp.status_code, total, filename
    finally:
        resp.close()


@dataclass
class Export:
    index: int
    url: str
    size: int
    filename: str
    downloaded: bool = False
    verified: bool = False


def discover_exports(payload: Payload, max_exports: int = DEFAULT_MAX_EXPORTS) -> list[Export]:
    """Use provided exports if present, otherwise sweep export indices."""
    if payload.exports:
        exports = []
        for e in payload.exports[:max_exports]:
            # Extension-provided filenames are often truncated/identical for all parts;
            # build a unique output name from the URL basename plus part index.
            from urllib.parse import urlparse
            base = urlparse(e.url).path.split("/")[-1] or "takeout.zip"
            if "." in base:
                stem, ext = base.rsplit(".", 1)
                unique_name = f"{stem}-part-{e.part_index:03d}.{ext}"
            else:
                unique_name = f"{base}-part-{e.part_index:03d}"
            exports.append(Export(index=e.part_index, url=e.url, size=e.size, filename=unique_name))
        total_size = sum(e.size for e in exports)
        print(cyan(f"Using {len(exports)} export URL(s) from extension payload, {human_size(total_size)} total"))
        return exports

    """Sweep export indices until two consecutive invalid responses."""
    exports: list[Export] = []
    consecutive_invalid = 0
    base_url = payload.url

    log.info("Discovering exports by sweeping i=0,1,2,... (keeping captured filename fixed)")
    print(cyan("Discovering exports..."))

    for i in range(0, max_exports):
        url = make_export_url(base_url, i)
        try:
            status, size, filename = probe_export(url, payload.headers, payload.cookie)
        except AuthError as e:
            log.error("Auth failed while discovering export i=%d: %s", i, e)
            raise
        except requests.RequestException as e:
            log.warning("Network error probing i=%d: %s", i, e)
            status = 0
            size = 0
            filename = ""

        if status in (200, 206) and size > 0 and filename:
            consecutive_invalid = 0
            exports.append(Export(index=i, url=url, size=size, filename=filename))
            print(f"  {green('✓')} i={i:02d}  {human_size(size):>10}  {filename}")
        else:
            consecutive_invalid += 1
            log.debug("i=%d invalid (status=%s size=%s filename=%s)", i, status, size, filename)
            if consecutive_invalid >= 2:
                log.info("Stopping discovery after 2 consecutive invalid exports at i=%d", i)
                break

    if not exports:
        raise RuntimeError("No downloadable exports discovered. Cookie may be expired or export is empty.")

    total_size = sum(e.size for e in exports)
    print(f"\n{green('Found')} {len(exports)} export(s), {human_size(total_size)} total\n")
    return exports


# -----------------------------------------------------------------------------
# aria2c RPC manager
# -----------------------------------------------------------------------------
class Aria2cManager:
    def __init__(self, port: Optional[int] = None, secret: Optional[str] = None,
                 download_dir: Path = Path("/downloads")):
        self.port = port or self._free_port()
        self.secret = secret or self._make_secret()
        self.download_dir = download_dir
        self.rpc_url = f"http://localhost:{self.port}/jsonrpc"
        self.proc: Optional[subprocess.Popen] = None
        self.gid_to_export: dict[str, Export] = {}
        self._seen_names: set[str] = set()

    @staticmethod
    def _free_port() -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _make_secret() -> str:
        return os.urandom(16).hex()

    def start(self) -> None:
        if shutil.which("aria2c") is None:
            raise RuntimeError("aria2c binary not found on PATH")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "aria2c",
            "--enable-rpc",
            f"--rpc-listen-port={self.port}",
            "--rpc-allow-origin-all",
            "--rpc-listen-all=false",
            f"--rpc-secret={self.secret}",
            "--daemon=false",
            "--disable-ipv6=false",
            "--file-allocation=none",
            "--max-concurrent-downloads=50",
            "--log-level=warn",
            f"--dir={self.download_dir}",
        ]
        log.info("Starting aria2c RPC on port %d", self.port)
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait for RPC to come up.
        for _ in range(30):
            if self._call("aria2.getVersion", []) is not None:
                return
            if self.proc.poll() is not None:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"aria2c exited early (code {self.proc.returncode}): {err}")
            time.sleep(0.2)
        raise RuntimeError("aria2c RPC did not start")

    def _call(self, method: str, params: list) -> Optional[dict]:
        payload = {
            "jsonrpc": "2.0",
            "id": f"tk-{random.randint(1, 1_000_000)}",
            "method": method,
            "params": [f"token:{self.secret}"] + params,
        }
        try:
            resp = requests.post(self.rpc_url, json=payload, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                log.debug("aria2c RPC error: %s", data["error"])
                return None
            return data.get("result")
        except requests.RequestException as e:
            log.debug("aria2c RPC call failed: %s", e)
            return None

    def add_download(self, export: Export, payload: Payload, out_filename: Optional[str] = None) -> Optional[str]:
        def safe_name(name):
            bad = "…"  # horizontal ellipsis
            n = name.replace(bad, "")
            n = n.replace("%20", " ")
            n = n.strip()
            if not n:
                n = f"takeout-part-{export.index:03d}"
            candidate = n
            if candidate in self._seen_names:
                stem, ext = (candidate.rsplit(".", 1) if "." in candidate else (candidate, ""))
                candidate = f"{stem}-{export.index:03d}.{ext}" if ext else f"{stem}-{export.index:03d}"
            self._seen_names.add(candidate)
            return candidate
        options = {
            "dir": str(self.download_dir),
            "out": safe_name(out_filename or export.filename),
            "header": [f"Cookie: {payload.cookie}"],
            "allow-overwrite": "false",
            "auto-file-renaming": "false",
            "continue": "true",
            "max-connection-per-server": "1",
            "split": "1",
            "max-tries": "10",
            "retry-wait": "10",
            "timeout": "60",
            "user-agent": payload.headers.get("User-Agent", ""),
        }
        for k, v in payload.headers.items():
            kl = k.lower()
            if kl == "cookie":
                continue
            if kl == "user-agent":
                options["user-agent"] = v
            else:
                options["header"].append(f"{k}: {v}")

        gid = self._call("aria2.addUri", [[export.url], options])
        if gid:
            self.gid_to_export[gid] = export
        return gid

    def tell_active(self) -> list[dict]:
        return self._call("aria2.tellActive", []) or []

    def tell_status(self, gid: str) -> Optional[dict]:
        return self._call("aria2.tellStatus", [gid, ["gid", "status", "totalLength",
                                                       "completedLength", "downloadSpeed",
                                                       "errorMessage", "files"]])

    def shutdown(self) -> None:
        try:
            self._call("aria2.shutdown", [])
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def parse_aria2_byte_field(value) -> int:
    try:
        return int(value) if value else 0
    except (ValueError, TypeError):
        return 0


# -----------------------------------------------------------------------------
# Progress display
# -----------------------------------------------------------------------------
class ProgressDisplay:
    def __init__(self, exports: list[Export], parallel: int):
        self.exports = exports
        self.parallel = parallel
        # Only ever render header + active slots + total = parallel + ~4 lines.
        self.rows = parallel + 4
        self.drawn = False

    def render(self, snapshot: list, done_count: int, verified_bytes: int) -> None:
        if not _IS_TTY:
            return

        total_size = sum(e.size for e in self.exports)
        n_total = len(self.exports)

        active = [p for p in snapshot if p.status == "active"]
        speed_sum = sum(p.speed for p in active)
        # Bytes done across everything (completed files + in-flight partials).
        completed_sum = sum(
            p.size if p.status == "done" else min(p.done, p.size or p.done)
            for p in snapshot
        )

        lines: list[str] = []
        lines.append(
            bold("Google Takeout Downloader")
            + "  "
            + dim(f"{done_count}/{n_total} done · {len(active)} active · "
                  f"concurrency={self.parallel}")
        )
        lines.append("")

        # Show only the in-flight files (cap at parallel rows).
        for p in active[: self.parallel]:
            total = p.total or p.size or p.done
            pct = (p.done / total * 100) if total else 0
            bar = progress_bar(pct, 14)
            name = p.filename if len(p.filename) <= 34 else p.filename[:31] + "..."
            lines.append(
                f"  {yellow('↓')} {name:<34} "
                f"{bar} {human_size(p.done):>9}/{human_size(total):>9} "
                f"{human_speed(p.speed):>12}"
            )
        if not active:
            lines.append(dim("  (waiting for downloads to start...)"))

        lines.append("")
        pct_all = (completed_sum / total_size * 100) if total_size else 0
        lines.append(
            f"  Total: {progress_bar(pct_all, 20)} "
            f"{human_size(completed_sum)}/{human_size(total_size)} "
            f"{human_speed(speed_sum)}"
        )

        if self.drawn:
            clear_screen_region(self.rows + 3)
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        self.drawn = True

    def teardown(self) -> None:
        if self.drawn and _IS_TTY:
            clear_screen_region(self.rows + 3)

# -----------------------------------------------------------------------------
# ZIP validation
# -----------------------------------------------------------------------------
def validate_zip(path: Path, expected_size: int) -> tuple[bool, str]:
    try:
        st = path.stat()
        if st.st_size != expected_size:
            return False, f"size mismatch: expected {expected_size}, got {st.st_size}"
        if st.st_size < 22:
            return False, "file too small to be a ZIP"
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad:
                return False, f"bad zip member: {bad}"
        return True, ""
    except zipfile.BadZipFile as e:
        return False, f"bad zip: {e}"
    except Exception as e:
        return False, f"validation error: {e}"


# -----------------------------------------------------------------------------
# Payload input
# -----------------------------------------------------------------------------
def read_payload_interactive() -> str:
    print(bold("\nPaste the JSON payload from the browser extension."))
    print(dim("  Right-click in the terminal to paste, then press Enter."))
    print(dim("  The reader detects when the JSON is complete automatically."))
    print(dim("  Press Ctrl-C to quit.\n"))

    lines = []
    brace_balance = 0
    in_string = False
    escape = False
    complete = False

    while True:
        try:
            line = input("" if brace_balance > 0 else "> ")
        except EOFError:
            break
        if not line and not lines:
            continue
        lines.append(line)
        for ch in line:
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    brace_balance += 1
                elif ch == "}":
                    brace_balance -= 1
        if not in_string and brace_balance == 0 and lines:
            complete = True
            break

    if not complete:
        raise ValueError("JSON appears incomplete")
    return "\n".join(lines)


def find_payload_file(output_dir: Path) -> Optional[Path]:
    candidates = [
        output_dir / "in.json",
        output_dir / "payload.json",
        Path.cwd() / "in.json",
        Path.cwd() / "payload.json",
        Path("/downloads") / "in.json",
        Path("/downloads") / "payload.json",
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return c
    return None


def _payload_cookie(text: str) -> str:
    """Best-effort extract the cookie from raw payload JSON for change detection."""
    try:
        d = json.loads(text)
    except Exception:
        return ""
    return (d.get("cookie") or "").strip()

def get_payload(output_dir: Path, args: argparse.Namespace, reprompt: bool = False,
                prev_cookie: str = "") -> Payload:
    text = ""

    if reprompt:
        # Cookie expired mid-run. Re-acquire through the SAME channel the
        # original payload came from, then continue (partials resume).
        if args.payload:
            pf = Path(args.payload)
            print(yellow(f"\nWaiting for a fresh payload at {pf}"))
            print(dim("  Re-capture in your proxied browser (Copy ALL exports),"))
            print(dim(f"  overwrite {pf} with the new JSON, and the download resumes automatically."))
            print(dim("  Polling every 5s — press Ctrl-C to stop (partials are kept).\n"))
            while True:
                try:
                    if pf.exists() and pf.stat().st_size > 0:
                        candidate = pf.read_text(encoding="utf-8")
                        cookie = _payload_cookie(candidate)
                        # Accept once the cookie is present, valid, and different
                        # from the stale one that just failed.
                        if cookie and cookie != prev_cookie:
                            ok, _ = validate_cookie(cookie)
                            if ok:
                                text = candidate
                                print(green("  Fresh payload detected — resuming.\n"))
                                break
                    time.sleep(5)
                except KeyboardInterrupt:
                    raise
        elif not sys.stdin.isatty():
            # Non-file, non-TTY (piped stdin) run: can't re-read a stream, so
            # the caller must restart with a fresh payload. Surface clearly.
            raise ValueError("cookie expired and no --payload file to watch; "
                             "re-run with a fresh payload")
        else:
            text = read_payload_interactive()

        payload = parse_payload(text)
        ok, msg = validate_cookie(payload.cookie)
        if not ok:
            raise ValueError(msg)
        return payload

    # First acquisition.
    # 1. Explicit payload file argument.
    if args.payload and Path(args.payload).exists():
        text = Path(args.payload).read_text(encoding="utf-8")

    # 2. Auto-discovered payload file.
    if not text:
        f = find_payload_file(output_dir)
        if f:
            log.info("Reading payload from %s", f)
            text = f.read_text(encoding="utf-8")

    # 3. Try stdin if it's not a TTY.
    if not text and not sys.stdin.isatty():
        text = read_payload_from_stdin()

    # 4. Interactive paste.
    if not text:
        text = read_payload_interactive()

    payload = parse_payload(text)
    ok, msg = validate_cookie(payload.cookie)
    if not ok:
        raise ValueError(msg)
    return payload
# -----------------------------------------------------------------------------
# Main download orchestration
# -----------------------------------------------------------------------------
@dataclass
class PartProgress:
    index: int
    filename: str
    url: str
    size: int
    done: int = 0
    total: int = 0
    speed: float = 0.0
    status: str = "queued"  # queued, active, done, error, auth


def _looks_like_html_bytes(body: bytes) -> bool:
    if not body:
        return False
    head = body[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head:
        return False
    if head.startswith(b"<!"):
        return b"html" in head[:32].lower() or b"doctype" in head[:32].lower()
    if head.lower().startswith(b"<html"):
        return True
    return False


class _AuthChallenge(Exception):
    def __init__(self, msg, body=b""):
        super().__init__(msg)
        self.body = body


def download_exports(exports: list[Export], payload: Payload, output_dir: Path,
                     parallel: int) -> None:
    """Internal threaded downloader (no aria2c).

    Every response's first bytes are inspected before the output file is
    opened for writing, so Google's HTML sign-in page can NEVER be saved as
    a .zip. On the first auth challenge the whole pool stops and AuthError is
    raised so main() can re-prompt for a fresh cookie; partials on disk are
    kept and resumed (HTTP Range) on the next pass.

    Only the in-flight files (parallel slots) are rendered, so a 290-part
    batch shows the 4-5 active downloads, not all 290 rows.
    """
    import threading
    import queue as _queue

    display = ProgressDisplay(exports, parallel)
    progress: dict[int, PartProgress] = {}
    lock = threading.Lock()
    stop = threading.Event()
    auth = threading.Event()
    auth_info = {"url": "", "body": b""}

    for e in exports:
        progress[e.index] = PartProgress(index=e.index, filename=e.filename,
                                         url=e.url, size=e.size,
                                         total=e.size, status="queued")

    chunk_size = 1024 * 1024
    timeout = (15, 60)
    max_tries = 5
    retry_wait = 5.0

    def _set(idx, **kw):
        with lock:
            p = progress.get(idx)
            if not p:
                return
            for k, v in kw.items():
                setattr(p, k, v)

    def _stream_one(exp, session):
        dest = output_dir / exp.filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, max_tries + 1):
            if stop.is_set():
                return
            existing = dest.stat().st_size if dest.exists() else 0
            if exp.size and existing >= exp.size:
                _set(exp.index, done=existing, total=exp.size, status="done")
                return
            headers = dict(payload.headers)
            headers["Cookie"] = payload.cookie
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
            try:
                with session.get(exp.url, headers=headers, stream=True,
                                 timeout=timeout, allow_redirects=True) as resp:
                    final_host = resp.url.split("/")[2] if "://" in resp.url else ""
                    ctype = resp.headers.get("content-type", "").lower()
                    if final_host.endswith("accounts.google.com"):
                        raise _AuthChallenge(f"redirected to {final_host}")
                    if "text/html" in ctype:
                        body = next(resp.iter_content(4096), b"")
                        raise _AuthChallenge(f"HTML response (ct={ctype[:40]})", body)
                    if resp.status_code in (401, 403):
                        raise _AuthChallenge(f"HTTP {resp.status_code}")
                    resp.raise_for_status()

                    mode = "wb"
                    start = 0
                    if existing > 0 and resp.status_code == 206:
                        mode = "ab"
                        start = existing
                    total = exp.size
                    cr = resp.headers.get("content-range", "")
                    if cr and "/" in cr:
                        try:
                            total = int(cr.rsplit("/", 1)[1])
                        except ValueError:
                            pass
                    elif resp.status_code == 200:
                        cl = resp.headers.get("content-length")
                        if cl and cl.isdigit():
                            total = int(cl)
                    _set(exp.index, total=total or exp.size, status="active")

                    done = start
                    first = True
                    ema = 0.0
                    t_prev = time.monotonic()
                    b_prev = done
                    with dest.open(mode) as fh:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if stop.is_set():
                                fh.flush()
                                return
                            if not chunk:
                                continue
                            if first:
                                first = False
                                if start == 0 and _looks_like_html_bytes(chunk):
                                    raise _AuthChallenge("first bytes look like HTML",
                                                         chunk[:4096])
                            fh.write(chunk)
                            done += len(chunk)
                            now = time.monotonic()
                            dt = now - t_prev
                            if dt >= 0.5:
                                inst = (done - b_prev) / dt
                                ema = inst if ema == 0 else 0.6 * ema + 0.4 * inst
                                t_prev, b_prev = now, done
                                _set(exp.index, done=done, speed=ema)
                            else:
                                _set(exp.index, done=done)
                final = dest.stat().st_size if dest.exists() else 0
                _set(exp.index, done=final, status="done", speed=0.0)
                return
            except _AuthChallenge:
                raise
            except (requests.RequestException, OSError) as ex:
                if attempt < max_tries and not stop.is_set():
                    _set(exp.index, speed=0.0)
                    time.sleep(retry_wait)
                    continue
                raise

    def _worker(work):
        session = requests.Session()
        try:
            while not stop.is_set():
                try:
                    exp = work.get_nowait()
                except _queue.Empty:
                    return
                try:
                    _stream_one(exp, session)
                except _AuthChallenge as e:
                    if not auth.is_set():
                        auth_info["url"] = exp.url
                        auth_info["body"] = e.body
                        auth.set()
                    stop.set()
                    _set(exp.index, status="auth")
                except Exception as e:
                    log.warning("part %03d failed: %r", exp.index, e)
                    _set(exp.index, status="error")
                finally:
                    work.task_done()
        finally:
            session.close()

    # Skip files already complete on disk; queue the rest.
    work: "_queue.Queue" = _queue.Queue()
    todo = []
    for e in exports:
        dest = output_dir / e.filename
        if dest.exists() and e.size and dest.stat().st_size >= e.size:
            _set(e.index, done=e.size, status="done")
        else:
            todo.append(e)
    for e in todo:
        work.put(e)

    if not todo:
        snap = [PartProgress(**vars(p)) for p in progress.values()]
        display.render(snap, len(exports), sum(e.size for e in exports))
        display.teardown()
        return

    n_workers = max(1, min(parallel, len(todo)))
    threads = [threading.Thread(target=_worker, args=(work,), daemon=True)
               for _ in range(n_workers)]
    for t in threads:
        t.start()

    try:
        while any(t.is_alive() for t in threads):
            with lock:
                snap = [PartProgress(**vars(p)) for p in progress.values()]
            done_count = sum(1 for p in snap if p.status == "done")
            verified_bytes = sum(p.size for p in snap if p.status == "done")
            display.render(snap, done_count, verified_bytes)
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
        raise
    finally:
        for t in threads:
            t.join(timeout=30)
        display.teardown()

    if auth.is_set():
        raise AuthError(f"auth challenge during download ({auth_info['url']})")

def verify_exports(exports: list[Export], output_dir: Path) -> tuple[list[Export], list[Export]]:
    complete: list[Export] = []
    incomplete: list[Export] = []
    for exp in exports:
        path = output_dir / exp.filename
        if not path.exists():
            incomplete.append(exp)
            continue
        ok, msg = validate_zip(path, exp.size)
        if ok:
            exp.verified = True
            complete.append(exp)
        else:
            log.warning("Validation failed for %s: %s", exp.filename, msg)
            incomplete.append(exp)
    return complete, incomplete


def prompt_for_output_dir(default: Path) -> Path:
    while True:
        raw = input(f"Output directory [{default}]: ").strip()
        path = Path(raw) if raw else default
        try:
            return validate_output_dir(path)
        except ValueError as e:
            print(red(f"Invalid directory: {e}"))


# -----------------------------------------------------------------------------
# CLI entrypoint
# -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download Google Takeout archives server-side (internal downloader, resume, ZIP validation).",
    )
    p.add_argument("-o", "--out", dest="output_dir",
                   help="output directory (prompted if omitted)")
    p.add_argument("-p", "--payload", help="path to JSON payload file")
    p.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL,
                   help=f"concurrent downloads (default {DEFAULT_PARALLEL})")
    p.add_argument("--max-exports", type=int, default=DEFAULT_MAX_EXPORTS,
                   help=f"discovery cap (default {DEFAULT_MAX_EXPORTS})")
    p.add_argument("--single", action="store_true",
                   help="download only the captured URL, do not discover other exports")
    p.add_argument("--no-validate", action="store_true",
                   help="skip ZIP validation")
    p.add_argument("--fresh", action="store_true",
                   help="ignore saved state and re-discover")
    return p


def main() -> int:
    args = build_parser().parse_args()

    output_dir = validate_output_dir(Path(args.output_dir or os.environ.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR))
    setup_logging(output_dir / "takeout_dl.log")

    log.info("Output directory: %s", output_dir)
    log.info("Parallelism: %d", args.parallel)

    print(bold("\nGoogle Takeout Downloader — server-side CLI\n"))

    if not args.output_dir and not os.environ.get("OUTPUT_DIR"):
        output_dir = prompt_for_output_dir(output_dir)
        setup_logging(output_dir / "takeout_dl.log")

    # Try to load prior state.
    state_path = output_dir / ".takeout_dl_state.json"
    state: dict = {}
    if state_path.exists() and not args.fresh:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    auth_failures = 0
    payload: Optional[Payload] = None
    exports: list[Export] = []

    while True:
        if payload is None:
            try:
                payload = get_payload(output_dir, args, reprompt=False)
            except ValueError as e:
                print(red(f"Invalid payload: {e}"))
                return 2

        # Verify auth from the server IP with a real probe.
        try:
            status, size, filename = probe_export(payload.url, payload.headers, payload.cookie)
        except AuthError as e:
            print(red(f"Auth check failed: {e}"))
            print(yellow("Re-capture in the browser and paste a fresh payload."))
            auth_failures += 1
            if auth_failures > DEFAULT_MAX_AUTH_REPROMPTS:
                print(red(f"Giving up after {DEFAULT_MAX_AUTH_REPROMPTS} auth failures."))
                return 1
            payload = None
            continue
        except requests.RequestException as e:
            print(red(f"Network error during auth check: {e}"))
            return 1

        if status not in (200, 206) or size <= 0 or not filename:
            print(red(f"Auth check returned unexpected response: status={status} size={size} filename={filename}"))
            return 1

        print(green(f"Auth OK from server IP — first export is {human_size(size)} ({filename})\n"))

        # Discover exports.
        try:
            exports = discover_exports(payload, max_exports=args.max_exports)
        except AuthError as e:
            print(red(f"Auth failed during discovery: {e}"))
            auth_failures += 1
            if auth_failures > DEFAULT_MAX_AUTH_REPROMPTS:
                return 1
            payload = get_payload(output_dir, args, reprompt=True, prev_cookie=payload.cookie)
            continue
        except RuntimeError as e:
            print(red(f"Discovery failed: {e}"))
            return 1

        # If the user only wants the single captured export.
        if args.single:
            captured_index = None
            parsed = urlparse(payload.url)
            qs = parse_qs(parsed.query)
            if "i" in qs:
                captured_index = int(qs["i"][0])
            exports = [e for e in exports if e.index == captured_index]
            if not exports:
                print(red("Could not locate the captured export after discovery."))
                return 1

        # Skip already-verified files from a previous run.
        if state.get("verified"):
            verified_names = set(state["verified"])
            for exp in exports:
                if exp.filename in verified_names:
                    exp.verified = True

        remaining = [e for e in exports if not e.verified]
        if not remaining:
            print(green("All exports already downloaded and verified."))
            break

        # Download.
        try:
            download_exports(remaining, payload, output_dir, args.parallel)
        except AuthError as e:
            print(red(f"\nAuth expired mid-download: {e}"))
            print(yellow("Re-capture in the browser and paste a fresh payload."))
            auth_failures += 1
            if auth_failures > DEFAULT_MAX_AUTH_REPROMPTS:
                return 1
            payload = get_payload(output_dir, args, reprompt=True, prev_cookie=payload.cookie)
            continue
        except Exception as e:
            log.exception("Download failed: %s", e)
            print(red(f"\nDownload failed: {e}"))
            return 1

        # Verify.
        if not args.no_validate:
            complete, incomplete = verify_exports(exports, output_dir)
        else:
            complete = [e for e in exports if (output_dir / e.filename).exists()]
            incomplete = [e for e in exports if e not in complete]

        # Save state.
        state["verified"] = [e.filename for e in exports if e.verified]
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        if not incomplete:
            total = sum(e.size for e in complete)
            print(green(f"\nAll done: {len(complete)} export(s), {human_size(total)} downloaded and verified."))
            break

        print(yellow(f"\n{len(incomplete)} export(s) incomplete; will retry with aria2c resume."))
        for exp in incomplete[:5]:
            print(f"  - {exp.filename}")
        if len(incomplete) > 5:
            print(f"  ... and {len(incomplete) - 5} more")
        # Loop back; already-downloaded files are marked verified, so only
        # incomplete ones will be re-added.

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(yellow("\nInterrupted. Partial downloads are kept; re-run to resume."))
        sys.exit(130)
