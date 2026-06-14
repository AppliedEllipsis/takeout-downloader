"""
obfuscation_lab.py — the encoding/encryption techniques used by the FakeGit /
LuaJIT SmartLoader campaign, implemented over BENIGN data only.

=============================================================================
ELI5
=============================================================================
Malware hides its real instructions by wrapping them in layers of "secret
writing" — first turn letters into numbers, then scramble with a password,
then re-spell using a custom alphabet. To read it you peel the layers in
reverse. This file knows how to wrap AND unwrap each layer, so you can learn
the trick and test your detector — but we only ever wrap the harmless word
BENIGN_TEST_MARKER, never a real payload.

=============================================================================
EXECUTIVE SUMMARY
=============================================================================
Layers documented and round-tripped here (encode + decode):

  1. lua_decimal_escape   Lua source string obfuscation: "A" -> "\\065".
                          The campaign's ptd.txt is full of these.
  2. custom_b64           base64 with a SCRAMBLED alphabet (the campaign's
                          documented alphabet is included as a constant for
                          recognition; encoding uses a benign demo alphabet).
  3. xor_cipher           repeating-key XOR (cheap, keyless-looking, common
                          first wrapping layer).
  4. deaddrop_chain       the documented 4-layer second-stage chain:
                          hex -> XOR -> base64url -> AES-ECB.
                          We implement the full inverse so you can prove you
                          understand it, over benign bytes.

Everything is reversible and unit-tested in tests/test_security.py.
No payloads, no network, no execution.
"""

from __future__ import annotations

import base64
import binascii

# Standard RFC 4648 alphabet, for reference / building the scrambled one.
_STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

# The ACTUAL custom alphabet documented for this campaign (FakeGit writeup).
# Present here ONLY as a recognition signature for the detector — we do not
# need it to encode anything.
CAMPAIGN_CUSTOM_ALPHABET = "L07ker/2Jn4Z+b6y8hfXYVpTiDCvQKc3qRjUEBdo9MzHxtGNgWuA1FSmlaPO5wIs"

# A harmless demo alphabet (also a permutation of the standard set) used for
# our own round-trip demos so we never normalize against the real one.
DEMO_CUSTOM_ALPHABET = "ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgfedcba9876543210+/"


# --------------------------------------------------------------------------- #
# Layer 1: Lua decimal escape sequences  ("A" <-> "\065")
# --------------------------------------------------------------------------- #
def lua_decimal_escape(text: str) -> str:
    """Encode a string as Lua \\NNN decimal escapes (what ptd.txt uses)."""
    return "".join(f"\\{b:03d}" for b in text.encode("utf-8"))


def lua_decimal_unescape(encoded: str) -> str:
    """Decode Lua \\NNN decimal escapes back to text."""
    import re

    nums = re.findall(r"\\(\d{1,3})", encoded)
    return bytes(int(n) for n in nums).decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Layer 2: custom-alphabet base64
# --------------------------------------------------------------------------- #
def custom_b64_encode(data: bytes, alphabet: str = DEMO_CUSTOM_ALPHABET) -> str:
    """base64-encode then remap standard alphabet -> custom alphabet."""
    if len(set(alphabet)) != 64:
        raise ValueError("alphabet must be 64 unique chars")
    std = base64.b64encode(data).decode("ascii").rstrip("=")
    table = {s: c for s, c in zip(_STD_B64, alphabet)}
    return "".join(table[ch] for ch in std)


def custom_b64_decode(text: str, alphabet: str = DEMO_CUSTOM_ALPHABET) -> bytes:
    """Inverse of custom_b64_encode."""
    table = {c: s for s, c in zip(_STD_B64, alphabet)}
    std = "".join(table[ch] for ch in text)
    pad = "=" * (-len(std) % 4)
    return base64.b64decode(std + pad)


# --------------------------------------------------------------------------- #
# Layer 3: repeating-key XOR
# --------------------------------------------------------------------------- #
def xor_cipher(data: bytes, key: bytes) -> bytes:
    """Symmetric repeating-key XOR (encode == decode)."""
    if not key:
        raise ValueError("key must be non-empty")
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


# --------------------------------------------------------------------------- #
# Layer 4: the documented dead-drop chain  hex -> XOR -> base64url -> AES-ECB
# --------------------------------------------------------------------------- #
# Encryption order to PRODUCE the blob (so decryption is the reverse):
#   plaintext
#     -> AES-ECB encrypt        (PKCS7 padded)
#     -> base64url encode
#     -> XOR with key
#     -> hex encode             => stored blob
#
# Decryption (what the loader does) reverses it:
#   blob -> hex decode -> XOR -> base64url decode -> AES-ECB decrypt
def _aes_ecb():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher, algorithms, modes


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("bad PKCS7 padding")
    return data[:-n]


def deaddrop_encrypt(plaintext: bytes, aes_key: bytes, xor_key: bytes) -> str:
    """Produce a dead-drop blob using the campaign's documented layer stack."""
    if len(aes_key) not in (16, 24, 32):
        raise ValueError("aes_key must be 16/24/32 bytes")
    Cipher, algorithms, modes = _aes_ecb()
    enc = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    ct = enc.update(_pkcs7_pad(plaintext)) + enc.finalize()
    b64u = base64.urlsafe_b64encode(ct)
    xored = xor_cipher(b64u, xor_key)
    return binascii.hexlify(xored).decode("ascii")


def deaddrop_decrypt(blob_hex: str, aes_key: bytes, xor_key: bytes) -> bytes:
    """Reverse deaddrop_encrypt — the exact peel order a real loader uses."""
    xored = binascii.unhexlify(blob_hex)
    b64u = xor_cipher(xored, xor_key)
    ct = base64.urlsafe_b64decode(b64u)
    Cipher, algorithms, modes = _aes_ecb()
    dec = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
    return _pkcs7_unpad(dec.update(ct) + dec.finalize())


# --------------------------------------------------------------------------- #
# Demo / self-check
# --------------------------------------------------------------------------- #
def _demo() -> None:
    from . import BENIGN_MARKER

    print("=== obfuscation_lab self-check (benign only) ===")
    esc = lua_decimal_escape(BENIGN_MARKER)
    print("lua escape:", esc[:40], "...")
    assert lua_decimal_unescape(esc) == BENIGN_MARKER

    cb = custom_b64_encode(BENIGN_MARKER.encode())
    print("custom b64:", cb)
    assert custom_b64_decode(cb) == BENIGN_MARKER.encode()

    x = xor_cipher(BENIGN_MARKER.encode(), b"key")
    assert xor_cipher(x, b"key").decode() == BENIGN_MARKER

    blob = deaddrop_encrypt(BENIGN_MARKER.encode(), b"0" * 16, b"xorkey")
    print("deaddrop blob:", blob)
    assert deaddrop_decrypt(blob, b"0" * 16, b"xorkey").decode() == BENIGN_MARKER
    print("ALL LAYERS ROUND-TRIP OK")


if __name__ == "__main__":
    _demo()
