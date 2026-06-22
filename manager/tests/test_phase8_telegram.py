"""Phase 8: Telegram notifier + command poller, fully offline.

We monkeypatch notify._api_call so no network is touched. Verifies:
  - notifier is a no-op when disabled / unconfigured
  - event formatting for each kind
  - milestone rate-limiting + mute
  - command dispatch maps to orchestrator control methods
  - chat-scoped authz (foreign chat ignored)
Run: .venv-manager/Scripts/python -m manager.tests.test_phase8_telegram
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from manager import notify as N  # noqa: E402


SENT = []


def _fake_api(token, method, params, timeout=10.0):
    SENT.append((method, params))
    if method == "getUpdates":
        return {"ok": True, "result": []}
    return {"ok": True, "result": {"message_id": len(SENT)}}


class FakeOrch:
    """Minimal orchestrator stand-in recording control calls."""
    def __init__(self):
        self.calls = []
        self._jobs = [{
            "job_id": "j1", "workflow": "braincreation", "status": "downloading",
            "totals": {"parts_done": 41, "parts_total": 290,
                       "bytes_done": 442_000_000_000, "bytes_total": 3_110_000_000_000,
                       "speed_bps": 88_000_000},
        }]

    def list_jobs(self):
        return self._jobs

    def pause(self, jid):
        self.calls.append(("pause", jid)); return True

    def resume(self, jid):
        self.calls.append(("resume", jid)); return True

    def request_recapture(self, jid):
        self.calls.append(("recapture", jid)); return True

    def diagnose(self, jid):
        return {"reason": "cookie_expired", "section": "§A",
                "recommended_action": "auto-recaptures"}


def main():
    N._api_call = _fake_api

    # 1. Disabled notifier is a no-op.
    SENT.clear()
    off = N.TelegramNotifier(token="", chat_id="", enabled=True)
    assert off.send("hi") is False, "no token => no send"
    assert not SENT, "disabled notifier must not call the API"
    print("[OK] unconfigured notifier is a no-op")

    # 2. Configured notifier sends.
    SENT.clear()
    n = N.TelegramNotifier(token="T", chat_id="123", enabled=True,
                           progress_interval=60, portal_url="https://portal")
    assert n.send("hello") is True
    assert SENT and SENT[-1][0] == "sendMessage"
    assert SENT[-1][1]["chat_id"] == "123"
    print("[OK] configured notifier sends to the chat")

    # 3. Event formatting.
    job = {"workflow": "braincreation",
           "totals": {"parts_done": 290, "parts_total": 290,
                      "bytes_done": 3_110_000_000_000, "bytes_total": 3_110_000_000_000,
                      "speed_bps": 0}}
    SENT.clear()
    n.send_event("started", job)
    n.send_event("complete", job)
    n.send_event("needs_cookie", job)
    n.send_event("error", {**job, "reason": "disk_full"})
    texts = [s[1]["text"] for s in SENT]
    assert any("started" in t for t in texts)
    assert any("done" in t for t in texts)
    assert any("cookie expired" in t for t in texts)
    assert any("disk_full" in t for t in texts), texts
    print("[OK] event formatting (started/complete/needs_cookie/error reason)")

    # 4. Milestone rate-limiting + mute.
    SENT.clear()
    n.send_event("milestone", job)          # first fires
    n.send_event("milestone", job)          # within interval => suppressed
    assert len(SENT) == 1, f"rate-limit failed: {len(SENT)}"
    n.set_muted(True)
    n._last_progress_at = 0                  # bypass interval to test mute alone
    n.send_event("milestone", job)
    assert len(SENT) == 1, "muted milestone must not send"
    print("[OK] milestone rate-limited + mute respected")

    # 5. Command dispatch maps to control calls.
    orch = FakeOrch()
    poller = N.CommandPoller(n, orch)
    SENT.clear()
    poller._handle("/status")
    poller._handle("/pause")
    poller._handle("/resume")
    # recapture is destructive => first call asks for confirm, /yes runs it
    poller._handle("/recapture")
    poller._handle("/yes")
    assert ("pause", "j1") in orch.calls, orch.calls
    assert ("resume", "j1") in orch.calls, orch.calls
    assert ("recapture", "j1") in orch.calls, orch.calls
    print("[OK] command dispatch -> pause/resume/recapture (with confirm step)")

    # 6. Chat-scoped authz: foreign chat ignored by the update filter.
    n2 = N.TelegramNotifier(token="T", chat_id="123", enabled=True)
    poller2 = N.CommandPoller(n2, FakeOrch())
    foreign = {"message": {"chat": {"id": 999}, "text": "/pause"}}
    assert poller2._chat_ok(foreign) is False, "foreign chat must be rejected"
    mine = {"message": {"chat": {"id": 123}, "text": "/pause"}}
    assert poller2._chat_ok(mine) is True
    print("[OK] foreign chat rejected; own chat accepted")

    print("\n[PASS] Phase 8: Telegram notifier + commands verified offline")


if __name__ == "__main__":
    main()
