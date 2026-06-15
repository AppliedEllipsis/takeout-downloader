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
  NO_COLOR            disable ANSI colors
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
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

from takeout import extract_url_parts, validate_output_dir, DEFAULT_OUTPUT_DIR, VERSION
from takeout_payload import parse_payload, parse_multi_payload, parse_multi_payload_meta, TakeoutPayload, MultiPayloadMeta, REQUIRED_COOKIE_MARKERS


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


log = logging.getLogger("takeout_cli")


# ===========================================================================
# Terminal renderer — control-char based grid UI
# ===========================================================================
class TermRender:
    """Fixed-area terminal display with control-char redraws.

    Owns a region of the screen starting at `start_row`. Every draw() call
    clears the previous content with \\x1b[K (erase to EOL) on each line and
    emits the new state. No flicker.

    Header line: live status (downloads active, total done, ETA, throughput).
    Body: one row per file, with progress bar + bytes + speed + ETA.
    Footer: cumulative stats.

    Falls back to plain line-printing when stdout is not a TTY (e.g. piped
    to a file) so tests / logs aren't polluted with escape sequences.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdout.isatty()
        self.start_row: int = 0
        self.body_rows: int = 0
        self.last_drawn_lines: int = 0
        self.header: str = ""
        self.rows: list[str] = []
        self.footer: str = ""
        # Track which file is on which row so we can keep redrawing.
        self._row_keys: list[str] = []

    def begin(self, n_rows: int) -> None:
        """Reserve `n_rows` body rows. Call once after discovery."""
        if not self.enabled:
            return
        self.body_rows = n_rows
        self.rows = [""] * n_rows
        self._row_keys = [""] * n_rows

    def set_header(self, text: str) -> None:
        self.header = text
        self._redraw()

    def set_footer(self, text: str) -> None:
        self.footer = text
        self._redraw()

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

    def _redraw(self) -> None:
        if not self.enabled:
            return
        out = []
        # Save cursor, move to start_row, clear lines, write new content,
        # restore cursor.
        out.append("\x1b[s")                    # save cursor
        out.append(f"\x1b[{self.start_row + 1};1H")  # move to start
        out.append(self._format_line(self.header))
        for row in self.rows:
            out.append(self._format_line(row))
        out.append(self._format_line(self.footer))
        out.append("\x1b[u")                    # restore cursor
        # Track height so the caller can position their prompt below us.
        self.last_drawn_lines = 1 + self.body_rows + 1
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    @staticmethod
    def _format_line(text: str) -> str:
        # Erase to EOL, write text, then move to next line.
        if not text:
            return "\x1b[K\n"
        return f"\x1b[K{text}\n"

    def reserve_lines_below(self) -> int:
        """After we're done redrawing, the cursor is somewhere inside our
        region. Caller needs to scroll past us before printing more text.
        Returns how many newlines are needed."""
        if not self.enabled:
            return 0
        sys.stdout.write("\n" * (1 + self.body_rows + 1))
        sys.stdout.flush()
        return 1 + self.body_rows + 1

    def teardown(self) -> None:
        """Move the cursor below our region so subsequent logs don't overwrite."""
        self.reserve_lines_below()


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
    parts: list[dict] = []
    for i, p in enumerate(payloads):
        url, filename = _split_url_filename_for_part(p, i)
        dest = output_dir / filename
        size = meta.sizes.get(i, 0) or 0
        have = dest.exists() and dest.stat().st_size > 0 and (
            size == 0 or dest.stat().st_size >= size
        )
        parts.append({
            "num": i + 1,
            "url": url,
            "filename": filename,
            "size": size,
            "have": have,
        })
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


def _update_from_aria2_line(line: str,
                            gid_state: dict,
                            gid_to_filename: dict,
                            filename_to_rowkey: dict,
                            filename_to_size: dict,
                            num_by_filename: dict,
                            render: TermRender) -> None:
    """Translate one aria2c stdout line into a grid row update."""
    # Detect the "Downloading" announcement, which gives us the filename.
    if line.startswith("Downloading"):
        # aria2 prints e.g. "[#abc 411.6MiB/8.0GiB(0%) CN:1 DL:0B]" but also
        # the path on a separate line. We rely on the per-file summary
        # block that comes later. Skip for now.
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
        # Look up filename for this GID. If unknown, defer the update.
        filename = gid_to_filename.get(gid)
        if not filename:
            # Heuristic: keep state so we can fill in once we know filename.
            gid_state[gid] = {
                "done": done, "total": total, "pct": pct,
                "speed": speed_bps, "eta": eta,
            }
            return
        _render_file_row(render, filename, done, total, pct,
                         speed_bps, eta, filename_to_size,
                         num_by_filename)
        return
    # aria2c downloads use one-line summaries like:
    #   [#abc 50MiB/100MiB(50%) CN:1 DL:50MiB ETA:1m]     (live)
    # When --download-result=full, on completion:
    #   Download Results:
    #   gid|stat|avg speed|path
    #   abc123|OK|  1.2MiB/s|./foo.zip
    # ...
    # We grep for the result row to bind GID -> filename once complete.
    m2 = re.search(
        r"\b([0-9a-f]{4,16})\s*\|\s*(OK|ERR|WARN)\s*\|.*?\|\s*(.+?\.zip)\s*$",
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


def _render_file_row(render: TermRender, filename: str, done: int,
                     total: int, pct: int, speed_bps: int, eta: str,
                     filename_to_size: dict, num_by_filename: dict) -> None:
    if total <= 0:
        total = filename_to_size.get(filename, 0)
    # Adapt the progress bar width to the terminal so all five concurrent
    # rows fit without clipping. Reserve 60 chars for the prefix
    # (number, bar, percent, done/total, speed, ETA, separators) and
    # give the rest to the filename. Falls back to 20 for unknown sizes.
    try:
        term_width = shutil.get_terminal_size((100, 20)).columns
    except (OSError, ValueError):
        term_width = 100
    fixed_overhead = 60  # the part of the row that isn't the bar or filename
    bar_width = max(8, min(20, term_width - fixed_overhead - len(filename)))
    bar = make_progress_bar(pct, width=bar_width)
    parts_str = human_size(done) + "/" + human_size(total) if total else human_size(done)
    speed_str = human_size(speed_bps) + "/s" if speed_bps else "    -    "
    eta_str = eta or "-"
    num = num_by_filename.get(filename, 0)
    row = (
        f"  #{num:03d}  {bar} {pct:3d}%  {parts_str:>20}  "
        f"{speed_str:>10}  ETA {eta_str:>6}  {filename}"
    )
    render.update_row(f"file:{filename}", row)


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
    for p in parts:
        dest = output_dir / p["filename"]
        if not dest.exists() or dest.stat().st_size == 0:
            p["have"] = False
            incomplete.append(p)
            continue
        if p["size"] and dest.stat().st_size < p["size"]:
            warn(f"{p['filename']}: "
                 f"size {human_size(dest.stat().st_size)} < "
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
    args = parser.parse_args()

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

    if not detect_aria2c():
        err("aria2c not found on PATH. Install it (apt install aria2) and retry.")
        return 2

    header(f"Google Takeout Downloader v{VERSION} — paste, go.")
    info("")

    # Ask for output dir FIRST, before the JSON paste. If --out is passed
    # on the command line, skip the prompt entirely.
    if args.output_dir:
        chosen_output_dir = output_dir
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
            info(f"  {status} {p['filename']:40s} "
                 f"{human_size(p['size']):>10s}  {p['url'][:80]}")
        ok(f"Dry-run complete. Run without --dry-run to download.")
        state = update_state_from_parts(state, parts)
        save_state(output_dir, state)
        return 0

    # ----------------------------------------------------------------
    # Pre-flight check: prove the cookie works for *full-file* GETs
    # (not just 1-byte Range probes) BEFORE we kick off aria2c.
    #
    # Why this matters: Google's auth-challenge response (a 1.2 MB
    # HTML sign-in page) is triggered by full GETs from a challenged
    # session, but a Range: bytes=0-0 probe passes through cleanly.
    # So the user can see "Found 5 parts, 1.8 GB total" (probes
    # succeeded) and then watch every download come back as 1.2 MB
    # of HTML (full GETs challenged). The script would then loop
    # asking for a new cookie — but the *new* cookie has the same
    # problem, because the issue isn't staleness, it's per-session
    # full-download rate limiting.
    #
    # We do a single full GET of the *smallest* part (the user can
    # have 5 parts totalling 2 GB but a 43 KB part to test with) to
    # confirm the session is healthy for the actual download path.
    # If the response is HTML, we save it for inspection and bail
    # out immediately — no more 5x failing-then-re-prompting loops.
    # ----------------------------------------------------------------
    _preflight_full_download(parts, payload, output_dir)

    # Main download loop
    attempt = 0
    use_grid = sys.stdout.isatty() and os.environ.get("NO_GRID") is None
    render = TermRender(enabled=use_grid)
    if use_grid:
        render.begin(n_rows=len(parts))
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
                   f"(aria2c, {args.parallel} concurrent)")
        body = build_aria2_input(parts, payload, output_dir)
        rc = run_aria2c(body, output_dir, args.parallel,
                        render=render if use_grid else None,
                        parts=parts)
        if rc != 0:
            warn(f"aria2c exited with code {rc} "
                 f"(some files may have failed).")

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
                 f"retrying in place (aria2c -c resumes).")
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
        for p in incomplete[:10]:
            info(f"   {p['num']:03d}  {p['filename']}")
        if len(incomplete) > 10:
            info(f"   ... and {len(incomplete) - 10} more")
        info("Re-capture in your browser, then paste the new JSON. "
             "aria2c -c will resume the partials.")
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
