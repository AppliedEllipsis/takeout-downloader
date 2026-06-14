"""
fixture_generator.py — builds BENIGN, structurally-realistic loader-chain
fixtures at runtime, so you can unit-test detection without ever storing or
shipping anything malicious.

=============================================================================
ELI5
=============================================================================
To test a smoke detector you don't burn the house down — you press the test
button. This module is that test button. It builds a fake "loader chain" that
*looks* like the real malware's shape (same filenames, same layout, same
encoding tricks, same scale) but whose payload does nothing except print a
harmless marker. Point your scanner at it; confirm it lights up.

=============================================================================
EXECUTIVE SUMMARY
=============================================================================
The real campaign ships a ZIP containing: a trusted runtime (renamed LuaJIT),
an obfuscated script, and a Launch.cmd. This generator reproduces that
STRUCTURE with safe stand-ins, AT REALISTIC SCALE so it trips the same
thresholds a production detector uses:
  * "runtime"  -> a text file with a fake-PE marker + LuaJIT banner string
                  (NOT an executable; starts with "MZ" but is inert text)
  * "script"   -> benign content run through the real encoding layers, padded
                  to thousands of \\NNN escapes and carrying the VM-bootstrap
                  trio (getfenv/setmetatable/newproxy) + shuffle markers, so it
                  matches the obfuscated-blob signature. Decodes only to:
                  print("BENIGN-FIXTURE-OK")
  * launcher   -> a .cmd with a REAL `start <runtime> <script>` line (the
                  interpreter-loader pattern), but the runtime is the inert
                  fixture, so running it does nothing harmful

NOTHING here executes, connects, or persists. Fixtures are generated into a
tmp dir at test time and are NOT committed.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from . import BENIGN_MARKER
from .obfuscation_lab import (
    deaddrop_encrypt,
    lua_decimal_escape,
)

# A benign stand-in for the obfuscated Lua payload. Real samples decode to a
# stealer; this decodes to a print statement.
BENIGN_LUA_SOURCE = f'print("{BENIGN_MARKER}")\n'

# A fake-PE marker. Starts with "MZ" so naive magic-byte checks see a PE, but
# it is plain text and 100% inert — it is NOT a real executable. We pad it out
# so its size is plausibly runtime-like while staying obviously a fixture.
FAKE_RUNTIME_BYTES = (
    b"MZ\x90\x00"
    + b"\x00" * 60
    + b"FAKE-LUAJIT-RUNTIME-FIXTURE-NOT-A-REAL-BINARY\n"
    + b"LuaJIT 2.1.0-beta3 -- Copyright (C) 2005-2017 Mike Pall. http://luajit.org/\n"
    + b"FIXTURE PADDING (inert): " + b"\x00" * 4096
)

# A launcher carrying the real interpreter-loader pattern. The EXE it names is
# the inert fixture runtime, so executing this does nothing dangerous — but it
# is structurally identical to the campaign's `start gcc.exe ptd.txt`.
def _launch_cmd(runtime_name: str) -> str:
    return (
        "@echo off\r\n"
        ":: BENIGN security-test fixture. The named EXE is an inert text file.\r\n"
        f"start {runtime_name} ptd.txt\r\n"
    )


def build_obfuscated_script_text() -> str:
    """
    Produce a benign script that mimics the visible texture AND scale of the
    real obfuscated Lua: thousands of \\NNN decimal escapes, a single very long
    line, the VM-bootstrap trio, and the documented table-shuffle range markers.
    Conceptually decodes to BENIGN_LUA_SOURCE.
    """
    escaped = lua_decimal_escape(BENIGN_LUA_SOURCE)
    # Repeat the escaped marker enough times to clear the 500-escape threshold
    # and the 50k-char single-line threshold a real detector keys on.
    bulk = escaped * 400  # BENIGN_LUA_SOURCE is ~20 bytes -> ~8000 escapes
    # Splice in the VM-bootstrap trio and shuffle markers the real loader shows.
    return (
        "return(function(...)"
        "local getfenv,setmetatable,newproxy=getfenv,setmetatable,newproxy "
        "local t={1064,658,659,1078} "
        'local s="' + bulk + '" '
        "return s end)(...)\n"
    )


def write_fixture_tree(dest: Path, runtime_name: str = "gcc.exe") -> dict:
    """
    Write the loose loader-chain files into `dest`. Returns a manifest dict.
    `runtime_name` lets you reproduce the documented variants
    (gcc.exe / luad.exe / luajit.exe / init.exe / vm_s390x.exe).
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    (dest / "ptd.txt").write_text(build_obfuscated_script_text(), encoding="utf-8")
    (dest / runtime_name).write_bytes(FAKE_RUNTIME_BYTES)
    (dest / "Launch.cmd").write_text(_launch_cmd(runtime_name), encoding="utf-8")

    return {
        "dir": str(dest),
        "runtime": runtime_name,
        "script": "ptd.txt",
        "launcher": "Launch.cmd",
    }


def build_fixture_zip(runtime_name: str = "gcc.exe",
                      inner_dir: str = "innkeeper") -> bytes:
    """
    Build a ZIP in memory matching the campaign's layout: a single oddly-named
    inner directory containing the loader chain. Returns raw zip bytes.
    `inner_dir` mimics the campaign's rare-word directory names.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{inner_dir}/ptd.txt", build_obfuscated_script_text())
        z.writestr(f"{inner_dir}/{runtime_name}", FAKE_RUNTIME_BYTES)
        z.writestr(f"{inner_dir}/Launch.cmd", _launch_cmd(runtime_name))
    return buf.getvalue()


def build_encoded_deaddrop() -> dict:
    """
    Produce a benign blob run through the real 4-layer chain
    (hex -> XOR -> base64url -> AES-ECB), mirroring the GitHub dead-drop second
    stage. Returns the blob plus the key material needed to decode it, so a
    unit test can round-trip it.
    """
    aes_key = b"0123456789abcdef"
    xor_key = b"fixturexor"
    plaintext = BENIGN_LUA_SOURCE
    blob_hex = deaddrop_encrypt(plaintext.encode(), aes_key, xor_key)
    return {
        "blob_hex": blob_hex,
        "aes_key": aes_key,
        "xor_key": xor_key,
        "plaintext": plaintext,
    }


if __name__ == "__main__":
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="fakegit_fixture_"))
    manifest = write_fixture_tree(d)
    print("wrote fixture tree:", manifest)
    print("zip bytes:", len(build_fixture_zip()))
    print("deaddrop keys:", list(build_encoded_deaddrop().keys()))
