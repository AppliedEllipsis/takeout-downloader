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
ZIP_EOCD = b"PK\x05\x06"
ZIP_EOCD_SCAN = 1024  # bytes to scan at end of file for the EOCD record


# ===========================================================================
# Logger
# ===========================================================================
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


log = logging.getLogger("takeout_cli")


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

    Returns total size in bytes if the part exists, None if 404.
    Raises AuthError if Google served a signin page or HTML.

    Google behavior to disambiguate:
      alive cookie + part OK     -> 206 + Content-Range: bytes 0-0/<size>
      alive cookie + part missing-> 404
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
        if resp.status_code == 416:
            return 0  # part exists with 0 bytes
        cr = resp.headers.get("content-range", "")
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if resp.status_code == 200:
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit():
                return int(cl)
        return 0  # exists but size unknown
    finally:
        resp.close()


def discover_parts(payload: TakeoutPayload, output_dir: Path) -> list[dict]:
    """Probe -001, -002, … until the set ends. Returns list of dicts:
    {num, url, filename, size, have}. Stops early on AuthError after part 1."""
    base, _, ext, query = extract_url_parts(payload.url)
    if not base:
        raise ValueError(f"Could not parse a Takeout URL pattern from:\n  {payload.url}")

    headers = dict(payload.headers)
    headers["Cookie"] = payload.cookie

    parts: list[dict] = []
    consecutive_misses = 0
    session = requests.Session()
    base_filename = base.split("/")[-1]

    info(f"Discovering parts (URL pattern: ...{base_filename}<NNN>{ext})")
    for num in range(1, MAX_PARTS + 1):
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
            debug(f"probe #{num:03d} -> 404 (end of set)")
            consecutive_misses += 1
            if consecutive_misses >= CONSECUTIVE_404_STOP:
                debug(f"  {CONSECUTIVE_404_STOP} consecutive misses; end of set")
                break
            continue
        consecutive_misses = 0
        debug(f"probe #{num:03d} -> {size} bytes")

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


def run_aria2c(input_body: str, output_dir: Path, parallel: int) -> int:
    """Run aria2c against the batch. Returns the aria2c exit code.
    aria2c's own console output IS the progress display."""
    if not input_body.strip():
        warn("Nothing to download; aria2c skipped.")
        return 0
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".aria2.txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(input_body)
        input_path = f.name

    cmd = [
        "aria2c",
        "-i", input_path,
        "-j", str(parallel),
        "-x", "1", "-s", "1",          # Takeout blocks multi-stream
        "-c",                          # resume partials
        "--auto-file-renaming=false",
        "--allow-overwrite=true",      # overwrite stale partial without .1 suffix
        "--console-log-level=warn",
        "--summary-interval=10",
        "--download-result=full",
        "--max-tries=5",
        "--retry-wait=10",
        "--timeout=60",
        "--file-allocation=none",
    ]
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass


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

    # Discover (also validates auth). Re-prompt on auth failure.
    auth_failures = 0
    while True:
        try:
            parts = discover_parts(payload, output_dir)
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

    total = sum(p["size"] for p in parts)
    have = [p for p in parts if p["have"]]
    need = [p for p in parts if not p["have"]]
    ok(f"Found {len(parts)} parts, {human_size(total)} total. "
       f"{len(have)} already complete, {len(need)} to download.")
    if not need:
        ok("Everything is already downloaded. Nothing to do.")
        return 0

    # Main download loop
    attempt = 0
    while need:
        attempt += 1
        header(f"Downloading {len(need)} parts — pass {attempt} "
               f"(aria2c, {args.parallel} concurrent)")
        body = build_aria2_input(parts, payload, output_dir)
        rc = run_aria2c(body, output_dir, args.parallel)
        if rc != 0:
            warn(f"aria2c exited with code {rc} "
                 f"(some files may have failed).")

        complete, incomplete = verify_parts(parts, output_dir)
        ok(f"Verified: {len(complete)}/{len(parts)} complete.")
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
    info("")
    if incomplete:
        err(f"{len(incomplete)} parts still missing in {output_dir}:")
        for p in incomplete[:10]:
            info(f"   {p['num']:03d}  {p['filename']}")
        if len(incomplete) > 10:
            info(f"   ... and {len(incomplete) - 10} more")
        return 1
    ok(f"All {len(complete)} parts downloaded — "
       f"{human_size(grand)} in {output_dir}")
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
