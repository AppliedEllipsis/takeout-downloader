#!/usr/bin/env python3
"""
Google Takeout Downloader — CLI (aria2c)
=========================================

A simple, terminal-native CLI for downloading Google Takeout archives.
Designed for the SSH -> tmux -> Docker chain where the Textual TUI's paste/
redraw behaviour is fragile.

Flow
----
1. Read a JSON payload (paste, stdin redirect, or PAYLOAD_FILE/in.json).
2. Parse + validate (reuses takeout_payload.parse_payload).
3. Discover the archive set by Range-probing each part. This also validates
   auth up front: an expired cookie returns an HTML login page, which we
   detect before downloading.
4. Download the missing parts with aria2c. aria2c's own console output IS the
   progress display (per-file speed, total throughput, ETA).
5. After the batch, verify which parts are complete. If anything is missing
   AND it looks like the cookie expired mid-run, re-prompt for a fresh
   capture and retry. aria2c -c resumes the partial files automatically.

Environment variables
---------------------
  OUTPUT_DIR          default output dir (JuiceFS path if present, else
                      ./downloads)
  PARALLEL_DOWNLOADS  concurrent downloads passed to aria2c -j (default 3)
  MAX_PARTS           safety cap on discovery probing (default 500)
  PAYLOAD_FILE        if set, read the payload from this file instead of stdin
  NO_COLOR            disable ANSI colors
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

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
# Tunables (env-overridable)
# ===========================================================================
PARALLEL = int(os.environ.get("PARALLEL_DOWNLOADS", "3"))
MAX_PARTS = int(os.environ.get("MAX_PARTS", "500"))
PROBE_TIMEOUT = (10, 30)
CONSECUTIVE_404_STOP = 2
PAYLOAD_SEARCH_FILENAMES = ("in.json", "payload.json", "curl.txt")
MAX_AUTH_REPROMPTS = int(os.environ.get("MAX_AUTH_REPROMPTS", "5"))


# ===========================================================================
# Output helpers
# ===========================================================================
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def info(msg: str) -> None:
    print(_c("36", "• ") + msg)


def ok(msg: str) -> None:
    print(_c("32", "OK  ") + msg)


def warn(msg: str) -> None:
    print(_c("33", "WARN") + " " + msg)


def err(msg: str) -> None:
    print(_c("31", "ERR ") + " " + msg)


def header(msg: str) -> None:
    bar = "=" * max(40, len(msg) + 4)
    print()
    print(_c("1;35", bar))
    print(_c("1;35", f"  {msg}"))
    print(_c("1;35", bar))


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
# Errors
# ===========================================================================
class AuthError(Exception):
    """Cookie expired (Google returned a signin page or auth challenge)."""


class NetworkError(Exception):
    """Network/DNS/HTTP failure during probing."""


# ===========================================================================
# Payload input
# ===========================================================================
def _search_payload_files() -> list[Path]:
    """Return candidate payload files in priority order."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    search_roots = [
        Path.cwd(),
        Path(os.environ.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR),
        Path("/downloads"),
        Path("/downloads/drop"),
        Path("/drop"),
        Path("/work"),
        Path("/work/drop"),
        Path.home(),
    ]
    for root in search_roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        for name in PAYLOAD_SEARCH_FILENAMES:
            p = resolved / name
            if p not in seen and p.is_file():
                seen.add(p)
                candidates.append(p)
    return candidates


def read_payload() -> str:
    """Read a JSON payload from (in priority order):
      1. PAYLOAD_FILE env var
      2. First existing file in search roots (in.json / payload.json / curl.txt)
      3. Piped stdin
      4. Interactive prompt with brace-balance detection
    """
    pf = os.environ.get("PAYLOAD_FILE")
    if pf:
        p = Path(pf).expanduser()
        if p.is_file():
            info(f"Reading payload from {p}")
            return p.read_text(encoding="utf-8", errors="replace")
        err(f"PAYLOAD_FILE set but not found: {p}")

    for p in _search_payload_files():
        info(f"Found payload file: {p}")
        return p.read_text(encoding="utf-8", errors="replace")

    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data

    print()
    print(_c("1", "Paste your JSON payload (the extension's 'Copy as JSON')."))
    print("  Auto-detects when the JSON is complete. Press Enter after paste.")
    print(f"  Or Ctrl-C to quit.")
    print()

    lines: list[str] = []
    depth = 0
    started = False
    in_string = False
    escape = False

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

    return "\n".join(lines)


def parse_payload_strict(text: str) -> TakeoutPayload:
    """Parse payload + raise a friendly error on failure."""
    if not text or not text.strip():
        raise ValueError("empty payload")
    try:
        payload = parse_payload(text)
    except ValueError as e:
        raise ValueError(f"could not parse JSON: {e}") from e
    good, message = payload.validate()
    if not good:
        raise ValueError(message or "payload failed validation")
    return payload


# ===========================================================================
# Discovery — Range probes that validate auth and record sizes
# ===========================================================================
def _probe_part(session: requests.Session, url: str,
                headers: dict) -> Optional[int]:
    """Probe one part with a 1-byte Range request.

    Returns total size in bytes if the part exists, None if 404.
    Raises AuthError if Google served a signin page or HTML error.

    Signals Google can return:
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
        if final_host.endswith("accounts.google.com"):
            raise AuthError(f"redirected to {final_host}")
        if "text/html" in ctype:
            raise AuthError(f"server returned HTML (ct={ctype[:40]})")
        if resp.status_code in (401, 403):
            raise AuthError(f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            return None
        if resp.status_code == 416:
            return 0  # Range not satisfiable but part exists with 0 bytes
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
    """Probe -001, -002, … until the set ends. Each part: {num, url,
    filename, size, have}. Stops early on AuthError after part 1."""
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

        try:
            size = _probe_part(session, url, headers)
        except AuthError as e:
            if num == 1:
                raise  # auth bad from the start
            warn(f"Auth failed probing part {num:03d} ({e}); "
                 f"stopping discovery, {len(parts)} parts discovered so far.")
            break

        if size is None:
            consecutive_misses += 1
            if consecutive_misses >= CONSECUTIVE_404_STOP:
                break
            continue
        consecutive_misses = 0

        dest = output_dir / filename
        have = dest.exists() and dest.stat().st_size > 0 and (
            size == 0 or dest.stat().st_size >= size
        )
        parts.append({
            "num": num, "url": url, "filename": filename,
            "size": size, "have": have,
        })
        flag = _c("32", "have") if have else "need"
        size_str = human_size(size) if size else "unknown"
        print(f"   {num:03d}  {size_str:>10}  {flag}")

    return parts


# ===========================================================================
# aria2c batch
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
        # Cookie header is mandatory; UA/Referer help Google accept the request.
        lines.append(f"  header=Cookie: {payload.cookie}")
        for k, v in payload.headers.items():
            if k.lower() == "cookie":
                continue
            lines.append(f"  header={k}: {v}")
    return "\n".join(lines) + ("\n" if lines else "")


def run_aria2c(input_body: str, output_dir: Path, parallel: int) -> int:
    """Run aria2c against the batch. Returns the aria2c exit code.
    aria2c's own console output IS the progress display — no wrapping."""
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
        "-j", str(parallel),       # concurrent downloads
        "-x", "1",                 # 1 connection per server (Takeout blocks multi)
        "-s", "1",                 # 1 split — same reason
        "-c",                      # resume partial files
        "--auto-file-renaming=false",
        "--allow-overwrite=true",  # overwrite stale partial without .1 suffix
        "--console-log-level=warn",
        "--summary-interval=10",
        "--download-result=full",
        "--max-tries=5",
        "--retry-wait=10",
        "--timeout=60",
        "--file-allocation=none",  # fast start on network FS
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
# Verification
# ===========================================================================
def verify_parts(parts: list[dict], output_dir: Path) -> tuple[list[dict], list[dict]]:
    """Re-check the output dir. Returns (complete, incomplete)."""
    complete, incomplete = [], []
    for p in parts:
        dest = output_dir / p["filename"]
        if dest.exists() and dest.stat().st_size > 0 and (
            p["size"] == 0 or dest.stat().st_size >= p["size"]
        ):
            p["have"] = True
            complete.append(p)
        else:
            p["have"] = False
            incomplete.append(p)
    return complete, incomplete


# ===========================================================================
# Output directory
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
# Main loop
# ===========================================================================
def prompt_payload_until_valid(reason: str = "") -> TakeoutPayload:
    """Read + parse + validate; loop until the user supplies a usable payload."""
    if reason:
        warn(reason)
    while True:
        try:
            raw = read_payload()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(1)
        if not raw.strip():
            err("No input received. Try again, or Ctrl-C to quit.")
            continue
        try:
            payload = parse_payload_strict(raw)
        except ValueError as e:
            err(f"Invalid payload: {e}")
            continue
        # parse_payload_strict already enforces good=True; warn on cookie age
        markers = [m for m in REQUIRED_COOKIE_MARKERS if m in payload.cookie]
        ok(f"Cookie OK: {len(payload.cookie)} chars "
           f"(markers: {', '.join(markers[:4])})")
        return payload


def looks_like_auth_failure(parts: list[dict], incomplete: list[dict]) -> bool:
    """Heuristic: most parts failed AND the ones that did get through are tiny.
    A burst of auth-expired errors usually leaves 0-byte or short files."""
    if not incomplete or not parts:
        return False
    # If >80% of parts are incomplete, very likely cookie expired.
    if len(incomplete) / len(parts) > 0.8:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a Google Takeout archive using aria2c.",
    )
    parser.add_argument("-p", "--parallel", type=int, default=PARALLEL,
                        help=f"concurrent downloads (default {PARALLEL})")
    parser.add_argument("--max-parts", type=int, default=MAX_PARTS,
                        help=f"max parts to discover (default {MAX_PARTS})")
    parser.add_argument("--payload-file", help="read payload from this file")
    parser.add_argument("--output-dir", help="override output directory")
    args = parser.parse_args()

    if args.payload_file:
        os.environ["PAYLOAD_FILE"] = args.payload_file
    if args.output_dir:
        os.environ["OUTPUT_DIR"] = args.output_dir

    header("Google Takeout Downloader (aria2c)")
    print()

    if not detect_aria2c():
        err("aria2c not found on PATH. Install it (apt install aria2) and retry.")
        return 2

    output_dir = resolve_output_dir()
    ok(f"Output directory: {output_dir}")

    payload = prompt_payload_until_valid()

    # Discover parts (also validates auth). Re-prompt on auth failure.
    auth_failures = 0
    while True:
        try:
            parts = discover_parts(payload, output_dir)
            break
        except AuthError as e:
            auth_failures += 1
            if auth_failures >= MAX_AUTH_REPROMPTS:
                err(f"Auth still failing after {MAX_AUTH_REPROMPTS} attempts. "
                    "Re-capture in your browser, then re-run.")
                return 1
            payload = prompt_payload_until_valid(
                f"Auth failed during discovery ({e}). "
                "Paste a FRESH capture to continue "
                f"(attempt {auth_failures + 1}/{MAX_AUTH_REPROMPTS})."
            )
        except ValueError as e:
            err(str(e))
            payload = prompt_payload_until_valid("Paste a corrected payload.")

    if not parts:
        err("No archive parts found. The URL may be wrong or the set is empty.")
        return 1

    total = sum(p["size"] for p in parts)
    need = [p for p in parts if not p["have"]]
    have = [p for p in parts if p["have"]]
    ok(f"Found {len(parts)} parts, {human_size(total)} total. "
       f"{len(have)} already complete, {len(need)} to download.")

    if not need:
        ok("Everything is already downloaded. Nothing to do.")
        return 0

    # Download loop: run aria2c, verify, re-prompt on auth-looking leftovers.
    attempt = 0
    while need:
        attempt += 1
        header(f"Downloading {len(need)} parts — attempt {attempt} "
               f"(aria2c, {args.parallel} concurrent)")
        body = build_aria2_input(parts, payload, output_dir)
        rc = run_aria2c(body, output_dir, args.parallel)
        if rc != 0:
            warn(f"aria2c exited with code {rc} (some files may have failed).")

        complete, incomplete = verify_parts(parts, output_dir)
        ok(f"Complete: {len(complete)}/{len(parts)} parts.")
        if not incomplete:
            break

        if not looks_like_auth_failure(parts, incomplete):
            # Partial network failure — let aria2c -c retry on next pass.
            warn(f"{len(incomplete)} parts still incomplete; "
                 f"retrying in place (aria2c -c resumes).")
            if attempt >= 5:
                warn("5 retries exhausted; giving up on these parts:")
                for p in incomplete[:10]:
                    print(f"   {p['num']:03d}  {p['filename']}")
                break
            continue

        # Looks like cookie expired mid-run.
        warn(f"{len(incomplete)} parts still incomplete; "
             "looks like the cookie expired mid-run.")
        for p in incomplete[:10]:
            print(f"   {p['num']:03d}  {p['filename']}")
        if len(incomplete) > 10:
            print(f"   ... and {len(incomplete) - 10} more")
        payload = prompt_payload_until_valid(
            "Re-capture the request in your browser, then paste the new JSON "
            "below (or save to in.json / payload.json in the output dir, or "
            "set PAYLOAD_FILE). aria2c -c will resume the partial parts."
        )
        # Reset attempt counter after a fresh capture.
        attempt = 0
        auth_failures += 1
        if auth_failures >= MAX_AUTH_REPROMPTS:
            err(f"Auth still failing after {MAX_AUTH_REPROMPTS} attempts. "
                "Re-capture in your browser, then re-run.")
            return 1

    header("Done")
    complete, incomplete = verify_parts(parts, output_dir)
    grand = sum(p["size"] for p in complete)
    print()
    if incomplete:
        err(f"{len(incomplete)} parts still missing in {output_dir}:")
        for p in incomplete[:10]:
            print(f"   {p['num']:03d}  {p['filename']}")
        if len(incomplete) > 10:
            print(f"   ... and {len(incomplete) - 10} more")
        return 1
    ok(f"All {len(complete)} parts downloaded — {human_size(grand)} in {output_dir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        warn("Interrupted. Partially-downloaded parts are kept; resume by re-running.")
        sys.exit(130)
