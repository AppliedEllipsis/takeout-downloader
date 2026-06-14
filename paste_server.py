#!/usr/bin/env python3
"""
Ephemeral paste relay — "ngrok but zero-config" for getting the JSON
payload into the CLI/TUI over SSH -> tmux -> Docker.

Why this exists
---------------
Pasting a big JSON blob through SSH -> tmux -> a Docker container's stdin is
fragile: bracketed-paste markers get stripped, long lines wrap, the terminal
chokes. This relay sidesteps the terminal entirely. The server prints a URL;
you open it in the SAME browser that has the extension, paste the payload into
a textarea (or the extension auto-posts it), and the CLI receives it directly.

Security model (the payload holds a LIVE Google session cookie)
---------------------------------------------------------------
The cookie is sensitive, so the relay is hardened on three axes:

  1. Random token in the URL path  -> unguessable; no token, no service.
  2. Single-use                    -> the first valid submission stops the
                                       server. A replay finds nothing.
  3. Short TTL                      -> the server self-destructs after
                                       `timeout` seconds even if unused.

Plus:
  - Binds to 127.0.0.1 by default. Public exposure is OPT-IN via a
    Cloudflare quick tunnel (no account, ephemeral *.trycloudflare.com URL).
  - Constant-time token comparison.
  - Never logs the cookie value.
  - GET on the token path serves a minimal paste form; everything else 404s.

Transport
---------
  - Local only (default): http://127.0.0.1:<port>/<token>
  - Cloudflare quick tunnel (--tunnel): launches `cloudflared tunnel
    --url http://127.0.0.1:<port>`, parses the ephemeral public URL from its
    output, and prints https://<random>.trycloudflare.com/<token>.

Stdlib only — no new dependencies.

Usage
-----
    # As a library (what the CLI does):
    from paste_server import serve_once
    payload_text = serve_once(timeout=600, use_tunnel=True)

    # Standalone, for testing:
    python paste_server.py --tunnel --timeout 600
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

# ===========================================================================
# Tunables
# ===========================================================================
DEFAULT_TIMEOUT = int(os.environ.get("PASTE_RELAY_TIMEOUT", "600"))  # seconds
DEFAULT_BIND = os.environ.get("PASTE_RELAY_BIND", "127.0.0.1")
TOKEN_BYTES = 24            # 192 bits of entropy in the URL path
MAX_BODY_BYTES = 4 * 1024 * 1024   # 4 MB cap — payloads are tens of KB
CLOUDFLARED_BIN = os.environ.get("CLOUDFLARED_BIN", "cloudflared")
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
TUNNEL_WAIT_SECONDS = 30


# ===========================================================================
# Minimal color helpers (mirrors takeout_cli house style, stdlib-only)
# ===========================================================================
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _say(msg: str = "") -> None:
    print(msg, flush=True)


# ===========================================================================
# HTML paste form
# ===========================================================================
_PASTE_FORM = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Takeout paste relay</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 760px;
           margin: 2rem auto; padding: 0 1rem; background: #111; color: #eee; }}
    h1 {{ font-size: 1.2rem; }}
    textarea {{ width: 100%; height: 18rem; font-family: ui-monospace, monospace;
               font-size: 13px; background: #1b1b1b; color: #eee;
               border: 1px solid #444; border-radius: 6px; padding: .6rem;
               box-sizing: border-box; }}
    button {{ margin-top: .8rem; padding: .6rem 1.4rem; font-size: 15px;
             border: 0; border-radius: 6px; background: #2d7; color: #042;
             font-weight: 600; cursor: pointer; }}
    button:disabled {{ opacity: .5; cursor: default; }}
    .note {{ color: #999; font-size: 13px; }}
    .ok {{ color: #2d7; }} .err {{ color: #f55; }}
    #status {{ margin-top: .8rem; min-height: 1.2rem; }}
  </style>
</head>
<body>
  <h1>Paste the Takeout JSON payload</h1>
  <p class="note">Single-use relay. This page works once, then the
     server shuts down. The payload is sent straight to the CLI.</p>
  <textarea id="payload" placeholder="Paste the JSON from the extension here..."></textarea>
  <button id="send">Send to CLI</button>
  <div id="status"></div>
  <script>
    const btn = document.getElementById('send');
    const ta = document.getElementById('payload');
    const status = document.getElementById('status');
    btn.addEventListener('click', async () => {{
      const text = ta.value.trim();
      if (!text) {{ status.innerHTML = '<span class="err">Nothing to send.</span>'; return; }}
      btn.disabled = true;
      status.textContent = 'Sending...';
      try {{
        const r = await fetch(location.pathname, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: text
        }});
        if (r.ok) {{
          status.innerHTML = '<span class="ok">Delivered. You can close this tab.</span>';
        }} else {{
          const msg = await r.text();
          status.innerHTML = '<span class="err">Rejected: ' + msg + '</span>';
          btn.disabled = false;
        }}
      }} catch (e) {{
        status.innerHTML = '<span class="err">Network error: ' + e.message + '</span>';
        btn.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""


# ===========================================================================
# HTTP handler
# ===========================================================================
class _PasteRelayState:
    """Shared state between the HTTP handler and the waiting caller."""

    def __init__(self, token: str):
        self.token = token
        self.token_path = "/" + token
        self.payload: Optional[str] = None
        self.delivered = threading.Event()
        self.consumed = False  # single-use guard


def _make_handler(state: _PasteRelayState):
    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr request logging (it can leak paths/timings).
        def log_message(self, fmt, *args):  # noqa: N802
            pass

        def _deny(self, code: int = 404, msg: str = "not found"):
            body = msg.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _path_ok(self) -> bool:
            # Constant-time compare on the path component, ignore query string.
            path = self.path.split("?", 1)[0]
            return hmac.compare_digest(path, state.token_path)

        def do_GET(self):  # noqa: N802
            if not self._path_ok():
                return self._deny()
            if state.consumed:
                return self._deny(410, "relay already used")
            page = _PASTE_FORM.format().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):  # noqa: N802
            if not self._path_ok():
                return self._deny()
            if state.consumed:
                return self._deny(410, "relay already used")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._deny(400, "bad length")
            if length <= 0:
                return self._deny(400, "empty body")
            if length > MAX_BODY_BYTES:
                return self._deny(413, "payload too large")
            raw = self.rfile.read(length)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return self._deny(400, "not utf-8")
            # Minimal sanity: must look like a JSON object/array.
            stripped = text.strip()
            if not stripped or stripped[0] not in "{[":
                return self._deny(400, "not JSON")
            # Single-use: claim it atomically.
            if state.consumed:
                return self._deny(410, "relay already used")
            state.consumed = True
            state.payload = text
            ok_body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(ok_body)))
            self.end_headers()
            self.wfile.write(ok_body)
            state.delivered.set()

    return Handler


# ===========================================================================
# Cloudflare quick tunnel
# ===========================================================================
def _start_cloudflared(port: int) -> tuple[Optional[subprocess.Popen], Optional[str]]:
    """Launch a cloudflared quick tunnel for http://127.0.0.1:<port>.

    Returns (process, public_url). On any failure returns (None, None) and the
    caller falls back to the local URL.
    """
    try:
        proc = subprocess.Popen(
            [CLOUDFLARED_BIN, "tunnel", "--url", f"http://127.0.0.1:{port}",
             "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        _say(_c("33", f"  cloudflared not found ({CLOUDFLARED_BIN}). "
                      "Falling back to local URL."))
        _say(_c("33", "  Install: https://developers.cloudflare.com/cloudflare-one/"
                      "connections/connect-networks/downloads/"))
        return None, None
    except Exception as e:  # noqa: BLE001
        _say(_c("33", f"  Could not start cloudflared: {e}. Using local URL."))
        return None, None

    public_url: Optional[str] = None
    deadline = time.monotonic() + TUNNEL_WAIT_SECONDS
    # cloudflared prints the URL to stdout/stderr within a few seconds.
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.1)
            continue
        m = TUNNEL_URL_RE.search(line)
        if m:
            public_url = m.group(0)
            break

    if not public_url:
        _say(_c("33", "  Tunnel did not report a URL in time; using local URL."))
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        return None, None

    # Drain remaining cloudflared output in the background so its pipe
    # buffer never fills and stalls the process.
    def _drain():
        try:
            for _ in proc.stdout:  # type: ignore[union-attr]
                pass
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_drain, daemon=True).start()
    return proc, public_url


# ===========================================================================
# Public API
# ===========================================================================
def serve_once(
    timeout: int = DEFAULT_TIMEOUT,
    use_tunnel: bool = False,
    bind: str = DEFAULT_BIND,
    port: int = 0,
    token: Optional[str] = None,
    on_url=None,
) -> Optional[str]:
    """Run the relay until one payload arrives or `timeout` elapses.

    Args:
        timeout:    seconds to wait before giving up (TTL). 0 = no timeout.
        use_tunnel: expose publicly via a Cloudflare quick tunnel.
        bind:       interface to bind locally (default 127.0.0.1).
        port:       local port (0 = OS picks a free one).
        token:      override the random URL token (testing only).
        on_url:     optional callback(url:str) invoked once the URL is known.

    Returns:
        The pasted payload text, or None on timeout.
    """
    token = token or secrets.token_urlsafe(TOKEN_BYTES)
    state = _PasteRelayState(token)
    handler = _make_handler(state)

    httpd = ThreadingHTTPServer((bind, port), handler)
    actual_port = httpd.server_address[1]
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    tunnel_proc = None
    public_url = None
    if use_tunnel:
        _say(_c("36", "  Starting Cloudflare quick tunnel (no account needed)..."))
        tunnel_proc, public_url = _start_cloudflared(actual_port)

    local_url = f"http://{bind}:{actual_port}/{token}"
    url = public_url + "/" + token if public_url else local_url

    _say()
    _say(_c("1;36", "  Paste relay is live (single-use, self-destructs)."))
    _say(_c("1;32", f"    {url}"))
    _say(_c("36", "    Open this in the browser that has the extension,"))
    _say(_c("36", "    paste the JSON, click Send. The CLI picks it up here."))
    if public_url:
        _say(_c("90", f"    (local: {local_url})"))
    if timeout:
        _say(_c("90", f"    Expires in {timeout}s if unused."))
    _say()

    if on_url:
        try:
            on_url(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        got = state.delivered.wait(timeout if timeout else None)
    finally:
        httpd.shutdown()
        httpd.server_close()
        if tunnel_proc is not None:
            try:
                tunnel_proc.terminate()
                tunnel_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    tunnel_proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    if not got:
        _say(_c("33", "  Relay timed out — no payload received."))
        return None

    _say(_c("32", "  Payload received via relay."))
    return state.payload


# ===========================================================================
# Standalone entrypoint
# ===========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ephemeral single-use paste relay for Takeout JSON payloads.",
    )
    parser.add_argument("--tunnel", action="store_true",
                        help="expose publicly via a Cloudflare quick tunnel")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"seconds before self-destruct (default {DEFAULT_TIMEOUT}; 0=never)")
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help=f"local bind address (default {DEFAULT_BIND})")
    parser.add_argument("--port", type=int, default=0,
                        help="local port (default: OS-assigned)")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the received payload to stdout and exit")
    args = parser.parse_args()

    payload = serve_once(
        timeout=args.timeout,
        use_tunnel=args.tunnel,
        bind=args.bind,
        port=args.port,
    )
    if payload is None:
        return 1
    if args.print_only:
        # Payload to stdout; the human-readable status went to stderr/print
        # above. Useful for: python paste_server.py --print | jq .
        sys.stdout.write(payload)
        if not payload.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
