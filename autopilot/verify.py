"""Local integrity verification for archive parts."""
from __future__ import annotations
import gzip
import hashlib
import os
import re
import tarfile
import zipfile
import zlib
from dataclasses import dataclass
from typing import Optional
from takeout2.contracts import VerifyState
from takeout2.verify import scan_parts_dir
from .errors import AutopilotError

__all__ = ["VerifyOutcome", "verify_local", "verify_dir", "verify_state_ok", "verify_state_resumable"]
_OK = {VerifyState.SIZE_OK, VerifyState.STRUCT_OK, VerifyState.HASH_OK}
_RESUMABLE = {VerifyState.UNVERIFIED}

@dataclass
class VerifyOutcome:
    ok: bool
    state: str
    detail: str
    size: int
    resumable: bool
    sha256: Optional[str] = None
    @property
    def corrupt(self) -> bool:
        return not self.ok and not self.resumable

def verify_state_ok(state) -> bool:
    return state in _OK

def verify_state_resumable(state) -> bool:
    return state in _RESUMABLE

def _out(state, detail, size, digest=None):
    return VerifyOutcome(state in _OK, state.name if hasattr(state, "name") else str(state), detail,
                         size, state in _RESUMABLE, digest)


def _validate_tar(path, compressed=False):
    # Consume every decompressed byte: gzip CRC/ISIZE checks happen only at EOF.
    # Retain only the TAR terminator, never the full decompressed payload.
    opener = gzip.open if compressed else open
    total, tail = 0, b""
    with opener(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            total += len(block)
            tail = (tail + block)[-1024:]
    if total < 1024 or total % 512 or tail != b"\0" * 1024:
        raise tarfile.ReadError("missing TAR end blocks or truncated record")
    with opener(path, "rb") as fh, tarfile.open(fileobj=fh, mode="r|") as tf:
        for member in tf:
            if member.isfile():
                with tf.extractfile(member) as contents:
                    for _ in iter(lambda: contents.read(8 << 20), b""):
                        pass
        # tarfile stops at the first zero OR invalid subsequent header. Verify
        # the entire remainder is genuinely zero padding, not ignored garbage.
        if total - tf.offset < 1024:
            raise tarfile.ReadError("missing TAR end blocks")
        fh.seek(tf.offset)
        for block in iter(lambda: fh.read(8 << 20), b""):
            if block.strip(b"\0"):
                raise tarfile.ReadError("invalid TAR trailing records")


def verify_local(path: str, *, size_expected: Optional[int] = None,
                 hash_check: bool = False, sha256_expected: Optional[str] = None,
                 crc_check: bool = True) -> VerifyOutcome:
    """Validate declared archives fully; loose parts require size/trusted digest.

    ``crc_check`` remains for caller compatibility but cannot disable ZIP CRCs.
    Computing a hash alone is not trusted evidence for unknown loose data.
    """
    try:
        size = os.path.getsize(path)
        if os.path.isdir(path):
            return _out(VerifyState.CORRUPT, "path is a directory", 0)
    except FileNotFoundError:
        return _out(VerifyState.UNVERIFIED, "file not present", 0)
    except OSError as exc:
        raise AutopilotError(f"cannot verify {path!r}: {exc}") from exc
    if size == 0:
        return _out(VerifyState.CORRUPT, "zero-length file", size)
    if size_expected is not None:
        if size < size_expected:
            return _out(VerifyState.UNVERIFIED, f"incomplete: {size}/{size_expected} bytes - resumable", size)
        if size > size_expected:
            return _out(VerifyState.CORRUPT, f"oversized: {size} > expected {size_expected}", size)
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError as exc:
        return _out(VerifyState.UNVERIFIED, f"read failed: {exc}", size)
    low = head.lstrip().lower()
    html = low.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in low[:256]
    extension = os.fspath(path).lower()
    # HTML exports are legitimate. Reject identifiable login/error documents or
    # HTML masquerading as media/archive bytes, not all loose HTML.
    if html and (not extension.endswith((".html", ".htm")) or re.search(
            rb"<title[^>]*>[^<]*(?:sign[ -]?in|error|forbidden|access denied)|"
            rb"servicelogin|accounts\.google\.com|<input[^>]+(?:password|passwd)|quota exceeded",
            low, re.I)):
        return _out(VerifyState.CORRUPT, "HTML login/error document", size)
    is_zip = head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    is_gzip = head.startswith(b"\x1f\x8b")
    declared_zip = extension.endswith(".zip")
    declared_tgz = extension.endswith((".tgz", ".tar.gz"))
    declared_gzip = declared_tgz or extension.endswith((".gz", ".gzip"))
    declared_tar = extension.endswith(".tar")
    if (declared_zip and not is_zip) or (declared_gzip and not is_gzip) or (declared_tar and (is_zip or is_gzip)):
        return _out(VerifyState.CORRUPT, "declared archive format does not match bytes", size)
    try:
        if is_zip:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    return _out(VerifyState.CORRUPT, f"CRC mismatch in ZIP member {bad!r}", size)
            state, detail = VerifyState.STRUCT_OK, "ZIP central directory and member CRCs valid"
        elif declared_tgz or declared_tar:
            _validate_tar(path, compressed=declared_tgz)
            state, detail = VerifyState.STRUCT_OK, "TAR structure and full stream valid"
        elif is_gzip:
            with gzip.open(path, "rb") as fh:
                for _ in iter(lambda: fh.read(8 << 20), b""):
                    pass
            state, detail = VerifyState.STRUCT_OK, "full gzip stream and checksum valid"
        elif size_expected is not None:
            state, detail = VerifyState.SIZE_OK, "known-size loose part"
        else:
            state, detail = VerifyState.UNVERIFIED, "unknown-size loose format needs trusted digest"
    except (zipfile.BadZipFile, tarfile.TarError, zlib.error, EOFError, OSError,
            RuntimeError, NotImplementedError, ValueError, OverflowError) as exc:
        return _out(VerifyState.CORRUPT, f"invalid archive: {type(exc).__name__}: {exc}", size)
    digest = None
    if hash_check or sha256_expected:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                    h.update(block)
        except OSError as exc:
            return _out(VerifyState.UNVERIFIED, f"hash read failed: {exc}", size)
        digest = h.hexdigest()
        if sha256_expected and digest.lower() != sha256_expected.lower():
            return _out(VerifyState.CORRUPT, "sha256 mismatch", size, digest)
        if sha256_expected or state in _OK:
            state = VerifyState.HASH_OK
        detail += "; sha256 computed" + (" and matched trusted expectation" if sha256_expected else " (not compared)")
    return _out(state, detail, size, digest)


def verify_dir(directory: str) -> dict:
    if not os.path.isdir(directory):
        return {}
    return scan_parts_dir(directory)
