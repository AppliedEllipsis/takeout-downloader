"""Telegram notifier + command long-poll.

Spec: docs/webgui/06-telegram.md. Uses only the Python stdlib (urllib) so the
manager gains no new dependency. Two roles:

  1. TelegramNotifier — formats + sends event messages (started, needs_cookie,
     error, complete, milestone) to a single chat. Rate-limits milestones.
  2. poll_commands — a background long-poll loop (getUpdates) that handles
     /status, /jobs, /health, /pause, /resume, /recapture, /diagnose, /mute,
     /unmute, /recipes, /run. Commands are scoped to TELEGRAM_CHAT_ID; messages
     from any other chat are ignored.

Design rules from the spec:
  - If disabled / no token / no chat id => every method is a safe no-op. The
    manager still runs fully.
  - Network failures to Telegram are logged + swallowed; Telegram being down
    must NEVER stall a download.
  - notify.py takes job SNAPSHOT dicts (not engine objects) so it is testable
    offline.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("manager.notify")

API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _human_size(n: int) -> int | str:
    if not n or n < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} PB"


def _human_speed(bps: float) -> str:
    return _human_size(int(bps)) + "/s" if bps else "0 B/s"


def _api_call(token: str, method: str, params: dict, timeout: float = 10.0) -> Optional[dict]:
    """POST to the Telegram Bot API. Returns parsed JSON or None on failure."""
    if not token:
        return None
    url = API_BASE.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — Telegram down must not break us
        log.debug("telegram %s failed: %r", method, e)
        return None


class TelegramNotifier:
    """Formats + sends event messages to one chat. No-op when not configured."""

    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = True,
                 progress_interval: int = 300, portal_url: str = ""):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.enabled = bool(enabled and self.token and self.chat_id)
        self.progress_interval = progress_interval
        self.portal_url = portal_url
        self._muted = False
        self._last_progress_at = 0.0
        self._lock = threading.Lock()

    # -- low level ------------------------------------------------------------
    def send(self, text: str) -> bool:
        """Send one message. Returns True on success. Safe no-op when disabled."""
        if not self.enabled:
            return False
        res = _api_call(self.token, "sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        return bool(res and res.get("ok"))

    # -- event formatting -----------------------------------------------------
    def send_event(self, kind: str, job: dict) -> None:
        """Format + send a state-change event from a job snapshot dict."""
        if not self.enabled:
            return
        try:
            text = self._format_event(kind, job)
        except Exception as e:  # noqa: BLE001
            log.debug("format event failed: %r", e)
            return
        if text is None:
            return
        # Milestones are rate-limited + mutable; state changes always fire.
        if kind == "milestone":
            if self._muted:
                return
            now = time.monotonic()
            with self._lock:
                if (now - self._last_progress_at) < self.progress_interval:
                    return
                self._last_progress_at = now
        self.send(text)

    def _format_event(self, kind: str, job: dict) -> Optional[str]:
        wf = job.get("workflow", "?")
        totals = job.get("totals", {}) or {}
        pdone = totals.get("parts_done", 0)
        ptotal = totals.get("parts_total", 0)
        bdone = _human_size(totals.get("bytes_done", 0))
        btotal = _human_size(totals.get("bytes_total", 0))
        speed = _human_speed(totals.get("speed_bps", 0))

        if kind == "started":
            return f"▶️ <b>{wf}</b>: started, {ptotal} parts, {btotal} total"
        if kind == "milestone":
            return f"⏳ <b>{wf}</b>: {pdone}/{ptotal} parts, {bdone} / {btotal}, {speed}"
        if kind == "needs_cookie":
            return f"🔑 <b>{wf}</b>: cookie expired. Auto-recapturing…"
        if kind == "login_needed":
            portal = self.portal_url or "(open the KasmVNC portal)"
            return f"🚨 <b>{wf}</b>: manual login needed. Open the portal: {portal}"
        if kind == "error":
            reason = job.get("reason") or (job.get("last_error") or "error")
            return f"❌ <b>{wf}</b>: {reason}. Partials kept."
        if kind == "complete":
            return f"✅ <b>{wf}</b>: done. {pdone}/{ptotal} parts, {btotal}."
        if kind == "resumed":
            return f"♻️ <b>{wf}</b>: resumed from {pdone}/{ptotal} parts."
        return None

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)


# ---------------------------------------------------------------------------
# Command long-poll
# ---------------------------------------------------------------------------
class CommandPoller:
    """Long-poll getUpdates and dispatch slash-commands to the orchestrator.

    `orch` is the manager Orchestrator. We only call its public methods, and we
    only act on messages from the configured chat id.
    """

    def __init__(self, notifier: TelegramNotifier, orch, recipes=None):
        self.n = notifier
        self.orch = orch
        self.recipes = recipes
        self._offset = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_confirm: dict[str, str] = {}  # destructive cmd confirm
        self._confirmed: Optional[str] = None  # the command text currently confirmed via /yes

    def start(self) -> None:
        if not self.n.enabled:
            log.info("telegram disabled; command poller not started")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("telegram command poller started")
        while not self._stop.is_set():
            res = _api_call(self.n.token, "getUpdates", {
                "offset": self._offset,
                "timeout": 25,
            }, timeout=30.0)
            if not res or not res.get("ok"):
                time.sleep(3)
                continue
            for upd in res.get("result", []):
                self._offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post")
                if not msg:
                    continue
                if not self._chat_ok(msg):
                    continue  # ignore anyone but the configured chat
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    try:
                        self._handle(text)
                    except Exception as e:  # noqa: BLE001
                        log.debug("command %r failed: %r", text, e)
                        self.n.send(f"command failed: {e}")

    def _chat_ok(self, src) -> bool:
        """True only for the configured chat. Simple authz for inbound commands.

        Accepts a raw chat id, a Telegram message dict, or a full update dict
        ({"message"|"channel_post": {"chat": {"id": ...}}}).
        """
        chat_id = src
        if isinstance(src, dict):
            msg = src.get("message") or src.get("channel_post") or src
            chat_id = (msg.get("chat") or {}).get("id", "")
        return str(chat_id) == self.n.chat_id

    # -- command dispatch -----------------------------------------------------
    def _active_job(self):
        jobs = self.orch.list_jobs()
        # Prefer a downloading/needs_cookie job, else the most recent.
        for j in jobs:
            if j["status"] in ("downloading", "needs_cookie", "queued", "paused"):
                return j
        return jobs[0] if jobs else None

    def _handle(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        args = parts[1:]

        # Confirm step for destructive commands.
        if cmd == "yes" and self._pending_confirm:
            real = self._pending_confirm.pop("cmd", None)
            if real:
                self._confirmed = real
                try:
                    return self._handle(real)
                finally:
                    self._confirmed = None
            return

        if cmd == "status":
            j = self._active_job()
            if not j:
                return void(self.n.send("No jobs yet."))
            t = j["totals"]
            self.n.send(
                f"<b>{j['workflow']}</b> — {j['status']}\n"
                f"{t['parts_done']}/{t['parts_total']} parts, "
                f"{_human_size(t['bytes_done'])} / {_human_size(t['bytes_total'])}, "
                f"{_human_speed(t['speed_bps'])}")
        elif cmd == "jobs":
            jobs = self.orch.list_jobs()
            if not jobs:
                return void(self.n.send("No jobs."))
            lines = [f"• {j['workflow']}: {j['status']} "
                     f"({j['totals']['parts_done']}/{j['totals']['parts_total']})"
                     for j in jobs[:20]]
            self.n.send("\n".join(lines))
        elif cmd == "health":
            jobs = self.orch.list_jobs()
            active = [j for j in jobs if j["status"] in ("downloading", "needs_cookie")]
            self.n.send(f"OK. {len(jobs)} job(s), {len(active)} active.")
        elif cmd == "diagnose":
            j = self._active_job()
            if not j:
                return void(self.n.send("No jobs."))
            d = self.orch.diagnose(j["job_id"]) or {}
            self.n.send(f"<b>{j['workflow']}</b>: {d.get('reason','?')} "
                        f"({d.get('section','')}). {d.get('recommended_action','')}")
        elif cmd == "pause":
            j = self._active_job()
            ok = self.orch.pause(j["job_id"]) if j else False
            self.n.send("Paused." if ok else "Nothing to pause.")
        elif cmd == "resume":
            j = self._active_job()
            ok = self.orch.resume(j["job_id"]) if j else False
            self.n.send("Resuming." if ok else "Nothing to resume.")
        elif cmd == "mute":
            self.n.set_muted(True)
            self.n.send("Progress milestones muted. State changes still fire.")
        elif cmd == "unmute":
            self.n.set_muted(False)
            self.n.send("Progress milestones unmuted.")
        elif cmd == "recapture":
            j = self._active_job()
            if not j:
                return void(self.n.send("No jobs."))
            if self._confirmed != text:
                self._pending_confirm["cmd"] = text
                return void(self.n.send("Force a cookie re-capture? Reply /yes to confirm."))
            self.orch.request_recapture(j["job_id"])
            self.n.send("Recapture requested.")
        elif cmd == "recipes":
            if not self.recipes:
                return void(self.n.send("Recipes not available."))
            names = self.recipes.list_names()
            self.n.send("Recipes:\n" + ("\n".join(f"• {n}" for n in names) if names else "(none)"))
        elif cmd == "run":
            if not self.recipes:
                return void(self.n.send("Recipes not available."))
            if not args:
                return void(self.n.send("Usage: /run <name>"))
            name = args[0]
            if self._confirmed != text:
                self._pending_confirm["cmd"] = text
                return void(self.n.send(f"Replay recipe '{name}'? Reply /yes to confirm."))
            ok = self.recipes.run(name)
            self.n.send(f"Replaying '{name}'." if ok else f"No such recipe '{name}'.")
        elif cmd in ("start", "help"):
            self.n.send(
                "Takeout manager bot. Commands: /status /jobs /health /diagnose "
                "/pause /resume /recapture /recipes /run &lt;name&gt; /mute /unmute")
        # unknown commands are silently ignored


def void(_):
    """Swallow a return value (keeps the dispatch branches one-liners)."""
    return None


# ---------------------------------------------------------------------------
# chat-id capture helper (CLI)
# ---------------------------------------------------------------------------
def capture_chat_id(token: str) -> Optional[str]:
    """Print + return the chat id from the latest getUpdates result."""
    res = _api_call(token, "getUpdates", {"timeout": 1}, timeout=10.0)
    if not res or not res.get("ok"):
        print("getUpdates failed. Is the token correct? Send the bot a message first.")
        return None
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post")
        if msg:
            cid = str((msg.get("chat") or {}).get("id", ""))
            if cid:
                print(f"chat_id = {cid}")
                return cid
    print("No messages found. Send a message in the chat/channel, then retry.")
    return None


if __name__ == "__main__":
    import argparse
    import os

    p = argparse.ArgumentParser(description="Telegram helper for the Takeout manager")
    p.add_argument("--capture-chat-id", action="store_true",
                   help="print the chat id from the latest message to the bot")
    p.add_argument("--hello", action="store_true", help="send a test message")
    p.add_argument("--token", default=os.environ.get("TELEGRAM_TOKEN", ""))
    p.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    a = p.parse_args()

    if a.capture_chat_id:
        capture_chat_id(a.token)
    elif a.hello:
        n = TelegramNotifier(a.token, a.chat_id, enabled=True)
        print("sent" if n.send("✅ Takeout manager: hello") else "send failed")
    else:
        p.print_help()
