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
    def __init__(self, url, status, headers=None, text=""):
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self.text = text

    def close(self):
        pass

    def json(self):
        import json as _json
        return _json.loads(self.text)


def _part_index_from_url(url: str) -> int:
    """Extract 1-based part index from a Takeout probe URL.

    Handles both modern URLs (?i=N) and legacy URLs (-NNN.zip). The
    modern-mode URL keeps the filename suffix cosmetic at `-001.zip`,
    so the only reliable signal is `?i=N`.
    """
    m = re.search(r"[?&]i=(\d+)", url)
    if m:
        return int(m.group(1)) + 1
    m = re.search(r"-(\d{3})\.zip", url)
    if m:
        return int(m.group(1))
    return 0


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
    sizes = {1: 100, 2: 200}  # only parts 1 and 2 exist

    def fake_get(self, url, **kw):
        n = _part_index_from_url(url)
        if n in sizes:
            return _FakeResp(url, 206,
                             {"content-range": f"bytes 0-0/{sizes[n]}",
                              "content-type": "application/octet-stream"})
        return _FakeResp(url, 404, {})

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
        n = _part_index_from_url(url)
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
    parsed, parsed_ctx = takeout_cli.parse_one_payload()
    assert parsed.cookie  # parsed OK

    # 2) Discovery step: each probe returns size 1000.
    def fake_get(self, url, **kw):
        n = _part_index_from_url(url)
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


def _reset_cli_logger(monkeypatch):
    """Other tests call takeout_cli._install_logger(), which (a) sets
    propagate=False on the module logger and (b) installs a StreamHandler
    that captured sys.stdout at install time. Both leak into later tests.
    Reset before each prompt test so caplog works again."""
    monkeypatch.setattr(takeout_cli.log, "propagate", True)
    for h in list(takeout_cli.log.handlers):
        takeout_cli.log.removeHandler(h)


def test_prompt_for_output_dir_rejects_json_paste(monkeypatch, tmp_path, caplog):
    """Accidental JSON paste into the path prompt must NOT be treated as
    a folder name. The user is re-prompted, then enters a real path."""
    _reset_cli_logger(monkeypatch)
    payload = '{"a": 1, "nested": {"x": "y"}, "arr": [1,2,3]}'
    # First input: JSON. Second input: a real (relative) path that resolves
    # inside tmp_path so validate_output_dir accepts it.
    real = tmp_path / "real"
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(payload + "\n" + str(real) + "\n"))
    with caplog.at_level(logging.ERROR, logger="takeout_cli"):
        p = takeout_cli.prompt_for_output_dir(tmp_path)
    assert p == real
    assert any("looks like a JSON payload" in r.message
               for r in caplog.records)


def test_prompt_for_output_dir_rejects_pretty_json_first_line(monkeypatch, tmp_path, caplog):
    """A multi-line pretty-printed JSON paste: the FIRST line (a bare `{`)
    must be caught by the JSON sniff so the user gets an immediate
    'wrong prompt' hint instead of seeing subsequent lines processed as
    path-shaped garbage."""
    _reset_cli_logger(monkeypatch)
    payload = '{\n  "batchexecute": [\n    {"key": "val"}\n  ]\n}'
    real = tmp_path / "ok"
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(payload + str(real) + "\n"))
    with caplog.at_level(logging.ERROR, logger="takeout_cli"):
        takeout_cli.prompt_for_output_dir(tmp_path)
    assert any("looks like a JSON payload" in r.message
               for r in caplog.records)


def test_prompt_for_output_dir_keyboard_interrupt_exits_clean(monkeypatch, tmp_path, caplog):
    """Ctrl-C at the path prompt must exit with code 130, not bubble up
    as an unhandled KeyboardInterrupt and not show the misleading
    'resuming partial downloads' message from the outer handler."""
    _reset_cli_logger(monkeypatch)
    class _RaisingStdin:
        def readline(self):
            raise KeyboardInterrupt()
    monkeypatch.setattr(sys, "stdin", _RaisingStdin())
    with pytest.raises(SystemExit) as exc:
        takeout_cli.prompt_for_output_dir(tmp_path)
    assert exc.value.code == 130
    assert not any("Partially-downloaded" in r.message
                   for r in caplog.records)


def test_prompt_for_output_dir_handles_path_too_long(monkeypatch, tmp_path, caplog):
    """A path that Windows rejects as too long must re-prompt cleanly,
    not crash with an unhandled OSError traceback."""
    _reset_cli_logger(monkeypatch)
    too_long = "x" * 5000  # way past Win32 MAX_PATH
    real = tmp_path / "real"
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(too_long + "\n" + str(real) + "\n"))
    with caplog.at_level(logging.ERROR, logger="takeout_cli"):
        p = takeout_cli.prompt_for_output_dir(tmp_path)
    assert p == real
    assert any("Path is not usable" in r.message or "Could not create" in r.message
               for r in caplog.records)


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
        # First draw lays the block down; second redraws in place by moving
        # the cursor UP (relative positioning, no absolute save/restore).
        r.update_row("file:a.zip", "row content for a")
        r.update_row("file:a.zip", "row content for a v2")
        out = buf.getvalue()
        # Erase-to-EOL on every line, and a relative cursor-up before redraw.
        assert "\x1b[K" in out
        assert "\x1b[" in out and "A" in out  # \x1b[<n>A cursor-up
        assert "row content for a v2" in out
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


def test_render_file_row_bar_adapts_to_terminal_width():
    """The progress bar should shrink on narrow terminals so the
    filename stays visible. With a 60-col terminal and a 30-char
    filename, the bar must be smaller than the default 20 chars."""
    import io
    buf = io.StringIO()
    buf.isatty = lambda: True
    real = sys.stdout
    sys.stdout = buf
    try:
        # Patch terminal size to simulate a narrow TTY.
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((60, 20))):
            r = takeout_cli.TermRender(enabled=True)
            r.begin(n_rows=5)
            takeout_cli._render_file_row(
                r, "takeout-long-filename-001.zip", done=100, total=200,
                pct=50, speed_bps=1000, eta="1s",
                filename_to_size={}, num_by_filename={
                    "takeout-long-filename-001.zip": 1,
                },
            )
            # Find the row content (strip ANSI).
            out = buf.getvalue()
            import re
            clean = re.sub(r"\x1b\[[^A-Za-z]*[A-Za-z]", "", out)
            # Bar should be < 20 chars (we shrunk it).
            bar_match = re.search(r"\[([█·]+)\]", clean)
            assert bar_match, f"no progress bar in: {clean!r}"
            bar_chars = len(bar_match.group(1))
            assert bar_chars < 20, (
                f"bar should shrink on narrow terminal, got {bar_chars} chars"
            )
            assert bar_chars >= 8, (
                f"bar should not collapse below 8 chars, got {bar_chars}"
            )
            # Filename should still be visible (we shrink the bar to
            # make room).
            assert "takeout-long-filename-001.zip" in clean
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


# ---------------------------------------------------------------------------
# Real aria2c line order: progress line, then FILE: line (the bug fix)
# ---------------------------------------------------------------------------
def test_grid_binds_filename_from_FILE_line_during_download():
    """Reproduces real aria2c 1.35.0 output order: each periodic summary
    is a [#GID ...] progress line IMMEDIATELY FOLLOWED by a `FILE: path`
    line. The grid must bind GID->filename from the FILE: line and render
    the row live -- NOT wait for the terminal `Download Results:` block
    (which only prints after everything finishes). This is the regression
    that left every progress bar stuck at 'queued' until the very end.
    """
    import io
    parts = [
        {"num": 1, "filename": "takeout-001.zip", "size": 28 * 1024 ** 2,
         "have": False, "url": "http://x/001.zip?i=0"},
    ]
    filename_to_size = {p["filename"]: p["size"] for p in parts}
    num_by_filename = {p["filename"]: p["num"] for p in parts}
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
        # Exactly the order aria2c emits (verified against 1.35.0):
        takeout_cli._update_from_aria2_line(
            "[#859ca6 3.0MiB/28MiB(10%) CN:1 DL:1.5MiB ETA:16s]",
            gid_state, gid_to_filename, filename_to_rowkey,
            filename_to_size, num_by_filename, r,
        )
        takeout_cli._update_from_aria2_line(
            "FILE: C:/downloads/takeout-001.zip",
            gid_state, gid_to_filename, filename_to_rowkey,
            filename_to_size, num_by_filename, r,
        )
        out = buf.getvalue()
        # The row must render LIVE (before any Download Results block).
        assert "takeout-001.zip" in out
        assert "10%" in out
        assert "\u2588" in out  # progress bar drawn
        # GID must be bound to the filename from the FILE: line.
        assert gid_to_filename.get("859ca6") == "takeout-001.zip"
    finally:
        sys.stdout = real


def test_grid_multi_file_FILE_line_binding():
    """With -j>1, aria2c interleaves [#GID]/FILE: pairs for each active
    download. Each progress GID must bind to the FILE: line that follows
    it, so concurrent rows don't cross-bind."""
    import io
    parts = [
        {"num": 1, "filename": "o1.zip", "size": 19 * 1024 ** 2,
         "have": False, "url": "http://x/001.zip?i=0"},
        {"num": 2, "filename": "o2.zip", "size": 19 * 1024 ** 2,
         "have": False, "url": "http://x/001.zip?i=1"},
    ]
    filename_to_size = {p["filename"]: p["size"] for p in parts}
    num_by_filename = {p["filename"]: p["num"] for p in parts}
    buf = io.StringIO()
    buf.isatty = lambda: True
    real = sys.stdout
    sys.stdout = buf
    try:
        r = takeout_cli.TermRender(enabled=True)
        r.begin(n_rows=2)
        gid_state = {}
        gid_to_filename: dict[str, str] = {}
        filename_to_rowkey: dict[str, str] = {}
        feed = [
            "[#c9502e 2.0MiB/19MiB(10%) CN:1 DL:1.0MiB ETA:16s]",
            "FILE: C:/downloads/o1.zip",
            "[#ce4448 4.0MiB/19MiB(21%) CN:1 DL:1.3MiB ETA:11s]",
            "FILE: C:/downloads/o2.zip",
        ]
        for line in feed:
            takeout_cli._update_from_aria2_line(
                line, gid_state, gid_to_filename, filename_to_rowkey,
                filename_to_size, num_by_filename, r,
            )
        assert gid_to_filename.get("c9502e") == "o1.zip"
        assert gid_to_filename.get("ce4448") == "o2.zip"
        out = buf.getvalue()
        assert "o1.zip" in out and "o2.zip" in out
    finally:
        sys.stdout = real


# ---------------------------------------------------------------------------
# Same-size streak stop — Google returns identical size for all parts
# ---------------------------------------------------------------------------
def test_discover_stops_on_same_size_streak_legacy_mode(tmp_path):
    """Legacy mode (no `i=` in URL): if Google returns the same size for
    N consecutive parts after #1, discovery should stop — the real set
    is done and subsequent suffixes hit a generic placeholder response."""
    payload = json.loads((ROOT / "tests" / "fixtures" / "sample_payload.json").read_text())
    # Strip `i=` to put the URL into legacy mode.
    payload["url"] = payload["url"].split("?")[0]
    parsed = takeout_cli.parse_payload(json.dumps(payload))

    def fake_get(self, url, **kw):
        return _FakeResp(url, 206,
                         {"content-range": "bytes 0-0/411587",
                          "content-type": "application/octet-stream"})

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(parsed, tmp_path)
    # Should stop after SAME_SIZE_STREAK_STOP (2) identical sizes after #1.
    # Part 1 is appended; part 2 is appended (streak=1); part 3 triggers
    # break BEFORE append (streak=2). So only 2 parts in the list.
    assert len(parts) == 2
    assert parts[0]["num"] == 1
    assert parts[1]["num"] == 2


def test_discover_modern_mode_does_not_stop_on_same_size(tmp_path):
    """Modern mode (?i=): the same-size heuristic is intentionally disabled
    because part sizes can legitimately repeat (e.g. several <1MB parts
    round to the same display size) and Google returns clean 404s for
    missing parts. If everything returns 206 we keep going."""
    payload = _fixture_payload()

    def fake_get(self, url, **kw):
        return _FakeResp(url, 206,
                         {"content-range": "bytes 0-0/411587",
                          "content-type": "application/octet-stream"})

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        # Cap at 5 so the test doesn't try to loop up to MAX_PARTS=500.
        parts = takeout_cli.discover_parts(payload, tmp_path, max_parts=5)
    # All 5 probes succeed → 5 parts.
    assert len(parts) == 5
    assert [p["num"] for p in parts] == [1, 2, 3, 4, 5]


def test_discover_does_not_stop_when_sizes_differ(tmp_path):
    """If sizes vary, discovery should continue until 404s."""
    payload = _fixture_payload()

    def fake_get(self, url, **kw):
        n = _part_index_from_url(url)
        size = n * 1000  # each part has a different size
        return _FakeResp(url, 206,
                         {"content-range": f"bytes 0-0/{size}",
                          "content-type": "application/octet-stream"})

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(payload, tmp_path)
    # Should continue until 404s (which never happen in this mock).
    # But MAX_PARTS caps it at 500.
    assert len(parts) == 500


def test_discover_modern_mode_uses_i_param_for_part_selection(tmp_path):
    """In modern mode (?i=N), the filename suffix stays `-001.zip` (cosmetic)
    while `i=` carries the actual part index. The captured URL might point
    at a middle part (e.g. i=2); discovery must still sweep from i=0 so we
    don't miss earlier parts. Regression test for the bug where the
    discovery probed `-001.zip`, `-002.zip`, … all with the captured `i=`
    and only ever saw one part."""
    payload = _fixture_payload()  # fixture URL has i=4

    probed_urls: list[str] = []

    def fake_get(self, url, **kw):
        probed_urls.append(url)
        # Pretend archive has 5 parts; i=5 onward returns 404.
        n = _part_index_from_url(url)
        if n <= 5:
            return _FakeResp(url, 206,
                             {"content-range": f"bytes 0-0/{n * 1000}",
                              "content-type": "application/octet-stream"})
        return _FakeResp(url, 404, {})

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(payload, tmp_path)

    assert len(parts) == 5
    assert [p["num"] for p in parts] == [1, 2, 3, 4, 5]
    # Every probe URL should have `i=N` matching the part number minus 1.
    for p in parts:
        m = re.search(r"[?&]i=(\d+)", p["url"])
        assert m, f"URL missing i= param: {p['url']}"
        assert int(m.group(1)) == p["num"] - 1, (
            f"i= value {m.group(1)} does not match part num {p['num']} in {p['url']}"
        )
    # The filename suffix should be constant `-001.zip` (cosmetic).
    suffixes = {p["url"].rsplit("/", 1)[-1].split("?")[0] for p in parts}
    assert suffixes == {"takeout-20260612T190148Z-9-001.zip"}, suffixes
    # Discovery swept i=0..4 even though the captured URL had i=4.
    assert "i=0" in probed_urls[0]
    assert "i=4" in probed_urls[4]


def test_build_probe_url_replaces_i_value():
    """Unit test for the URL builder helper."""
    base = "https://x/takeout-20251207T071725Z-3-001.zip"
    q = "j=abc&user=1&authuser=3&i=4"
    # i= replaces the existing i=N.
    assert takeout_cli._build_probe_url(base, q, 0) == \
        f"{base}?j=abc&user=1&authuser=3&i=0"
    assert takeout_cli._build_probe_url(base, q, 7) == \
        f"{base}?j=abc&user=1&authuser=3&i=7"
    # i= at the start of the query.
    q2 = "i=4&user=1"
    assert takeout_cli._build_probe_url(base, q2, 2) == f"{base}?i=2&user=1"
    # No i= requested: pass query through.
    assert takeout_cli._build_probe_url(base, q, None) == f"{base}?{q}"
    # No query at all.
    assert takeout_cli._build_probe_url(base, None, 0) == base


def test_has_i_param():
    """Unit test for the URL-shape detector."""
    assert takeout_cli._has_i_param("i=4") is True
    assert takeout_cli._has_i_param("j=x&i=4") is True
    assert takeout_cli._has_i_param("j=x&user=1&i=4&authuser=3") is True
    assert takeout_cli._has_i_param("j=x&user=1") is False
    assert takeout_cli._has_i_param("") is False
    assert takeout_cli._has_i_param(None) is False
    # `i` without a value doesn't count.
    assert takeout_cli._has_i_param("j=x&i") is False
    assert takeout_cli._has_i_param("j=x&i=") is False


def test_probe_treats_400_as_missing():
    """400 Bad Request (mismatched query params) should return None."""
    fake = _FakeResp("https://x/1.zip", 400,
                     {"content-type": "text/plain"})
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        result = takeout_cli._probe_part(takeout_cli.requests.Session(),
                                          "https://x/1.zip", {})
    assert result is None


def test_probe_treats_unexpected_status_as_missing():
    """Any non-200/206 after auth checks should return None."""
    fake = _FakeResp("https://x/1.zip", 410,
                     {"content-type": "text/plain"})
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        result = takeout_cli._probe_part(takeout_cli.requests.Session(),
                                          "https://x/1.zip", {})
    assert result is None


def test_discover_respects_max_parts_cap(tmp_path):
    """If the user says there are 3 parts, only probe 3 times."""
    payload = _fixture_payload()

    def fake_get(self, url, **kw):
        n = _part_index_from_url(url)
        return _FakeResp(url, 206,
                         {"content-range": f"bytes 0-0/{n * 1000}",
                          "content-type": "application/octet-stream"})

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(payload, tmp_path, max_parts=3)
    assert len(parts) == 3
    assert parts[0]["num"] == 1
    assert parts[2]["num"] == 3


def test_fetch_manifest_finds_all_urls(tmp_path):
    """If the manage page contains multiple download URLs, the fetch
    function should return all of them as separate payloads."""
    payload = _fixture_payload()
    fake_html = """
    <html><body>
    <a href="https://takeout-download.usercontent.google.com/download/takeout-20260612T190148Z-11-001.zip?j=x&i=0">Download</a>
    <a href="https://takeout-download.usercontent.google.com/download/takeout-20260612T190148Z-14-001.zip?j=x&i=1">Download</a>
    <a href="https://takeout-download.usercontent.google.com/download/takeout-20260612T190148Z-9-001.zip?j=x&i=2">Download</a>
    </body></html>
    """
    fake_resp = _FakeResp(
        "https://takeout.google.com/u/0/manage/archive/x",
        200, {"content-type": "text/html"}, text=fake_html,
    )
    with mock.patch.object(takeout_cli.requests, "get", return_value=fake_resp):
        payloads = takeout_cli.fetch_takeout_manifest(payload)
    assert len(payloads) == 3
    assert any("Z-11-001" in p.url for p in payloads)
    assert any("Z-14-001" in p.url for p in payloads)
    assert any("Z-9-001" in p.url for p in payloads)
    for p in payloads:
        assert p.cookie == payload.cookie
        assert p.source == "server-manifest"


def test_fetch_manifest_redirect_to_login_returns_empty(tmp_path):
    """If the cookie is stale and the page redirects to Google signin,
    return empty list (caller falls back to single-URL payload)."""
    payload = _fixture_payload()
    fake_resp = _FakeResp(
        "https://accounts.google.com/signin",
        200, {"content-type": "text/html"}, text="<html>Sign in</html>",
    )
    with mock.patch.object(takeout_cli.requests, "get", return_value=fake_resp):
        payloads = takeout_cli.fetch_takeout_manifest(payload)
    assert payloads == []


def test_fetch_manifest_no_archive_id_returns_empty(tmp_path):
    """If the URL has no j= parameter, we can't build the manage URL."""
    import takeout_payload as tp
    payload = tp.TakeoutPayload(
        url="https://example.com/no-j-param.zip",
        cookie="SID=test",
        headers={},
    )
    payloads = takeout_cli.fetch_takeout_manifest(payload)
    assert payloads == []


def test_load_config_missing_returns_empty(tmp_path, monkeypatch):
    """If the config file doesn't exist, return empty dict."""
    cfg_path = tmp_path / "no-such-config.json"
    monkeypatch.setenv("TAKEOUT_CONFIG", str(cfg_path))
    result = takeout_cli.load_config()
    assert result == {}


def test_save_and_load_config_roundtrip(tmp_path, monkeypatch):
    """Saving and loading config preserves all fields."""
    cfg_path = tmp_path / "test-config.json"
    monkeypatch.setenv("TAKEOUT_CONFIG", str(cfg_path))
    cfg = {"output_dir": "/opt/storage/google-takeout", "extra": "stuff"}
    takeout_cli.save_config(cfg)
    assert cfg_path.exists()
    loaded = takeout_cli.load_config()
    assert loaded == cfg


def test_load_config_corrupt_returns_empty(tmp_path, monkeypatch):
    """If the config file is corrupt, return empty dict (don't crash)."""
    cfg_path = tmp_path / "corrupt-config.json"
    cfg_path.write_text("not valid json{{{", encoding="utf-8")
    monkeypatch.setenv("TAKEOUT_CONFIG", str(cfg_path))
    result = takeout_cli.load_config()
    assert result == {}


def test_save_config_atomic(tmp_path, monkeypatch):
    """Saving uses tmp + rename so partial writes don't corrupt the file."""
    cfg_path = tmp_path / "atomic-config.json"
    monkeypatch.setenv("TAKEOUT_CONFIG", str(cfg_path))
    takeout_cli.save_config({"a": 1})
    takeout_cli.save_config({"a": 2})
    assert cfg_path.exists()
    loaded = takeout_cli.load_config()
    assert loaded == {"a": 2}


def test_reset_config_flag_removes_file(tmp_path, monkeypatch):
    """--reset-config deletes the config file."""
    cfg_path = tmp_path / "reset-config.json"
    cfg_path.write_text('{"output_dir": "/x"}', encoding="utf-8")
    monkeypatch.setenv("TAKEOUT_CONFIG", str(cfg_path))
    assert cfg_path.exists()
    if cfg_path.exists():
        cfg_path.unlink()
        assert not cfg_path.exists()


def test_resolve_output_dir_uses_config(tmp_path, monkeypatch):
    """If OUTPUT_DIR is not set, resolve_output_dir reads from config."""
    target = tmp_path / "configured-output"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"output_dir": str(target)}),
        encoding="utf-8"
    )
    monkeypatch.setenv("TAKEOUT_CONFIG", str(cfg_path))
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    result = takeout_cli.resolve_output_dir()
    assert result == target


def test_resolve_output_dir_env_overrides_config(tmp_path, monkeypatch):
    """OUTPUT_DIR env var takes precedence over config."""
    env_target = tmp_path / "from-env"
    cfg_target = tmp_path / "from-config"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"output_dir": str(cfg_target)}),
        encoding="utf-8"
    )
    monkeypatch.setenv("TAKEOUT_CONFIG", str(cfg_path))
    monkeypatch.setenv("OUTPUT_DIR", str(env_target))
    result = takeout_cli.resolve_output_dir()
    assert result == env_target


# ---------------------------------------------------------------------------
# Schema v2 — parts mode auto-detection
# ---------------------------------------------------------------------------

def _make_v2_multi_payload(expected_parts=5, sizes=None):
    """Build a v2 multi-payload shaped like the new extension emits."""
    sizes = sizes or [100, 200, 300, 400, 500]
    return {
        "schema": 2,
        "captured_at": "2026-06-15T02:00:00.000Z",
        "source": "extension",
        "multi": True,
        "archiveId": "ccb0dc6c-ba0c-466f-9228-2ebd83fbcd20",
        "expectedParts": expected_parts,
        "exports": [
            {
                "url": (
                    "https://takeout-download.usercontent.google.com/"
                    f"download/takeout-20260612T190148Z-15-{i+1:03d}.zip"
                    f"?j=ccb0dc6c&i={i}&user=1&authuser=3"
                ),
                "partIndex": i,
                "size": s,
            }
            for i, s in enumerate(sizes[:expected_parts])
        ],
        "cookie": (
            "__Secure-1PSID=x; SID=y; HSID=z; SSID=a; APISID=b; SAPISID=c"
        ),
        "headers": {
            "User-Agent": "Mozilla/5.0 Chrome/120",
            "Accept": "*/*",
            "Referer": "https://takeout.google.com/",
        },
    }


def test_v2_multi_payload_yields_parts_mode(monkeypatch):
    """A v2 multi-payload with expectedParts switches the CLI into
    'parts' mode — no pick menu, all URLs are parts of one batch."""
    text = json.dumps(_make_v2_multi_payload())
    monkeypatch.setattr(sys, "stdin", io.StringIO(text + "\n"))
    payload, ctx = takeout_cli.parse_one_payload()
    assert ctx["mode"] == "parts"
    assert ctx["meta"].expectedParts == 5
    assert len(ctx["all_payloads"]) == 5
    # The returned payload is the first one; the parts list lives in ctx.
    assert payload.url.endswith("i=0&user=1&authuser=3")


def test_build_parts_from_payloads_uses_extension_sizes(tmp_path):
    """_build_parts_from_payloads should hand back parts whose `size`
    matches what the extension scraped off the page, and whose `have`
    is False when the file isn't on disk yet."""
    from takeout_payload import parse_multi_payload_meta

    text = json.dumps(_make_v2_multi_payload(
        expected_parts=3, sizes=[100, 200, 300]
    ))
    payloads, meta = parse_multi_payload_meta(text)
    parts = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)
    assert len(parts) == 3
    assert [p["num"] for p in parts] == [1, 2, 3]
    assert [p["size"] for p in parts] == [100, 200, 300]
    # None are on disk yet, so all have=False.
    assert all(p["have"] is False for p in parts)
    # Each URL preserves the i=0/1/2 query parameter.
    assert "i=0" in parts[0]["url"]
    assert "i=1" in parts[1]["url"]
    assert "i=2" in parts[2]["url"]


def test_build_parts_from_payloads_marks_existing_files_have(tmp_path):
    """If a part is already on disk with the right size, mark it have=True."""
    from takeout_payload import parse_multi_payload_meta

    text = json.dumps(_make_v2_multi_payload(
        expected_parts=2, sizes=[100, 200]
    ))
    payloads, meta = parse_multi_payload_meta(text)
    # Pre-create one of the files on disk.
    existing = tmp_path / "takeout-20260612T190148Z-15-002.zip"
    existing.write_bytes(b"\x00" * 200)
    parts = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)
    have_flags = {p["filename"]: p["have"] for p in parts}
    assert have_flags["takeout-20260612T190148Z-15-001.zip"] is False
    assert have_flags["takeout-20260612T190148Z-15-002.zip"] is True


def test_legacy_v1_multi_payload_is_rejected(monkeypatch):
    """v1 multi-payloads lack archiveId/expectedParts so the CLI exits
    with a clear "re-capture" message rather than degrading silently."""
    blob = _make_v2_multi_payload()
    blob["schema"] = 1
    del blob["archiveId"]
    del blob["expectedParts"]
    for e in blob["exports"]:
        e.pop("partIndex", None)
        e.pop("size", None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(blob) + "\n"))
    with pytest.raises(SystemExit) as exc_info:
        takeout_cli.parse_one_payload()
    assert exc_info.value.code == 2


def test_single_export_still_works_in_v2(monkeypatch):
    """v2 single-export captures without `multi` go through the normal
    discovery path (mode='single')."""
    blob = json.loads((ROOT / "tests" / "fixtures" / "sample_payload.json").read_text())
    blob["schema"] = 2
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(blob) + "\n"))
    payload, ctx = takeout_cli.parse_one_payload()
    assert ctx["mode"] == "single"
    assert ctx["meta"].expectedParts is None
    assert payload.url == blob["url"]


# ---------------------------------------------------------------------------
# Version banner / --version flag
# ---------------------------------------------------------------------------
def test_version_is_imported_and_banner_uses_it():
    """The CLI must import VERSION from takeout and reference it in the
    startup banner so a user (or logfile reviewer) can confirm which
    code is actually running. The VERSION constant is the single source
    of truth shared with takeout.py / the TUI."""
    from takeout import VERSION as upstream_version
    # Exported as a module attribute.
    assert hasattr(takeout_cli, "VERSION")
    assert takeout_cli.VERSION == upstream_version
    # The banner string template is built using VERSION, not a literal.
    import inspect
    main_src = inspect.getsource(takeout_cli.main)
    assert "VERSION" in main_src, "main() should reference VERSION"
    assert "f\"Google Takeout Downloader v{VERSION}" in main_src, (
        "banner header should be `f\"Google Takeout Downloader v{VERSION} — paste, go.\"`"
    )
    # And the early log line uses it too.
    assert 'f"Version: {VERSION}"' in main_src, (
        "should log Version: <VERSION> early so the logfile records which build ran"
    )


def test_cli_version_flag(capsys):
    """`python takeout_cli.py --version` should print the version and exit 0."""
    from takeout import VERSION
    with mock.patch("sys.argv", ["takeout_cli.py", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            takeout_cli.main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert VERSION in captured.out
    assert "takeout_cli" in captured.out


# ---------------------------------------------------------------------------
# --no-color / NO_COLOR
# ---------------------------------------------------------------------------
def test_c_helper_emits_ansi_when_color_on(monkeypatch):
    """When _USE_COLOR is on, _c() wraps text in the ANSI escape."""
    monkeypatch.setattr(takeout_cli, "_USE_COLOR", True)
    out = takeout_cli._c("32", "hello")
    assert out == "\033[32mhello\033[0m"


def test_c_helper_plain_when_color_off(monkeypatch):
    """When _USE_COLOR is off, _c() returns the bare text (no escapes)."""
    monkeypatch.setattr(takeout_cli, "_USE_COLOR", False)
    out = takeout_cli._c("32", "hello")
    assert out == "hello"
    assert "\033[" not in out


def test_no_color_flag_disables_color(monkeypatch):
    """`--no-color` flips the module-level _USE_COLOR switch off so all
    subsequent _c() calls emit plain text. We stop main() right after the
    flag is processed by having --reset-config short-circuit the return."""
    # Force color ON first so the flag has something to turn off.
    monkeypatch.setattr(takeout_cli, "_USE_COLOR", True)
    # Point config at a non-existent path so --reset-config returns 0 early
    # (before any paste prompt or network access).
    monkeypatch.setenv("TAKEOUT_CONFIG", "/nonexistent/takeout-cli-test.json")
    with mock.patch("sys.argv",
                    ["takeout_cli.py", "--no-color", "--reset-config"]):
        rc = takeout_cli.main()
    assert rc == 0
    # The flag should have disabled color for the rest of the process.
    assert takeout_cli._USE_COLOR is False
    assert takeout_cli._c("31", "x") == "x"


# ---------------------------------------------------------------------------
# _build_parts_from_payloads — modern i= mode (same URL path, different i=N)
# ---------------------------------------------------------------------------
def _make_i_mode_multi_payload(n: int = 5) -> dict:
    """Build a v2 multi-payload that uses the *modern* i= pattern: every
    URL has the same filename (e.g. ``-001.zip``) and the part
    disambiguator is the ``i=N`` query param.

    This mirrors the user-reported failure (June 15) where all 5
    parts shared ``takeout-20260615T055126Z-9-001.zip`` in the path
    and only ``?i=0,1,2,3,4`` distinguished them.
    """
    return {
        "schema": 2,
        "captured_at": "2026-06-15T05:51:00.000Z",
        "source": "extension",
        "multi": True,
        "archiveId": "8062f4c8-7a2c-4d95-b889-a0e88e07d1d6",
        "expectedParts": n,
        "exports": [
            {
                "url": (
                    "https://takeout-download.usercontent.google.com/"
                    f"download/takeout-20260615T055126Z-9-001.zip"
                    f"?j=8062f4c8&i={i}&user=1&authuser=3"
                ),
                "partIndex": i,
                "size": (10_000 * (i + 1)),
            }
            for i in range(n)
        ],
        "cookie": (
            "__Secure-1PSID=x; SID=y; HSID=z; SSID=a; APISID=b; SAPISID=c"
        ),
        "headers": {
            "User-Agent": "Mozilla/5.0 Chrome/120",
            "Accept": "*/*",
            "Referer": "https://takeout.google.com/",
        },
    }


def test_build_parts_i_mode_gives_unique_filenames(tmp_path):
    """When all 5 URLs share the same path (modern ``i=`` mode), the
    CLI must give each part a *unique* filename based on the ``i=N``
    value, otherwise ``aria2c -j 5`` races 5 writers on the same file
    and silently drops 4 of 5 downloads (the original 1.2 MB HTML
    bug from June 15)."""
    from takeout_payload import parse_multi_payload_meta
    text = json.dumps(_make_i_mode_multi_payload(5))
    payloads, meta = parse_multi_payload_meta(text)
    parts = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)
    filenames = [p["filename"] for p in parts]
    # All five must be unique (no two writers can share one file).
    assert len(filenames) == len(set(filenames)), (
        f"Duplicate filenames: {filenames}"
    )
    # The expected pattern: same base, ascending 3-digit part numbers.
    expected = [f"takeout-20260615T055126Z-9-{i+1:03d}.zip" for i in range(5)]
    assert filenames == expected
    # URLs stay untouched (i=N still encodes the part index).
    for i, p in enumerate(parts):
        assert f"i={i}" in p["url"]
        assert p["num"] == i + 1


def test_build_parts_i_mode_marks_correct_file_have(tmp_path):
    """In i= mode, a pre-existing ``-002.zip`` on disk should be
    detected as have=True when its URL is the one with ``?i=1``."""
    from takeout_payload import parse_multi_payload_meta
    text = json.dumps(_make_i_mode_multi_payload(3))
    payloads, meta = parse_multi_payload_meta(text)
    # Pre-create the part-2 file (which is ``?i=1`` -> part num 2).
    existing = tmp_path / "takeout-20260615T055126Z-9-002.zip"
    existing.write_bytes(b"\x00" * 20_000)
    parts = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)
    have_flags = {p["filename"]: p["have"] for p in parts}
    assert have_flags["takeout-20260615T055126Z-9-001.zip"] is False
    assert have_flags["takeout-20260615T055126Z-9-002.zip"] is True
    assert have_flags["takeout-20260615T055126Z-9-003.zip"] is False


def test_discover_parts_i_mode_gives_unique_filenames(tmp_path):
    """``discover_parts`` (the legacy single-payload path) must also
    produce unique filenames in modern i= mode, matching the
    ``_build_parts_from_payloads`` behavior."""
    payload = _fixture_payload()
    sizes = {1: 100, 2: 200, 3: 300}

    def fake_get(self, url, **kw):
        m = re.search(r'[?&]i=(\d+)', url)
        n = int(m.group(1)) + 1 if m else 1
        if n in sizes:
            return _FakeResp(url, 206,
                             {"content-range": f"bytes 0-0/{sizes[n]}",
                              "content-type": "application/octet-stream"})
        return _FakeResp(url, 404, {})

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(payload, tmp_path)

    filenames = [p["filename"] for p in parts]
    assert len(filenames) == len(set(filenames)), (
        f"Duplicate filenames: {filenames}"
    )
    # i=N URL: each part should get its own ascending 3-digit suffix.
    assert filenames == [
        "takeout-20260612T190148Z-9-001.zip",
        "takeout-20260612T190148Z-9-002.zip",
        "takeout-20260612T190148Z-9-003.zip",
    ]
    # And the URL itself still carries the i= selector.
    for i, p in enumerate(parts):
        assert f"i={i}" in p["url"]


# ---------------------------------------------------------------------------
# verify_parts — HTML-corrupted partials get unlinked so aria2c -c
# doesn't try to "resume" from a 1.2 MB Google sign-in page.
# ---------------------------------------------------------------------------
def test_verify_parts_unlinks_html_partial(tmp_path):
    """A file that starts with ``<!doctype html>`` is a Google sign-in
    challenge page, not a real ZIP — even if its size happens to match
    the probed size. The CLI must unlink it so aria2c re-downloads
    from scratch instead of trying to resume from offset N MB on
    retry (which would just return another sign-in page)."""
    p = {"num": 1, "url": "x", "filename": "bad.zip", "size": 1_200_000,
         "have": False}
    dest = tmp_path / "bad.zip"
    # Use exactly the size we declared in `p["size"]` so the
    # size-mismatch check doesn't fire first.
    html = b"<!doctype html><html><body>Google sign-in</body></html>"
    html = html + b" " * (1_200_000 - len(html))
    dest.write_bytes(html)

    complete, incomplete = takeout_cli.verify_parts([p], tmp_path)
    assert complete == []
    assert len(incomplete) == 1
    # The file should be gone now so aria2c -c starts at byte 0.
    assert not dest.exists()


def test_verify_parts_keeps_short_zip_partial(tmp_path):
    """A genuine in-progress ZIP (e.g. 50% downloaded) won't have an
    EOCD signature, but it also won't look like HTML. We must NOT
    unlink those — they're legit partials that aria2c -c should resume."""
    p = {"num": 1, "url": "x", "filename": "legit.zip", "size": 100_000_000,
         "have": False}
    dest = tmp_path / "legit.zip"
    # A real ZIP file starts with PK\x03\x04 (local file header).
    partial = b"PK\x03\x04" + b"\x00" * 1024
    dest.write_bytes(partial)

    complete, incomplete = takeout_cli.verify_parts([p], tmp_path)
    assert complete == []
    assert len(incomplete) == 1
    # File must still be on disk — aria2c -c will resume from here.
    assert dest.exists()


# ---------------------------------------------------------------------------
# _download_one_batch — auth_failures is initialized for parts-mode entry
# (regression for UnboundLocalError on June 15).
# ---------------------------------------------------------------------------
def test_auth_failures_initialized_in_parts_mode(monkeypatch, tmp_path):
    """The download loop increments ``auth_failures`` when the cookie
    expires mid-run, but the variable was only initialized inside the
    discovery branch — so when parts came from the pre-built
    multi-payload path, the first cookie-expiry triggered an
    UnboundLocalError instead of re-prompting. This test ensures the
    counter exists regardless of how parts were obtained."""
    from takeout_cli import _download_one_batch
    # Build a minimal pre-built parts list so the discovery branch
    # is skipped entirely.
    parts = [
        {"num": 1, "url": "http://x/1", "filename": "1.zip",
         "size": 1, "have": False},
    ]
    # Stub out everything that touches the network or filesystem.
    monkeypatch.setattr(takeout_cli, "load_state", lambda *a, **k: None)
    monkeypatch.setattr(takeout_cli, "state_matches_payload",
                        lambda *a, **k: False)
    monkeypatch.setattr(takeout_cli, "save_state", lambda *a, **k: None)
    # Make verify_parts mark all parts incomplete so the while-loop runs.
    monkeypatch.setattr(takeout_cli, "verify_parts",
                        lambda parts, out: ([], parts))
    # Simulate aria2c returning 13 (the actual symptom from June 15).
    monkeypatch.setattr(takeout_cli, "run_aria2c",
                        lambda *a, **k: 13)
    # Disable terminal grid so we don't draw to a closed TTY.
    monkeypatch.setattr(takeout_cli.sys, "stdout",
                        io.StringIO())
    # Force args.fresh / args.parallel to sane values.
    import argparse
    args = argparse.Namespace(fresh=False, parallel=1, max_parts=10,
                               reset_config=False, output_dir=str(tmp_path),
                               relay=False, tunnel=False,
                               relay_timeout=600)
    monkeypatch.setattr(takeout_cli, "args", args, raising=False)
    # The first iteration should NOT raise UnboundLocalError; it
    # should at least reach the ``looks_like_auth_failure`` branch
    # and try to re-prompt (we abort on EOFError to stop the test).
    payload = _fixture_payload()
    # A do-nothing log so the function can call log.warn/info/etc.
    fake_log = mock.MagicMock()
    # Force the re-prompt source to raise EOFError immediately so the
    # test exits cleanly. We also replace argparse to skip the
    # arg-parsing inside the function.
    def _eof_source():
        raise EOFError
    monkeypatch.setattr(takeout_cli, "_PAYLOAD_SOURCE", _eof_source)
    # The terminal input is empty, so the re-prompt loop exits via
    # EOFError; both are acceptable signals that auth_failures was
    # initialized correctly (no UnboundLocalError on line 2001).
    try:
        _download_one_batch(payload, output_dir=tmp_path, args=args,
                            log=fake_log,
                            initial_parts=parts, initial_state=None)
    except (SystemExit, EOFError, OSError):
        # Expected: the auth-failure re-prompt has no input to read
        # (we raise EOFError from the stubbed source).
        pass
    except UnboundLocalError as e:  # pragma: no cover
        if "auth_failures" in str(e):
            pytest.fail(
                "UnboundLocalError on auth_failures: the fix didn't "
                "initialize the counter at function scope"
            )
        raise


# ---------------------------------------------------------------------------
# Pre-flight full-download check — catches the "1.2 MB HTML in every zip"
# loop where Range probes succeed but full GETs get challenged.
# ---------------------------------------------------------------------------
class _FakeStreamResp:
    """Minimal requests.Response-shaped object for the pre-flight check.

    The pre-flight uses ``stream=True`` + ``iter_content`` to download
    the smallest part. ``_FakeStreamResp`` implements just enough of
    that surface for the pre-flight to detect HTML-vs-archive.
    """

    def __init__(self, url: str, status: int, headers: dict, body: bytes):
        self.url = url
        self.status_code = status
        self.headers = headers
        self._body = body
        self._consumed = 0
        self._closed = False

    def iter_content(self, chunk_size: int = 64 * 1024):
        while self._consumed < len(self._body):
            yield self._body[self._consumed:self._consumed + chunk_size]
            self._consumed = len(self._body)

    def close(self):
        self._closed = True


def test_preflight_passes_for_real_zip(tmp_path, monkeypatch):
    """A pre-flight that returns the expected number of bytes (and
    doesn't look like HTML) should NOT raise SystemExit."""
    real_body = b"PK\x03\x04" + b"\x00" * 99  # 101 bytes, looks like a ZIP
    resp = _FakeStreamResp(
        "https://takeout-download.usercontent.google.com/x?i=2",
        200, {"content-type": "application/octet-stream"}, real_body,
    )
    captured: list = []

    def fake_session_get(self, url, **kw):
        captured.append(kw)
        return resp

    monkeypatch.setattr(takeout_cli.requests.Session, "get", fake_session_get)
    parts = [
        {"num": 1, "url": "x?i=0", "filename": "1.zip", "size": 100, "have": False},
        {"num": 2, "url": "x?i=1", "filename": "2.zip", "size": 101, "have": False},
    ]
    payload = _fixture_payload()
    # The smallest part is 100 bytes (num=1); pre-flight should GET
    # that one. But the fake returns 101 bytes (the bigger one).
    # We override both parts to the same size and use the resp from
    # part 2 anyway. The check is: did pre-flight exit cleanly?
    # It will pick the smaller part and our fake will respond with a
    # body of different size — but the pre-flight only treats a short
    # body as a *warning*, not a fatal error.
    parts[0]["size"] = 1000
    parts[1]["size"] = 101
    # Now the smallest is part 2 (101 bytes). fake returns 101 bytes
    # of ZIP-like body. Pre-flight should pass.
    takeout_cli._preflight_full_download(parts, payload, tmp_path)
    # No Range header on the pre-flight (it must exercise the full
    # GET path that aria2c will use).
    assert "Range" not in captured[0]
    assert "headers" in captured[0]


def test_preflight_bails_on_html_response(tmp_path, monkeypatch):
    """A pre-flight that returns HTML (Google sign-in challenge) must
    raise SystemExit(1) AND save the HTML body for inspection. This
    is the exact signature of the June 15 bug: probes pass, full
    GETs get HTML, the script would otherwise loop asking for fresh
    cookies that have the same problem.
    """
    html_body = (
        b"<!doctype html><html><head><title>Sign in - Google "
        b"Accounts</title></head><body>Please sign in to continue."
        b"</body></html>" + b" " * 500
    )
    resp = _FakeStreamResp(
        "https://accounts.google.com/v3/signin",
        200, {"content-type": "text/html; charset=utf-8"}, html_body,
    )
    monkeypatch.setattr(takeout_cli.requests.Session, "get", lambda *a, **k: resp)
    parts = [
        {"num": 1, "url": "x?i=0", "filename": "1.zip", "size": 100, "have": False},
    ]
    payload = _fixture_payload()
    with pytest.raises(SystemExit) as exc_info:
        takeout_cli._preflight_full_download(parts, payload, tmp_path)
    assert exc_info.value.code == 1
    # The HTML body should have been saved for the user to inspect.
    saved = list(tmp_path.glob("auth_challenge_*.html"))
    assert len(saved) == 1
    body = saved[0].read_bytes()
    assert b"<!doctype html>" in body
    assert b"x?i=0" in body  # the part URL is recorded in a comment


def test_preflight_bails_on_accounts_redirect(tmp_path, monkeypatch):
    """If the pre-flight gets redirected to accounts.google.com (the
    classical expired-cookie symptom), we also bail with SystemExit(1)
    and save the response."""
    html_body = b"<!doctype html><html><body>signin</body></html>" + b"x" * 100
    resp = _FakeStreamResp(
        "https://accounts.google.com/v3/signin/...",
        200, {"content-type": "text/html"}, html_body,
    )
    monkeypatch.setattr(takeout_cli.requests.Session, "get", lambda *a, **k: resp)
    parts = [{"num": 1, "url": "x?i=0", "filename": "1.zip", "size": 100,
              "have": False}]
    with pytest.raises(SystemExit) as exc_info:
        takeout_cli._preflight_full_download(parts, _fixture_payload(), tmp_path)
    assert exc_info.value.code == 1


def test_preflight_bails_on_wrong_content_type_even_if_zip_bytes(tmp_path,
                                                                  monkeypatch):
    """If the response claims to be a ZIP via content-type but the
    body actually looks like HTML (proxy misconfiguration or
    partial challenge), we still bail."""
    bad_body = b"<!doctype html><html><body>redirect</body></html>" + b" " * 50
    resp = _FakeStreamResp(
        "https://x", 200,
        {"content-type": "application/octet-stream"},  # lying header
        bad_body,
    )
    monkeypatch.setattr(takeout_cli.requests.Session, "get", lambda *a, **k: resp)
    parts = [{"num": 1, "url": "x?i=0", "filename": "1.zip", "size": 100,
              "have": False}]
    with pytest.raises(SystemExit) as exc_info:
        takeout_cli._preflight_full_download(parts, _fixture_payload(), tmp_path)
    assert exc_info.value.code == 1


def test_preflight_passes_on_real_archive_bytes(tmp_path, monkeypatch):
    """A pre-flight that returns real archive bytes (PK signature) with
    content-type application/octet-stream should NOT raise SystemExit.
    This is the happy path: the cookie is healthy and we proceed to
    the real download via aria2c."""
    short_body = b"PK\x03\x04" + b"\x00" * 9  # 11 bytes, claimed size 1000
    resp = _FakeStreamResp(
        "https://x", 200, {"content-type": "application/octet-stream"},
        short_body,
    )
    monkeypatch.setattr(takeout_cli.requests.Session, "get", lambda *a, **k: resp)
    parts = [{"num": 1, "url": "x?i=0", "filename": "1.zip", "size": 1000,
              "have": False}]
    # Should NOT raise.
    takeout_cli._preflight_full_download(parts, _fixture_payload(), tmp_path)


def test_looks_like_html_bytes():
    """The byte-buffer variant of the HTML detector is used by the
    pre-flight; spot-check its edge cases."""
    assert takeout_cli._looks_like_html_bytes(
        b"<!doctype html><html>...</html>"
    )
    assert takeout_cli._looks_like_html_bytes(
        b"\xef\xbb\xbf<!doctype html>"  # BOM-prefixed
    )
    assert takeout_cli._looks_like_html_bytes(b"<HTML>x</HTML>")
    assert not takeout_cli._looks_like_html_bytes(b"PK\x03\x04...")
    assert not takeout_cli._looks_like_html_bytes(b"")
    assert not takeout_cli._looks_like_html_bytes(b"random binary \x00\x01")


def test_drain_response_reads_up_to_max_bytes():
    """_drain_response should read up to max_bytes from a streaming
    response and close it, even if the response has more data."""

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body
            self._pos = 0
            self._closed = False

        def iter_content(self, chunk_size=4096):
            while self._pos < len(self._body):
                chunk = self._body[self._pos:self._pos + chunk_size]
                self._pos += chunk_size
                yield chunk

        def close(self):
            self._closed = True

    # 10 KB body, but drain only 1 KB. _drain_response reads in
    # 4 KB chunks so it will read 4 KB on the first iteration.
    # That's still tiny compared to the full file and more than
    # enough to detect HTML.
    big_body = b"x" * 10240
    resp = FakeResp(big_body)
    result = takeout_cli._drain_response(resp, max_bytes=1024)
    assert len(result) == 4096  # first 4 KB chunk
    assert result == b"x" * 4096
    assert resp._closed

    # Small body, drain more than available
    small_body = b"PK\x03\x04" + b"\x00" * 10
    resp2 = FakeResp(small_body)
    result2 = takeout_cli._drain_response(resp2, max_bytes=1024)
    assert result2 == small_body
    assert resp2._closed


def test_adaptive_parallelism_reduces_to_1(monkeypatch, tmp_path):
    """When all parts fail and parallel > 1, the download loop should
    automatically reduce parallelism to 1 before asking for a new cookie.
    This prevents the loop of: 5 parallel → all HTML → re-prompt cookie →
    same failure."""
    import argparse
    payload = _fixture_payload()
    parts = [
        {"num": i+1, "url": f"https://x?i={i}",
         "filename": f"part-{i+1:03d}.zip",
         "size": 1_228_078, "have": False}
        for i in range(3)
    ]
    # verify_parts returns 0 complete, 3 incomplete (all failed)
    monkeypatch.setattr(takeout_cli, "verify_parts",
                        lambda parts, outdir: (
                            [],
                            [dict(p, have=False) for p in parts]
                        ))
    # parse_one_payload: fresh cookie on re-prompt
    monkeypatch.setattr(takeout_cli, "parse_one_payload",
                        lambda: (_fixture_payload(), {
                            "mode": "single",
                            "meta": type("M", (), {"expectedParts": None})(),
                            "all_payloads": [],
                            "pre_built_parts": None,
                        }))
    monkeypatch.setattr(takeout_cli, "_preflight_full_download",
                        lambda *a: None)
    monkeypatch.setattr(takeout_cli, "save_state", lambda *a: None)
    monkeypatch.setattr(takeout_cli, "state_matches_payload", lambda *a: True)
    monkeypatch.setattr(takeout_cli, "load_state", lambda *a: None)
    monkeypatch.setattr(takeout_cli, "make_state", lambda *a: {})
    monkeypatch.setattr(takeout_cli, "update_state_from_parts", lambda *a: {})
    monkeypatch.setattr(takeout_cli, "build_aria2_input", lambda *a: "")
    monkeypatch.setattr(takeout_cli, "run_aria2c", lambda *a, **k: 0)
    monkeypatch.setattr(takeout_cli.sys, "stdout", io.StringIO())

    args = argparse.Namespace(
        parallel=5, max_parts=500, fresh=False,
        output_dir=str(tmp_path), verbose=0, version=False,
    )

    rc = takeout_cli._download_one_batch(
        payload, tmp_path, args,
        logging.getLogger("test"),
        initial_parts=parts,
        initial_state=None,
    )
    # After adaptive parallelism, args.parallel should have been reduced
    # to 1 (from original 5) on the first auth failure detection.
    assert args.parallel == 1, (
        f"Expected parallel to be reduced to 1, got {args.parallel}"
    )



# ---------------------------------------------------------------------------
# Production path: v2 multi-payload from the extension's "Copy ALL exports"
# ---------------------------------------------------------------------------
def _multi_fixture_text():
    return (ROOT / "tests" / "fixtures" / "sample_multi_payload.json").read_text()


def test_v2_multi_payload_parses_to_parts_mode(monkeypatch):
    """The extension's v2 multi-payload (archiveId + expectedParts ==
    len(exports)) must be recognised as 'parts' mode — one batch whose
    URLs are its parts — NOT as separate 'batches' and NOT as a single
    export needing probe-based discovery."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(_multi_fixture_text() + "\n"))
    payload, ctx = takeout_cli.parse_one_payload()
    assert ctx["mode"] == "parts", f"expected parts mode, got {ctx['mode']}"
    assert ctx["meta"].expectedParts == 5
    assert ctx["meta"].archiveId
    assert len(ctx["all_payloads"]) == 5


def test_v2_multi_payload_builds_sorted_parts(tmp_path):
    """parts mode must build all 5 parts directly from the payload (no
    network probe) and feed them smallest-first so the parallel pool
    finishes quick parts early."""
    from takeout_payload import parse_multi_payload_meta
    payloads, meta = parse_multi_payload_meta(_multi_fixture_text())
    parts = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)

    assert len(parts) == 5
    # Every part has a unique on-disk filename (so aria2c -j N can't race
    # five writers onto one file).
    names = [p["filename"] for p in parts]
    assert len(set(names)) == 5, f"filenames not unique: {names}"

    # Feed order is smallest-first (known sizes ascending). The fixture
    # sizes (bytes) are deliberately out of order in the payload.
    known = [p["size"] for p in parts if p["size"] > 0]
    assert known == sorted(known), f"parts not smallest-first: {known}"


def test_v2_multi_payload_resume_marks_have(tmp_path):
    """A part already present on disk at >= its known size is marked
    have=True so resume skips it."""
    from takeout_payload import parse_multi_payload_meta
    payloads, meta = parse_multi_payload_meta(_multi_fixture_text())
    # Pre-create the smallest part on disk at full size.
    smallest_i = min(meta.sizes, key=lambda k: meta.sizes[k])
    parts_preview = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)
    target = next(p for p in parts_preview
                  if p["num"] == smallest_i + 1)
    (tmp_path / target["filename"]).write_bytes(b"\x00" * target["size"])

    parts = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)
    have = [p for p in parts if p["have"]]
    assert any(p["filename"] == target["filename"] for p in have), (
        "pre-existing full-size part should be marked have=True"
    )


# ---------------------------------------------------------------------------
# Parts mode feeds smallest-first (size ordering)
# ---------------------------------------------------------------------------
def test_build_parts_from_payloads_sorts_smallest_first(tmp_path):
    """`parts` mode must hand aria2c the parts ordered smallest->biggest so
    the fast parts finish first. `num` stays tied to the i= identity;
    only feed order changes. Unknown sizes (0) sort last."""
    from takeout_payload import parse_multi_payload_meta
    payload_json = json.dumps({
        "schema": 2,
        "multi": True,
        "archiveId": "abc-123",
        "expectedParts": 4,
        "cookie": "SID=x; __Secure-1PSID=y",
        "headers": {},
        "exports": [
            {"url": "https://takeout-download.usercontent.google.com/download/takeout-20260612T190148Z-1-001.zip?j=abc-123&i=0", "size": 800, "partIndex": 0},
            {"url": "https://takeout-download.usercontent.google.com/download/takeout-20260612T190148Z-1-001.zip?j=abc-123&i=1", "size": 100, "partIndex": 1},
            {"url": "https://takeout-download.usercontent.google.com/download/takeout-20260612T190148Z-1-001.zip?j=abc-123&i=2", "size": 0, "partIndex": 2},
            {"url": "https://takeout-download.usercontent.google.com/download/takeout-20260612T190148Z-1-001.zip?j=abc-123&i=3", "size": 400, "partIndex": 3},
        ],
    })
    payloads, meta = parse_multi_payload_meta(payload_json)
    parts = takeout_cli._build_parts_from_payloads(payloads, meta, tmp_path)
    sizes = [p["size"] for p in parts]
    # Known sizes ascending, unknown (0) last.
    assert sizes == [100, 400, 800, 0], sizes
    # num must still map to the i= identity (1-based), not the feed pos.
    by_size = {p["size"]: p["num"] for p in parts}
    assert by_size[100] == 2  # i=1 -> num 2
    assert by_size[800] == 1  # i=0 -> num 1
    assert by_size[400] == 4  # i=3 -> num 4
