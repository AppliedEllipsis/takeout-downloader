#!/usr/bin/env python3
"""Reload the Takeout helper extension over CDP and report real load errors.

Pure stdlib (no websocket-client / node needed). Drives the already-open
chrome://extensions page via CDP and uses chrome.developerPrivate, which can
reload an extension by id AND surface actual manifest/install/runtime errors.
Checking for a service-worker target is NOT a reliable health signal: MV3
service workers unload after ~30s idle, so their absence is normal.

Procedure:
  1. attach to the chrome://extensions page target,
  2. developerPrivate.reload(id, {failQuietly:false}) -> rejects on manifest err,
  3. developerPrivate.getExtensionInfo(id) -> read installWarnings + manifestErrors
     + runtimeErrors + enabled/state,
  4. reload the takeout.google.com tab so the new content script injects.

Usage: python3 cdp_reload_ext.py [cdp_host:port]
Exit 0 = reloaded clean, 2 = reloaded with errors / not found.
"""
import json
import socket
import base64
import hashlib
import os
import struct
import sys
import time
import urllib.request

CDP = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:9222"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # WS handshake magic
EXT_ID = "dgbbpdjpfeeaiheekoclkkkbipkikejl"


def http_get(path):
    with urllib.request.urlopen(f"http://{CDP}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


class WS:
    """Minimal RFC6455 client (client->server frames must be masked)."""

    def __init__(self, ws_url):
        assert ws_url.startswith("ws://")
        hostport, _, path = ws_url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = self._read_until(b"\r\n\r\n")
        accept = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()
        ).decode()
        if accept not in resp.decode(errors="replace"):
            raise RuntimeError("WS handshake failed: " + resp.decode(errors="replace")[:200])
        self._buf = b""
        self._id = 0

    def _read_until(self, marker):
        data = b""
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def send(self, obj):
        payload = json.dumps(obj).encode()
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_frame(self, timeout=10):
        self.sock.settimeout(timeout)
        while len(self._buf) < 2:
            self._buf += self.sock.recv(4096)
        b2 = self._buf[1]
        ln = b2 & 0x7F
        idx = 2
        if ln == 126:
            while len(self._buf) < 4:
                self._buf += self.sock.recv(4096)
            ln = struct.unpack(">H", self._buf[2:4])[0]
            idx = 4
        elif ln == 127:
            while len(self._buf) < 10:
                self._buf += self.sock.recv(4096)
            ln = struct.unpack(">Q", self._buf[2:10])[0]
            idx = 10
        while len(self._buf) < idx + ln:
            self._buf += self.sock.recv(4096)
        payload = self._buf[idx:idx + ln]
        self._buf = self._buf[idx + ln:]
        return payload.decode(errors="replace")

    def call(self, method, params=None, session_id=None, timeout=10):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.send(msg)
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self.recv_frame(timeout=timeout)
            try:
                obj = json.loads(frame)
            except ValueError:
                continue
            if obj.get("id") == mid:
                return obj
        raise TimeoutError(f"no reply to {method}")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def evaluate(ws, expr, timeout=15):
    """Runtime.evaluate an async expression, return the resolved JSON value."""
    r = ws.call("Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True,
        "returnByValue": True,
    }, timeout=timeout)
    res = r.get("result", {})
    if "exceptionDetails" in res:
        exc = res["exceptionDetails"]
        raise RuntimeError("JS exception: " + json.dumps(exc)[:300])
    return res.get("result", {}).get("value")


def main():
    targets = http_get("/json")
    ext_page = next((t for t in targets
                     if t.get("type") == "page"
                     and t.get("url", "").startswith("chrome://extensions")), None)
    if not ext_page or not ext_page.get("webSocketDebuggerUrl"):
        print("ERROR: chrome://extensions tab not open; cannot drive developerPrivate")
        sys.exit(2)

    ws = WS(ext_page["webSocketDebuggerUrl"])
    ws.call("Runtime.enable")

    # 1. Reload by id. developerPrivate.reload rejects if the manifest is bad.
    reload_js = """
    (async () => {
      const id = "%s";
      const out = { reloaded:false, reloadError:null };
      try {
        await new Promise((resolve, reject) => {
          chrome.developerPrivate.reload(id, {failQuietly:false}, () => {
            const e = chrome.runtime.lastError;
            if (e) reject(new Error(e.message)); else resolve();
          });
        });
        out.reloaded = true;
      } catch (e) { out.reloadError = String(e && e.message || e); }
      return JSON.stringify(out);
    })()
    """ % EXT_ID
    reload_res = json.loads(evaluate(ws, reload_js))
    print("reload issued:", reload_res)

    time.sleep(2)

    # 2. Pull full extension info: state + every error bucket.
    info_js = """
    (async () => {
      const id = "%s";
      try {
        const info = await new Promise((resolve, reject) => {
          chrome.developerPrivate.getExtensionInfo(id, (i) => {
            const e = chrome.runtime.lastError;
            if (e) reject(new Error(e.message)); else resolve(i);
          });
        });
        return JSON.stringify({
          found:true,
          name: info.name,
          version: info.version,
          state: info.state,
          enabled: info.state === 'ENABLED',
          manifestErrors: info.manifestErrors || [],
          runtimeErrors: info.runtimeErrors || [],
          installWarnings: info.installWarnings || [],
        });
      } catch (e) {
        return JSON.stringify({found:false, error:String(e && e.message || e)});
      }
    })()
    """ % EXT_ID
    info = json.loads(evaluate(ws, info_js))
    ws.close()

    if not info.get("found"):
        print("RESULT: FAIL — extension not found:", info.get("error"))
        sys.exit(2)

    print(f"extension: {info['name']} v{info['version']} state={info['state']}")
    me = info.get("manifestErrors", [])
    re_ = info.get("runtimeErrors", [])
    iw = info.get("installWarnings", [])
    if iw:
        print("install warnings:")
        for w in iw:
            print("  -", (w.get("message") if isinstance(w, dict) else w))
    if me:
        print("MANIFEST ERRORS:")
        for e in me:
            print("  -", json.dumps(e)[:300])
    if re_:
        print("RUNTIME ERRORS:")
        for e in re_:
            print("  -", json.dumps(e)[:300])

    # 3. Reload the takeout tab so the new content script injects.
    targets = http_get("/json")
    takeout = next((t for t in targets
                    if t.get("type") == "page"
                    and "takeout.google.com" in t.get("url", "")), None)
    if takeout and takeout.get("webSocketDebuggerUrl"):
        tw = WS(takeout["webSocketDebuggerUrl"])
        tw.call("Page.enable")
        tw.call("Page.reload", {"ignoreCache": True})
        tw.close()
        print("takeout tab reloaded (ignoreCache)")
    else:
        print("note: no takeout.google.com tab open to reload")

    if not info.get("enabled") or me:
        print("RESULT: FAIL — extension not cleanly enabled")
        sys.exit(2)
    print("RESULT: OK — extension reloaded clean" + (" (with install warnings)" if iw else ""))


if __name__ == "__main__":
    main()
