"""Offline reliability regressions: actual ZIPs and temporary files only."""
import asyncio
import hashlib
import io
import sqlite3
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import autopilot.run as runner
from autopilot.ledger import open_ledger, LedgerError
from autopilot.mover import MoveRefused
from autopilot.remote import RemoteResult
from autopilot.transport import TransferResult
from fake_http import FakeHttpClient, FakeStream
from test_integration import scenario, go, ARCHIVE_ID, redirector, CHALLENGE_URL
from fake_cdp import redirect_event


def zipped(payload=b"archive payload"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("member.txt", payload)
    return out.getvalue()


class Cookies:
    async def call(self, method, *args, **kwargs):
        assert method == "Storage.getCookies", "cached transfer must not drive browser tabs"
        return {"cookies": [{"name": "SID", "value": "offline", "domain": ".google.com"}]}


@asynccontextmanager
async def cookies():
    yield Cookies()


def no_browser():
    raise AssertionError("verified staged bytes must not require a browser")


def seed(tmp_path, bodies=None, *, cached=True, staged=False, count=None):
    bodies = bodies or {"part.zip": zipped()}
    staging = tmp_path / "staging"
    archive = tmp_path / "archive"
    staging.mkdir()
    archive.mkdir()
    cfg = runner.RunConfig("export", str(tmp_path / "state.db"), str(staging), str(archive),
                           account="owner", require_mount=False, archive_remote="offline:exports/job",
                           cached_transfer_only=True, clean_staging=True)
    led = open_ledger(cfg.ledger_path)
    led.upsert_job(cfg.archive_id, account=cfg.account)
    led.upsert_parts(cfg.archive_id, [(i, name, len(body)) for i, (name, body) in enumerate(bodies.items())])
    led.record_part_list(cfg.archive_id, expected_count=count or len(bodies))
    for i, (name, body) in enumerate(bodies.items()):
        if cached:
            led.record_mint(cfg.archive_id, i, "https://takeout-download.usercontent.google.com/download/" + name)
        if staged:
            (staging / name).write_bytes(body)
    led.close()
    return cfg, bodies


def execute(cfg, client=None, factory=cookies):
    return asyncio.run(runner.run_once(cfg, http_client=client or FakeHttpClient([]), session_factory=factory))


def row(cfg, idx=0):
    led = open_ledger(cfg.ledger_path)
    result = dict(led.part(cfg.archive_id, idx))
    led.close()
    return result


def backend(monkeypatch, *, fail=False):
    calls = []
    def upload(source, cfg, *, filename, expected_size, sha256):
        data = Path(source).read_bytes()
        assert expected_size == len(data)
        assert sha256 == hashlib.sha256(data).hexdigest()
        calls.append((source, filename))
        return RemoteResult("failed") if fail else RemoteResult("uploaded", True, cfg.destination + "/" + filename,
                                                                expected_size, sha256)
    monkeypatch.setattr(runner, "archive_remote_part", upload)
    return calls


def test_mixed_zip_mp4_mbox_remote_receipts_and_cleanup(tmp_path, monkeypatch):
    cfg, bodies = seed(tmp_path, {"part.zip": zipped(), "clip.mp4": b"\x00\x00\x00\x18ftypmp42movie", "mail.mbox": b"From user@example.test\nSubject: offline\n\nbody\n"})
    calls = backend(monkeypatch)
    monkeypatch.setattr(runner, "move_part", lambda *a, **k: pytest.fail("remote must not use FUSE"))
    monkeypatch.setattr(runner, "index_destination", lambda *a, **k: pytest.fail("remote must not stat destination"))
    client = FakeHttpClient([FakeStream(200, {"Content-Length": str(len(b))}, b) for b in bodies.values()])
    result = execute(cfg, client)
    assert result.status == "complete" and result.parts_done == 3
    assert len(calls) == 3
    for i, (name, b) in enumerate(bodies.items()):
        r = row(cfg, i)
        assert r["sha256"] == hashlib.sha256(b).hexdigest()
        assert r["remote_path"] == cfg.archive_remote + "/" + name and r["remote_verified_at"]
        assert not (Path(cfg.staging_dir) / name).exists()


def test_backend_failure_keeps_verified_staging_and_can_reupload_without_browser(tmp_path, monkeypatch):
    cfg, bodies = seed(tmp_path, staged=True)
    backend(monkeypatch, fail=True)
    assert execute(cfg, factory=no_browser).status == "incomplete"
    r = row(cfg)
    assert r["sha256"] and r["status"] != "done" and not r["remote_verified_at"]
    assert (Path(cfg.staging_dir) / "part.zip").read_bytes() == bodies["part.zip"]
    backend(monkeypatch)
    assert execute(cfg, factory=no_browser).status == "complete"


def test_staged_known_part_without_url_reuploads_in_normal_mode(tmp_path, monkeypatch):
    cfg, _ = seed(tmp_path, cached=False, staged=True)
    backend(monkeypatch)
    assert execute(replace(cfg, cached_transfer_only=False), factory=no_browser).status == "complete"
    assert not row(cfg)["minted_url"]


def test_size_only_fuse_existing_file_is_never_remote_done(tmp_path, monkeypatch):
    cfg, bodies = seed(tmp_path)
    (Path(cfg.archive_dir) / "part.zip").write_bytes(bodies["part.zip"])
    calls = backend(monkeypatch, fail=True)
    client = FakeHttpClient([FakeStream(200, {}, bodies["part.zip"])])
    result = execute(cfg, client)
    assert result.parts_done == 0 and result.status == "incomplete" and calls
    assert not row(cfg)["remote_path"]


def test_bad_crc_byte_complete_is_quarantined_then_cached_retry(tmp_path, monkeypatch):
    body = zipped(b"distinct-CRC-payload")
    bad = body.replace(b"distinct-CRC-payload", b"distinct-CRC-payloae")
    cfg, _ = seed(tmp_path, {"part.zip": body}, staged=True)
    staged = Path(cfg.staging_dir) / "part.zip"
    staged.write_bytes(bad)
    calls = backend(monkeypatch)
    assert execute(cfg, factory=no_browser).status == "incomplete"
    assert not staged.exists() and not calls
    assert list(Path(cfg.staging_dir).glob("part.zip.corrupt-*"))[0].read_bytes() == bad
    assert row(cfg)["minted_url"]
    client = FakeHttpClient([FakeStream(200, {}, body)])
    assert execute(cfg, client).status == "complete"


def test_interrupted_partial_preserves_validator_and_error(tmp_path, monkeypatch):
    cfg, bodies = seed(tmp_path)
    async def partial(client, url, dest, **kwargs):
        Path(dest).write_bytes(bodies["part.zip"][:20])
        return TransferResult("partial", 20, 20, etag='"v1"', error="read interrupted")
    monkeypatch.setattr(runner, "download", partial)
    assert execute(cfg).status == "incomplete"
    r = row(cfg)
    assert r["etag"] == '"v1"' and r["size_on_disk"] == 20 and "interrupted" in r["error"]
    for _ in range(4):
        execute(cfg)
    assert row(cfg)["minted_url"], "transient lifetime failure count must not remint"


def test_free_space_rechecked_before_each_transfer(tmp_path, monkeypatch):
    cfg, bodies = seed(tmp_path, {"a.zip": zipped(), "b.zip": zipped()})
    cfg.staging_reserve = 7
    cfg.min_headroom = 11
    checks = []
    def guard(path, *, min_headroom=None):
        checks.append(min_headroom)
        if len(checks) == 3:
            raise MoveRefused("disk became full")
    monkeypatch.setattr(runner, "assert_staging_ready", guard)
    backend(monkeypatch)
    client = FakeHttpClient([FakeStream(200, {}, b) for b in bodies.values()])
    result = execute(cfg, client)
    assert result.status == "failed" and result.parts_done == 1 and len(client.requests) == 1
    assert checks == [11, len(bodies["a.zip"]) + 18, len(bodies["b.zip"]) + 18]

def test_mount_lost_before_copy_keeps_staging_not_done(tmp_path, monkeypatch):
    cfg, _ = seed(tmp_path, staged=True)
    cfg.archive_remote = None
    checks = []
    def guard(*a, **k):
        checks.append(1)
        if len(checks) == 2:
            raise MoveRefused("mount lost")
    monkeypatch.setattr(runner, "assert_destination_ready", guard)
    monkeypatch.setattr(runner, "move_part", lambda *a, **k: pytest.fail("must not copy on lost mount"))
    result = execute(cfg, factory=no_browser)
    assert result.status == "failed" and result.parts_done == 0
    assert (Path(cfg.staging_dir) / "part.zip").exists()


def test_fuse_fsync_never_authorizes_cleanup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    cfg, _ = seed(tmp_path, staged=True)
    cfg.archive_remote = None
    monkeypatch.setattr(runner, "assert_destination_ready", lambda *a, **k: None)
    monkeypatch.setattr(runner, "fuse_mount_for", lambda *a, **k: "/mount")
    monkeypatch.setattr(runner, "move_part", lambda *a, **k: SimpleNamespace(action="moved"))
    assert execute(cfg, factory=no_browser).parts_done == 0
    assert (Path(cfg.staging_dir) / "part.zip").exists()


def test_native_local_hash_verified_copy_cleanup(tmp_path):
    cfg, _ = seed(tmp_path, staged=True)
    cfg.archive_remote = None
    assert execute(cfg, factory=no_browser).status == "complete"
    assert row(cfg)["sha256"] and not row(cfg)["remote_path"]
    assert not (Path(cfg.staging_dir) / "part.zip").exists()


def test_cached_partial_mint_authblock_transfers_without_scrape_or_subset_complete(tmp_path, monkeypatch):
    s = scenario(tmp_path, n_parts=2, mint_plan={redirector(1): [redirect_event(redirector(1), CHALLENGE_URL)]})
    s["cfg"].mint_only = True
    assert go(s).status == "needs_reauth"
    cfg = replace(s["cfg"], mint_only=False, cached_transfer_only=True, archive_remote="offline:job")
    backend(monkeypatch)
    result = execute(cfg, FakeHttpClient([FakeStream(200, {}, s["bodies"][0])]))
    assert result.status == "incomplete" and result.parts_expected == 2 and result.parts_done == 1
    led = open_ledger(cfg.ledger_path)
    assert len(led.parts(ARCHIVE_ID)) == 2 and not led.part(ARCHIVE_ID, 1)["minted_url"]
    led.close()


def test_done_rows_without_urls_and_missing_mints_retain_expected_evidence(tmp_path):
    cfg, _ = seed(tmp_path, cached=False, count=4)
    led = open_ledger(cfg.ledger_path)
    led.set_part_status(cfg.archive_id, 0, "done")
    led.upsert_parts(cfg.archive_id, [(1, "download", 100)])
    page = runner.page_from_ledger(led, cfg.archive_id, cached_transfer_only=True)
    assert len(page.parts) == 2 and page.part_of_N == [4]
    led.close()
    assert execute(cfg, factory=no_browser).parts_expected == 4


def test_done_without_url_tolerated_by_ordinary_page_from_ledger(tmp_path):
    cfg, _ = seed(tmp_path, cached=False)
    led = open_ledger(cfg.ledger_path)
    led.set_part_status(cfg.archive_id, 0, "done")
    assert runner.page_from_ledger(led, cfg.archive_id) is not None
    led.close()


def test_stale_remote_receipt_path_is_revalidated_and_not_trusted(tmp_path, monkeypatch):
    cfg, _ = seed(tmp_path, cached=False)
    led = open_ledger(cfg.ledger_path)
    led.record_verified(cfg.archive_id, 0, sha256="a" * 64, size=120, remote_path="old:job/part.zip")
    led.set_part_status(cfg.archive_id, 0, "done")
    led.close()
    monkeypatch.setattr(runner, "verify_remote_part", lambda *a, **k: RemoteResult("failed"))
    result = execute(cfg, factory=no_browser)
    assert result.parts_done == 0 and result.status == "incomplete"
    assert not row(cfg)["remote_path"]


def test_confirmed_receipt_at_new_remote_requires_fresh_backend_digest(tmp_path, monkeypatch):
    cfg, _ = seed(tmp_path, cached=False)
    led = open_ledger(cfg.ledger_path)
    size = led.part(cfg.archive_id, 0)["size_expected"]
    led.record_verified(cfg.archive_id, 0, sha256="a" * 64, size=size, remote_path="old:job/part.zip")
    led.set_part_status(cfg.archive_id, 0, "done")
    led.close()
    monkeypatch.setattr(runner, "verify_remote_part", lambda cfg, **k: RemoteResult("already-present", True,
                        cfg.destination + "/" + k["filename"], k["expected_size"], k["sha256"]))
    assert execute(cfg, factory=no_browser).status == "complete"
    assert row(cfg)["remote_path"] == cfg.archive_remote + "/part.zip"


def test_account_mismatch_refused_without_overwriting_owner(tmp_path):
    cfg, _ = seed(tmp_path)
    result = execute(replace(cfg, account="different"), factory=no_browser)
    assert result.status == "failed" and "account mismatch" in result.error
    led = open_ledger(cfg.ledger_path)
    assert led.job(cfg.archive_id)["account"] == "owner"
    with pytest.raises(LedgerError):
        led.upsert_job(cfg.archive_id, account="different")
    led.close()


def test_discovered_user_mismatch_refused_before_mint(tmp_path):
    s = scenario(tmp_path)
    cfg = replace(s["cfg"], work_url="https://takeout.google.com/manage/archive/" + ARCHIVE_ID + "?user=WRONG")
    s["cfg"] = cfg
    result = go(s)
    assert result.status == "failed" and "account mismatch" in result.error
    assert not s["harness"].mint_navigations()


@pytest.mark.parametrize("name", ["../escape.zip", "a%2fb.zip", "a%255cb.zip", "C:evil.zip", "NUL.zip"])
def test_unsafe_ledger_names_and_decoded_paths_refused(tmp_path, name):
    led = open_ledger(str(tmp_path / "db"))
    with pytest.raises(LedgerError):
        led.upsert_parts("x", [(0, name, 12)])
    led.close()


def test_unknown_size_stays_pending_without_transfer(tmp_path, monkeypatch):
    cfg, _ = seed(tmp_path)
    led = open_ledger(cfg.ledger_path)
    led.conn.execute("UPDATE parts SET size_expected=NULL")
    led.conn.commit()
    led.close()
    monkeypatch.setattr(runner, "download", lambda *a, **k: pytest.fail("no unknown-size download"))
    assert execute(cfg, factory=no_browser).status == "incomplete"
    assert row(cfg)["status"] == "pending"


def test_old_schema_migrates_without_losing_urls(tmp_path):
    cfg, _ = seed(tmp_path)
    db = sqlite3.connect(cfg.ledger_path)
    for column in ("sha256", "remote_path", "remote_verified_at"):
        db.execute("ALTER TABLE parts DROP COLUMN " + column)
    for column in ("expected_count", "user_id"):
        db.execute("ALTER TABLE jobs DROP COLUMN " + column)
    db.commit()
    db.close()
    led = open_ledger(cfg.ledger_path)
    assert led.part(cfg.archive_id, 0)["minted_url"] and led.part(cfg.archive_id, 0)["sha256"] is None
    led.close()


@pytest.mark.parametrize("field,value", [("size", 1), ("sha256", "b" * 64), ("remote_path", "wrong:job/part.zip")])
def test_claimed_remote_confirmation_requires_exact_digest_size_path(tmp_path, monkeypatch, field, value):
    cfg, _ = seed(tmp_path, staged=True)
    def false_receipt(source, remote_cfg, *, filename, expected_size, sha256):
        r = RemoteResult("uploaded", True, remote_cfg.destination + "/" + filename, expected_size, sha256)
        setattr(r, field, value)
        return r
    monkeypatch.setattr(runner, "archive_remote_part", false_receipt)
    assert execute(cfg, factory=no_browser).parts_done == 0
    assert (Path(cfg.staging_dir) / "part.zip").exists()
    assert not row(cfg)["remote_verified_at"]


def test_resume_headroom_uses_remaining_with_validator(tmp_path, monkeypatch):
    cfg, body = seed(tmp_path)
    cfg.staging_reserve = 7
    cfg.min_headroom = 11
    (Path(cfg.staging_dir) / "part.zip").write_bytes(body["part.zip"][:20])
    led = open_ledger(cfg.ledger_path)
    led.record_transfer(cfg.archive_id, 0, "transfer", "partial", size_on_disk=20, etag='"v1"')
    led.close()
    checks = []
    def guard(path, *, min_headroom=None):
        checks.append(min_headroom)
        if len(checks) == 2:
            raise MoveRefused("offline stop")
    monkeypatch.setattr(runner, "assert_staging_ready", guard)
    assert execute(cfg).status == "failed"
    assert checks == [11, len(body["part.zip"]) - 20 + 18]


def test_mint_only_cached_does_not_read_cookies_or_call_byte_completion(tmp_path):
    cfg, _ = seed(tmp_path)
    result = execute(replace(cfg, mint_only=True, cached_transfer_only=False), factory=no_browser)
    assert result.status == "incomplete" and result.parts_done == 0


def test_size_only_opaque_native_existing_file_stays_unadopted(tmp_path, monkeypatch):
    cfg, body = seed(tmp_path, {"clip.mp4": b"opaque movie payload"})
    cfg.archive_remote = None
    (Path(cfg.archive_dir) / "clip.mp4").write_bytes(body["clip.mp4"])
    client = FakeHttpClient([])
    # A network outage supplies no trustworthy source bytes; same size cannot adopt.
    result = execute(cfg, client)
    assert result.parts_done == 0 and result.status == "incomplete"


def test_legacy_unknown_count_cached_subset_never_claimed_complete(tmp_path, monkeypatch):
    cfg, _ = seed(tmp_path, staged=True)
    led = open_ledger(cfg.ledger_path)
    led.conn.execute("UPDATE jobs SET expected_count=NULL")
    led.conn.commit()
    led.close()
    backend(monkeypatch)
    result = execute(cfg, factory=no_browser)
    assert result.status == "incomplete" and result.parts_done == 1
    assert "Expected-count evidence is absent" in result.report_markdown
    led = open_ledger(cfg.ledger_path)
    assert led.job(cfg.archive_id)["expected_count"] is None
    led.close()


def test_unsafe_work_url_refused_without_browser_navigation(tmp_path):
    cfg, _ = seed(tmp_path)
    assert execute(replace(cfg, work_url="https://example.test/human-tab"), factory=no_browser).status == "failed"


def test_older_transfer_adapter_is_not_forced_to_accept_user_agent(tmp_path, monkeypatch):
    from autopilot.jar import CookieJar
    cfg, body = seed(tmp_path)
    async def jar(session):
        return CookieJar(header="SID=offline", user_agent="Mozilla/5.0 offline")
    async def old_download(client, url, dest, *, jar_header, expected_size, etag, expected_filename):
        Path(dest).write_bytes(body["part.zip"])
        return TransferResult("complete", len(body["part.zip"]), len(body["part.zip"]))
    monkeypatch.setattr(runner, "pull_jar", jar)
    monkeypatch.setattr(runner, "download", old_download)
    backend(monkeypatch)
    assert execute(cfg).status == "complete"


@pytest.mark.parametrize("status", ["partial", "complete"])
def test_full_size_failed_stream_quarantined_not_adopted_and_cached_retry(tmp_path, monkeypatch, status):
    cfg, body = seed(tmp_path)
    real_download = runner.download
    async def failed(client, url, dest, **kwargs):
        Path(dest).write_bytes(body["part.zip"])
        return TransferResult(status, len(body["part.zip"]), len(body["part.zip"]), etag='"v1"', error="failed stream at EOF")
    monkeypatch.setattr(runner, "download", failed)
    calls = backend(monkeypatch)
    assert execute(cfg).status == "incomplete" and not calls
    assert not (Path(cfg.staging_dir) / "part.zip").exists()
    assert list(Path(cfg.staging_dir).glob("part.zip.corrupt-*"))
    assert row(cfg)["minted_url"] and row(cfg)["etag"] == '"v1"'
    monkeypatch.setattr(runner, "download", real_download)
    assert execute(cfg, FakeHttpClient([FakeStream(200, {}, body["part.zip"])])).status == "complete"


def test_one_part_passes_trust_stable_receipts_without_quadratic_backend_reads(tmp_path, monkeypatch):
    cfg, _ = seed(tmp_path, {"a.zip": zipped(b"a"), "b.zip": zipped(b"b"), "c.zip": zipped(b"c")}, staged=True)
    cfg.max_parts = 1
    calls = backend(monkeypatch)
    monkeypatch.setattr(runner, "verify_remote_part", lambda *a, **k: pytest.fail("stable receipt must not trigger backend re-read"))
    for done in (1, 2, 3):
        result = execute(cfg, factory=no_browser)
        assert result.parts_done == done
    assert result.status == "complete" and len(calls) == 3
    assert execute(cfg, factory=no_browser).status == "complete" and len(calls) == 3
