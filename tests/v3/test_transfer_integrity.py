"""Integrity release regressions: only synthetic bytes and localhost HTTP."""
import asyncio
import builtins
import gzip
import hashlib
import io
import socket
import tarfile
import threading
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autopilot.errors import NeedsReauth
from autopilot.transport import download, RemoteChanged, TransferError, UrllibHttpClient
from autopilot.verify import verify_local
from fake_http import FakeHttpClient, FakeStream

URL = 'https://takeout-download.usercontent.google.com/part?token=SECRET&rapt=RAPT'
ETAG = '"stable"'


def transfer(client, path, **kw):
    return asyncio.run(download(client, URL, str(path), jar_header='SID=synthetic', **kw))


def tar_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        info = tarfile.TarInfo('member')
        info.size = 1000
        tar.addfile(info, io.BytesIO(b'x' * info.size))
    return buf.getvalue()


@pytest.mark.parametrize('name,data', [
    ('opaque.zip', b'opaque'), ('bad.tgz', gzip.compress(b'not tar')),
    ('wrong.zip', gzip.compress(tar_bytes())), ('wrong.tgz', b'PK\x03\x04opaque'),
    ('bad.tar', b'not tar'), ('cut.tar', tar_bytes()[:700]),
    ('crc.tgz', gzip.compress(tar_bytes())[:-8] + b'\0' * 8),
    ('cut.gz', gzip.compress(b'payload')[:-5]),
], ids=['opaque-zip','bad-tgz','wrong-zip','wrong-tgz','bad-tar','cut-tar','crc-tgz','cut-gz'])
def test_declared_archive_must_validate(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    out = verify_local(str(p), size_expected=len(data))
    assert not out.ok and out.state == 'CORRUPT', out


@pytest.mark.parametrize('name,data', [('valid.tgz', gzip.compress(tar_bytes())),
    ('valid.tar.gz', gzip.compress(tar_bytes())), ('valid.tar', tar_bytes()),
    ('valid.gz', gzip.compress(b'payload'))], ids=['tgz','tar-gz','tar','gz'])
def test_valid_archives(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    assert verify_local(str(p), size_expected=len(data)).ok


def test_loose_html_distinguished_from_login(tmp_path):
    p = tmp_path / 'document.html'
    p.write_bytes(b'<html><body>My exported document</body></html>')
    assert verify_local(str(p), size_expected=p.stat().st_size).ok
    p.write_bytes(b'<!doctype html><title>Sign in</title>')
    assert not verify_local(str(p), size_expected=p.stat().st_size).ok


def test_unknown_loose_bytes_need_trusted_evidence(tmp_path):
    p = tmp_path / 'loose.bin'
    p.write_bytes(b'bytes')
    assert not verify_local(str(p)).ok
    assert not verify_local(str(p), hash_check=True).ok
    out = verify_local(str(p), sha256_expected=hashlib.sha256(b'bytes').hexdigest())
    assert out.ok and out.sha256


def test_hash_second_open_failure_is_not_ok(tmp_path, monkeypatch):
    p = tmp_path / 'hash.bin'
    p.write_bytes(b'bytes')
    real_open = builtins.open
    calls = 0
    def flaky(*a, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('injected hash read failure')
        return real_open(*a, **kw)
    monkeypatch.setattr(builtins, 'open', flaky)
    out = verify_local(str(p), size_expected=5, hash_check=True)
    assert not out.ok and out.state == 'UNVERIFIED' and out.sha256 is None


@pytest.mark.parametrize('exc', [zlib.error('bad deflate'), RuntimeError('encrypted'),
                                NotImplementedError('compression'), ValueError('parser')])
def test_zip_parser_errors_are_verdicts(tmp_path, monkeypatch, exc):
    p = tmp_path / 'part.zip'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('member', b'payload')
    def fail(self):
        raise exc
    monkeypatch.setattr(zipfile.ZipFile, 'testzip', fail)
    out = verify_local(str(p), size_expected=p.stat().st_size)
    assert not out.ok and out.state == 'CORRUPT'


def test_zip_crc_cannot_be_disabled(tmp_path):
    p = tmp_path / 'part.zip'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('member', b'PAYLOAD')
    data = p.read_bytes().replace(b'PAYLOAD', b'CORRUPT')
    p.write_bytes(data)
    assert not verify_local(str(p), size_expected=len(data), crc_check=False).ok


@pytest.mark.parametrize('headers,payload,expected,complete', [
    ({'Content-Length': '32'}, b'x' * 16, None, False),
    ({'Content-Length': '16'}, b'x' * 16, None, True),
    ({}, b'x' * 16, None, False),
    ({}, b'x' * 24, 16, False),
])
def test_transport_eof_needs_size_and_no_error(tmp_path, headers, payload, expected, complete):
    p = tmp_path / 'part'
    out = transfer(FakeHttpClient([FakeStream(200, headers, payload)]), p,
                   expected_size=expected, chunk=8)
    assert out.complete is complete


@pytest.mark.parametrize('headers', [{'Content-Length': 'junk'}, {'Content-Length': '-1'},
                                     {'Content-Length': '17'}])
def test_bad_declared_length_rejected_before_write(tmp_path, headers):
    p = tmp_path / 'part'
    with pytest.raises(RemoteChanged):
        transfer(FakeHttpClient([FakeStream(200, headers, b'x' * 16)]), p, expected_size=16)
    assert not p.exists()


@pytest.mark.parametrize('failure', ['reset', 'overrun'])
def test_tainted_full_length_cannot_shortcut_next_attempt(tmp_path, failure):
    p = tmp_path / 'part'
    class Reset(FakeStream):
        async def read(self, n):
            if self._pos == 16:
                raise ConnectionResetError('reset ' + URL)
            return await super().read(n)
    stream = Reset(200, {'ETag': ETAG}, b'x' * 16) if failure == 'reset' else FakeStream(200, {'ETag': ETAG}, b'x' * 24)
    out = transfer(FakeHttpClient([stream]), p, expected_size=16, chunk=8, rewind=0)
    assert not out.complete and out.error and 'SECRET' not in out.error
    assert out.etag == ETAG and 0 < p.stat().st_size < 16
    start = p.stat().st_size
    client = FakeHttpClient([FakeStream(206, {'ETag': ETAG,
        'Content-Range': f'bytes {start}-15/16'}, b'x' * (16-start))])
    out2 = transfer(client, p, expected_size=16, etag=out.etag, rewind=0)
    assert out2.status == 'complete' and client.requests
    assert p.read_bytes() == b'x' * 16


@pytest.mark.parametrize('status,headers', [(401, {}), (403, {}),
    (302, {'Location': 'https://user:PASSWORD@elsewhere/path?token=SECRET&rapt=RAPT'}),
    (302, {'Location': 'https://accounts.google.com/signin?rapt=RAPT&token=SECRET'})])
def test_exposed_http_errors_redact_secrets(tmp_path, status, headers):
    with pytest.raises((NeedsReauth, TransferError)) as e:
        transfer(FakeHttpClient([FakeStream(status, headers, b'')]), tmp_path / 'part')
    for secret in ('SECRET', 'RAPT', 'PASSWORD', '?token=', 'user:'):
        assert secret not in str(e.value)


def test_client_failure_redacted(tmp_path):
    class Fail:
        async def get(self, url, headers):
            raise OSError(url + ' token=SECRET rapt=RAPT')
    out = transfer(Fail(), tmp_path / 'part')
    assert not out.complete and 'SECRET' not in out.error and 'RAPT' not in out.error


def test_urllib_interrupted_stream_then_strong_resume(tmp_path):
    data = bytes(range(256)) * 4
    requests = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(dict(self.headers))
            if len(requests) == 1:
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('ETag', ETAG)
                self.end_headers()
                self.wfile.write(data[:512])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            else:
                start = int(self.headers['Range'].split('=')[1].split('-')[0])
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{len(data)-1}/{len(data)}')
                self.send_header('Content-Length', str(len(data)-start))
                self.send_header('ETag', ETAG)
                self.end_headers()
                self.wfile.write(data[start:])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    p = tmp_path / 'part'
    url = f'http://127.0.0.1:{server.server_port}/part?token=synthetic'
    try:
        async def attempt(etag=None):
            return await download(UrllibHttpClient(timeout=2), url, str(p),
                jar_header='SID=synthetic', etag=etag, rewind=64, chunk=64,
                host_validator=lambda host: host == '127.0.0.1')
        first = asyncio.run(attempt())
        assert first.status == 'partial' and first.etag == ETAG
        assert p.read_bytes() == data[:512]
        second = asyncio.run(attempt(first.etag))
        assert second.complete and p.read_bytes() == data
        assert requests[1]['If-Range'] == ETAG and requests[1]['Range'] == 'bytes=448-'
        assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize('name', ['trailer.tar', 'trailer.tgz'])
def test_tar_rejects_garbage_after_members(tmp_path, name):
    data = bytearray(tar_bytes())
    data[2048:2055] = b'garbage'
    p = tmp_path / name
    p.write_bytes(gzip.compress(data) if name.endswith('tgz') else data)
    assert not verify_local(str(p), size_expected=p.stat().st_size).ok


def test_close_failure_is_partial_not_trace_or_success(tmp_path):
    class CloseFailure(FakeStream):
        async def aclose(self):
            raise OSError('close failed ' + URL)
    p = tmp_path / 'part'
    out = transfer(FakeHttpClient([CloseFailure(200, {'ETag': ETAG}, b'x' * 16)]),
                   p, expected_size=16, chunk=8, rewind=0)
    assert not out.complete and 'SECRET' not in out.error
    assert 0 < p.stat().st_size < 16 and out.etag == ETAG


def test_unknown_size_content_range_is_authoritative(tmp_path):
    p = tmp_path / 'part'
    p.write_bytes(b'x' * 8)
    out = transfer(FakeHttpClient([FakeStream(206, {'ETag': ETAG,
        'Content-Range': 'bytes 8-15/16'}, b'x' * 8)]), p, etag=ETAG, rewind=0)
    assert out.complete and p.read_bytes() == b'x' * 16


def test_invalid_response_preserves_existing_partial(tmp_path):
    p = tmp_path / 'part'
    p.write_bytes(b'prefix' * 8)
    with pytest.raises(RemoteChanged):
        transfer(FakeHttpClient([FakeStream(206, {'ETag': ETAG,
            'Content-Range': 'bytes 32-64/64'}, b'x' * 33)]), p,
            expected_size=64, etag=ETAG, rewind=16)
    assert p.read_bytes() == b'prefix' * 8


def test_html_mentioning_signin_is_still_loose_export(tmp_path):
    p = tmp_path / 'document.html'
    p.write_bytes(b'<html><title>My document</title><body>Instructions to sign in</body></html>')
    assert verify_local(str(p), size_expected=p.stat().st_size).ok


def test_cancellation_cannot_leave_full_length_shortcut(tmp_path):
    p = tmp_path / 'part'
    class Cancel(FakeStream):
        async def read(self, n):
            if self._pos == 16:
                raise asyncio.CancelledError()
            return await super().read(n)
    with pytest.raises(asyncio.CancelledError):
        transfer(FakeHttpClient([Cancel(200, {}, b'x' * 16)]), p,
                 expected_size=16, chunk=8, rewind=0)
    assert p.read_bytes() == b'x' * 15
