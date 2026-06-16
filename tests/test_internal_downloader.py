"""
Tests for the in-process parallel downloader (``takeout_downloader``).

These spin up a real local HTTP server (no network) that serves byte
payloads with Range support, a 404 path, and an HTML "sign-in challenge"
path. That lets us exercise the actual socket/stream/resume code instead
of mocking ``requests``, so the tests prove the behavior the user cares
about: parallel downloads, live progress, Range resume, and never writing
an auth-challenge page into an archive file.
"""
from __future__ import annotations

import http.server
import os
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import takeout_downloader as D  # noqa: E402


# ---------------------------------------------------------------------------
# Local test server
# ---------------------------------------------------------------------------
_DATA: dict[str, bytes] = {
    "p1.zip": b"PK\x03\x04" + os.urandom(3_000_000 - 4),
    "p2.zip": b"PK\x03\x04" + os.urandom(1_000_000 - 4),
    "p3.zip": b"PK\x03\x04" + os.urandom(5_000_000 - 4),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        name = self.path.lstrip("/").split("?")[0]
        if name == "challenge.zip":
            body = b"<!doctype html><html><body>sign in</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = _DATA.get(name)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        chunk = data[start:]
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Type", "application/zip")
        self.end_headers()
        # Trickle so concurrent transfers actually overlap and progress
        # callbacks fire mid-stream.
        for i in range(0, len(chunk), 250_000):
            try:
                self.wfile.write(chunk[i:i + 250_000])
                time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                return


@pytest.fixture(scope="module")
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _parts(base: str) -> list[dict]:
    return [
        {"num": 1, "url": f"{base}/p1.zip", "filename": "p1.zip",
         "size": 3_000_000, "have": False},
        {"num": 2, "url": f"{base}/p2.zip", "filename": "p2.zip",
         "size": 1_000_000, "have": False},
        {"num": 3, "url": f"{base}/p3.zip", "filename": "p3.zip",
         "size": 5_000_000, "have": False},
    ]


def _mk(out: Path, parallel: int = 3) -> "D.InternalDownloader":
    return D.InternalDownloader(
        cookie="SID=x", headers={"User-Agent": "test"},
        output_dir=out, parallel=parallel, retry_wait=0.1, max_tries=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_parallel_download_all_parts(server, tmp_path):
    dl = _mk(tmp_path)
    res = dl.download(_parts(server), on_progress=lambda s: None)
    assert res.ok
    assert sorted(res.completed) == [1, 2, 3]
    assert not res.failed
    for name, size in (("p1.zip", 3_000_000), ("p2.zip", 1_000_000),
                       ("p3.zip", 5_000_000)):
        assert (tmp_path / name).stat().st_size == size


def test_live_progress_callbacks_fire_midstream(server, tmp_path):
    """The whole point of the rewrite: progress must update *during* the
    download, not only at the end. We require at least one snapshot where
    a part is strictly between 0% and 100%."""
    dl = _mk(tmp_path, parallel=3)
    seen_partial = {"v": False}

    def on_prog(snap):
        for p in snap:
            if 0 < p.pct < 100 or (p.status == "active" and 0 < p.done < p.total):
                seen_partial["v"] = True

    dl.download(_parts(server), on_progress=on_prog, progress_interval=0.05)
    assert seen_partial["v"], "no mid-stream progress was ever observed"


def test_range_resume_keeps_existing_bytes(server, tmp_path):
    # First, full download of p3.
    dl = _mk(tmp_path)
    dl.download([{"num": 3, "url": f"{server}/p3.zip", "filename": "p3.zip",
                  "size": 5_000_000, "have": False}],
                on_progress=lambda s: None)
    full = (tmp_path / "p3.zip").read_bytes()
    # Truncate to simulate an interrupted partial.
    (tmp_path / "p3.zip").write_bytes(full[:2_000_000])
    # Resume: should Range-GET the rest and end up byte-identical.
    res = dl.download([{"num": 3, "url": f"{server}/p3.zip",
                        "filename": "p3.zip", "size": 5_000_000,
                        "have": False}],
                      on_progress=lambda s: None)
    assert res.ok
    assert (tmp_path / "p3.zip").read_bytes() == full


def test_auth_challenge_not_written_to_archive(server, tmp_path):
    """An HTML sign-in page must NEVER land in the archive file, and the
    result must flag auth_failed so the caller re-prompts."""
    dl = _mk(tmp_path)
    res = dl.download([{"num": 9, "url": f"{server}/challenge.zip",
                        "filename": "challenge.zip", "size": 0,
                        "have": False}],
                      on_progress=lambda s: None)
    assert res.auth_failed
    assert res.auth_body  # challenge HTML captured for inspection
    # The archive file must not contain the HTML page.
    dest = tmp_path / "challenge.zip"
    assert not dest.exists() or dest.stat().st_size == 0


def test_have_parts_are_skipped(server, tmp_path):
    parts = _parts(server)
    # Pre-create p2 and mark it have=True; it must not be re-fetched.
    (tmp_path / "p2.zip").write_bytes(_DATA["p2.zip"])
    parts[1]["have"] = True
    res = dl = _mk(tmp_path).download(parts, on_progress=lambda s: None)
    assert 2 in res.completed
    assert sorted(res.completed) == [1, 2, 3]


def test_404_is_recorded_as_failure(server, tmp_path):
    dl = _mk(tmp_path)
    res = dl.download([{"num": 7, "url": f"{server}/missing.zip",
                        "filename": "missing.zip", "size": 0,
                        "have": False}],
                      on_progress=lambda s: None)
    assert not res.ok
    assert 7 in res.failed

def test_per_part_start_and_done_are_logged(server, tmp_path):
    """Each part must emit a 'start' and a 'done' INFO line via the wired
    logger, so a second SSH session tailing the log file sees a heartbeat
    even when the live grid is only painting the (invisible-to-the-log)
    terminal. This is the regression behind 'it just sits there with
    nothing in the log'."""
    import logging
    logger = logging.getLogger("test_dl_logging")
    logger.setLevel(logging.INFO)
    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger.addHandler(_Cap())
    dl = D.InternalDownloader(
        cookie="SID=x", headers={"User-Agent": "test"},
        output_dir=tmp_path, parallel=2, retry_wait=0.1, max_tries=2,
        logger=logger)
    dl.download(_parts(server), on_progress=lambda s: None)
    starts = [m for m in records if " start: " in m]
    dones = [m for m in records if " done: " in m]
    assert len(starts) == 3, f"expected 3 start lines, got {starts}"
    assert len(dones) == 3, f"expected 3 done lines, got {dones}"
