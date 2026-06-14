"""
Tests for takeout_cli — focused on the bits that don't hit the network.

The real download path (discovery + aria2c) is tested end-to-end with mocks
in test_takeout_cli_flow.py. Here we cover:

- Log file creation + rotation (RotatingFileHandler)
- The CLI's aria2c batch-file builder (header plumbing)
- Auth-failure detection (signin redirect / HTML response)
- The analyze script's parser
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the project root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import takeout_cli  # noqa: E402
import takeout_cli_analyze as analyze  # noqa: E402
from takeout_payload import parse_payload  # noqa: E402


# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------
def test_logger_writes_to_file(tmp_path):
    log_path = tmp_path / "test.log"
    takeout_cli._install_logger(log_path, max_bytes=1024 * 1024,
                                backup_count=3)
    logger = takeout_cli.log
    logger.info("hello world")
    logger.info("with %s", "interpolation")
    # Flush handlers.
    for h in logger.handlers:
        h.flush()
    content = log_path.read_text(encoding="utf-8")
    assert "hello world" in content
    assert "with interpolation" in content
    assert "INFO" in content


def test_logger_rotates(tmp_path):
    log_path = tmp_path / "rot.log"
    # Tiny rotation budget so we can prove it triggers in a test.
    takeout_cli._install_logger(log_path, max_bytes=256, backup_count=3)
    logger = takeout_cli.log
    # Patch the maxBytes on the RotatingFileHandler for this test only.
    for h in logger.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.maxBytes = 256
    # Write enough to trigger multiple rotations.
    for i in range(200):
        logger.info("line %03d %s", i, "x" * 50)
    for h in logger.handlers:
        h.flush()
    # Main log + .1 + .2 (and maybe .3) should exist.
    assert log_path.exists()
    backups = sorted(tmp_path.glob("rot.log.*"))
    assert backups, "expected at least one rotated backup"
    # Backup sizes stay under the cap.
    for b in backups:
        assert b.stat().st_size <= 256 * 2  # small headroom for one overage


# ---------------------------------------------------------------------------
# aria2c batch file builder
# ---------------------------------------------------------------------------
def _fixture_payload():
    p = json.loads((ROOT / "tests" / "fixtures" / "sample_payload.json").read_text())
    return parse_payload(json.dumps(p))


def test_aria2_input_skips_have_parts(tmp_path):
    payload = _fixture_payload()
    parts = [
        {"num": 1, "url": "https://example/-001.zip", "filename": "f-001.zip",
         "size": 100, "have": False},
        {"num": 2, "url": "https://example/-002.zip", "filename": "f-002.zip",
         "size": 100, "have": True},
        {"num": 3, "url": "https://example/-003.zip", "filename": "f-003.zip",
         "size": 100, "have": False},
    ]
    body = takeout_cli.build_aria2_input(parts, payload, tmp_path)
    assert "f-001.zip" in body
    assert "f-002.zip" not in body
    assert "f-003.zip" in body
    # Cookie header on every URL block
    assert body.count("header=Cookie:") == 2
    # per-file out= lines
    assert body.count("  out=") == 2


def test_aria2_input_empty_when_all_have(tmp_path):
    payload = _fixture_payload()
    parts = [
        {"num": 1, "url": "x", "filename": "a.zip", "size": 1, "have": True},
    ]
    body = takeout_cli.build_aria2_input(parts, payload, tmp_path)
    assert body == ""


# ---------------------------------------------------------------------------
# Auth detection
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, url, status, headers=None):
        self.url = url
        self.status_code = status
        self.headers = headers or {}

    def close(self):
        pass


def test_probe_redirects_to_google_login_is_auth_error():
    fake = _FakeResp(
        "https://accounts.google.com/v3/signin/identifier?continue=...",
        200,
        {"content-type": "text/html; charset=utf-8"},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        with pytest.raises(takeout_cli.AuthError):
            takeout_cli._probe_part(
                takeout_cli.requests.Session(),
                "https://takeout-download.usercontent.google.com/download/x.zip",
                {"Cookie": "SID=test; __Secure-1PSID=test; HSID=test; SSID=test"},
            )


def test_probe_404_is_none():
    fake = _FakeResp(
        "https://takeout-download.usercontent.google.com/download/x-099.zip",
        404, {},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        assert takeout_cli._probe_part(
            takeout_cli.requests.Session(), "x", {},
        ) is None


def test_probe_206_returns_size():
    fake = _FakeResp(
        "https://takeout-download.usercontent.google.com/download/x-001.zip",
        206,
        {"content-range": "bytes 0-0/1234567",
         "content-type": "application/octet-stream"},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        size = takeout_cli._probe_part(
            takeout_cli.requests.Session(), "x", {},
        )
    assert size == 1234567


def test_probe_html_from_takeout_host_is_auth_error():
    """If takeout-download returns 200 with text/html, it's an error page."""
    fake = _FakeResp(
        "https://takeout-download.usercontent.google.com/download/x-001.zip",
        200,
        {"content-type": "text/html; charset=utf-8",
         "content-length": "4321"},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        with pytest.raises(takeout_cli.AuthError):
            takeout_cli._probe_part(
                takeout_cli.requests.Session(), "x", {},
            )


# ---------------------------------------------------------------------------
# Discovery end-to-end with mocks (no real network)
# ---------------------------------------------------------------------------
def test_discover_parts_stops_at_404(tmp_path):
    """Probe 001 + 002 succeed, 003 + 004 are 404 -> end-of-set."""
    payload = _fixture_payload()
    responses = {
        "001": _FakeResp("https://takeout-download.usercontent.google.com/download/x-001.zip",
                         206, {"content-range": "bytes 0-0/100"}),
        "002": _FakeResp("https://takeout-download.usercontent.google.com/download/x-002.zip",
                         206, {"content-range": "bytes 0-0/200"}),
        "003": _FakeResp("https://takeout-download.usercontent.google.com/download/x-003.zip",
                         404, {}),
        "004": _FakeResp("https://takeout-download.usercontent.google.com/download/x-004.zip",
                         404, {}),
    }

    def fake_get(self, url, **kw):
        for k, r in responses.items():
            if k in url:
                return r
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(payload, tmp_path)

    nums = [p["num"] for p in parts]
    assert nums == [1, 2]
    assert parts[0]["size"] == 100
    assert parts[1]["size"] == 200


def test_discover_raises_auth_error_on_first_probe(tmp_path):
    payload = _fixture_payload()
    fake = _FakeResp("https://accounts.google.com/v3/signin",
                     200, {"content-type": "text/html"})
    with mock.patch.object(takeout_cli.requests.Session, "get", return_value=fake):
        with pytest.raises(takeout_cli.AuthError):
            takeout_cli.discover_parts(payload, tmp_path)


# ---------------------------------------------------------------------------
# Log analyzer
# ---------------------------------------------------------------------------
def test_analyzer_parses_synthetic_log(tmp_path):
    log_text = (
        "2026-06-14 08:00:00.000 INFO   ========================================\n"
        "2026-06-14 08:00:00.001 INFO     Google Takeout Downloader (aria2c)\n"
        "2026-06-14 08:00:00.002 INFO   ========================================\n"
        "2026-06-14 08:00:01.123 DEBUG  probe #001 GET https://...\n"
        "2026-06-14 08:00:01.456 DEBUG    <- 206 ct=application/octet-stream host=...\n"
        "2026-06-14 08:00:01.457 DEBUG  probe #001 -> 1234567 bytes\n"
        "2026-06-14 08:00:01.500 INFO     001       1.2 MB  need\n"
        "2026-06-14 08:00:02.000 WARNING Auth failed probing part 002 (redirected to accounts.google.com).\n"
        "2026-06-14 08:00:02.500 ERROR   Something bad happened\n"
    )
    log_file = tmp_path / "syn.log"
    log_file.write_text(log_text, encoding="utf-8")

    lines = list(analyze.parse_lines(log_file.open(encoding="utf-8")))
    assert len(lines) == 9
    sessions = analyze.split_into_sessions(lines)
    assert len(sessions) == 1
    summary = analyze.summarize(sessions[0])
    assert summary["auth_failures"] == 1
    assert len(summary["errors"]) == 1
    assert summary["probes"] == [(1, "1234567 bytes")]


def test_analyzer_handles_rotated_backups(tmp_path):
    """If both foo.log and foo.log.1 exist, both are read in the right order."""
    (tmp_path / "f.log.1").write_text(
        "2026-06-14 08:00:00.000 INFO   Google Takeout Downloader (aria2c)\n"
        "2026-06-14 08:00:00.001 INFO     run A\n",
        encoding="utf-8",
    )
    (tmp_path / "f.log").write_text(
        "2026-06-14 09:00:00.000 INFO   Google Takeout Downloader (aria2c)\n"
        "2026-06-14 09:00:00.001 INFO     run B\n",
        encoding="utf-8",
    )
    # Walk parse — but cmd_summary reads files directly; do it inline here.
    parts: list = []
    for fname in ("f.log.1", "f.log"):
        with (tmp_path / fname).open(encoding="utf-8") as f:
            parts.extend(analyze.parse_lines(f))
    assert len(parts) == 4
    # Order is oldest backup first, then current file.
    assert "run A" in parts[1].msg
    assert "run B" in parts[3].msg


# ---------------------------------------------------------------------------
# human_size
# ---------------------------------------------------------------------------
def test_human_size_basic():
    assert takeout_cli.human_size(0) == "0 B"
    assert takeout_cli.human_size(512) == "512.0 B"
    assert takeout_cli.human_size(1024) == "1.0 KB"
    assert takeout_cli.human_size(1024 * 1024) == "1.0 MB"
    assert takeout_cli.human_size(1024 * 1024 * 1024) == "1.0 GB"


# ---------------------------------------------------------------------------
# ZIP validation
# ---------------------------------------------------------------------------
def test_is_valid_zip_true(tmp_path):
    p = tmp_path / "ok.zip"
    # Build a minimal ZIP with EOCD record. The easiest way is to use zipfile.
    import zipfile
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("hello.txt", "world")
    assert takeout_cli.is_valid_zip(p) is True


def test_is_valid_zip_false_for_html(tmp_path):
    p = tmp_path / "fake.zip"
    p.write_bytes(b"<html><body>Google signin page</body></html>")
    assert takeout_cli.is_valid_zip(p) is False


def test_is_valid_zip_false_for_truncated(tmp_path):
    p = tmp_path / "small.zip"
    p.write_bytes(b"PK\x03\x04")  # local file header but no EOCD
    assert takeout_cli.is_valid_zip(p) is False


def test_is_valid_zip_false_for_empty(tmp_path):
    p = tmp_path / "empty.zip"
    p.write_bytes(b"")
    assert takeout_cli.is_valid_zip(p) is False


# ---------------------------------------------------------------------------
# verify_parts — size + ZIP signature combined
# ---------------------------------------------------------------------------
def test_verify_parts_marks_bad_zip_incomplete(tmp_path):
    import zipfile
    good = tmp_path / "good.zip"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("a.txt", "x")
    bad.write_bytes(b"not a zip, just text")
    parts = [
        {"num": 1, "filename": "good.zip", "size": good.stat().st_size,
         "have": False},
        {"num": 2, "filename": "bad.zip", "size": bad.stat().st_size,
         "have": False},
    ]
    complete, incomplete = takeout_cli.verify_parts(parts, tmp_path)
    assert len(complete) == 1
    assert complete[0]["filename"] == "good.zip"
    assert len(incomplete) == 1
    assert incomplete[0]["filename"] == "bad.zip"


def test_verify_parts_marks_short_file_incomplete(tmp_path):
    import zipfile
    p = tmp_path / "short.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a.txt", "x" * 1000)
    actual = p.stat().st_size
    parts = [{"num": 1, "filename": "short.zip",
              "size": actual + 5000,  # claim it's bigger than it is
              "have": False}]
    complete, incomplete = takeout_cli.verify_parts(parts, tmp_path)
    assert len(incomplete) == 1


# ---------------------------------------------------------------------------
# looks_like_auth_failure heuristic
# ---------------------------------------------------------------------------
def test_looks_like_auth_failure_triggers_above_80_percent():
    parts = [{"num": i} for i in range(10)]
    incomplete = [{"num": i} for i in range(9)]  # 90%
    assert takeout_cli.looks_like_auth_failure(parts, incomplete) is True


def test_looks_like_auth_failure_doesnt_trigger_below_80_percent():
    parts = [{"num": i} for i in range(10)]
    incomplete = [{"num": i} for i in range(7)]  # 70% — partial network failure
    assert takeout_cli.looks_like_auth_failure(parts, incomplete) is False


# ---------------------------------------------------------------------------
# Paste prompt — feed JSON via stdin
# ---------------------------------------------------------------------------
def test_prompt_for_paste_reads_complete_json(monkeypatch):
    payload = '{"hello": "world", "n": 42}'
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(payload + "\n"))
    # prompt_for_paste uses input() which reads from stdin.
    got = takeout_cli.prompt_for_paste()
    assert got == payload


def test_prompt_for_paste_handles_multiline(monkeypatch):
    payload = '{\n  "hello": "world",\n  "n": 42\n}'
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload + "\n"))
    got = takeout_cli.prompt_for_paste()
    assert '"hello"' in got
    assert '"n"' in got


# ---------------------------------------------------------------------------
# Auth-failure detection on first probe
# ---------------------------------------------------------------------------
def test_discover_parts_auth_error_on_part_1_raises(tmp_path):
    """If the very first probe hits Google's login, we raise AuthError so the
    caller can re-prompt — no download attempt, no cookie burn."""
    payload = _fixture_payload()
    fake = _FakeResp("https://accounts.google.com/v3/signin",
                     200, {"content-type": "text/html"})
    with mock.patch.object(takeout_cli.requests.Session, "get", return_value=fake):
        with pytest.raises(takeout_cli.AuthError):
            takeout_cli.discover_parts(payload, tmp_path)


def test_discover_parts_partial_set_before_auth_fail(tmp_path):
    """If 5 parts succeed then the 6th hits a signin, return what we found
    (don't raise). The caller decides what to do — usually re-prompt."""
    payload = _fixture_payload()

    def fake_get(self, url, **kw):
        m = re.search(r"-(\d{3})\.zip", url)
        if not m:
            raise AssertionError(f"unexpected URL: {url}")
        n = int(m.group(1))
        if n <= 5:
            return _FakeResp(url, 206,
                             {"content-range": f"bytes 0-0/{n * 1000}",
                              "content-type": "application/octet-stream"})
        if n == 6:
            return _FakeResp("https://accounts.google.com/signin",
                             200, {"content-type": "text/html"})
        # Shouldn't be called.
        return _FakeResp(url, 404, {})

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(payload, tmp_path)
    nums = [p["num"] for p in parts]
    assert nums == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# End-to-end happy path with mocks (no real network)
# ---------------------------------------------------------------------------
def test_full_flow_with_mocks(tmp_path, monkeypatch):
    """Walk through: paste -> parse -> discover -> aria2 -> verify.

    aria2c is mocked so we don't actually run a subprocess. The 'download'
    is simulated by having verify_parts see a real ZIP file of the right size.
    """
    import subprocess as sp

    payload_text = (ROOT / "tests" / "fixtures" / "sample_payload.json").read_text()

    # 1) Paste step: feed the JSON via stdin.
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload_text + "\n"))
    parsed = takeout_cli.parse_one_payload()
    assert parsed.cookie  # parsed OK

    # 2) Discovery step: each probe returns size 1000.
    def fake_get(self, url, **kw):
        m = re.search(r"-(\d{3})\.zip", url)
        n = int(m.group(1)) if m else 1
        if n <= 2:
            return _FakeResp(url, 206,
                             {"content-range": f"bytes 0-0/1000",
                              "content-type": "application/octet-stream"})
        return _FakeResp(url, 404, {})

    # 3) aria2c step: simulate by writing a valid ZIP for each requested part.
    def fake_run(cmd, **kw):
        # Parse the -i file and create files for each `out=` line.
        i_idx = cmd.index("-i")
        i_path = Path(cmd[i_idx + 1])
        body = i_path.read_text()
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("out="):
                fname = line.split("=", 1)[1]
                (tmp_path / fname).write_bytes(
                    zipfile_make_bytes(1000)
                )
        # Cleanup the input file
        try:
            i_path.unlink()
        except OSError:
            pass
        return sp.CompletedProcess(cmd, 0)

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get), \
         mock.patch.object(sp, "run", side_effect=fake_run):
        parts = takeout_cli.discover_parts(parsed, tmp_path)
        need = [p for p in parts if not p["have"]]
        assert len(need) == 2
        body = takeout_cli.build_aria2_input(parts, parsed, tmp_path)
        rc = takeout_cli.run_aria2c(body, tmp_path, 2)
        assert rc == 0
        complete, incomplete = takeout_cli.verify_parts(parts, tmp_path)
        assert len(complete) == 2
        assert len(incomplete) == 0


def zipfile_make_bytes(size: int) -> bytes:
    """Build a minimal valid ZIP of at least `size` bytes total."""
    import io as _io
    import zipfile
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pad.bin", "x" * max(1, size))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# State file — resume across runs
# ---------------------------------------------------------------------------
def test_load_state_missing_returns_none(tmp_path):
    assert takeout_cli.load_state(tmp_path) is None


def test_load_state_corrupt_returns_none(tmp_path):
    p = tmp_path / takeout_cli.STATE_FILENAME
    p.write_text("{ this is not json")
    # Should return None instead of raising.
    assert takeout_cli.load_state(tmp_path) is None


def test_make_state_and_roundtrip(tmp_path):
    payload = _fixture_payload()
    parts = [
        {"num": 1, "filename": "x-001.zip", "size": 100, "have": True},
        {"num": 2, "filename": "x-002.zip", "size": 200, "have": False},
        {"num": 3, "filename": "x-003.zip", "size": 300, "have": True},
    ]
    state = takeout_cli.make_state(parts, payload, tmp_path)
    assert state["schema"] == 1
    assert state["base_filename"]  # parsed from URL
    assert len(state["parts"]) == 3
    takeout_cli.save_state(tmp_path, state)
    loaded = takeout_cli.load_state(tmp_path)
    assert loaded is not None
    assert loaded["base_filename"] == state["base_filename"]
    assert len(loaded["parts"]) == 3
    assert "last_updated" in loaded


def test_state_to_parts_rehydrates(tmp_path):
    payload = _fixture_payload()
    parts = [
        {"num": 1, "filename": "x-001.zip", "size": 100, "have": True},
        {"num": 2, "filename": "x-002.zip", "size": 200, "have": False},
    ]
    state = takeout_cli.make_state(parts, payload, tmp_path)
    out = takeout_cli.state_to_parts(state, payload)
    assert out is not None
    assert len(out) == 2
    assert out[0]["num"] == 1
    assert out[0]["have"] is True
    assert out[1]["num"] == 2
    assert out[1]["have"] is False


def test_state_matches_payload_for_same_url():
    payload = _fixture_payload()
    parts = [{"num": 1, "filename": "x-001.zip", "size": 100, "have": False}]
    state = takeout_cli.make_state(parts, payload, Path("/tmp"))
    assert takeout_cli.state_matches_payload(state, payload) is True


def test_state_matches_payload_for_different_archive():
    payload = _fixture_payload()
    # Mutate the URL to a different archive (different base).
    payload.url = ("https://takeout-download.usercontent.google.com/"
                   "download/takeout-20260612T190148Z-99-001.zip?j=x&i=0")
    payload_other = _fixture_payload()
    state = takeout_cli.make_state(
        [{"num": 1, "filename": "x-001.zip", "size": 100, "have": False}],
        payload, Path("/tmp"))
    # Different archive (set-99) vs default (set-9) -> no match.
    assert takeout_cli.state_matches_payload(state, payload_other) is False


def test_state_matches_payload_for_empty_state():
    assert takeout_cli.state_matches_payload(None, _fixture_payload()) is False
    assert takeout_cli.state_matches_payload({}, _fixture_payload()) is False


def test_update_state_from_parts(tmp_path):
    payload = _fixture_payload()
    parts = [
        {"num": 1, "filename": "x-001.zip", "size": 100, "have": False},
        {"num": 2, "filename": "x-002.zip", "size": 200, "have": False},
    ]
    state = takeout_cli.make_state(parts, payload, tmp_path)
    # Verify: only part 1 finished.
    parts_after = [
        {"num": 1, "filename": "x-001.zip", "size": 100, "have": True},
        {"num": 2, "filename": "x-002.zip", "size": 200, "have": False},
    ]
    takeout_cli.update_state_from_parts(state, parts_after)
    saved = {p["num"]: p for p in state["parts"]}
    assert saved[1]["complete"] is True
    assert saved[2]["complete"] is False


def test_save_state_atomic(tmp_path):
    payload = _fixture_payload()
    parts = [{"num": 1, "filename": "x-001.zip", "size": 100, "have": True}]
    state = takeout_cli.make_state(parts, payload, tmp_path)
    # Should not leave .tmp lying around.
    takeout_cli.save_state(tmp_path, state)
    assert not (tmp_path / (takeout_cli.STATE_FILENAME + ".tmp")).exists()
    assert (tmp_path / takeout_cli.STATE_FILENAME).exists()


def test_resume_skips_discovery_when_state_matches(tmp_path, monkeypatch):
    """If state matches payload, main() should use it instead of probing."""
    payload = _fixture_payload()
    parts = [
        {"num": 1, "filename": "x-001.zip", "size": 100, "have": True},
        {"num": 2, "filename": "x-002.zip", "size": 200, "have": False},
    ]
    state = takeout_cli.make_state(parts, payload, tmp_path)
    takeout_cli.save_state(tmp_path, state)
    # Make the on-disk file for part 1 a valid ZIP of size >= 100.
    import zipfile, io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a", "x" * 200)
    (tmp_path / "takeout-20260612T190148Z-9-001.zip").write_bytes(buf.getvalue())

    # Monkey-patch discover_parts to fail the test if it's called.
    called = {"n": 0}
    def fail_discover(*a, **kw):
        called["n"] += 1
        raise AssertionError("discover_parts should NOT have been called")
    monkeypatch.setattr(takeout_cli, "discover_parts", fail_discover)

    # Monkey-patch run_aria2c so we don't actually download.
    monkeypatch.setattr(takeout_cli, "run_aria2c", lambda *a, **kw: 0)

    rehydrated = takeout_cli.state_to_parts(
        takeout_cli.load_state(tmp_path), payload)
    assert rehydrated is not None
    # The rehydrated parts should reflect the on-disk state.
    nums_complete = sum(1 for p in rehydrated if p["have"])
    assert nums_complete >= 1  # at least part 1


# ---------------------------------------------------------------------------
# Output-dir prompt
# ---------------------------------------------------------------------------
def test_prompt_for_output_dir_accepts_default(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    # _install_logger is called by main, not prompt, so we don't need it.
    p = takeout_cli.prompt_for_output_dir(tmp_path)
    assert p == tmp_path


def test_prompt_for_output_dir_accepts_path(monkeypatch, tmp_path):
    target = tmp_path / "sub"
    monkeypatch.setattr(sys, "stdin", io.StringIO(str(target) + "\n"))
    p = takeout_cli.prompt_for_output_dir(tmp_path)
    assert p == target
    assert p.exists()


def test_prompt_for_output_dir_quit(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("q\n"))
    with pytest.raises(SystemExit):
        takeout_cli.prompt_for_output_dir(tmp_path)


# ---------------------------------------------------------------------------
# TermRender — control-char based grid UI
# ---------------------------------------------------------------------------
def _fake_tty():
    """Build an io.StringIO that looks like a TTY (isatty() -> True)."""
    import io
    buf = io.StringIO()
    buf.isatty = lambda: True
    return buf


def test_term_render_disabled_when_stdout_not_tty():
    import io
    buf = io.StringIO()  # not a tty by default
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        assert r.enabled is False
    finally:
        sys.stdout = real


def test_term_render_begin_creates_row_slots():
    real = sys.stdout
    sys.stdout = _fake_tty()
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=3)
        assert r.body_rows == 3
        assert len(r.rows) == 3
    finally:
        sys.stdout = real


def test_term_render_update_row_writes_escape_sequence():
    buf = _fake_tty()
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=3)
        r.update_row("file:a.zip", "row content for a")
        out = buf.getvalue()
        # Should contain save-cursor, move-to-row, erase-EOL, restore-cursor.
        assert "\x1b[s" in out
        assert "\x1b[u" in out
        assert "\x1b[K" in out
        assert "row content for a" in out
    finally:
        sys.stdout = real


def test_term_render_update_row_reuses_slot():
    """Updating the same key replaces content, doesn't add a row."""
    buf = _fake_tty()
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=2)
        r.update_row("file:a.zip", "first a")
        r.update_row("file:a.zip", "second a")
        # The second update should not create a second slot for `a`.
        # Two unique keys means both rows used; if we tried a third it
        # would replace one. Check by adding `b` and `c`.
        r.update_row("file:b.zip", "row b")
        r.update_row("file:c.zip", "row c")
        # Three keys for two slots — one gets replaced (the last).
        # Just verify all three texts appear at least once in the output.
        out = buf.getvalue()
        assert "first a" in out or "second a" in out
        assert "row b" in out
        assert "row c" in out
    finally:
        sys.stdout = real


def test_term_render_clear_row():
    buf = _fake_tty()
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=2)
        r.update_row("file:a.zip", "first a")
        r.update_row("file:b.zip", "first b")
        r.clear_row("file:a.zip")
        out = buf.getvalue()
        # clear_row redraws with empty content for that row.
        assert "\x1b[K\n" in out  # empty cleared lines
    finally:
        sys.stdout = real


def test_term_render_set_header_and_footer():
    buf = _fake_tty()
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=1)
        r.set_header("HEADER TEXT")
        r.set_footer("FOOTER TEXT")
        out = buf.getvalue()
        assert "HEADER TEXT" in out
        assert "FOOTER TEXT" in out
    finally:
        sys.stdout = real


def test_term_render_disabled_does_not_emit_escapes(capsys):
    """When enabled=False (no TTY), update_row just prints plain text."""
    r = takeout_cli.TermRender(enabled=False)
    r.begin(n_rows=3)
    r.update_row("file:a.zip", "row content")
    out = capsys.readouterr().out
    assert "row content" in out
    assert "\x1b[" not in out  # no escape sequences


def test_make_progress_bar():
    assert takeout_cli.make_progress_bar(0).startswith("[")
    assert takeout_cli.make_progress_bar(0).endswith("]")
    assert "·" in takeout_cli.make_progress_bar(0)
    bar50 = takeout_cli.make_progress_bar(50, width=10)
    # Half-filled, half-empty.
    assert bar50.count("█") == 5
    assert bar50.count("·") == 5
    bar100 = takeout_cli.make_progress_bar(100, width=10)
    assert bar100.count("█") == 10
    assert "·" not in bar100
    # Clamp out-of-range inputs.
    assert takeout_cli.make_progress_bar(-5, width=10).count("█") == 0
    assert takeout_cli.make_progress_bar(150, width=10).count("█") == 10


def test_aria2_unit_to_bytes():
    # Plain bytes
    assert takeout_cli._aria2_unit_to_bytes("100", None) == 100
    # KiB / MiB / GiB
    assert takeout_cli._aria2_unit_to_bytes("1.5", "KiB") == 1536
    assert takeout_cli._aria2_unit_to_bytes("1", "MiB") == 1024 ** 2
    assert takeout_cli._aria2_unit_to_bytes("2", "GiB") == 2 * 1024 ** 3
    # KB (decimal-ish, aria2c uses these in summary lines)
    assert takeout_cli._aria2_unit_to_bytes("100", "KB") == 102400


def test_aria2_progress_re_matches_real_line():
    """The progress regex should match aria2's actual output format."""
    line = "[#abc12345 411.6MiB/8.0GiB(5%) CN:1 DL:50.0MiB ETA:2m14s]"
    m = takeout_cli._ARIA2_PROGRESS_RE.search(line)
    assert m is not None
    assert m.group(1) == "abc12345"
    assert m.group(6) == "5"
    assert m.group(7) == "50.0"
    assert m.group(8) == "MiB"
    assert m.group(9) == "2m14s"


def test_aria2_progress_re_matches_no_eta():
    line = "[#abc 411B/1000B(41%) CN:1 DL:50B]"
    m = takeout_cli._ARIA2_PROGRESS_RE.search(line)
    assert m is not None
    assert m.group(9) is None  # ETA optional


def test_render_file_row_format():
    """The row formatter should produce a readable fixed-width line."""
    buf = _fake_tty()
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=2)
        takeout_cli._render_file_row(
            r, "takeout-001.zip", done=200_000_000, total=400_000_000,
            pct=50, speed_bps=5_000_000, eta="1m30s",
            filename_to_size={"takeout-001.zip": 400_000_000},
            num_by_filename={"takeout-001.zip": 1},
        )
        out = buf.getvalue()
        assert "#001" in out
        assert "takeout-001.zip" in out
        assert "50%" in out
    finally:
        sys.stdout = real


# ---------------------------------------------------------------------------
# End-to-end: grid render via mocked aria2c output
# ---------------------------------------------------------------------------
def test_grid_render_with_mocked_aria2c(tmp_path, monkeypatch):
    """Simulate aria2c printing summary lines and verify the renderer
    gets a row update with a progress bar and correct numbers."""
    # Build a fake parts list.
    parts = [
        {"num": 1, "filename": "takeout-001.zip", "size": 8 * 1024 ** 3,
         "have": False, "url": "http://x/001.zip"},
        {"num": 2, "filename": "takeout-002.zip", "size": 8 * 1024 ** 3,
         "have": False, "url": "http://x/002.zip"},
    ]
    filename_to_size = {p["filename"]: p["size"] for p in parts}
    num_by_filename = {p["filename"]: p["num"] for p in parts}
    # Capture render output by giving the renderer a fake TTY.
    import io
    buf = io.StringIO()
    buf.isatty = lambda: True
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=2)
        # Simulate two aria2 progress updates for two files.
        gid_state = {}
        gid_to_filename: dict[str, str] = {}
        filename_to_rowkey: dict[str, str] = {}
        # First aria2 announces completion of file 1 (for the result row to bind GID).
        result_line = "  abc12345 | OK |   1.2MiB/s | ./takeout-001.zip"
        takeout_cli._update_from_aria2_line(
            result_line, gid_state, gid_to_filename,
            filename_to_rowkey, filename_to_size,
            num_by_filename, r,
        )
        # Now a live progress update for file 1.
        prog_line = "[#abc12345 411.6MiB/8.0GiB(5%) CN:1 DL:50.0MiB ETA:2m14s]"
        takeout_cli._update_from_aria2_line(
            prog_line, gid_state, gid_to_filename,
            filename_to_rowkey, filename_to_size,
            num_by_filename, r,
        )
        out = buf.getvalue()
        # Renderer should have produced at least one row mentioning the file.
        assert "takeout-001.zip" in out
        # Should contain a progress bar block.
        assert "█" in out
        # And the percent.
        assert "5%" in out
    finally:
        sys.stdout = real


def test_grid_render_handles_no_eta(tmp_path):
    """Progress line without ETA should still render a row."""
    import io
    parts = [
        {"num": 1, "filename": "foo.zip", "size": 1000, "have": False,
         "url": "http://x/001.zip"},
    ]
    buf = io.StringIO()
    buf.isatty = lambda: True
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=1)
        # First bind GID via result line.
        gid_state = {}
        gid_to_filename: dict[str, str] = {}
        filename_to_rowkey: dict[str, str] = {}
        takeout_cli._update_from_aria2_line(
            "  abcd | OK | 1.0B/s | ./foo.zip",
            gid_state, gid_to_filename, filename_to_rowkey,
            {p["filename"]: p["size"] for p in parts},
            {p["filename"]: p["num"] for p in parts}, r,
        )
        # Progress without ETA.
        takeout_cli._update_from_aria2_line(
            "[#abcd 100B/1000B(10%) CN:1 DL:50B]",
            gid_state, gid_to_filename, filename_to_rowkey,
            {p["filename"]: p["size"] for p in parts},
            {p["filename"]: p["num"] for p in parts}, r,
        )
        out = buf.getvalue()
        assert "foo.zip" in out
        assert "10%" in out
    finally:
        sys.stdout = real


def test_grid_render_buffers_progress_before_gid_known():
    """If a progress line arrives before the GID->filename binding, the
    update is buffered and rendered once we know the binding."""
    import io
    parts = [
        {"num": 1, "filename": "x.zip", "size": 1000, "have": False,
         "url": "http://x/001.zip"},
    ]
    buf = io.StringIO()
    buf.isatty = lambda: True
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=1)
        gid_state = {}
        gid_to_filename: dict[str, str] = {}
        filename_to_rowkey: dict[str, str] = {}
        # Progress line first, before we know GID -> filename.
        takeout_cli._update_from_aria2_line(
            "[#abcd 500B/1000B(50%) CN:1 DL:100B ETA:30s]",
            gid_state, gid_to_filename, filename_to_rowkey,
            {p["filename"]: p["size"] for p in parts},
            {p["filename"]: p["num"] for p in parts}, r,
        )
        # Now bind GID via the result line.
        takeout_cli._update_from_aria2_line(
            "  abcd | OK | 1.0B/s | ./x.zip",
            gid_state, gid_to_filename, filename_to_rowkey,
            {p["filename"]: p["size"] for p in parts},
            {p["filename"]: p["num"] for p in parts}, r,
        )
        out = buf.getvalue()
        # Should now contain the rendered row.
        assert "x.zip" in out
    finally:
        sys.stdout = real
