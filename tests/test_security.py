#!/usr/bin/env python3
"""
Tests for the `security/` defensive kit.

These tests prove three things:
  1. The obfuscation/encryption layers round-trip (you understand the encoding).
  2. The static detector fires on realistic loader-chain fixtures and stays
     quiet on clean files (low false-positive rate).
  3. The dynamic sandbox is genuinely contained (no FFI, no network) and the
     blockchain C2 decoder is correct — both WITHOUT executing malware.

Everything operates on BENIGN, generated fixtures. Nothing malicious is stored
in the repo; fixtures are built in a tmp dir at test time.

Run:  pytest tests/test_security.py -v
"""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security import obfuscation_lab as lab  # noqa: E402
from security import detector  # noqa: E402
from security import fixture_generator as fix  # noqa: E402
from security import c2_resolver  # noqa: E402
from security import sandbox  # noqa: E402
from security import BENIGN_MARKER  # noqa: E402


def _max_score(findings):
    return max((f.score for f in findings), default=0)


def _all_reasons(findings):
    return [r for f in findings for r in f.reasons]


# --------------------------------------------------------------------------- #
# 1. Obfuscation / encryption layers round-trip
# --------------------------------------------------------------------------- #
def test_lua_decimal_escape_roundtrip():
    enc = lab.lua_decimal_escape(BENIGN_MARKER)
    assert "\\066" in enc  # 'B'
    assert lab.lua_decimal_unescape(enc) == BENIGN_MARKER


def test_custom_b64_roundtrip():
    data = BENIGN_MARKER.encode()
    enc = lab.custom_b64_encode(data)
    assert lab.custom_b64_decode(enc) == data


def test_custom_b64_rejects_bad_alphabet():
    with pytest.raises(ValueError):
        lab.custom_b64_encode(b"x", alphabet="too-short")


def test_xor_is_symmetric():
    ct = lab.xor_cipher(BENIGN_MARKER.encode(), b"key")
    assert ct != BENIGN_MARKER.encode()
    assert lab.xor_cipher(ct, b"key").decode() == BENIGN_MARKER


def test_deaddrop_chain_roundtrip():
    blob = lab.deaddrop_encrypt(BENIGN_MARKER.encode(), b"0" * 16, b"xorkey")
    # blob is hex text — the outermost documented layer
    assert all(c in "0123456789abcdef" for c in blob)
    out = lab.deaddrop_decrypt(blob, b"0" * 16, b"xorkey")
    assert out.decode() == BENIGN_MARKER


def test_deaddrop_rejects_bad_aes_key():
    with pytest.raises(ValueError):
        lab.deaddrop_encrypt(b"x", b"shortkey", b"xor")


# --------------------------------------------------------------------------- #
# 2. Static detector: true positives on fixtures, quiet on clean input
# --------------------------------------------------------------------------- #
def test_detector_flags_fixture_tree(tmp_path):
    fix.write_fixture_tree(tmp_path, runtime_name="gcc.exe")
    findings = detector.scan_dir(tmp_path)
    detected, _ = detector.loader_chain_verdict(findings)
    assert detected, "should detect the runtime+script+launcher triad"
    assert _max_score(findings) >= 70


def test_detector_flags_each_documented_runtime_name(tmp_path):
    for i, name in enumerate(
        ["gcc.exe", "luad.exe", "luajit.exe", "init.exe", "vm_s390x.exe"]
    ):
        d = tmp_path / f"v{i}"
        fix.write_fixture_tree(d, runtime_name=name)
        findings = detector.scan_dir(d)
        detected, _ = detector.loader_chain_verdict(findings)
        assert detected, f"missed triad with runtime {name}"


def test_detector_quiet_on_clean_tree(tmp_path):
    (tmp_path / "hello.py").write_text("print('hello world')\n")
    (tmp_path / "README.md").write_text("# A normal project\n")
    (tmp_path / "data.json").write_text('{"ok": true}\n')
    findings = detector.scan_dir(tmp_path)
    detected, _ = detector.loader_chain_verdict(findings)
    assert not detected
    assert _max_score(findings) < 40


def test_detector_recognizes_known_bad_hash(tmp_path):
    # We can't ship the real binary, so synthesize a file whose SHA256 we
    # inject into the known-bad set for this test only.
    f = tmp_path / "thing.bin"
    f.write_bytes(b"unit-test-only content")
    import hashlib

    h = hashlib.sha256(f.read_bytes()).hexdigest()
    detector.KNOWN_BAD_SHA256[h] = "unit-test marker"
    try:
        findings = detector.scan_file(f)
        reasons = _all_reasons(findings)
        assert any("known-bad" in r.lower() or "unit-test" in r.lower()
                   for r in reasons)
    finally:
        del detector.KNOWN_BAD_SHA256[h]


def test_detector_flags_fake_pe_runtime(tmp_path):
    f = tmp_path / "gcc.exe"
    f.write_bytes(fix.FAKE_RUNTIME_BYTES)
    findings = detector.scan_file(f)
    # MZ header + LuaJIT string + .exe name = suspicious runtime
    assert _max_score(findings) > 0


# --------------------------------------------------------------------------- #
# 3. Dead-drop fixture decodes through the real chain
# --------------------------------------------------------------------------- #
def test_fixture_deaddrop_roundtrip():
    d = fix.build_encoded_deaddrop()
    out = lab.deaddrop_decrypt(d["blob_hex"], d["aes_key"], d["xor_key"])
    assert out.decode() == d["plaintext"]


def test_fixture_zip_layout():
    raw = fix.build_fixture_zip(runtime_name="luad.exe", inner_dir="innkeeper")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
    assert "innkeeper/ptd.txt" in names
    assert "innkeeper/luad.exe" in names
    assert "innkeeper/Launch.cmd" in names


# --------------------------------------------------------------------------- #
# 4. Blockchain C2 decoder (offline) is correct
# --------------------------------------------------------------------------- #
def test_c2_decode_string_result():
    ip = "http://89.169.12.241"
    body = ip.encode().hex()
    encoded = (
        "0x"
        + "20".rjust(64, "0")
        + format(len(ip), "x").rjust(64, "0")
        + body.ljust(64, "0")
    )
    assert c2_resolver.decode_string_result(encoded) == ip


def test_c2_encode_eth_call_shape():
    body = c2_resolver.encode_eth_call("0xabc", "0xdeadbeef")
    assert body["method"] == "eth_call"
    assert body["params"][0]["to"] == "0xabc"
    assert body["params"][0]["data"] == "0xdeadbeef"
    assert body["params"][1] == "latest"


def test_c2_known_contracts_present():
    assert "v2" in c2_resolver.CONTRACTS
    assert c2_resolver.CONTRACTS["v2"]["selector"] == "0x3bc5de30"


# --------------------------------------------------------------------------- #
# 5. Sandbox is genuinely contained (skips cleanly if lupa absent)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not sandbox.lupa_available(), reason="lupa not installed")
def test_sandbox_containment_is_clean():
    lua, _log = sandbox.make_safe_runtime()
    problems = sandbox.verify_containment(lua)
    assert problems == [], f"containment violations: {problems}"


@pytest.mark.skipif(not sandbox.lupa_available(), reason="lupa not installed")
def test_sandbox_records_load_attempts_without_executing():
    # A benign self-decoding shape: calls load() on a string.
    src = 'return load("print(1)")'
    log = sandbox.safe_decode_only(src)
    # load was intercepted and recorded, not actually compiled/run.
    assert any(name == "load" for name, _ in log.calls)
