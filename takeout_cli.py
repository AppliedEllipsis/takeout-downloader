#!/usr/bin/env python3
"""
Google Takeout Downloader — paste, go.

A terminal-native CLI. You paste a JSON payload from the browser extension,
it discovers the parts, hands them to aria2c, validates ZIPs, moves on
to the next ones concurrently. If the cookie expires it asks for a new
payload and resumes the partials.

Flow
----
  1. Prompt: paste the JSON. Reader auto-detects when the JSON is complete
     (brace-balance scan, string-aware) so you don't need Ctrl-D / EOF.
  2. Parse + validate. If invalid -> clean error + exit (no looping).
  3. Discover parts: probe each numbered ZIP via Range requests, record size,
     stop at end-of-set. This also validates auth up front.
  4. Build an aria2c -i batch for parts we still need. Run aria2c natively
     (its console output IS the progress display — we don't wrap it).
  5. After aria2c exits, verify each part:
       - size matches what the probe said
       - file is a real ZIP (EOCD signature in last 1KB)
     Drop any failing files from the queue (they'll retry next pass).
  6. If anything is still missing AND it looks like the cookie died
     mid-run, re-prompt for a fresh payload. aria2c's -c resumes the
     partials from where they stopped.
  7. Loop 4-6 until done or you Ctrl-C.

Environment
-----------
  OUTPUT_DIR          default output dir (auto-detects JuiceFS, else ./downloads)
  PARALLEL_DOWNLOADS  concurrent downloads, passed to aria2c -j (default 3)
  MAX_PARTS           safety cap on discovery probing (default 500)
  MAX_AUTH_REPROMPTS  how many times to ask for a fresh cookie before giving up
                      (default 5; on the Nth re-prompt we exit cleanly)
  TAKEOUT_LOG_FILE    log file path (default: <OUTPUT_DIR>/takeout_cli.log)
  TAKEOUT_LOG_MAX_BYTES / TAKEOUT_LOG_BACKUP_COUNT  rotation settings
  NO_COLOR            disable ANSI colors (or pass --no-color)
  NO_HYPERLINKS       disable OSC 8 clickable URLs (auto-off when piped)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

# Force UTF-8 stdout/stderr on Windows so banner chars print.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from takeout import (extract_url_parts, validate_output_dir, DEFAULT_OUTPUT_DIR,
                     VERSION, _TAKEOUT_DIR_NAME, _detect_takeout_base)
from takeout_payload import parse_payload, parse_multi_payload, parse_multi_payload_meta, TakeoutPayload, MultiPayloadMeta, REQUIRED_COOKIE_MARKERS
from takeout_downloader import InternalDownloader, PartProgress


# ===========================================================================
# Tunables
# ===========================================================================
PARALLEL = int(os.environ.get("PARALLEL_DOWNLOADS", "5"))
MAX_PARTS = int(os.environ.get("MAX_PARTS", "500"))
MAX_AUTH_REPROMPTS = int(os.environ.get("MAX_AUTH_REPROMPTS", "5"))
PROBE_TIMEOUT = (10, 30)
CONSECUTIVE_404_STOP = 2
SAME_SIZE_STREAK_STOP = 2  # stop if N consecutive parts have identical size
ZIP_EOCD = b"PK\x05\x06"
ZIP_EOCD_SCAN = 1024  # bytes to scan at end of file for the EOCD record
STATE_FILENAME = "takeout_state.json"


# ===========================================================================
# Logger
# ===========================================================================
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


# OSC 8 hyperlinks let modern terminals (Windows Terminal, iTerm2, kitty,
# WezTerm, GNOME Terminal, recent VTE) turn text into a ctrl+click /
# ctrl+shift+click target without showing the raw escape codes. Honored
# only on a TTY; piped/redirected output and --no-color stay plain so logs
# and non-supporting terminals aren't polluted with control characters.
#   ESC ] 8 ; ; <url> ST   <label>   ESC ] 8 ; ; ST
# (ST = ESC \). Terminals that don't understand OSC 8 ignore it and just
# print the label, so this degrades cleanly.
_USE_HYPERLINKS = (
    sys.stdout.isatty()
    and os.environ.get("NO_HYPERLINKS") is None
    and os.environ.get("TERM") != "dumb"
)


def _link(url: str, label: str | None = None) -> str:
    """Wrap `url` as a clickable OSC 8 terminal hyperlink.

    `label` is the visible text (defaults to the URL itself). Falls back to
    plain text when hyperlinks are disabled (non-TTY, NO_HYPERLINKS, dumb
    terminal, or --no-color). The module-level switch is re-read on every
    call so the --no-color flag can flip it after argparse runs.
    """
    text = label if label is not None else url
    if not (_USE_HYPERLINKS and _USE_COLOR):
        return text
    esc = "\033"
    return f"{esc}]8;;{url}{esc}\\{text}{esc}]8;;{esc}\\"


log = logging.getLogger("takeout_cli")


# ===========================================================================
# Terminal renderer — control-char based grid UI
# ===========================================================================
class TermRender:
    """Fixed-area terminal display with control-char redraws.

    Uses *relative* cursor positioning: the grid is printed once wherever
    the cursor happens to be (i.e. right after whatever log output came
    before it), and every subsequent redraw moves the cursor UP by the
    number of lines last drawn, then rewrites each line clearing to EOL
    (\\x1b[K). This means the grid anchors itself to the surrounding log
    flow instead of jumping to an absolute screen row — so it never
    collides with scrolled-up log lines, and it survives terminal resize
    because no absolute coordinates are baked in.

    Every emitted line is hard-truncated to the current terminal width so
    a narrowed terminal can never wrap a row (a wrapped row would occupy
    two physical lines and desync the move-up count, corrupting the grid).

    Header line: live status (downloads active, total done, ETA, throughput).
    Body: one row per file, with progress bar + bytes + speed + ETA.
    Footer: cumulative stats.

    Falls back to plain line-printing when stdout is not a TTY (e.g. piped
    to a file) so tests / logs aren't polluted with escape sequences.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdout.isatty()
        self.body_rows: int = 0
        self.requested_rows: int = 0
        self.last_drawn_lines: int = 0
        self.header: str = ""
        self.rows: list[str] = []
        self.footer: str = ""
        self.drawn: bool = False
        self._resized: bool = False
        self._prev_sigwinch = None
        # Track which file is on which row so we can keep redrawing.
        self._row_keys: list[str] = []

    # -- terminal geometry -------------------------------------------------
    @staticmethod
    def _term_size() -> tuple[int, int]:
        try:
            sz = shutil.get_terminal_size((100, 24))
            return max(20, sz.columns), max(6, sz.lines)
        except (OSError, ValueError):
            return 100, 24

    def _max_body_rows(self) -> int:
        """How many file rows fit, leaving room for header + footer + a
        prompt line below the grid."""
        _, lines = self._term_size()
        return max(1, lines - 3)

    def _on_resize(self, *_args) -> None:
        # Signal handler: just flag it. The next redraw repaints with the
        # new geometry. Doing real work in a signal handler is unsafe.
        self._resized = True

    def begin(self, n_rows: int) -> None:
        """Reserve `n_rows` body rows. Call once after discovery. The row
        count is clamped to what fits on screen; if there are more parts
        than rows, the overflow shares the last row (best-effort)."""
        if not self.enabled:
            return
        self.requested_rows = n_rows
        self.body_rows = min(n_rows, self._max_body_rows())
        self.rows = [""] * self.body_rows
        self._row_keys = [""] * self.body_rows
        self.drawn = False
        # Install a SIGWINCH handler where the platform supports it
        # (POSIX). Windows has no SIGWINCH; per-redraw geometry checks
        # still adapt the bar width, just without instant repaint.
        if hasattr(signal, "SIGWINCH"):
            try:
                self._prev_sigwinch = signal.signal(signal.SIGWINCH,
                                                     self._on_resize)
            except (ValueError, OSError):
                # Not in the main thread, or unsupported — degrade quietly.
                self._prev_sigwinch = None

    def set_header(self, text: str) -> None:
        self.header = text
        self._redraw()

    def set_footer(self, text: str) -> None:
        self.footer = text
        self._redraw()

    def _reflow_for_resize(self) -> None:
        """Re-clamp the visible row count when the terminal was resized.
        Keeps existing row->key assignments; only grows/shrinks the slot
        list. Called lazily from _redraw when the resize flag is set."""
        new_max = self._max_body_rows()
        target = min(self.requested_rows, new_max)
        if target == self.body_rows:
            return
        if target > self.body_rows:
            self.rows.extend([""] * (target - self.body_rows))
            self._row_keys.extend([""] * (target - self.body_rows))
        else:
            self.rows = self.rows[:target]
            self._row_keys = self._row_keys[:target]
        self.body_rows = target
        # Force a clean repaint: the block height changed, so the old
        # move-up count is stale. Start fresh on the next write.
        self.drawn = False

    def update_row(self, key: str, content: str) -> None:
        """Update one row by key. Re-uses the same screen line across
        redraws so the grid doesn't jump around when files change order."""
        if not self.enabled:
            # Non-TTY fallback: log to stdout, line by line.
            print(content, flush=True)
            return
        try:
            idx = self._row_keys.index(key)
        except ValueError:
            # Find first empty slot.
            for i, k in enumerate(self._row_keys):
                if not k:
                    self._row_keys[i] = key
                    idx = i
                    break
            else:
                # All slots used; replace last row.
                idx = self.body_rows - 1
                self._row_keys[idx] = key
        self.rows[idx] = content
        self._redraw()

    def clear_row(self, key: str) -> None:
        """Blank out a row but keep its slot."""
        if not self.enabled:
            return
        try:
            idx = self._row_keys.index(key)
        except ValueError:
            return
        self.rows[idx] = ""
        self._redraw()

    def set_rows(self, rows: list[str]) -> None:
        """Replace ALL body rows at once and redraw a SINGLE time.

        This is the path for the internal downloader, which has the full
        snapshot of every part on every tick. Calling :meth:`update_row`
        once per part would emit one full-screen repaint per part (290
        parts -> 290 repaints/tick -> megabytes/sec of escape codes that
        flood an SSH pipe and stall the process). Here the caller hands us
        the already-rendered, already-clamped list of visible rows and we
        paint the whole block in one write.

        ``rows`` is truncated/padded to ``body_rows`` so it always matches
        the reserved block height."""
        if not self.enabled:
            for r in rows:
                if r:
                    print(r, flush=True)
            return
        if self._resized:
            self._resized = False
            self._reflow_for_resize()
        n = self.body_rows
        self.rows = (list(rows) + [""] * n)[:n]
        self._redraw()

    def _redraw(self) -> None:
        if not self.enabled:
            return
        if self._resized:
            self._resized = False
            self._reflow_for_resize()
        cols, _ = self._term_size()
        lines = [self.header] + self.rows + [self.footer]
        out = []
        # Move the cursor back up to the top of our block (relative), so we
        # overwrite in place instead of scrolling. On the very first draw
        # there is nothing to move over.
        if self.drawn and self.last_drawn_lines:
            out.append(f"\x1b[{self.last_drawn_lines}A")
        for ln in lines:
            out.append(self._format_line(ln, cols))
        self.last_drawn_lines = len(lines)
        self.drawn = True
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    @staticmethod
    def _format_line(text: str, cols: int) -> str:
        # Hard-truncate to the terminal width so a row can never wrap onto
        # a second physical line (which would desync the move-up count).
        # Reserve 1 column so the cursor sitting at EOL doesn't auto-wrap.
        if text:
            text = text[: max(0, cols - 1)]
        # Erase to EOL (clears leftover chars from a previous longer line),
        # write text, advance to next line.
        return f"\x1b[K{text}\n"

    def reserve_lines_below(self) -> int:
        """After a redraw the cursor sits just below our block already
        (we end every line with \\n). Nothing extra to scroll; return the
        block height for callers that want to know it."""
        if not self.enabled:
            return 0
        sys.stdout.flush()
        return self.last_drawn_lines

    def teardown(self) -> None:
        """Restore the SIGWINCH handler and leave the cursor below our
        region so subsequent logs don't overwrite the final grid state."""
        if not self.enabled:
            return
        if hasattr(signal, "SIGWINCH") and self._prev_sigwinch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._prev_sigwinch)
            except (ValueError, OSError):
                pass
            self._prev_sigwinch = None
        # Cursor is already below the block (last redraw ended with \n per
        # line). Nothing more to emit.
        sys.stdout.flush()


def make_progress_bar(pct: float, width: int = 24) -> str:
    """Build a unicode block progress bar. width is total char count."""
    if pct < 0:
        pct = 0
    if pct > 100:
        pct = 100
    filled = int(round(width * pct / 100))
    empty = width - filled
    return "[" + "█" * filled + "·" * empty + "]"


def info(msg: str) -> None:
    log.info(msg)


def ok(msg: str) -> None:
    log.info(msg)


def warn(msg: str) -> None:
    log.warning(msg)


def err(msg: str) -> None:
    log.error(msg)


def debug(msg: str) -> None:
    log.debug(msg)


def header(msg: str) -> None:
    bar = "=" * max(40, len(msg) + 4)
    info("")
    info(bar)
    info(f"  {msg}")
    info(bar)


def section(msg: str) -> None:
    info("")
    info(f"--- {msg} ---")


# ===========================================================================
# Errors
# ===========================================================================
class AuthError(Exception):
    """Cookie expired (Google returned a signin page or auth challenge)."""


# ===========================================================================
# Log file installation
# ===========================================================================
def _install_logger(log_path: Path, max_bytes: int, backup_count: int) -> None:
    """Wire a rotating file handler + a stream handler onto the module logger.

    DEBUG lines (probe URLs, response codes) go to file only.
    INFO+ go to both file and stdout.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        fh = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8", delay=True,
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        sys.stderr.write(f"WARN: cannot open log file {log_path}: {e}\n")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ===========================================================================
# Output dir
# ===========================================================================
def _config_path() -> Path:
    """Where we persist last-used options. ~/.takeout-cli.json"""
    return Path(os.environ.get("TAKEOUT_CONFIG") or
                (Path.home() / ".takeout-cli.json"))


def load_config() -> dict:
    """Read persisted config. Returns {} if missing/corrupt."""
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    """Persist config atomically. Best-effort; never raises."""
    p = _config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        debug(f"save_config failed: {e}")


def resolve_output_dir() -> Path:
    raw = (os.environ.get("OUTPUT_DIR")
           or load_config().get("output_dir")
           or DEFAULT_OUTPUT_DIR)
    try:
        d = validate_output_dir(raw)
    except ValueError as e:
        warn(f"{e}")
        warn("Falling back to ./downloads")
        d = Path("./downloads").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_takeout_manifest(payload: TakeoutPayload) -> list[TakeoutPayload]:
    """Use the captured cookie to fetch the Takeout manage page for this
    archive and scrape all download URLs from it. This finds all parts
    without needing the extension to scrape the page.

    Returns an empty list on failure (the caller falls back to the
    extension-scraped or single-URL payload).
    """
    # Extract archive ID from the download URL's `j=` parameter.
    archive_id = None
    authuser = "0"
    rapt = None
    user_id = None
    m = re.search(r"[?&]j=([a-f0-9-]+)", payload.url)
    if m:
        archive_id = m.group(1)
    m = re.search(r"[?&]authuser=(\d+)", payload.url)
    if m:
        authuser = m.group(1)
    m = re.search(r"[?&]rapt=([^&]+)", payload.url)
    if m:
        rapt = m.group(1)
    m = re.search(r"[?&]user=(\d+)", payload.url)
    if m:
        user_id = m.group(1)
    # Also check the cookie for authuser.
    cm = re.search(r"authuser=(\d+)", payload.cookie)
    if cm:
        authuser = cm.group(1)
    if not archive_id:
        debug("No archive ID in URL; cannot fetch manifest")
        return []

    headers = dict(payload.headers)
    headers["Cookie"] = payload.cookie
    headers.setdefault("Accept", "text/html,application/xhtml+xml")

    def _q(base: str, extras: dict) -> str:
        parts = [base]
        for k, v in extras.items():
            if v is not None:
                parts.append(f"{k}={v}")
        return "&".join(parts)

    common = {"archiveId": archive_id, "authuser": authuser}
    if rapt:
        common["rapt"] = rapt
    if user_id:
        common["user"] = user_id

    api_endpoints = [
        _q("https://takeout.google.com/_/TakeoutApiUi/data?", common),
        _q(f"https://takeout.google.com/api/v2/manage/archive?id={archive_id}&authuser={authuser}",
           {"rapt": rapt, "user": user_id}),
        _q(f"https://takeout.google.com/api/v2/manage/archives?authuser={authuser}",
           {"rapt": rapt, "user": user_id}),
        _q(f"https://takeout.google.com/u/{authuser}/manage/archive/{archive_id}?json=1", common),
        _q(f"https://takeout.google.com/u/{authuser}/manage/archive/{archive_id}?", common),
        _q(f"https://takeout.google.com/settings/takeout/downloads?", common),
    ]

    for api_url in api_endpoints:
        debug(f"Trying API endpoint: {api_url}")
        try:
            resp = requests.get(api_url, headers=headers,
                                allow_redirects=True, timeout=15)
        except requests.RequestException as e:
            debug(f"API request failed: {e}")
            continue

        if resp.status_code != 200:
            debug(f"API returned {resp.status_code}")
            continue
        if "accounts.google.com" in resp.url:
            debug("API redirected to Google signin; cookie invalid")
            return []

        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            try:
                data = resp.json()
                # Look for our archive in the response
                payloads = _parse_manifest_json(data, payload)
                if payloads:
                    ok(f"Got {len(payloads)} archives from API.")
                    return payloads
            except Exception as e:
                debug(f"JSON parse failed: {e}")

    # Fall back to fetching the manage page HTML and scraping URLs.
    page_params = {"rapt": rapt, "user": user_id} if rapt or user_id else {}
    page_url = _q(
        f"https://takeout.google.com/u/{authuser}/manage/archive/{archive_id}",
        page_params
    )
    debug(f"Fetching manage page: {page_url}")
    try:
        resp = requests.get(page_url, headers=headers,
                            allow_redirects=True, timeout=15)
    except requests.RequestException as e:
        debug(f"Manage page fetch failed: {e}")
        return []

    if resp.status_code != 200:
        debug(f"Manage page returned {resp.status_code}")
        return []
    if "accounts.google.com" in resp.url:
        debug("Manage page redirected to Google signin; cookie invalid")
        return []

    html = resp.text
    urls = re.findall(
        r'https://takeout-download\.usercontent\.google\.com/'
        r'download/takeout-[^"\'<>\s]+\.zip(?:\?[^"\'<>\s]*)?',
        html,
    )
    if not urls:
        debug("No takeout-download URLs found in manage page HTML")
        return []

    seen = set()
    payloads = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        new_payload = TakeoutPayload(
            url=url,
            cookie=payload.cookie,
            headers=dict(payload.headers),
            method="GET",
            captured_at=payload.captured_at,
            source="server-manifest",
            schema=payload.schema,
        )
        payloads.append(new_payload)
    return payloads


def _parse_manifest_json(data: dict, payload: TakeoutPayload) -> list[TakeoutPayload]:
    """Parse a JSON Takeout manifest response into TakeoutPayloads.

    Handles several shapes:
    - {"exports": [{"downloadUrl": "..."}]}
    - {"archive": {"parts": [{"url": "..."}]}}
    - {"archives": [{"id": "...", "parts": [...]}]} — list of all archives
    - [{"downloadUrl": "..."}] — plain array
    """
    payloads = []
    candidates: list = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("exports", "parts", "items", "downloads", "files"):
            if key in data and isinstance(data[key], list):
                candidates = data[key]
                break
        if not candidates and "archive" in data:
            archive = data["archive"]
            if isinstance(archive, dict):
                for key in ("parts", "downloads", "files"):
                    if key in archive and isinstance(archive[key], list):
                        candidates = archive[key]
                        break
        # List of all archives for this user
        if not candidates and "archives" in data:
            archives = data["archives"]
            if isinstance(archives, list):
                for arch in archives:
                    if not isinstance(arch, dict):
                        continue
                    for key in ("parts", "downloads", "files", "exportUrls"):
                        if key in arch and isinstance(arch[key], list):
                            candidates.extend(arch[key])
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = (item.get("downloadUrl") or item.get("url")
               or item.get("link") or item.get("href")
               or item.get("download_url"))
        if not url:
            continue
        if "takeout" not in url.lower():
            continue
        new_payload = TakeoutPayload(
            url=url,
            cookie=payload.cookie,
            headers=dict(payload.headers),
            method="GET",
            captured_at=payload.captured_at,
            source="server-manifest",
            schema=payload.schema,
        )
        payloads.append(new_payload)
    return payloads


def _looks_like_json(text: str) -> bool:
    """Heuristic sniff for accidental JSON paste into a non-JSON prompt.

    Cheap — we only look at the first non-whitespace character. Avoids
    catching real paths (which never start with { or [) while still
    flagging both compact and pretty-printed payloads.
    """
    s = text.lstrip()
    return bool(s) and s[0] in "{["


# ===========================================================================
# Subfolder picker — arrow-key selectable list of subfolders under a base
# ===========================================================================
def _list_subfolders(base: Path) -> list[str]:
    """Sorted names of immediate subdirectories of ``base`` (no hidden ones).
    Returns [] if base is missing/unreadable."""
    try:
        return sorted(
            p.name for p in base.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    except OSError:
        return []


def _arrow_menu(title: str, options: list[str]) -> int | None:
    """Render an arrow-key selectable menu and return the chosen index, or
    None if the user pressed 'q'/Esc to cancel.

    Uses raw terminal mode on POSIX (the deploy target is Ubuntu). Arrow
    keys (or j/k) move, Enter selects, q/Esc cancels. Falls back to a
    numbered prompt when not on a POSIX TTY (Windows, pipes, Docker without
    -t) so it degrades cleanly. The caller is responsible for any
    follow-up prompts (e.g. typing a new folder name).
    """
    if not options:
        return None

    # Fallback: numbered menu for non-TTY or non-POSIX (no termios).
    posix_tty = (
        sys.stdin.isatty() and sys.stdout.isatty()
        and os.name == "posix"
    )
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        posix_tty = False

    if not posix_tty:
        info(_c("1;36", title))
        for i, opt in enumerate(options, 1):
            info(_c("36", f"  [{i}] {opt}"))
        while True:
            try:
                raw = input("  pick > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if raw in ("q", "quit", ""):
                return None
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(options):
                    return idx
            except ValueError:
                pass
            err(f"  Enter 1-{len(options)}, or 'q' to cancel.")

    # POSIX raw-mode arrow navigation.
    import termios
    import tty

    sel = 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def draw(first: bool) -> None:
        if not first:
            # Move cursor up over the previously drawn option lines so the
            # menu redraws in place instead of scrolling.
            sys.stdout.write(f"\x1b[{len(options)}A")
        for i, opt in enumerate(options):
            marker = "\u276f" if i == sel else " "
            line = f"  {marker} {opt}"
            if i == sel:
                line = _c("1;36", line)
            # In raw mode the terminal does NOT translate \n to CR+LF, so
            # end each line with \r\n to return to column 0 (otherwise the
            # rows stair-step to the right).
            sys.stdout.write("\r\x1b[K" + line + "\r\n")
        sys.stdout.flush()

    info(_c("1;36", title))
    info(_c("2", "  \u2191/\u2193 (or j/k) to move, Enter to select, q to cancel"))
    try:
        tty.setraw(fd)
        draw(first=True)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                return sel
            if ch in ("q", "\x1b"):
                # Esc may begin an arrow sequence; peek to disambiguate.
                if ch == "\x1b":
                    seq = sys.stdin.read(2)
                    if seq == "[A":          # up arrow
                        sel = (sel - 1) % len(options)
                        draw(first=False)
                        continue
                    if seq == "[B":          # down arrow
                        sel = (sel + 1) % len(options)
                        draw(first=False)
                        continue
                    # Bare Esc -> cancel.
                return None
            if ch in ("k",):
                sel = (sel - 1) % len(options)
                draw(first=False)
            elif ch in ("j",):
                sel = (sel + 1) % len(options)
                draw(first=False)
            elif ch == "\x03":               # Ctrl-C
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _derive_picker_base(resolved: Path) -> Path | None:
    """Decide which directory the subfolder picker should be rooted at.

    The problem this solves: ``OUTPUT_DIR`` (from the env / ``.env``) often
    points at a *subfolder* the user previously downloaded into (e.g.
    ``.../google-takeout/braincreation``), not the shared base. If we rooted
    the picker there, the user could never pick a *sibling* folder or create
    a new one next to it -- they'd be stuck inside one person's folder.

    Resolution order:
      1. ``TAKEOUT_BASE_DIR`` env var -- explicit override, used verbatim.
      2. Walk ``resolved`` upward looking for the ``<TAKEOUT_DIR_NAME>``
         component (default ``google-takeout``); if found, root the picker
         at that directory so every sibling subfolder is selectable.
      3. Auto-detect a ``google-takeout`` base under the known storage roots.
      4. If ``resolved`` itself exists, use it (last resort).

    Returns the base dir, or None if nothing usable exists (caller then
    falls back to the free-form typed-path prompt).
    """
    override = os.environ.get("TAKEOUT_BASE_DIR", "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None

    # Walk up to the named base component (e.g. .../google-takeout/foo -> .../google-takeout).
    name = _TAKEOUT_DIR_NAME
    cur = resolved
    for ancestor in [cur, *cur.parents]:
        if ancestor.name == name and ancestor.is_dir():
            return ancestor

    detected = _detect_takeout_base()
    if detected and Path(detected).is_dir():
        return Path(detected)

    return resolved if resolved.is_dir() else None


def prompt_for_output_subfolder(base: Path, default: Path) -> Path:
    """Pick a subfolder under ``base`` with arrow keys, or create a new one.

    The menu lists every existing immediate subfolder of ``base`` plus two
    actions: create a new subfolder, or use ``base`` itself. Selecting a
    folder returns ``base/<name>`` (created if needed). Choosing "create"
    prompts for a name. Cancelling (q/Esc) falls back to the typed-path
    prompt so the user can still enter an arbitrary location.

    When ``base`` doesn't exist or isn't a directory (e.g. running off the
    server), this defers entirely to :func:`prompt_for_output_dir`.
    """
    base = Path(base)
    if not base.is_dir():
        return prompt_for_output_dir(default)

    subs = _list_subfolders(base)
    CREATE = "\u2795 Create a new subfolder\u2026"
    USE_BASE = f"\U0001f4c1 Use this folder directly ({base})"
    TYPE_PATH = "\u270f\ufe0f  Type a different path\u2026"
    options = subs + [CREATE, USE_BASE, TYPE_PATH]

    info("")
    title = f"Where under {base} do you want to save the archives?"
    idx = _arrow_menu(title, options)
    if idx is None:
        # Cancelled — fall back to free-form path entry.
        return prompt_for_output_dir(default)

    choice = options[idx]
    if choice == TYPE_PATH:
        return prompt_for_output_dir(default)
    if choice == USE_BASE:
        target = base
    elif choice == CREATE:
        info(_c("1;36", "  New subfolder name:"))
        while True:
            try:
                name = input("  name > ").strip()
            except (EOFError, KeyboardInterrupt):
                return prompt_for_output_dir(default)
            if not name:
                err("  Name cannot be empty (or Ctrl-C to go back).")
                continue
            # Reject path separators / traversal — this is a single
            # subfolder name under base, not an arbitrary path.
            if "/" in name or "\\" in name or name in (".", ".."):
                err("  Use a plain folder name (no slashes). "
                    "For an arbitrary path, cancel and pick 'Type a path'.")
                continue
            target = base / name
            break
    else:
        target = base / choice

    try:
        validated = validate_output_dir(str(target))
        validated.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError) as e:
        err(f"  Could not use {target} ({e}).")
        return prompt_for_output_dir(default)
    ok(f"  Saving to {validated}")
    return validated


def prompt_for_output_dir(default: Path) -> Path:
    """Ask the user where to save archives. Empty input keeps the default.
    Type 'q' to quit. Anything else is treated as a path and validated.

    Defensive against an accidental paste of the JSON payload here:
      - JSON-shaped input (starts with { or [) is rejected up front with
        a clear "wrong prompt" hint instead of being treated as a path.
      - OSError from a too-long / malformed path is caught and re-prompted
        instead of crashing with a traceback.
      - KeyboardInterrupt exits cleanly (SystemExit 130) instead of
        bubbling to the outer handler that warns about resuming partial
        downloads — there are no partials yet at this prompt.
    """
    info("")
    info(_c("1;36", f"Where do you want to save the archives?"))
    info(_c("36", f"  Default [{default}] (Enter to accept, or type a path):"))
    info(_c("36", "  Type 'q' to quit, or Ctrl-C to abort."))
    while True:
        try:
            raw = input(f"  save to > ").strip()
        except EOFError:
            return default
        except KeyboardInterrupt:
            info("")
            raise SystemExit(130)
        if raw.lower() in ("q", "quit", "exit"):
            raise SystemExit(0)
        if not raw:
            return default
        # Accidental JSON paste — reject before it becomes a folder name.
        if _looks_like_json(raw):
            err("  That input looks like a JSON payload, not a directory path.")
            err("  This prompt expects a folder path. The JSON goes at the")
            err("  'Paste the JSON' prompt further up — re-run and paste it there.")
            err("  Try again (Enter for default, 'q' to quit, Ctrl-C to abort).")
            continue
        # Expand ~ and make absolute relative to cwd.
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        try:
            validated = validate_output_dir(str(candidate))
        except ValueError as e:
            err(f"  {e}")
            info("  Try again (or Enter for default, 'q' to quit).")
            continue
        except OSError as e:
            # Common after an accidental paste of a long blob: Windows
            # raises [WinError 206] "filename or extension is too long".
            err(f"  Path is not usable ({e}).")
            info("  Try again (or Enter for default, 'q' to quit).")
            continue
        try:
            validated.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            err(f"  Could not create {validated} ({e}).")
            info("  Try again (or Enter for default, 'q' to quit).")
            continue
        return validated


# ===========================================================================
# State file — resume across runs
# ===========================================================================
def state_path(output_dir: Path) -> Path:
    return output_dir / STATE_FILENAME


def load_state(output_dir: Path) -> dict | None:
    """Load the per-folder state file. Returns None if missing or corrupt."""
    p = state_path(output_dir)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        warn(f"State file {p} unreadable ({e}); ignoring it.")
        return None


def save_state(output_dir: Path, state: dict) -> None:
    """Persist the state file. Atomic-ish write via a sibling tmp file."""
    p = state_path(output_dir)
    state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime())
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
        debug(f"state saved -> {p}")
    except OSError as e:
        warn(f"Could not save state file {p}: {e}")


def state_matches_payload(state: dict, payload: TakeoutPayload) -> bool:
    """True if the saved state is for the same Takeout archive (same URL
    pattern + base host). Used to decide whether to resume or start fresh."""
    if not state:
        return False
    base, _, ext, _ = extract_url_parts(payload.url)
    if not base:
        return False
    expected = base.split("/")[-1]
    saved = state.get("base_filename", "")
    return bool(saved) and saved == expected


def state_to_parts(state: dict, payload: TakeoutPayload) -> list[dict] | None:
    """Rehydrate `parts` from saved state. Returns None if it can't
    (e.g. URL pattern mismatch). The caller decides what to do."""
    base, _, ext, query = extract_url_parts(payload.url)
    if not base:
        return None
    parts: list[dict] = []
    for entry in state.get("parts", []):
        n = entry.get("num")
        size = entry.get("size", 0)
        if not isinstance(n, int) or n <= 0:
            continue
        filename = f"{base.split('/')[-1]}{n:03d}{ext}"
        url = f"{base}{n:03d}{ext}"
        if query:
            url += f"?{query}"
        # `have` is recomputed at verify time; default to True only if the
        # saved entry marked it complete AND the size matches what we saved.
        saved_size = entry.get("size", 0)
        saved_complete = entry.get("complete", False)
        parts.append({
            "num": n,
            "url": url,
            "filename": filename,
            "size": size,
            "have": saved_complete,
            "_saved_size": saved_size,
        })
    return parts


def make_state(parts: list[dict], payload: TakeoutPayload,
               output_dir: Path) -> dict:
    """Build a fresh state dict from discovery results."""
    base, _, ext, _ = extract_url_parts(payload.url)
    return {
        "schema": 1,
        "base_filename": base.split("/")[-1] if base else "",
        "url_sample": payload.url,
        "output_dir": str(output_dir),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parts": [
            {"num": p["num"], "size": p["size"], "complete": p["have"]}
            for p in parts
        ],
    }


def update_state_from_parts(state: dict, parts: list[dict]) -> dict:
    """Merge verify results into the state dict (mutates state["parts"])."""
    by_num = {p["num"]: p for p in parts}
    out_parts: list[dict] = []
    for entry in state.get("parts", []):
        n = entry.get("num")
        if n in by_num:
            p = by_num[n]
            out_parts.append({
                "num": n,
                "size": p["size"],
                "complete": p["have"],
            })
        else:
            out_parts.append(entry)
    state["parts"] = out_parts
    return state


# ===========================================================================
# Pretty size helper
# ===========================================================================
def human_size(n: int) -> str:
    if not n:
        return "0 B"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ===========================================================================
# Paste the JSON
# ===========================================================================
def prompt_for_paste() -> str:
    """Prompt the user to paste a JSON payload, return the pasted text.

    Auto-detects when the JSON object is complete by tracking brace depth
    (string-aware). Works over SSH/tmux/Docker because it doesn't rely on
    bracketed paste markers — just plain line input.
    """
    info("")
    info(_c("1;36", "Paste the JSON payload from the browser extension."))
    info(_c("36", "  (Right-click in terminal -> Paste, then press Enter.)"))
    info(_c("36", "  The reader detects when the JSON is complete automatically."))
    info(_c("36", "  Press Ctrl-C to quit."))
    info("")

    lines: list[str] = []
    depth = 0
    started = False
    in_string = False
    escape = False
    started_at = time.monotonic()

    while True:
        try:
            line = input()
        except EOFError:
            break
        lines.append(line)
        for ch in line:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
        if started and depth <= 0:
            break
        # Safety: if the user pasted for 5 minutes without completing, bail.
        if time.monotonic() - started_at > 300:
            warn("No complete JSON received in 5 minutes; aborting paste.")
            break

    return "\n".join(lines)


# ===========================================================================
# Payload parse + validate (single-shot, no loops)
# ===========================================================================
# Module-level payload source. main() sets this to the relay reader when
# --relay is passed; otherwise it stays None and the terminal reader is used.
# All re-prompt call sites (stale-cookie resume loop) inherit the same source.
_PAYLOAD_SOURCE = None


def relay_paste_source(use_tunnel: bool, timeout: int):
    """Build a zero-arg callable that blocks on the ephemeral relay and
    returns the received payload text. Falls back to terminal paste if the
    relay times out or fails to start."""
    def _read() -> str:
        try:
            from paste_server import serve_once
        except Exception as e:  # noqa: BLE001
            warn(f"Relay unavailable ({e}); falling back to terminal paste.")
            return prompt_for_paste()
        text = serve_once(timeout=timeout, use_tunnel=use_tunnel)
        if not text:
            warn("Relay returned nothing; falling back to terminal paste.")
            return prompt_for_paste()
        return text
    return _read


def parse_one_payload(source=None) -> tuple[TakeoutPayload, dict]:
    """Read + parse + validate one payload. If the extension scraped the
    whole page and returned multiple exports, show a menu so the user
    picks which archive to download. Failures are terminal.

    Returns ``(payload, ctx)`` where ``ctx`` carries multi-payload
    metadata the caller needs:

      - ``mode``: ``"single"`` | ``"parts"`` | ``"batches"``
      - ``meta``: MultiPayloadMeta (archiveId, expectedParts, sizes)
      - ``pre_built_parts``: list of part dicts ready for download, set
        in ``"parts"`` mode so the caller can skip discovery entirely.
      - ``all_payloads``: full multi-payload list (for state, re-prompt)

    Mode semantics:
      - ``"single"``: one URL, one export. Caller does discovery.
      - ``"parts"``: multi-payload v2 with expectedParts. Each URL is a
        part of ONE batch; caller downloads all parts without a menu.
      - ``"batches"``: legacy multi-payload (or v2 without expectedParts).
        URLs are separate batches; caller shows the pick menu.

    `source` is a zero-arg callable returning the raw payload text.
    Defaults to the module-level `_PAYLOAD_SOURCE` (set by --relay),
    then the terminal paste reader."""
    raw = (source or _PAYLOAD_SOURCE or prompt_for_paste)()
    if not raw.strip():
        err("No JSON received.")
        _payload_fix_hint()
        raise SystemExit(1)
    try:
        payloads, meta = parse_multi_payload_meta(raw)
    except ValueError as e:
        err(f"Could not parse JSON: {e}")
        _payload_fix_hint()
        raise SystemExit(2)
    if not payloads:
        err("Payload parsed but produced no exports.")
        _payload_fix_hint()
        raise SystemExit(2)
    for payload in payloads:
        good, message = payload.validate()
        if not good:
            err(f"Payload failed validation: {message}")
            _payload_fix_hint()
            raise SystemExit(2)
        if message:
            warn(message)
    # Log summary
    first = payloads[0]
    markers = [m for m in REQUIRED_COOKIE_MARKERS if m in first.cookie]
    ok(f"Cookie OK: {len(first.cookie)} chars "
       f"(markers: {', '.join(markers[:4])})")

    ctx: dict = {
        "mode": "single",
        "meta": meta,
        "pre_built_parts": None,
        "all_payloads": payloads,
    }

    # If we only got one URL, try to fetch the full manifest from the
    # Takeout manage page using the captured cookie. This finds all parts
    # automatically without needing the extension to scrape.
    if len(payloads) == 1:
        manifest = fetch_takeout_manifest(payloads[0])
        if manifest and len(manifest) > 1:
            ok(f"Server-side manifest: found {len(manifest)} archives.")
            payloads = manifest
            ctx["all_payloads"] = payloads
            # Manifest result is always "batches" mode (no expectedParts).
            ctx["mode"] = "batches"

    # Decide mode based on what we have now.
    if len(payloads) == 1:
        ctx["mode"] = "single"
        return payloads[0], ctx

    # Multi-payload: parts mode if v2 metadata says so, else batches.
    if meta.has_full_metadata and meta.expectedParts == len(payloads):
        ctx["mode"] = "parts"
        ok(f"Multi-payload has expectedParts={meta.expectedParts}: "
           f"treating {len(payloads)} URLs as parts of one batch.")
        return payloads[0], ctx
    # Multi-export: probe sizes so we can show "123 MB" next to each and
    # sort by smallest first (fastest download first).
    info("")
    info("Probing archive sizes via Range requests...")
    sizes = []
    for i, p in enumerate(payloads):
        session = requests.Session()
        headers = dict(p.headers)
        headers["Cookie"] = p.cookie
        try:
            size = _probe_part(session, p.url, headers)
            sizes.append((i, size))
            size_str = human_size(size) if size else "?"
            info(f"  [{i+1}/{len(payloads)}] {size_str}  "
                 f"{p.filename_hint()}")
        except AuthError as e:
            err(f"  [{i+1}/{len(payloads)}] AUTH FAIL: {e}")
            sizes.append((i, None))
        except Exception as e:
            debug(f"  [{i+1}/{len(payloads)}] probe failed: {e}")
            sizes.append((i, None))
        finally:
            session.close()
    # Sort payloads by size (smallest first), keeping unknown sizes at the end
    sorted_indices = sorted(
        range(len(payloads)),
        key=lambda i: (sizes[i][1] is None, sizes[i][1] or 0)
    )
    sorted_payloads = [payloads[i] for i in sorted_indices]
    sorted_sizes = [sizes[i] for i in sorted_indices]
    info("")
    info(_c("1;36", f"Multiple exports detected ({len(payloads)} archives, "
                    "sorted smallest first):"))
    for i, (orig_i, size) in enumerate(sorted_sizes, 1):
        size_str = human_size(size) if size else "unknown"
        hint = sorted_payloads[i-1].filename_hint()
        info(_c("36", f"  [{i}] {size_str:>10}  {hint}"))
    info(_c("36", "  [a] Download ALL archives one after another"))
    info(_c("36", "  Type the number or 'a', then press Enter."))
    while True:
        try:
            choice = input("  pick > ").strip().lower()
        except EOFError:
            choice = "1"
        if choice == "a":
            # "Download ALL" used to just return the smallest and tell
            # the user to re-run. That's frustrating when the user has
            # 10 archives and wants them all. Now we hand back the
            # *sorted* list so the caller can iterate.
            info("Downloading ALL archives sequentially (smallest first). "
                 "This may take a while; Ctrl-C to stop after the current one.")
            # Reuse the (already sorted, smallest-first) list via a
            # sentinel in ctx. The caller checks ctx.get("all_sorted")
            # and loops over it instead of doing single-batch discovery.
            ctx["mode"] = "batches_all"
            ctx["all_sorted"] = sorted_payloads
            return sorted_payloads[0], ctx
        try:
            idx = int(choice)
            if 1 <= idx <= len(sorted_payloads):
                return sorted_payloads[idx - 1], ctx
        except ValueError:
            pass
        err(f"  Please enter 1-{len(sorted_payloads)} or 'a'.")


def _payload_fix_hint() -> None:
    out = os.environ.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR
    info("How to fix:")
    info("  1. Re-capture in your browser: takeout.google.com -> Manage "
         "exports -> Download -> let the request fire -> click the extension "
         "icon -> Copy as JSON")
    info("  2. Right-click in the terminal to paste, then press Enter.")
    info(f"  (Or save to {out}/in.json and re-run.)")


def _dir_size_map(output_dir: Path) -> dict[str, int]:
    """Return ``{filename: size_bytes}`` for every file in ``output_dir`` via
    a SINGLE ``os.scandir`` pass.

    Why: building the parts list used to call ``exists()`` + ``stat()`` per
    part. On a slow network filesystem (JuiceFS/encfs) that is two
    round-trips per part, so 290 parts cost ~580 serial syscalls and several
    minutes of dead time before the download even started. One ``scandir``
    is a single directory read, and ``entry.stat()`` on the yielded entries
    is served from the readdir cache on most filesystems. Missing files are
    simply absent from the map (callers treat absent as size 0).
    """
    sizes: dict[str, int] = {}
    try:
        with os.scandir(output_dir) as it:
            for entry in it:
                try:
                    if entry.is_file():
                        sizes[entry.name] = entry.stat().st_size
                except OSError:
                    continue
    except (OSError, ValueError):
        # Dir missing/unreadable -> empty map; every part counts as "need".
        pass
    return sizes


def _build_parts_from_payloads(payloads: list[TakeoutPayload],
                                meta: MultiPayloadMeta,
                                output_dir: Path) -> list[dict]:
    """Build the internal ``parts`` list directly from a v2 multi-payload
    whose URLs are the parts of one batch (i.e. ``parts`` mode from
    :func:`parse_one_payload`).

    Each entry is shaped exactly like ``discover_parts`` produces:
        ``{"num", "url", "filename", "size", "have"}``

    Sizes come from ``meta.sizes`` when the extension knew them (it scraped
    ``data-size`` off the page). Unknown sizes stay at 0 and ``aria2c``
    falls back to whatever the server returns. ``have`` is computed from
    the filesystem so already-downloaded parts are skipped on resume.

    Filenames are made unique by replacing the trailing ``-NNN.ext`` (or
    inserting one if the URL doesn't carry a numeric suffix) with the
    1-based part number from the URL's ``i=N`` query (or the list index
    when ``i=`` is absent). The reason: in modern Google Takeout, the
    same ``-001.zip`` path can address all parts of an archive — the
    disambiguator is the ``i=N`` query param, not the path. Letting
    ``aria2c -j 5`` race five writers on the same filename silently
    drops 4 of the 5 downloads and overwrites the one that finishes
    last (this was the "all 5 parts are 1.2 MB of HTML" bug from
    June 15 — the on-disk 001.zip was an auth-challenge page that
    aria2c wrote from one of the parallel attempts).
    """
    # Snapshot the output dir once (a single scandir) instead of doing
    # exists()+stat() per part. On a slow network FS (JuiceFS/encfs) 290
    # parts x 2 syscalls each was ~7 minutes of dead time before download
    # even started. One scandir is a single round-trip.
    on_disk = _dir_size_map(output_dir)
    parts: list[dict] = []
    for i, p in enumerate(payloads):
        url, filename = _split_url_filename_for_part(p, i)
        size = meta.sizes.get(i, 0) or 0
        disk_size = on_disk.get(filename, 0)
        have = disk_size > 0 and (size == 0 or disk_size >= size)
        parts.append({
            "num": i + 1,
            "url": url,
            "filename": filename,
            "size": size,
            "have": have,
        })
    # Order smallest-first so the fastest parts finish first and the
    # parallel pool (aria2c -j N) consumes them in size order. ``num``
    # stays tied to the part's ``i=`` identity; only the list (feed)
    # order changes. Unknown sizes (0) sort to the end so known-small
    # parts lead.
    parts.sort(key=lambda p: (p["size"] == 0, p["size"]))
    return parts


def _split_url_filename_for_part(payload: TakeoutPayload,
                                 list_index: int) -> tuple[str, str]:
    """Derive a unique (url, filename) pair for one part in a batch.

    The modern Google Takeout URL pattern serves all parts of one
    archive from the *same* path (``takeout-TS-BATCH-001.zip``) and
    uses the ``?i=N`` query param to select which part to return. So
    ``filename_hint()`` — which strips the query — would return the
    same name for every part, and ``aria2c -j 5`` would race 5
    writers on one file.

    We rebuild the filename with the part number from ``i=N`` (or the
    list index as a fallback) so each part lands on its own file. The
    URL itself is returned untouched — ``i=N`` already encodes the
    part index, so no further URL rewriting is needed.
    """
    url = payload.url
    base, file_num, ext, query = extract_url_parts(url)
    if not base or not ext:
        # Couldn't parse the takeout pattern (e.g. URL doesn't end in
        # .zip/.tgz). Fall back to filename_hint and let aria2c try.
        hint = payload.filename_hint()
        if hint == "unknown":
            hint = f"part-{list_index + 1:03d}.zip"
        return url, hint

    i_match = re.search(r'(?:^|[?&])i=(\d+)', query or "")
    part_num = int(i_match.group(1)) + 1 if i_match else (list_index + 1)
    # The URL path is the same for every part in modern mode, but we
    # still keep the original ``file_num`` (``001``) as a cosmetic hint
    # in the filename — the canonical disambiguator is ``i=N``.
    base_filename = base.split("/")[-1]
    filename = f"{base_filename}{part_num:03d}{ext}"
    return url, filename


def _drain_response(resp, max_bytes: int = 1024) -> bytes:
    """Read up to ``max_bytes`` from a streaming response and close it.

    Used by the pre-flight check to peek at the first ~1 KB of a
    response body without buffering the entire file (which could be
    500+ MB for a real archive). The response is closed afterward.
    """
    body = b""
    try:
        for chunk in resp.iter_content(chunk_size=4096):
            body += chunk
            if len(body) >= max_bytes:
                break
    except requests.RequestException:
        pass  # best effort — we already have the headers
    finally:
        resp.close()
    return body


def _save_auth_challenge_body(body: bytes, part_url: str,
                                output_dir: Path, *,
                                ctype: str = "?",
                                status: int = 0) -> Path:
    """Save a pre-flight HTML body to a debug file and return the path.

    Unlike :func:`_save_auth_challenge` which takes a live ``Response``,
    this takes the already-drained ``body`` bytes. Used when the pre-flight
    has already closed the response and only has the first ~1 KB.
    """
    if not body:
        return Path("<empty response body>")
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    debug_dir = output_dir if output_dir.is_dir() else Path.cwd()
    debug_path = debug_dir / f"auth_challenge_{ts}.html"
    try:
        header = (
            f"<!-- Pre-flight auth challenge for: {part_url} -->\n"
            f"<!-- Content-Type: {ctype} -->\n"
            f"<!-- HTTP {status} -->\n"
        ).encode("utf-8")
        debug_path.write_bytes(header + body)
    except OSError as e:
        debug(f"  could not save auth challenge: {e}")
        return Path("<unwriteable>")
    return debug_path


def _save_auth_challenge(response, part_url: str, output_dir: Path) -> Path:
    """Save a Google sign-in HTML response to a debug file and return
    the path. Used by the pre-flight check and post-download HTML
    detection so the user can inspect what Google actually returned.
    """
    body = b""
    try:
        # Prefer .content if available (real requests.Response has it).
        if hasattr(response, "content") and response.content:
            body = response.content
        # Fall back to draining iter_content() if the response was
        # opened with stream=True (which the pre-flight does to avoid
        # buffering a 1.2 GB archive in memory).
        if not body and hasattr(response, "iter_content"):
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    body += chunk
                    # Cap at a reasonable size for inspection — the
                    # first 64 KB is plenty to identify the challenge
                    # page and we don't want to write 1.2 MB of HTML.
                    if len(body) >= 64 * 1024:
                        break
            except Exception:
                pass
    except Exception:
        body = b""
    if not body:
        return Path("<unable to read response body>")
    # Save next to the takeout_state.json so the user can find it
    # without having to dig through the output dir tree.
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    debug_dir = output_dir if output_dir.is_dir() else Path.cwd()
    debug_path = debug_dir / f"auth_challenge_{ts}.html"
    try:
        # Include the URL in a comment so the user can match it to the
        # part that was being requested.
        header = (
            f"<!-- Pre-flight auth challenge for: {part_url} -->\n"
            f"<!-- Content-Type: {response.headers.get('content-type', '?')} -->\n"
            f"<!-- HTTP {response.status_code} -->\n"
        ).encode("utf-8")
        debug_path.write_bytes(header + body)
    except OSError as e:
        debug(f"  could not save auth challenge: {e}")
        return Path("<unwriteable>")
    return debug_path


def _preflight_full_download(parts: list[dict], payload: TakeoutPayload,
                              output_dir: Path) -> None:
    """Do a *full* GET of the smallest part to confirm the cookie
    works for the actual download path (not just Range probes).

    Raises ``SystemExit(1)`` with a clear error and a saved HTML
    blob if the response is the Google sign-in challenge. Returns
    silently if the response is a real archive (or a network blip —
    we don't want to over-block on transient errors).

    The check downloads only the first ~1 KB of the smallest part
    (stream-abort), so even a 500 MB part costs almost nothing.
    This is enough to distinguish a real ZIP archive (starts with
    ``PK\x03\x04``) from the Google sign-in HTML page (starts with
    ``<!doctype html>``), which is the key auth-challenge signal.
    """
    # Pick the smallest incomplete part. Skip if all complete (the
    # caller already checked, but be defensive).
    candidates = [p for p in parts if not p["have"] and p.get("size", 0) > 0]
    if not candidates:
        return
    target = min(candidates, key=lambda p: p["size"])
    info(_c("36", f"  Pre-flight: verifying cookie with a full GET of "
                  f"{target['filename']} ({human_size(target['size'])})..."))
    headers = dict(payload.headers)
    headers["Cookie"] = payload.cookie
    # NOTE: deliberately NO Range header here. The whole point is to
    # exercise the same code path aria2c will hit on the real download.
    #
    # We only read the first ~1 KB though: enough to distinguish a
    # real ZIP (PK\x03\x04) from the Google sign-in HTML page
    # (<!doctype html>), but cheap enough to run on every attempt.
    #
    # If the pre-flight passes, the actual download still starts from
    # byte 0 — we did NOT consume any of the response body.
    PREFLIGHT_PEEK = 1024  # bytes to read before aborting
    session = requests.Session()
    try:
        resp = session.get(target["url"], headers=headers, stream=True,
                           timeout=PROBE_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        warn(f"  Pre-flight request failed: {e}. Proceeding anyway "
             f"(aria2c may succeed).")
        return
    try:
        ctype = resp.headers.get("content-type", "")
        final_host = (resp.url.split("/")[2]
                      if "/" in resp.url else "")
        if final_host.endswith("accounts.google.com"):
            # Drain enough of the body to save for inspection, then abort.
            body = _drain_response(resp, PREFLIGHT_PEEK)
            saved = _save_auth_challenge_body(body, target["url"],
                                              output_dir,
                                              ctype=ctype,
                                              status=resp.status_code)
            err(f"Cookie is being challenged: pre-flight GET was "
                f"redirected to {final_host}.")
            err(f"  Google's sign-in page was saved to: {saved}")
            err(f"  This is the *probe-path vs download-path* mismatch: "
                f"Range probes succeed but full GETs get the sign-in page.")
            err(f"  Re-capture the cookie from a *download* request, not "
                f"the manage page:")
            err(f"    1. Open takeout.google.com/settings")
            err(f"    2. Click 'Download' on your export")
            err(f"    3. Wait for the browser to issue the actual file "
                f"request to takeout-download.usercontent.google.com")
            err(f"    4. THEN click the extension icon to capture")
            raise SystemExit(1)
        if "text/html" in ctype:
            body = _drain_response(resp, PREFLIGHT_PEEK)
            saved = _save_auth_challenge_body(body, target["url"],
                                              output_dir,
                                              ctype=ctype,
                                              status=resp.status_code)
            err(f"Cookie is being challenged: pre-flight GET returned "
                f"HTML (ct={ctype[:40]}).")
            err(f"  Google's sign-in page was saved to: {saved}")
            err(f"  See the top of that file for the URL that was "
                f"challenged and the full HTTP status.")
            err(f"  Re-capture from the *download* request as shown above, "
                f"or pass --parallel 1 to bypass Google's per-session "
                f"parallel-download limit.")
            raise SystemExit(1)
        if resp.status_code in (401, 403):
            body = _drain_response(resp, PREFLIGHT_PEEK)
            saved = _save_auth_challenge_body(body, target["url"],
                                              output_dir,
                                              ctype=ctype,
                                              status=resp.status_code)
            err(f"Cookie is being challenged: pre-flight GET returned "
                f"HTTP {resp.status_code}.")
            err(f"  Response saved to: {saved}")
            err(f"  Re-capture in your browser and try again.")
            raise SystemExit(1)
        # Read just enough to detect HTML-in-disguise (wrong content-type
        # but HTML body) and to confirm we got real archive bytes.
        body = _drain_response(resp, PREFLIGHT_PEEK)
        if _looks_like_html_bytes(body):
            # We received bytes that look like HTML even though the
            # Content-Type was something else. Some misconfigured
            # proxies return the wrong header but the right body.
            debug_path = output_dir / (
                f"auth_challenge_preflight_{datetime.now().strftime('%Y%m%dT%H%M%SZ')}.bin"
            )
            try:
                debug_path.write_bytes(body)
            except OSError:
                pass
            err(f"Cookie is being challenged: pre-flight body looks "
                f"like HTML even though Content-Type was {ctype!r}.")
            err(f"  Body saved to: {debug_path}")
            err(f"  Re-capture from the *download* request and try again.")
            raise SystemExit(1)
        # If we got here, the response is a real archive (or at least
        # not HTML). We only read ~1 KB, so we can't verify the full
        # file — but the key signal (HTML auth challenge) is gone.
        ok(f"  Pre-flight OK: got {human_size(len(body))} of "
           f"archive data, cookie is healthy for full downloads.")
    finally:
        resp.close()
        session.close()


def _looks_like_html_bytes(body: bytes) -> bool:
    """Like :func:`_looks_like_html` but on a raw byte buffer instead
    of a Path. The first 64 bytes are enough — Google sign-in pages
    start with ``<!doctype html>``, ``<html ...>``, or (rarely)
    ``<!-- comment --><html ...>``.
    """
    if not body:
        return False
    head = body[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head:
        return False
    # Match ``<!doctype html>`` / ``<!DOCTYPE HTML>``.
    if head.startswith(b"<!"):
        return b"html" in head[:32].lower() or b"doctype" in head[:32].lower()
    # Match ``<html ...>`` (no doctype, just bare HTML).
    if head.lower().startswith(b"<html"):
        return True
    return False


# ===========================================================================
# Discovery — Range probes that validate auth and record sizes
# ===========================================================================
def _probe_part(session: requests.Session, url: str,
                headers: dict) -> int | None:
    """Probe one part with a 1-byte Range request.

    Returns total size in bytes if the part exists, None if missing (404,
    400 bad-request from a mismatched query string, or any other non-OK
    response after auth checks).
    Raises AuthError if Google served a signin page or HTML.

    Google behavior to disambiguate:
      alive cookie + part OK     -> 206 + Content-Range: bytes 0-0/<size>
      alive cookie + part missing-> 404
      alive cookie + bad query   -> 400 (query params like `i=` may be
                                   part-specific; we treat as "missing")
      expired cookie             -> 302 -> accounts.google.com (final url),
                                     then 200 text/html (Google signin page)
      some servers ignore Range  -> 200 + Content-Length = full size
    """
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    resp = session.get(url, headers=h, stream=True, timeout=PROBE_TIMEOUT,
                       allow_redirects=True)
    try:
        ctype = resp.headers.get("content-type", "")
        final_host = resp.url.split("/")[2] if "/" in resp.url else ""
        debug(f"  <- {resp.status_code} "
              f"ct={ctype[:30]} host={final_host} "
              f"cr={resp.headers.get('content-range','')[:40]} "
              f"cl={resp.headers.get('content-length','')}")
        if final_host.endswith("accounts.google.com"):
            raise AuthError(f"redirected to {final_host}")
        if "text/html" in ctype:
            raise AuthError(f"server returned HTML (ct={ctype[:40]})")
        if resp.status_code in (401, 403):
            raise AuthError(f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            return None
        if resp.status_code == 400:
            # Mismatched query params (e.g. reusing `i=3` on part 002).
            # Treat as "this part doesn't exist".
            debug(f"  <- 400 (bad request); treating as missing part")
            return None
        if resp.status_code == 416:
            return 0  # part exists with 0 bytes
        if resp.status_code in (200, 206):
            cr = resp.headers.get("content-range", "")
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                return int(m.group(1))
            if resp.status_code == 200:
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit():
                    return int(cl)
            return 0  # 200/206 but no size info
        # Any other response (410 Gone, 500, etc.) → treat as missing.
        debug(f"  <- {resp.status_code} (unexpected); treating as missing part")
        return None
    finally:
        resp.close()


def _has_i_param(query: str) -> bool:
    """True if the query string contains an `i=N` part selector."""
    return bool(re.search(r'(?:^|[?&])i=\d+', query or ""))


def _build_probe_url(base_url: str, query: str | None,
                     i_value: int | None) -> str:
    """Build a probe URL.

    If `i_value` is not None, replace (or insert) `i=<i_value>` in the
    query string and return the full URL with `?…` appended. Otherwise
    pass the query through unchanged.
    """
    if query is None:
        return base_url
    if i_value is None:
        return f"{base_url}?{query}"
    # Replace `i=N` (or append if absent, though _has_i_param should be True).
    new_q = re.sub(r'(^|[?&])i=\d+', lambda m: f"{m.group(1)}i={i_value}", query, count=1)
    if not re.search(r'(?:^|[?&])i=\d+', new_q):
        new_q = (new_q + ("&" if new_q else "") + f"i={i_value}")
    return f"{base_url}?{new_q}"


def discover_parts(payload: TakeoutPayload, output_dir: Path,
                    max_parts: int | None = None) -> list[dict]:
    """Probe to discover all parts of an archive. Returns list of dicts:
    {num, url, filename, size, have}. Stops early on AuthError after part 1.

    Two URL patterns are supported:

    1. Modern Google Takeout (URL has `i=N` query param):
       - The `-NNN.zip` filename suffix is *cosmetic* — Google ignores it
         and uses `i=` to select which part of the archive to serve.
       - We sweep `i=0, 1, 2, …` and rely on HTTP 404/400 to signal
         end-of-set (the same-size heuristic is disabled because it
         false-positives on archives whose part sizes happen to repeat).
       - The original `i=N` in the captured URL is replaced — sweeping
         always starts from i=0 so we never miss earlier parts just
         because the captured URL happened to point at a middle part.

    2. Legacy Google Takeout (no `i=` in URL):
       - The `-NNN.zip` suffix is the actual part selector.
       - We vary the suffix and use a same-size heuristic to detect
         end-of-set, since some legacy servers don't return a clean
         404 for missing parts.
    """
    base, _, ext, query = extract_url_parts(payload.url)
    if not base:
        raise ValueError(f"Could not parse a Takeout URL pattern from:\n  {payload.url}")

    headers = dict(payload.headers)
    headers["Cookie"] = payload.cookie

    parts: list[dict] = []
    consecutive_misses = 0
    same_size_streak = 0
    last_size: int | None = None
    session = requests.Session()
    base_filename = base.split("/")[-1]

    modern = _has_i_param(query)
    # In modern mode the displayed part number is i+1 (1-based to match
    # what Google's manage page shows: "Part 1, Part 2, …"). In legacy
    # mode the displayed number is the filename suffix (also 1-based).
    loop_limit = max_parts if max_parts else MAX_PARTS

    if modern:
        info(f"Discovering parts (URL pattern: ...{base_filename}001.zip?i=<N>) "
             f"up to {loop_limit}")
    elif max_parts:
        info(f"Discovering {max_parts} part(s) (URL pattern: "
             f"...{base_filename}<NNN>{ext})")
    else:
        info(f"Discovering parts (URL pattern: "
             f"...{base_filename}<NNN>{ext})")

    # num is 1-based for display. In modern mode it maps to i=num-1.
    for num in range(1, loop_limit + 1):
        if modern:
            i_value = num - 1
            # The URL path ``-001.zip`` is shared across all parts of one
            # archive in modern Google Takeout — the disambiguator is
            # ``i=N``, not the path. We must still give each part a
            # unique on-disk filename, otherwise ``aria2c -j 5`` will
            # race 5 writers on one file. Use the part number (1-based
            # ``num``) to construct a unique filename; the URL itself
            # carries ``i=N`` so it stays correct.
            filename = f"{base_filename}{num:03d}{ext}"
            url = _build_probe_url(f"{base}001{ext}", query, i_value)
        else:
            filename = f"{base_filename}{num:03d}{ext}"
            url = f"{base}{num:03d}{ext}"
            if query:
                url += f"?{query}"

        debug(f"probe #{num:03d} GET {url}")
        try:
            size = _probe_part(session, url, headers)
        except AuthError as e:
            debug(f"probe #{num:03d} -> AuthError({e})")
            if num == 1:
                raise
            warn(f"Auth failed probing part {num:03d} ({e}); "
                 f"stopping discovery, {len(parts)} parts so far.")
            break

        if size is None:
            debug(f"probe #{num:03d} -> missing (end of set)")
            consecutive_misses += 1
            if consecutive_misses >= CONSECUTIVE_404_STOP:
                debug(f"  {CONSECUTIVE_404_STOP} consecutive misses; end of set")
                break
            continue
        consecutive_misses = 0
        debug(f"probe #{num:03d} -> {size} bytes")

        # Same-size heuristic is ONLY used in legacy mode. In modern
        # mode the server gives a clean 404 for missing parts, and the
        # heuristic false-positives on archives whose part sizes repeat
        # (e.g. several <1MB parts all rounding to the same display size).
        if not modern:
            if num > 1 and size == last_size:
                same_size_streak += 1
                if same_size_streak >= SAME_SIZE_STREAK_STOP:
                    debug(f"  {SAME_SIZE_STREAK_STOP} consecutive parts "
                          f"with identical size ({human_size(size)}); "
                          f"real set has {len(parts)} parts")
                    info(f"   Stopping: Google returned the same size "
                         f"({human_size(size)}) for {SAME_SIZE_STREAK_STOP} "
                         f"consecutive parts — this archive has {len(parts)} "
                         f"real part(s).")
                    break
            else:
                same_size_streak = 0
        last_size = size

        dest = output_dir / filename
        have = dest.exists() and dest.stat().st_size > 0 and (
            size == 0 or dest.stat().st_size >= size
        )
        parts.append({
            "num": num, "url": url, "filename": filename,
            "size": size, "have": have,
        })
        info(f"   {num:03d}  {human_size(size) if size else 'unknown':>10}  "
             f"{'have' if have else 'need'}")

    # Feed smallest-first so the fastest parts finish first and the
    # parallel pool (aria2c -j N) starts them in size order. ``num`` keeps
    # the part's probe identity; only list (feed) order changes. Unknown
    # sizes (0) sort last so known-small parts lead.
    parts.sort(key=lambda p: (p["size"] == 0, p["size"]))
    return parts


# ===========================================================================
# aria2c
# ===========================================================================
def detect_aria2c() -> bool:
    return shutil.which("aria2c") is not None


def build_aria2_input(parts: list[dict], payload: TakeoutPayload,
                      output_dir: Path) -> str:
    """Build an aria2c -i batch body for parts that still need downloading."""
    lines: list[str] = []
    for p in parts:
        if p["have"]:
            continue
        lines.append(p["url"])
        lines.append(f"  out={p['filename']}")
        lines.append(f"  dir={output_dir}")
        lines.append(f"  header=Cookie: {payload.cookie}")
        for k, v in payload.headers.items():
            if k.lower() == "cookie":
                continue
            lines.append(f"  header={k}: {v}")
    return "\n".join(lines) + ("\n" if lines else "")


def run_aria2c(input_body: str, output_dir: Path, parallel: int,
              render: TermRender | None = None,
              parts: list[dict] | None = None) -> int:
    """Run aria2c against the batch, optionally rendering a per-file grid.

    Returns the aria2c exit code. When `render` is provided, we spawn
    aria2c with --summary-interval=1 and parse the per-file summary lines
    into grid rows. Without `render`, aria2c streams directly to stdout.
    """
    if not input_body.strip():
        warn("Nothing to download; aria2c skipped.")
        return 0
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".aria2.txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(input_body)
        input_path = f.name

    # Build a filename -> num map so we can identify rows in aria2c output.
    num_by_filename: dict[str, int] = {}
    if parts:
        for p in parts:
            num_by_filename[p["filename"]] = p["num"]

    console_log = "info" if render else "warn"
    summary_interval = 1 if render else 10

    cmd = [
        "aria2c",
        "-i", input_path,
        "-j", str(parallel),
        "-x", "1", "-s", "1",          # Takeout blocks multi-stream
        "-c",                          # resume partials
        "--auto-file-renaming=false",
        "--allow-overwrite=true",      # overwrite stale partial without .1 suffix
        f"--console-log-level={console_log}",
        f"--summary-interval={summary_interval}",
        "--download-result=full",
        "--max-tries=5",
        "--retry-wait=10",
        "--timeout=60",
        "--file-allocation=none",
    ]

    if not render:
        # Plain streaming mode (no grid).
        try:
            proc = subprocess.run(cmd)
            return proc.returncode
        finally:
            try:
                os.unlink(input_path)
            except OSError:
                pass

    # Rendered mode: capture aria2c's stdout, parse, dispatch to grid.
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if proc.stdout is None:
            return proc.wait()
        # Per-file trackers keyed by aria2 GID.
        gid_state: dict[str, dict] = {}
        # Map GID -> filename via "added" / "Downloading" lines.
        gid_to_filename: dict[str, str] = {}
        filename_to_rowkey: dict[str, str] = {}
        filename_to_size: dict[str, int] = {
            p["filename"]: p["size"] for p in (parts or [])
        }
        for line in proc.stdout:
            line = line.rstrip("\n")
            # aria2c formats progress like:
            #   [#abc123 50MiB/100MiB(50%) CN:1 DL:50MiB ETA:1m]
            # We use the summary lines that aria2c emits with
            # --console-log-level=info.
            try:
                _update_from_aria2_line(
                    line, gid_state, gid_to_filename,
                    filename_to_rowkey, filename_to_size,
                    num_by_filename, render,
                )
            except Exception as e:
                debug(f"aria2c line parse error: {e!r} line={line!r}")
        return proc.wait()
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass


# Pattern that matches a per-file progress line aria2c emits, e.g.
#   [#abc12345 411.6MiB/8.0GiB(5%) CN:1 DL:50.0MiB ETA:2m14s]
# Note: aria2's DL field has no trailing /s — it's `DL:50.0MiB`.
_ARIA2_PROGRESS_RE = re.compile(
    r"\[#([0-9a-f]+)\s+"          # GID
    r"([\d.]+)([KMGT]?i?B)?"       # completed amount (unit may be plain B)
    r"/"
    r"([\d.]+)([KMGT]?i?B)?"       # total
    r"\((\d+)%\)"                  # percent
    r"\s+CN:\d+\s+DL:([\d.]+)([KMGT]?i?B)"   # speed
    r"(?:\s+ETA:([^\]\s]+))?"       # eta (stop at ] or whitespace)
)


def _aria2_unit_to_bytes(n_str: str, unit: str | None) -> int:
    """Convert aria2's `411.6MiB`-style strings to bytes."""
    try:
        n = float(n_str)
    except ValueError:
        return 0
    if not unit:
        return int(n)
    u = unit.rstrip("iB").rstrip("B")
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}.get(
        u, 1)
    return int(n * mult)


# Matches aria2c's per-download path line, emitted right after each
# download's progress line in the periodic summary:
#   FILE: /downloads/takeout-20260612T190148Z-15-001.zip
_ARIA2_FILE_RE = re.compile(r"^FILE:\s*(.+?)\s*$")


def _flush_buffered_progress(gid: str,
                             gid_state: dict,
                             gid_to_filename: dict,
                             filename_to_size: dict,
                             num_by_filename: dict,
                             render: TermRender) -> None:
    """Render the last buffered progress sample for ``gid`` now that its
    filename is known. No-op if there's nothing buffered."""
    filename = gid_to_filename.get(gid)
    if not filename:
        return
    st = gid_state.get(gid)
    if not st:
        return
    _render_file_row(render, filename, st.get("done", 0), st.get("total", 0),
                     st.get("pct", 0), st.get("speed", 0), st.get("eta", ""),
                     filename_to_size, num_by_filename)


def _update_from_aria2_line(line: str,
                            gid_state: dict,
                            gid_to_filename: dict,
                            filename_to_rowkey: dict,
                            filename_to_size: dict,
                            num_by_filename: dict,
                            render: TermRender) -> None:
    """Translate one aria2c stdout line into a grid row update.

    Real aria2c (verified against 1.35.0) emits its periodic summary as
    pairs of lines, one pair per active download::

        [#GID 16KiB/28MiB(0%) CN:1 DL:15KiB ETA:30m48s]
        FILE: /downloads/takeout-...-001.zip

    The progress ``[#GID ...]`` line carries the live numbers but NOT the
    filename; the ``FILE:`` line that immediately follows carries the
    filename but NOT the GID. So we bind GID->filename by remembering the
    GID from the most recent progress line and attaching it to the next
    ``FILE:`` line. The terminal ``Download Results:`` block (printed only
    after everything finishes) is used as a backstop for final status.

    ``gid_state['_last_gid']`` holds the most-recent progress GID across
    calls (the caller passes the same ``gid_state`` dict on every line).
    """
    # Detect the "Downloading" announcement, which gives us the filename.
    if line.startswith("Downloading"):
        return
    if line.startswith("[#"):
        m = _ARIA2_PROGRESS_RE.search(line)
        if not m:
            return
        gid = m.group(1)
        done = _aria2_unit_to_bytes(m.group(2), m.group(3))
        total = _aria2_unit_to_bytes(m.group(4), m.group(5))
        pct = int(m.group(6))
        speed_bps = _aria2_unit_to_bytes(m.group(7), m.group(8))
        eta = m.group(9) or ""
        # Always buffer the latest sample so the FILE: line (or a late
        # GID binding) can render it.
        gid_state[gid] = {
            "done": done, "total": total, "pct": pct,
            "speed": speed_bps, "eta": eta,
        }
        # Remember this GID so the FILE: line that follows can bind it.
        gid_state["_last_gid"] = gid
        filename = gid_to_filename.get(gid)
        if not filename:
            # Filename not known yet — the FILE: line right after this
            # will bind it and flush the buffered sample.
            return
        _render_file_row(render, filename, done, total, pct,
                         speed_bps, eta, filename_to_size,
                         num_by_filename)
        return
    # FILE: path line — binds the most-recent progress GID to a filename
    # *during* the download, so the grid updates live instead of only at
    # the end.
    mf = _ARIA2_FILE_RE.match(line)
    if mf:
        path = mf.group(1).strip()
        basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        # Ignore non-archive paths aria2c may print (e.g. .aria2 control).
        if not basename or basename.endswith(".aria2"):
            return
        last_gid = gid_state.get("_last_gid")
        if last_gid and gid_to_filename.get(last_gid) != basename:
            gid_to_filename[last_gid] = basename
            filename_to_rowkey.setdefault(basename, f"file:{basename}")
            _flush_buffered_progress(last_gid, gid_state, gid_to_filename,
                                     filename_to_size, num_by_filename, render)
        elif last_gid:
            # Already bound; just refresh the row from the buffered sample.
            _flush_buffered_progress(last_gid, gid_state, gid_to_filename,
                                     filename_to_size, num_by_filename, render)
        return
    # Terminal "Download Results:" block (printed once at the very end):
    #   gid|stat|avg speed|path
    #   abc123|OK|  1.2MiB/s|./foo.zip
    # Backstop for final status when the FILE: binding never arrived
    # (e.g. an instant 404).
    m2 = re.search(
        r"\b([0-9a-f]{4,16})\s*\|\s*(OK|ERR|WARN)\s*\|.*?\|\s*(.+?\.(?:zip|tgz))\s*$",
        line)
    if m2:
        gid, status, filename = m2.group(1), m2.group(2), m2.group(3).strip()
        # `path` may be a full path; extract basename.
        basename = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        gid_to_filename[gid] = basename
        if gid not in filename_to_rowkey:
            filename_to_rowkey[basename] = f"file:{basename}"
        if status in ("OK", "ERR", "WARN"):
            # Final render: 100% for OK, current pct otherwise.
            size = filename_to_size.get(basename, 0)
            pct = 100 if status == "OK" else gid_state.get(gid, {}).get("pct", 0)
            done = size if status == "OK" else gid_state.get(gid, {}).get("done", 0)
            _render_file_row(render, basename, done, size or total_of(gid_state, gid),
                             pct, 0, "", filename_to_size, num_by_filename)


def total_of(gid_state: dict, gid: str) -> int:
    return gid_state.get(gid, {}).get("total", 0)


def _format_file_row(filename: str, done: int, total: int, pct: int,
                     speed_bps: int, eta: str, num: int,
                     cols: int | None = None) -> str:
    """Build a single width-fitted progress row string (no rendering).

    Layout priority, narrow -> wide:
      1. "#NNN <filename> [bar] PP%"   (always shown; bar shrinks 8..20)
      2. " done/total"                  (added if it fits)
      3. " speed"                       (added if it fits)
      4. " ETA eta"                     (added if it fits)
    The filename and a >=8-char bar are protected; trailing columns drop
    first when space is tight. Never wraps (caller's redraw truncates too).
    """
    if cols is None:
        try:
            cols = shutil.get_terminal_size((100, 20)).columns
        except (OSError, ValueError):
            cols = 100
    cols = max(20, cols) - 1  # leave 1 col so EOL doesn't auto-wrap

    parts_str = (human_size(done) + "/" + human_size(total)) if total else human_size(done)
    speed_str = (human_size(speed_bps) + "/s") if speed_bps else "-"
    eta_str = eta or "-"

    prefix = f"  #{num:03d}  {filename}  "
    pct_str = f" {pct:3d}%"
    room_for_bar = cols - len(prefix) - len(pct_str)
    bar_width = max(8, min(20, room_for_bar))
    bar = make_progress_bar(pct, width=bar_width)

    row = f"{prefix}{bar}{pct_str}"
    for extra in (f"  {parts_str}", f"  {speed_str}", f"  ETA {eta_str}"):
        if len(row) + len(extra) <= cols:
            row += extra
        else:
            break
    return row


def _render_file_row(render: TermRender, filename: str, done: int,
                     total: int, pct: int, speed_bps: int, eta: str,
                     filename_to_size: dict, num_by_filename: dict) -> None:
    if total <= 0:
        total = filename_to_size.get(filename, 0)
    row = _format_file_row(filename, done, total, pct, speed_bps, eta,
                           num_by_filename.get(filename, 0))
    render.update_row(f"file:{filename}", row)


def _eta_str(done: int, total: int, speed_bps: float) -> str:
    """Human ETA from remaining bytes and current speed. '-' when unknown."""
    if speed_bps <= 0 or total <= 0 or done >= total:
        return "-"
    secs = int((total - done) / speed_bps)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


# ===========================================================================
# Internal downloader driver (replaces aria2c)
# ===========================================================================
def run_internal(parts: list[dict], payload: TakeoutPayload, output_dir: Path,
                 parallel: int, render: "TermRender | None" = None) -> dict:
    """Download the parts that still need fetching using the in-process
    :class:`InternalDownloader`, driving the live grid (when ``render`` is
    given) from the downloader's exact byte counters.

    Returns a dict ``{"auth_failed": bool, "auth_body": bytes,
    "auth_url": str, "failed": [num,...]}`` so the caller's retry/re-prompt
    loop can react the same way it did to aria2c's exit code.

    Unlike the aria2c path, progress here does NOT depend on stdout being a
    TTY: the grid is fed from in-process counters, and when there's no grid
    we emit periodic one-line status logs so SSH/Docker/piped runs still
    show movement.
    """
    from takeout_downloader import InternalDownloader

    need = [p for p in parts if not p["have"]]
    if not need:
        return {"auth_failed": False, "auth_body": b"", "auth_url": "", "failed": []}

    num_by_filename = {p["filename"]: p["num"] for p in parts}
    size_by_filename = {p["filename"]: p["size"] for p in parts}

    dl = InternalDownloader(
        cookie=payload.cookie,
        headers=payload.headers,
        output_dir=output_dir,
        parallel=parallel,
        logger=log,
    )

    # Throttle a plain one-line summary to the LOG FILE on every run
    # (grid or not), so `tail -f takeout_cli.log` from a second SSH session
    # always shows a heartbeat -- the grid only paints the terminal, which
    # is invisible to the log and to anyone not watching that exact pane.
    last_log = [0.0]
    total_parts = len(parts)
    grand_total = sum((p["size"] or 0) for p in parts)

    def _log_heartbeat(done_n, active, got, spd) -> None:
        now = time.monotonic()
        if now - last_log[0] < 5.0:
            return
        last_log[0] = now
        log.info(f"  progress: {done_n}/{total_parts} done | {len(active)} active | "
                 f"{human_size(got)}/{human_size(grand_total)} | "
                 f"{human_size(int(spd))}/s")

    def on_progress(snapshot) -> None:
        done_n = sum(1 for pp in snapshot if pp.status == "done")
        active = [pp for pp in snapshot if pp.status == "active"]
        got = sum(pp.done for pp in snapshot)
        spd = sum(pp.speed_bps for pp in active)
        # Always emit a throttled heartbeat to the log file.
        _log_heartbeat(done_n, active, got, spd)
        if render is not None and getattr(render, "enabled", False):
            # ONE aggregate repaint per tick. With hundreds of parts we
            # can't (and shouldn't) draw a row per part: that floods the
            # terminal and stalls the process. Show a summary header plus
            # only the currently-active parts, clamped to the reserved
            # body height.
            try:
                cols = shutil.get_terminal_size((100, 24)).columns
            except (OSError, ValueError):
                cols = 100
            overall_pct = int(got * 100 / grand_total) if grand_total else 0
            render.set_header(
                f"  {done_n}/{total_parts} done | {len(active)} active | "
                f"{human_size(got)}/{human_size(grand_total)} "
                f"({overall_pct}%) | {human_size(int(spd))}/s"
            )
            rows = [
                _format_file_row(
                    pp.filename, pp.done, pp.total, pp.pct,
                    int(pp.speed_bps),
                    _eta_str(pp.done, pp.total, pp.speed_bps),
                    num_by_filename.get(pp.filename, 0), cols=cols)
                for pp in active
            ]
            render.set_rows(rows)
        # No grid (non-TTY/NO_GRID): the log heartbeat above is the only
        # progress channel, which is exactly what a piped/SSH run needs.

    result = dl.download(need, on_progress=on_progress)

    return {
        "auth_failed": result.auth_failed,
        "auth_body": result.auth_body,
        "auth_url": result.auth_url,
        "failed": result.failed,
    }


# ===========================================================================
# Verification — size + ZIP signature
# ===========================================================================
def is_valid_zip(path: Path) -> bool:
    """Quick check that `path` looks like a ZIP archive (EOCD in last 1KB).

    On Windows, seeking before byte 0 raises OSError, so clamp the offset
    to the actual file size.
    """
    try:
        size = path.stat().st_size
        if size < 22:  # EOCD minimum size
            return False
        with path.open("rb") as f:
            offset = min(ZIP_EOCD_SCAN, size)
            f.seek(-offset, 2)
            tail = f.read(offset)
        return ZIP_EOCD in tail
    except OSError:
        return False


def _looks_like_html(path: Path) -> bool:
    """True if the first bytes of `path` look like an HTML document.

    The Google sign-in challenge page is ~1.2 MB of HTML — when the
    cookie is challenged mid-download, ``aria2c`` writes that page to
    disk in place of the real archive. We detect it cheaply by peeking
    at the first few bytes. The check is permissive (whitespace, BOM,
    or doctype) because Google's actual response starts with
    ``<!doctype html>`` or ``<html ...>``.
    """
    try:
        with path.open("rb") as f:
            head = f.read(64)
    except OSError:
        return False
    return _looks_like_html_bytes(head)

def verify_parts(parts: list[dict], output_dir: Path) -> tuple[list[dict], list[dict]]:
    """Re-check the output dir. Returns (complete, incomplete).
    A part is complete if:
      - the file exists,
      - the size matches what the probe said (or is >= the probed size),
      - the file looks like a real ZIP (EOCD signature present).
    """
    complete, incomplete = [], []
    # One scandir snapshot instead of exists()+stat() per part. On a slow
    # network FS a fresh 290-part run otherwise spent minutes here just
    # confirming nothing is on disk yet.
    on_disk = _dir_size_map(output_dir)
    for p in parts:
        dest = output_dir / p["filename"]
        disk_size = on_disk.get(p["filename"], 0)
        if disk_size == 0:
            p["have"] = False
            incomplete.append(p)
            continue
        if p["size"] and disk_size < p["size"]:
            warn(f"{p['filename']}: "
                 f"size {human_size(disk_size)} < "
                 f"expected {human_size(p['size'])} (will retry)")
            p["have"] = False
            incomplete.append(p)
            continue
        if not is_valid_zip(dest):
            # If the partial actually contains an HTML page (Google
            # sign-in challenge), unlink it so aria2c -c doesn't try
            # to "resume" from a 1.2 MB HTML blob. A legit in-progress
            # ZIP partial would not look like HTML, so this is a safe
            # heuristic.
            if _looks_like_html(dest):
                warn(f"{p['filename']}: looks like an HTML sign-in "
                     f"page (auth challenge) — deleting partial so "
                     f"aria2c re-downloads from scratch.")
                try:
                    dest.unlink()
                except OSError as e:
                    debug(f"  unlink failed: {e}")
                p["have"] = False
                incomplete.append(p)
                continue
            warn(f"{p['filename']}: not a valid ZIP (missing EOCD signature)")
            p["have"] = False
            incomplete.append(p)
            continue
        p["have"] = True
        complete.append(p)
    return complete, incomplete


# ===========================================================================
# Heuristic: did the cookie die mid-run?
# ===========================================================================
def looks_like_auth_failure(parts: list[dict], incomplete: list[dict]) -> bool:
    """True if more than 80% of parts are incomplete — the typical cookie
    expiry signature."""
    if not parts or not incomplete:
        return False
    return len(incomplete) / len(parts) > 0.8


# ===========================================================================
# Main
# ===========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a Google Takeout archive using aria2c. "
                    "Paste a JSON payload, the CLI does the rest.",
    )
    parser.add_argument("-p", "--parallel", type=int, default=PARALLEL,
                        help=f"concurrent downloads (default {PARALLEL})")
    parser.add_argument("--max-parts", type=int, default=MAX_PARTS,
                        help=f"max parts to discover (default {MAX_PARTS})")
    parser.add_argument("--version", action="version",
                        version=f"takeout_cli {VERSION}")
    parser.add_argument("--out", "--output-dir", dest="output_dir",
                        help="output directory for archives (prompted if omitted)")
    parser.add_argument("--fresh", "--no-resume", dest="fresh",
                        action="store_true",
                        help="ignore saved state, re-discover from scratch")
    parser.add_argument("--dry-run", dest="dry_run",
                        action="store_true",
                        help="validate cookie and discover parts without downloading")
    parser.add_argument("--reset-config", dest="reset_config",
                        action="store_true",
                        help="clear ~/.takeout-cli.json (forget last folder)")
    parser.add_argument("--relay", dest="relay", action="store_true",
                        help="receive the payload via an ephemeral browser "
                             "relay instead of terminal paste (good for "
                             "SSH/tmux/Docker where paste is unreliable)")
    parser.add_argument("--tunnel", dest="tunnel", action="store_true",
                        help="with --relay, expose the relay publicly via a "
                             "Cloudflare quick tunnel (no account needed)")
    parser.add_argument("--relay-timeout", dest="relay_timeout", type=int,
                        default=int(os.environ.get("PASTE_RELAY_TIMEOUT", "600")),
                        help="seconds the relay waits before self-destruct "
                             "(default 600)")
    parser.add_argument("--no-color", dest="no_color", action="store_true",
                        help="disable ANSI colors (same as NO_COLOR=1); "
                             "useful for logs, pipes, and dumb terminals")
    parser.add_argument("--engine", dest="engine",
                        choices=("internal", "aria2c"),
                        default=os.environ.get("TAKEOUT_ENGINE", "internal"),
                        help="download engine: 'internal' (in-process, exact "
                             "live progress, no external binary; default) or "
                             "'aria2c' (legacy subprocess, needs aria2c on "
                             "PATH)")
    args = parser.parse_args()

    # Honor --no-color by flipping the module-level color switch the
    # `_c()` helper reads. The NO_COLOR env var is already respected at
    # import time; this lets a user force it off per-invocation without
    # setting an env var. Color stays on only when the output is a TTY.
    if args.no_color:
        global _USE_COLOR
        _USE_COLOR = False

    # Wire up the payload source. With --relay, every paste prompt (including
    # the stale-cookie re-prompt loop) reads from the ephemeral browser relay.
    global _PAYLOAD_SOURCE
    if args.relay:
        _PAYLOAD_SOURCE = relay_paste_source(args.tunnel, args.relay_timeout)

    if args.reset_config:
        p = _config_path()
        if p.exists():
            p.unlink()
            ok(f"Config cleared: {p}")
        else:
            info(f"No config file at {p}")
        return 0

    if args.output_dir:
        os.environ["OUTPUT_DIR"] = args.output_dir
        # Also persist it so subsequent runs default to the same folder.
        cfg = load_config()
        cfg["output_dir"] = str(Path(args.output_dir).expanduser().resolve())
        save_config(cfg)

    output_dir = resolve_output_dir()
    log_path = Path(
        os.environ.get(
            "TAKEOUT_LOG_FILE",
            str(output_dir / "takeout_cli.log"),
        )
    ).expanduser()
    max_bytes = int(os.environ.get("TAKEOUT_LOG_MAX_BYTES", str(500 * 1024)))
    backup_count = int(os.environ.get("TAKEOUT_LOG_BACKUP_COUNT", "3"))

    _install_logger(log_path, max_bytes, backup_count)
    info(f"Version: {VERSION}")
    info(f"Log file: {log_path} "
         f"(rotating at {max_bytes // 1024}KB, "
         f"keeping {backup_count} backups)")
    info(f"Python: {sys.version.split()[0]} on {sys.platform}")
    info(f"Working dir: {Path.cwd()}")
    info(f"Output directory: {output_dir}")

    if args.engine == "aria2c" and not detect_aria2c():
        err("aria2c not found on PATH. Install it (apt install aria2) and "
            "retry, or use the default --engine internal (no binary needed).")
        return 2

    header(f"Google Takeout Downloader v{VERSION} — paste, go.")
    info("")

    # Ask for output dir FIRST, before the JSON paste. If --out is passed
    # on the command line, skip the prompt entirely.
    if args.output_dir:
        chosen_output_dir = output_dir
    else:
        # Root the arrow-key picker at the shared Takeout *base*
        # (e.g. /opt/.../google-takeout), NOT at whatever subfolder
        # OUTPUT_DIR happens to point at. _derive_picker_base walks up to
        # the google-takeout component (or honors TAKEOUT_BASE_DIR), so the
        # user can always pick a sibling folder or create a new one rather
        # than being trapped inside one previously-used subfolder.
        picker_base = _derive_picker_base(output_dir)
        if picker_base is not None and picker_base.is_dir():
            chosen_output_dir = prompt_for_output_subfolder(picker_base, output_dir)
        else:
            chosen_output_dir = prompt_for_output_dir(output_dir)
        if chosen_output_dir != output_dir:
            # Re-aim the logger at the chosen folder. Logger handlers are
            # already installed pointing at the env-derived path; swap them.
            new_log_path = chosen_output_dir / "takeout_cli.log"
            _install_logger(new_log_path, max_bytes, backup_count)
            info(f"Log file: {new_log_path} "
                 f"(rotating at {max_bytes // 1024}KB, "
                 f"keeping {backup_count} backups)")
        output_dir = chosen_output_dir
        # Persist the chosen folder so next run skips the prompt.
        cfg = load_config()
        cfg["output_dir"] = str(output_dir)
        save_config(cfg)
        debug(f"Saved output_dir to {_config_path()}")

    payload, payload_ctx = parse_one_payload()
    payload_mode = payload_ctx["mode"]
    payload_meta = payload_ctx["meta"]
    payload_all = payload_ctx["all_payloads"]
    payload_pre_built = payload_ctx.get("pre_built_parts")

    # Try to resume from state in the chosen folder.
    state = load_state(output_dir)
    parts: list[dict] | None = None
    if state and state_matches_payload(state, payload) and not args.fresh:
        parts = state_to_parts(state, payload)
        if parts:
            saved_complete = sum(1 for p in parts if p["have"])
            # Sanity check: a typical Takeout export has 3+ parts. If the
            # saved state has only 1-2 parts, discovery probably ran with a
            # stale cookie and missed the rest. Warn and offer to re-run.
            if len(parts) <= 2:
                warn(f"State file has only {len(parts)} parts. "
                     "This may be incomplete from a previous run.")
                info("Pass --fresh to force re-discovery, or type the actual "
                     "part count below to extend the list.")
                # Don't trust the state; fall through to discovery. The
                # discovery loop will detect existing files via verify.
            else:
                ok(f"Resuming from {state_path(output_dir)}: "
                   f"{saved_complete}/{len(parts)} parts marked complete.")
                info("Re-verifying files on disk before downloading anything.")
                # Re-verify saved parts against the actual filesystem. The
                # size on disk could differ from what's in state if files
                # changed.
                _, incomplete = verify_parts(parts, output_dir)
                parts = [p for p in parts if p["have"]] + incomplete

    # Parts mode: the multi-payload's URLs ARE the parts of one batch.
    # Skip discovery entirely — we already know N, sizes, and have the
    # exact URLs the extension built from the page's [data-download-uri]
    # buttons. No probe, no "how many parts?" prompt, no menu.
    if not parts and payload_mode == "parts":
        info("")
        info(_c("1;36", f"Detected {payload_meta.expectedParts} part(s) "
                        f"from the multi-payload (extension scraped them "
                        f"from the page)."))
        parts = _build_parts_from_payloads(
            payload_all, payload_meta, output_dir
        )
        payload_pre_built = parts

    # Discover (also validates auth). Re-prompt on auth failure.
    if not parts:
        # If we already have a multi-payload that lists all parts, skip
        # the prompt and let discover_parts auto-stop at the end-of-set
        # rather than asking the user for a part count.
        if payload_mode == "single" and payload_meta.expectedParts:
            user_part_count = payload_meta.expectedParts
            info("")
            info(_c("1;36", f"Multi-payload says this export has "
                            f"{user_part_count} part(s); skipping prompt."))
        else:
            info("")
            info(_c("1;36", "How many parts are in this export?"))
            info(_c("36", "  (Check your Google Takeout page — it shows "
                          "the part count."))
            info(_c("36", "  Press Enter to auto-detect via probes, or "
                          "type a number.)"))
            while True:
                try:
                    raw = input("  parts > ").strip()
                except EOFError:
                    raw = ""
                if not raw:
                    user_part_count = None
                    break
                if raw.lower() in ("q", "quit", "exit"):
                    raise SystemExit(0)
                try:
                    user_part_count = int(raw)
                    if user_part_count <= 0:
                        err("  Part count must be a positive number.")
                        continue
                    break
                except ValueError:
                    err("  Please enter a number, or press Enter for "
                        "auto-detect.")
        info(_c("1;36", "How many parts are in this export?"))
        info(_c("36", "  (Check your Google Takeout page — it shows the part count."))
        info(_c("36", "  Press Enter to auto-detect via probes, or type a number.)"))
        while True:
            try:
                raw = input("  parts > ").strip()
            except EOFError:
                raw = ""
            if not raw:
                user_part_count = None
                break
            if raw.lower() in ("q", "quit", "exit"):
                raise SystemExit(0)
            try:
                user_part_count = int(raw)
                if user_part_count <= 0:
                    err("  Part count must be a positive number.")
                    continue
                break
            except ValueError:
                err("  Please enter a number, or press Enter for auto-detect.")

        auth_failures = 0
        while True:
            try:
                parts = discover_parts(payload, output_dir,
                                        max_parts=user_part_count)
                break
            except AuthError as e:
                auth_failures += 1
                if auth_failures > MAX_AUTH_REPROMPTS:
                    err(f"Auth still failing after {MAX_AUTH_REPROMPTS} attempts. "
                        "Re-capture in your browser, then re-run this script.")
                    return 1
                err(f"Auth failed during discovery ({e}).")
                info("Re-capture in your browser, then paste the new JSON below.")
                payload, payload_ctx = parse_one_payload()
                payload_mode = payload_ctx["mode"]
                payload_meta = payload_ctx["meta"]
                payload_all = payload_ctx["all_payloads"]
                payload_pre_built = payload_ctx.get("pre_built_parts")
            except ValueError as e:
                err(str(e))
                info("Paste a corrected JSON payload below.")
                payload, payload_ctx = parse_one_payload()
                payload_mode = payload_ctx["mode"]
                payload_meta = payload_ctx["meta"]
                payload_all = payload_ctx["all_payloads"]
                payload_pre_built = payload_ctx.get("pre_built_parts")

    if not parts:
        err("No archive parts found. The URL may be wrong or the set is empty.")
        return 1

    # Iterate over each batch. The single-batch case is just a 1-element
    # list. The batches_all mode (user picked 'a' in the multi-export
    # menu) sets up all_sorted so we loop through every archive
    # smallest-first, asking for a fresh cookie when one expires and
    # resuming partials on the next pass.
    batches = payload_ctx.get("all_sorted") or [payload]
    batches_total = len(batches)
    batches_failed = 0

    for batch_idx, batch_payload in enumerate(batches, 1):
        if batches_total > 1:
            header(f"Batch {batch_idx}/{batches_total}: "
                   f"{batch_payload.filename_hint()}")

        rc = _download_one_batch(
            batch_payload, output_dir, args, log,
            initial_parts=parts if batch_idx == 1 else None,
            initial_state=state if batch_idx == 1 else None,
        )
        if rc != 0:
            batches_failed += 1

    if batches_total > 1:
        if batches_failed == 0:
            ok(f"All {batches_total} batches downloaded to {output_dir}")
            return 0
        err(f"{batches_failed}/{batches_total} batches had failures; "
            "see log for details.")
        return 1
    return 0


def _download_one_batch(payload: TakeoutPayload,
                          output_dir: Path,
                          args,
                          log,
                          initial_parts: list[dict] | None = None,
                          initial_state: dict | None = None) -> int:
    """Download one Takeout batch: discover parts, run aria2c, verify.

    Extracted from main() so that ``batches_all`` mode (user picks ``[a]``
    in the multi-export menu) can call it once per archive in the
    smallest-first list. Returns 0 on full success, 1 on any incomplete
    part. The caller decides whether the overall exit code reflects
    a partial failure across multiple batches.

    ``initial_parts`` / ``initial_state`` let the caller reuse work from
    the first call (typically only set for batch 1).
    """
    # Counter for the auth-failure re-prompt loop. Must be initialized
    # here (outside the `if not parts:` discovery block) because the
    # `while need:` download loop below also references it when the
    # cookie expires mid-download. The previous placement (inside the
    # discovery-only branch) caused UnboundLocalError whenever parts
    # came from the multi-payload pre-built path (which skips the
    # `if not parts:` branch entirely).
    auth_failures = 0
    # Try to resume from state in the chosen folder.
    state = initial_state if initial_state is not None else load_state(output_dir)
    parts: list[dict] | None = initial_parts
    if parts is None and state and state_matches_payload(state, payload) and not args.fresh:
        parts = state_to_parts(state, payload)
        if parts:
            saved_complete = sum(1 for p in parts if p["have"])
            if len(parts) <= 2:
                warn(f"State file has only {len(parts)} parts. "
                     "This may be incomplete from a previous run.")
                info("Pass --fresh to force re-discovery, or type the actual "
                     "part count below to extend the list.")
            else:
                ok(f"Resuming from {state_path(output_dir)}: "
                   f"{saved_complete}/{len(parts)} parts marked complete.")
                info("Re-verifying files on disk before downloading anything.")
                _, incomplete = verify_parts(parts, output_dir)
                parts = [p for p in parts if p["have"]] + incomplete

    if not parts:
        # Last-resort probe-based discovery for the single-batch case.
        # In multi-batch mode the manifest fetch already pre-resolved
        # everything so this should always be a no-op for batch > 1.
        info("")
        info(_c("1;36", "How many parts are in this export?"))
        info(_c("36", "  (Check your Google Takeout page — it shows "
                      "the part count."))
        info(_c("36", "  Press Enter to auto-detect via probes, or "
                      "type a number.)"))
        user_part_count: int | None = None
        while True:
            try:
                raw = input("  parts > ").strip()
            except EOFError:
                raw = ""
            if not raw:
                break
            if raw.lower() in ("q", "quit", "exit"):
                raise SystemExit(0)
            try:
                user_part_count = int(raw)
                if user_part_count <= 0:
                    err("  Part count must be a positive number.")
                    continue
                break
            except ValueError:
                err("  Please enter a number, or press Enter for "
                    "auto-detect.")

        while True:
            try:
                parts = discover_parts(payload, output_dir,
                                        max_parts=user_part_count)
                break
            except AuthError as e:
                auth_failures += 1
                if auth_failures > MAX_AUTH_REPROMPTS:
                    err(f"Auth still failing after {MAX_AUTH_REPROMPTS} "
                        "attempts. Re-capture in your browser, then re-run "
                        "this script.")
                    return 1
                err(f"Auth failed during discovery ({e}).")
                info("Re-capture in your browser, then paste the new JSON below.")
                payload, payload_ctx = parse_one_payload()
                payload_mode = payload_ctx["mode"]
                payload_meta = payload_ctx["meta"]
                payload_all = payload_ctx["all_payloads"]
                payload_pre_built = payload_ctx.get("pre_built_parts")
            except ValueError as e:
                err(str(e))
                info("Paste a corrected JSON payload below.")
                payload, payload_ctx = parse_one_payload()
                payload_mode = payload_ctx["mode"]
                payload_meta = payload_ctx["meta"]
                payload_all = payload_ctx["all_payloads"]
                payload_pre_built = payload_ctx.get("pre_built_parts")

    if not parts:
        err("No archive parts found. The URL may be wrong or the set is empty.")
        return 1

    # Persist state immediately so a Ctrl-C mid-run doesn't lose progress.
    if not state or not state_matches_payload(state, payload):
        state = make_state(parts, payload, output_dir)
        save_state(output_dir, state)

    total = sum(p["size"] for p in parts)
    have = [p for p in parts if p["have"]]
    need = [p for p in parts if not p["have"]]
    ok(f"Found {len(parts)} parts, {human_size(total)} total. "
       f"{len(have)} already complete, {len(need)} to download.")
    if not need:
        ok("Everything is already downloaded. Nothing to do.")
        state = update_state_from_parts(state, parts)
        save_state(output_dir, state)
        return 0

    # ----------------------------------------------------------------
    # Dry-run mode: validate cookie + discover parts, then exit.
    # Useful for verifying everything looks right before committing to
    # a long download.
    # ----------------------------------------------------------------
    if getattr(args, 'dry_run', False):
        ok(f"Dry-run: {len(parts)} parts, {human_size(total)} total, "
           f"cookie is valid.")
        for p in parts:
            status = _c("32", "✓") if p["have"] else _c("33", "↓")
            # The filename is a ctrl+click target opening the part's URL;
            # the trimmed URL stays visible for terminals without OSC 8.
            name = _link(p["url"], f"{p['filename']:40s}")
            info(f"  {status} {name} "
                 f"{human_size(p['size']):>10s}  {_link(p['url'], p['url'][:80])}")
        ok(f"Dry-run complete. Run without --dry-run to download.")
        state = update_state_from_parts(state, parts)
        save_state(output_dir, state)
        return 0

    # Main download loop
    attempt = 0
    engine = getattr(args, "engine", "internal")

    # ----------------------------------------------------------------
    # Pre-flight check: prove the cookie works for *full-file* GETs
    # before committing to the download. Only meaningful for aria2c,
    # which would blindly write a 1.2 MB HTML sign-in page to disk if
    # the session were challenged mid-download.
    #
    # The internal engine does NOT need this: every worker inspects the
    # first chunk of each part and raises an AuthChallenge *before* any
    # bytes touch the archive file, so a sign-in page can never corrupt
    # an output file. Running the pre-flight there just adds a blocking,
    # feedback-less request before downloads start (the exact "it hangs
    # on pre-flight" symptom). So we skip it for the internal engine and
    # go straight to downloading. Set TAKEOUT_PREFLIGHT=1 to force it on,
    # or =0 to force it off, regardless of engine.
    # ----------------------------------------------------------------
    _pf = os.environ.get("TAKEOUT_PREFLIGHT", "").strip()
    do_preflight = (_pf == "1") or (_pf != "0" and engine == "aria2c")
    if do_preflight:
        _preflight_full_download(parts, payload, output_dir)

    use_grid = sys.stdout.isatty() and os.environ.get("NO_GRID") is None
    render = TermRender(enabled=use_grid)
    if use_grid:
        # The internal engine paints an aggregate header + only the active
        # rows (one batched repaint per tick), so it needs just enough rows
        # for the concurrent slots -- NOT one row per part. Reserving
        # len(parts) rows for 290 parts is what flooded the terminal and
        # stalled the run. aria2c's parser still uses one row per file.
        if engine == "aria2c":
            grid_rows = len(parts)
        else:
            grid_rows = max(1, min(args.parallel, len(parts)))
        render.begin(n_rows=grid_rows)
        info("")  # leave room above the grid for the header banner we just drew

    def header_line() -> str:
        done = sum(1 for p in parts if p["have"])
        active = sum(1 for p in parts
                     if not p["have"]
                     and (output_dir / p["filename"]).exists()
                     and (output_dir / p["filename"]).stat().st_size > 0)
        n_need = sum(1 for p in parts if not p["have"])
        return (f"  Pass {attempt} | {done}/{len(parts)} done | "
                f"{active} active | {n_need} pending | "
                f"output: {output_dir}")

    def footer_line() -> str:
        return ""

    while need:
        attempt += 1
        if use_grid:
            render.set_header(header_line())
        else:
            header(f"Downloading {len(need)} parts — pass {attempt} "
                   f"({engine}, {args.parallel} concurrent)")

        auth_challenge_body = b""
        auth_challenge_url = ""
        if engine == "aria2c":
            body = build_aria2_input(parts, payload, output_dir)
            rc = run_aria2c(body, output_dir, args.parallel,
                            render=render if use_grid else None,
                            parts=parts)
            if rc != 0:
                warn(f"aria2c exited with code {rc} "
                     f"(some files may have failed).")
        else:
            res = run_internal(parts, payload, output_dir, args.parallel,
                               render=render if use_grid else None)
            if res["auth_failed"]:
                auth_challenge_body = res["auth_body"]
                auth_challenge_url = res["auth_url"]

        complete, incomplete = verify_parts(parts, output_dir)
        if use_grid:
            render.set_header(header_line())
            for p in complete:
                render.clear_row(f"file:{p['filename']}")
            render.teardown()
        else:
            ok(f"Verified: {len(complete)}/{len(parts)} complete.")
        state = update_state_from_parts(state, parts)
        save_state(output_dir, state)
        need = incomplete
        if not need:
            break

        if not looks_like_auth_failure(parts, incomplete):
            warn(f"{len(incomplete)} parts still incomplete; "
                 f"retrying in place (resume continues from on-disk bytes).")
            if attempt >= 5:
                warn("5 retries exhausted; giving up on these parts:")
                for p in incomplete[:10]:
                    info(f"   {p['num']:03d}  {p['filename']}")
                if len(incomplete) > 10:
                    info(f"   ... and {len(incomplete) - 10} more")
                break
            continue

        # Looks like cookie expired mid-run, OR the parallel
        # download triggered Google's anti-bot challenge. Before
        # asking for a new cookie, try reducing parallelism to 1
        # (sequential downloads). This fixes the common case where
        # 5 parallel connections trigger the challenge but a single
        # connection works fine.
        if args.parallel > 1 and auth_failures == 0:
            warn(f"{len(incomplete)} parts still incomplete after "
                 f"parallel download with --parallel {args.parallel}.")
            info(f"  Retrying with --parallel 1 (sequential) to avoid "
                 f"Google's anti-bot challenge.")
            args.parallel = 1
            attempt = 0
            continue

        # Cookie likely expired, or sequential download also failed.
        warn(f"{len(incomplete)} parts still incomplete; "
             "cookie likely expired mid-run.")
        # Save the captured sign-in HTML (internal engine grabs the first
        # bytes of the challenge) so the user can inspect what Google sent.
        if auth_challenge_body:
            saved = _save_auth_challenge_body(
                auth_challenge_body, auth_challenge_url or "(unknown)",
                output_dir, ctype="text/html", status=0)
            info(f"Saved sign-in challenge page to {saved}")
        for p in incomplete[:10]:
            info(f"   {p['num']:03d}  {p['filename']}")
        if len(incomplete) > 10:
            info(f"   ... and {len(incomplete) - 10} more")
        info("Re-capture in your browser, then paste the new JSON. "
             "Resume continues from the partials already on disk.")
        auth_failures += 1
        if auth_failures > MAX_AUTH_REPROMPTS:
            err(f"Auth still failing after {MAX_AUTH_REPROMPTS} attempts. "
                "Re-capture and re-run.")
            return 1
        payload, payload_ctx = parse_one_payload()
        payload_mode = payload_ctx["mode"]
        payload_meta = payload_ctx["meta"]
        payload_all = payload_ctx["all_payloads"]
        payload_pre_built = payload_ctx.get("pre_built_parts")
        attempt = 0

    header("Done")
    complete, incomplete = verify_parts(parts, output_dir)
    grand = sum(p["size"] for p in complete)
    state = update_state_from_parts(state, parts)
    save_state(output_dir, state)
    info("")
    if incomplete:
        err(f"{len(incomplete)} parts still missing in {output_dir}:")
        for p in incomplete[:10]:
            info(f"   {p['num']:03d}  {p['filename']}")
        if len(incomplete) > 10:
            info(f"   ... and {len(incomplete) - 10} more")
        info(f"State saved to {state_path(output_dir)} — "
             f"re-run to resume the missing parts.")
        return 1
    ok(f"All {len(complete)} parts downloaded — "
       f"{human_size(grand)} in {output_dir}")
    ok(f"State saved to {state_path(output_dir)}")
    return 0


if __name__ == "__main__":
    # Catch uncaught exceptions in the log file with full traceback.
    def _excepthook(exc_type, exc_value, exc_tb):
        log.error("Unhandled exception",
                  exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        info("")
        warn("Interrupted. Partially-downloaded parts are kept; "
             "re-run to resume.")
        sys.exit(130)
