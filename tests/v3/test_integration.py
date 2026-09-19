"""Integration test for the orchestrator, `autopilot.run`.

Written against `docs/v3/02-RUN-INTERFACE.md`, which is a **frozen contract** —
deliberately *not* against the implementation. Where a rule here looks odd, the
spec is the source of truth and the mismatch is a bug in `run.py`.

No pytest plugins: async cases are driven with `asyncio.run` inside plain sync
tests, exactly as `test_transport.py` and `test_mint.py` do.

The whole file leans on one property: **injection is total** (rule 9). With
`session_factory` and `http_client` supplied, `run_once` must not open a socket
and must not connect to a browser — so the orchestrator can be driven end to end
against a scripted CDP transport and a scripted HTTP client.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import zipfile
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from autopilot.cdp import CdpSession
from autopilot.ledger import LedgerOnFuseMount, open_ledger
from autopilot.run import RunConfig, RunOutcome, run_once, run_once_sync
from fake_cdp import FakeTransport, redirect_event
from fake_http import FakeHttpClient, FakeStream

ARCHIVE_ID = "f470fe32-76d6-43a6-9321-dbc76834919e"
USER = "116943325794238708860"
ORIGIN = "https://takeout.google.com"
FILE_HOST = "takeout-download.usercontent.google.com"
ACCOUNT = "tester@example.com"

CHALLENGE_URL = (
    "https://accounts.google.com/v3/signin/challenge/pwd"
    "?TL=ACv9tzEyMERAmxQTT5X-GlF72fkOYy-6&authuser=0"
)
LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=x"
FUTURE = "September 26, 2099 at 4:58 AM"
PAST = "January 1, 2000 at 4:58 AM"
NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _boom(*_a, **_k):
    raise AssertionError("real network I/O attempted during an injected run")


# ---------------------------------------------------------------------------
# URL / payload builders
# ---------------------------------------------------------------------------
def archive_url() -> str:
    return f"{ORIGIN}/manage/archive/{ARCHIVE_ID}"


def raw_uri(i: int) -> str:
    return f"takeout/download?j={ARCHIVE_ID}&i={i}&user={USER}&rapt=R{i}"


def redirector(i: int) -> str:
    """The absolute redirector URL `scrape` will resolve the raw uri into."""
    return f"{ORIGIN}/{raw_uri(i)}"


def file_url(i: int) -> str:
    return (
        f"https://{FILE_HOST}/download/takeout-part{i}.zip"
        f"?j={ARCHIVE_ID}&i={i}&user=1005482974000&authuser=0"
    )


def part_filename(i: int) -> str:
    return f"takeout-20260919T045820Z-1-00{i + 1}.zip"


def make_zip_bytes(tmp_path, name: str, payload: bytes) -> bytes:
    """A real (structurally valid) zip, so `verify_local` accepts it.

    STRUCT_OK is `PK\\x03\\x04` at the head plus an EOCD in the tail, so an empty
    archive would not do — this writes one stored member.
    """
    p = tmp_path / name
    info = zipfile.ZipInfo("a.txt", date_time=(2026, 1, 1, 0, 0, 0))
    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as z:
        z.writestr(info, payload)
    return p.read_bytes()


# ---------------------------------------------------------------------------
# the injected CDP side
# ---------------------------------------------------------------------------
class _PageTransport(FakeTransport):
    """`FakeTransport` whose `Runtime.evaluate` answers by expression content.

    Dispatch is by content, not FIFO, so the harness does not depend on the order
    in which the orchestrator happens to evaluate scripts (that order is not part
    of the frozen contract).
    """

    def __init__(self, page_json: str, page_url: str, **kw) -> None:
        super().__init__(**kw)
        self._page_json = page_json
        self._page_url = page_url
        self.evaluated: list[str] = []

    async def send(self, payload: str) -> None:
        msg = json.loads(payload)
        if msg.get("method") == "Runtime.evaluate":
            expr = (msg.get("params") or {}).get("expression") or ""
            self.sent.append(payload)
            self.evaluated.append(expr)
            if "data-download-uri" in expr:
                value = self._page_json
            elif "location" in expr:
                value = self._page_url
            else:
                value = self._page_json
            reply = {
                "id": msg.get("id"),
                "result": {"result": {"type": "string", "value": value}},
            }
            await self._queue().put(json.dumps(reply))
            return
        await super().send(payload)


class Harness:
    """A `session_factory`: `() -> async CM yielding CdpSession`.

    Every session handed out records into `self.transports`, so assertions can
    aggregate across however many sessions the orchestrator chooses to open.
    """

    def __init__(self, page_json: str, plan=None, cookies=None, page_url=None) -> None:
        self.page_json = page_json
        self.plan = dict(plan or {})
        self.cookies = cookies if cookies is not None else [
            {"name": "SID", "value": "1", "domain": ".google.com"},
            {"name": "HSID", "value": "2", "domain": ".google.com"},
        ]
        self.page_url = page_url or archive_url()
        self.transports: list[_PageTransport] = []

    def __call__(self) -> CdpSession:
        t = _PageTransport(
            self.page_json,
            self.page_url,
            plan=self.plan,
            command_results={"Storage.getCookies": {"cookies": self.cookies}},
        )
        self.transports.append(t)
        return CdpSession(t)

    def navigated(self) -> list[str]:
        out: list[str] = []
        for t in self.transports:
            out.extend(t.navigated())
        return out

    def methods(self) -> list[str]:
        out: list[str] = []
        for t in self.transports:
            out.extend(t.methods())
        return out

    def mint_navigations(self) -> list[str]:
        """Navigations to a *mint redirector* — i.e. attempts to mint."""
        return [u for u in self.navigated() if "/takeout/download?" in u]


# ---------------------------------------------------------------------------
# scenario builder
# ---------------------------------------------------------------------------
def scenario(tmp_path, *, n_parts: int = 1, expiry: str = FUTURE,
             max_parts=None, require_mount: bool = False,
             mint_plan=None, http_responses=None, preseed_mint: bool = False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    ledger_path = str(tmp_path / "ledger.db")

    bodies: dict[int, bytes] = {}
    sizes: dict[int, int] = {}
    plan: dict[str, list] = {}
    part_rows = []
    for i in range(n_parts):
        body = make_zip_bytes(tmp_path, f"part-{i}.zip",
                              b"takeout payload %d " % i * 8)
        bodies[i] = body
        sizes[i] = len(body)
        rd = redirector(i)
        plan[rd] = [redirect_event(rd, file_url(i))]
        part_rows.append({
            "raw_uri": raw_uri(i),
            "index": i,
            "archive_id": ARCHIVE_ID,
            "user": USER,
            "has_rapt": True,
            "size": str(sizes[i]),
            "aria": "",
            "filename": part_filename(i),
        })
    if mint_plan:
        plan.update(mint_plan)

    page = json.dumps({
        "url": archive_url(),
        "title": "Google Takeout",
        "parts": part_rows,
        "dl_counts": {},
        "expiry": expiry,
        "status": "Completed",
        "challenged": False,
        "part_of_N": [n_parts] if n_parts else [],
    })

    if preseed_mint:
        led = open_ledger(ledger_path)
        led.upsert_job(ARCHIVE_ID, account=ACCOUNT)
        led.upsert_parts(ARCHIVE_ID, [(i, part_filename(i), sizes[i])
                                      for i in range(n_parts)])
        for i in range(n_parts):
            led.record_mint(ARCHIVE_ID, i, file_url(i))
        led.close()

    if http_responses is None:
        http_responses = [
            FakeStream(200, {"Content-Length": str(sizes[i])}, bodies[i])
            for i in range(n_parts)
        ]

    return {
        "cfg": RunConfig(
            archive_id=ARCHIVE_ID,
            ledger_path=ledger_path,
            staging_dir=str(staging),
            archive_dir=str(archive),
            account=ACCOUNT,
            require_mount=require_mount,
            max_parts=max_parts,
            settle=0.01,
        ),
        "harness": Harness(page, plan=plan),
        "client": FakeHttpClient(list(http_responses)),
        "ledger_path": ledger_path,
        "staging": staging,
        "archive": archive,
        "bodies": bodies,
        "sizes": sizes,
    }


def attempts(ledger_path) -> list[str]:
    con = sqlite3.connect(ledger_path)
    try:
        return [r[0] for r in con.execute("SELECT kind FROM attempts ORDER BY id")]
    finally:
        con.close()


def go(s: dict, **kw):
    kw.setdefault("now", NOW)
    return run(run_once(s["cfg"], session_factory=s["harness"],
                        http_client=s["client"], **kw))


# ---------------------------------------------------------------------------
# rule 9 — injection is total
# ---------------------------------------------------------------------------
def test_rule9_injection_is_total_no_real_network_no_real_cdp(tmp_path, monkeypatch):
    """With both fakes injected there is no socket and no browser connection.

    Everything else in this file depends on this, so it is asserted directly:
    the OS-level connect paths and the CDP websocket connect are booby-trapped.
    """
    import autopilot.ws_transport as ws

    s = scenario(tmp_path, n_parts=1)

    # Build the event loop BEFORE booby-trapping sockets: on Windows the
    # proactor loop's self-pipe is created via socketpair(), which would trip
    # the trap during `asyncio.run` startup rather than during the run.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        monkeypatch.setattr(socket, "create_connection", _boom)
        monkeypatch.setattr(socket.socket, "connect", _boom)

        async def _no_ws(cls, url, **kw):
            raise AssertionError(f"real CDP websocket connect attempted: {url}")

        monkeypatch.setattr(ws.WebSocketTransport, "connect", classmethod(_no_ws))

        outcome = loop.run_until_complete(
            run_once(s["cfg"], session_factory=s["harness"],
                     http_client=s["client"], now=NOW))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert isinstance(outcome, RunOutcome)
    assert s["harness"].navigated(), "the injected CDP session was never driven"


# ---------------------------------------------------------------------------
# rule 2 — the mint cache is honoured
# ---------------------------------------------------------------------------
def test_rule2_a_cached_mint_is_not_re_minted(tmp_path):
    # A mint is measured Δ1; re-minting a part that already holds a URL is that
    # cost for nothing.
    s = scenario(tmp_path, n_parts=1, preseed_mint=True)
    go(s)

    assert s["harness"].mint_navigations() == [], (
        "a part whose minted URL is cached must not be minted again"
    )
    assert attempts(s["ledger_path"]).count("mint") == 1, (
        "only the seeded mint should be recorded — no re-mint"
    )


# ---------------------------------------------------------------------------
# rule 3 — NeedsReauth is a status, not an exception
# ---------------------------------------------------------------------------
def test_rule3_needs_reauth_from_mint_becomes_a_status(tmp_path):
    rd = redirector(0)
    s = scenario(tmp_path, n_parts=1,
                 mint_plan={rd: [redirect_event(rd, CHALLENGE_URL)]},
                 http_responses=[])

    outcome = go(s)                                  # must not raise

    assert outcome.status == "needs_reauth"
    assert "accounts.google.com" in (outcome.error or "")
    led = open_ledger(s["ledger_path"])
    try:
        job = led.job(ARCHIVE_ID)
        assert job is not None, "rule 3: the job must be recorded in the ledger"
        assert job["status"] == "needs_reauth"
        assert all(p.status != "failed" for p in led.parts(ARCHIVE_ID)), (
            "a ReAuth challenge must not mark any part failed"
        )
    finally:
        led.close()


def test_rule3_needs_reauth_from_transfer_becomes_a_status(tmp_path):
    s = scenario(tmp_path, n_parts=1, http_responses=[
        FakeStream(302, {"Location": LOGIN_URL}, b""),
    ])

    outcome = go(s)                                  # must not raise

    assert outcome.status == "needs_reauth"
    assert "accounts.google.com" in (outcome.error or "")
    led = open_ledger(s["ledger_path"])
    try:
        assert all(p.status != "failed" for p in led.parts(ARCHIVE_ID)), (
            "a ReAuth challenge must not mark any part failed"
        )
    finally:
        led.close()


# ---------------------------------------------------------------------------
# rule 4 — an expired export is terminal
# ---------------------------------------------------------------------------
def test_rule4_an_expired_export_is_terminal_and_never_mints(tmp_path):
    # The fix for the three-month tab-flood loop: once the export has expired
    # there is nothing to mint, so no browser work may be attempted at all.
    s = scenario(tmp_path, n_parts=1, expiry=PAST)

    outcome = go(s)

    assert outcome.status == "expired_unrecoverable"
    assert s["harness"].mint_navigations() == [], (
        "an expired export must not attempt a mint"
    )
    assert s["client"].requests == [], (
        "an expired export must not attempt a transfer"
    )


# ---------------------------------------------------------------------------
# rule 5 — max_parts bounds the work
# ---------------------------------------------------------------------------
def test_rule5_max_parts_bounds_the_work(tmp_path):
    s = scenario(tmp_path, n_parts=2, max_parts=1)

    outcome = go(s)

    assert len(s["client"].requests) <= 1, "max_parts=1 must transfer at most one part"
    assert outcome.status == "incomplete", (
        "one part of two is not complete — it is resumable"
    )
    assert outcome.parts_done <= 1


# ---------------------------------------------------------------------------
# rule 6 — never a FUSE-backed ledger
# ---------------------------------------------------------------------------
def test_rule6_a_fuse_backed_ledger_path_is_refused(tmp_path, monkeypatch):
    import autopilot.ledger as ledger_mod

    # The host's real /proc/mounts is unavailable on Windows, so the mount table
    # is injected — the refusal itself is what is under test, not the reader.
    monkeypatch.setattr(ledger_mod, "_read_mounts",
                        lambda: [("/", "ext4"), ("/opt/archives", "fuse.rclone")])
    s = scenario(tmp_path, n_parts=1)
    cfg = replace(s["cfg"], ledger_path="/opt/archives/state.db")

    with pytest.raises(LedgerOnFuseMount):
        run(run_once(cfg, session_factory=s["harness"], http_client=s["client"]))


# ---------------------------------------------------------------------------
# rule 7 — empty scrape is not success
# ---------------------------------------------------------------------------
def test_rule7_zero_parts_is_never_complete(tmp_path):
    s = scenario(tmp_path, n_parts=0)

    outcome = go(s)

    assert outcome.status != "complete"
    assert outcome.status in ("failed", "incomplete")
    assert (outcome.error or "").strip(), "zero parts must carry an explanatory error"


# ---------------------------------------------------------------------------
# rule 8 — the report is always returned
# ---------------------------------------------------------------------------
def test_rule8_the_report_is_always_returned(tmp_path):
    rd = redirector(0)
    cases = {
        "needs_reauth": dict(
            n_parts=1,
            mint_plan={rd: [redirect_event(rd, CHALLENGE_URL)]},
            http_responses=[],
        ),
        "expired": dict(n_parts=1, expiry=PAST),
        "empty_scrape": dict(n_parts=0),
    }
    for name, kw in cases.items():
        s = scenario(tmp_path / name, **kw)
        outcome = go(s)
        assert outcome.report_markdown.strip(), (
            f"report_markdown was empty on the {name!r} path"
        )


# ---------------------------------------------------------------------------
# rule 1 — ordering of effects, and the ledger written as we go
# ---------------------------------------------------------------------------
def test_rule1_effects_run_in_order_and_are_ledgered(tmp_path):
    s = scenario(tmp_path, n_parts=1)

    outcome = go(s)

    assert outcome.status == "complete", (outcome.status, outcome.error)
    assert outcome.parts_expected == 1 and outcome.parts_done == 1

    moved = list(s["archive"].glob("*.zip"))
    assert moved, "the verified part was never moved onto the archive"

    kinds = attempts(s["ledger_path"])
    assert kinds, "nothing was recorded in the ledger"
    assert kinds[0] == "mint", f"mint must come first, saw {kinds}"
    assert kinds[1] in ("transfer", "resume"), f"transfer must second, saw {kinds}"

    led = open_ledger(s["ledger_path"])
    try:
        assert led.parts(ARCHIVE_ID)[0].status == "done"
        assert led.job(ARCHIVE_ID)["status"] == "complete"
    finally:
        led.close()


# ---------------------------------------------------------------------------
# the sync wrapper the CLI uses
# ---------------------------------------------------------------------------
def test_run_once_sync_wraps_the_async_entrypoint(tmp_path):
    rd = redirector(0)
    s = scenario(tmp_path, n_parts=1,
                 mint_plan={rd: [redirect_event(rd, CHALLENGE_URL)]},
                 http_responses=[])

    outcome = run_once_sync(s["cfg"], session_factory=s["harness"],
                            http_client=s["client"], now=NOW)

    assert isinstance(outcome, RunOutcome)
    assert outcome.status == "needs_reauth"
