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
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

# Force UTF-8 stdout/stderr on Windows so banner chars print.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from takeout import extract_url_parts, validate_output_dir, DEFAULT_OUTPUT_DIR
from takeout_payload import parse_payload, TakeoutPayload, REQUIRED_COOKIE_MARKERS


# ===========================================================================
# Tunables
# ===========================================================================
PARALLEL = int(os.environ.get("PARALLEL_DOWNLOADS", "3"))
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
def resolve_output_dir() -> Path:
    raw = os.environ.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR
    try:
        d = validate_output_dir(raw)
    except ValueError as e:
        warn(f"{e}")
        warn("Falling back to ./downloads")
        d = Path("./downloads").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def prompt_for_output_dir(default: Path) -> Path:
    """Ask the user where to save archives. Empty input keeps the default.
    Type 'q' to quit. Anything else is treated as a path and validated.
    """
    info("")
    info(_c("1;36", f"Where do you want to save the archives?"))
    info(_c("36", f"  Default [{default}] (Enter to accept, or type a path):"))
    info(_c("36", "  Type 'q' to quit."))
    while True:
        try:
            raw = input(f"  save to > ").strip()
        except EOFError:
            return default
        if raw.lower() in ("q", "quit", "exit"):
            raise SystemExit(0)
        if not raw:
            return default
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
        validated.mkdir(parents=True, exist_ok=True)
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
def parse_one_payload() -> TakeoutPayload:
    """Read + parse + validate one payload. Failures are terminal."""
    raw = prompt_for_paste()
    if not raw.strip():
        err("No JSON received.")
        _payload_fix_hint()
        raise SystemExit(1)
    try:
        payload = parse_payload(raw)
    except ValueError as e:
        err(f"Could not parse JSON: {e}")
        _payload_fix_hint()
        raise SystemExit(2)
    good, message = payload.validate()
    if not good:
        err(f"Payload failed validation: {message}")
        _payload_fix_hint()
        raise SystemExit(2)
    if message:
        warn(message)  # non-fatal (e.g. cookie age)
    markers = [m for m in REQUIRED_COOKIE_MARKERS if m in payload.cookie]
    ok(f"Cookie OK: {len(payload.cookie)} chars "
       f"(markers: {', '.join(markers[:4])})")
    return payload


def _payload_fix_hint() -> None:
    out = os.environ.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR
    info("How to fix:")
    info("  1. Re-capture in your browser: takeout.google.com -> Manage "
         "exports -> Download -> let the request fire -> click the extension "
         "icon -> Copy as JSON")
    info("  2. Right-click in the terminal to paste, then press Enter.")
    info(f"  (Or save to {out}/in.json and re-run.)")


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


def discover_parts(payload: TakeoutPayload, output_dir: Path,
                    max_parts: int | None = None) -> list[dict]:
    """Probe -001, -002, … until the set ends. Returns list of dicts:
    {num, url, filename, size, have}. Stops early on AuthError after part 1."""
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

    loop_limit = max_parts if max_parts else MAX_PARTS
    if max_parts:
        info(f"Discovering {max_parts} part(s) (URL pattern: ...{base_filename}<NNN>{ext})")
    else:
        info(f"Discovering parts (URL pattern: ...{base_filename}<NNN>{ext})")
    for num in range(1, loop_limit + 1):
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

        # Heuristic: if N consecutive parts have the exact same size,
        # Google is serving generic placeholder responses. The real set
        # is done. This catches single-part archives where Google returns
        # 206 for every part number with the same token.
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
    bar = make_progress_bar(pct, width=20)
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
    parser.add_argument("--output-dir",
                        help="override output directory")
    args = parser.parse_args()

    if args.output_dir:
        os.environ["OUTPUT_DIR"] = args.output_dir

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
    info(f"Log file: {log_path} "
         f"(rotating at {max_bytes // 1024}KB, "
         f"keeping {backup_count} backups)")
    info(f"Python: {sys.version.split()[0]} on {sys.platform}")
    info(f"Working dir: {Path.cwd()}")
    info(f"Output directory: {output_dir}")

    if not detect_aria2c():
        err("aria2c not found on PATH. Install it (apt install aria2) and retry.")
        return 2

    header("Google Takeout Downloader — paste, go.")
    info("")

    payload = parse_one_payload()

    # Ask for output dir AFTER we know what payload we're downloading.
    # The user might want different folders for different Takeout exports.
    # If --output-dir was passed on the command line, skip the prompt.
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

    # Try to resume from state in the chosen folder.
    state = load_state(output_dir)
    parts: list[dict] | None = None
    if state and state_matches_payload(state, payload):
        parts = state_to_parts(state, payload)
        if parts:
            saved_complete = sum(1 for p in parts if p["have"])
            ok(f"Resuming from {state_path(output_dir)}: "
               f"{saved_complete}/{len(parts)} parts marked complete.")
            info("Re-verifying files on disk before downloading anything.")
            # Re-verify saved parts against the actual filesystem. The size
            # on disk could differ from what's in state if files changed.
            _, incomplete = verify_parts(parts, output_dir)
            parts = [p for p in parts if p["have"]] + incomplete

    # Discover (also validates auth). Re-prompt on auth failure.
    if not parts:
        info("")
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
                payload = parse_one_payload()
            except ValueError as e:
                err(str(e))
                info("Paste a corrected JSON payload below.")
                payload = parse_one_payload()

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
        # Refresh state with verified results and exit cleanly.
        state = update_state_from_parts(state, parts)
        save_state(output_dir, state)
        return 0

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
            # Refresh header to reflect newly-completed counts.
            render.set_header(header_line())
            # Clear any rows that just finished (so the grid shows progress,
            # not stale 100% bars for already-done files).
            for p in complete:
                render.clear_row(f"file:{p['filename']}")
            render.teardown()
        else:
            ok(f"Verified: {len(complete)}/{len(parts)} complete.")
        # Persist progress.
        state = update_state_from_parts(state, parts)
        save_state(output_dir, state)
        need = incomplete
        if not need:
            break

        if not looks_like_auth_failure(parts, incomplete):
            # Partial network failure — re-run aria2c to let -c resume.
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

        # Looks like cookie expired mid-run.
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
        payload = parse_one_payload()
        # Reset attempt counter after a fresh payload.
        attempt = 0

    header("Done")
    complete, incomplete = verify_parts(parts, output_dir)
    grand = sum(p["size"] for p in complete)
    # Final state save: capture verified completion even if we exit non-zero.
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
