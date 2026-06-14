"""
c2_resolver.py — READ-ONLY resolver for blockchain-hosted C2 ("EtherHiding").

=============================================================================
ELI5
=============================================================================
Modern malware sometimes hides its "where do I phone home" address on a public
blockchain instead of in the file. That way the criminal can change the
address any time without re-shipping the virus. The good news: reading the
blockchain is public and harmless — like looking up a phone number in a
directory. This module looks up that number WITHOUT ever dialing it or running
any malware.

=============================================================================
EXECUTIVE SUMMARY
=============================================================================
The FakeGit loader resolves its C2 by calling a getter on a Polygon Mainnet
smart contract (`getDomain()` / `getData()`). This is the EtherHiding
technique. Reading the stored value is a single `eth_call` against any public
Polygon RPC endpoint — no transaction, no gas, no malware execution.

Use this to enrich a report with the *current* attacker IP, or to unit-test
your own EtherHiding detection logic against a deterministic offline decoder.

SAFETY: this performs an outbound HTTPS request to a public RPC endpoint ONLY
if you call `resolve_live()`. The pure-decode helpers (`encode_eth_call`,
`decode_string_result`) are fully offline and are what the tests exercise.
"""

from __future__ import annotations

import json
import urllib.request

# Documented campaign contracts (Polygon Mainnet). Source: published intel.
CONTRACTS = {
    "v1": {
        "address": "0xd68910ED4D4A5A9bAdF9ec95604CAE0f3378479B",
        "selector": "0xb68d1809",  # getDomain()
        "note": "FakeGit V1 loader contract",
    },
    "v1_alt": {
        "address": "0x2cbd2464dd749f5c0034fc9cddc6db2d53dea400",
        "selector": "0xb68d1809",  # getDomain()
        "note": "FakeGit V1 alternate contract",
    },
    "v2": {
        "address": "0x1823A9a0Ec8e0C25dD957D0841e3D41a4474bAdc",
        "selector": "0x3bc5de30",  # getData()
        "note": "FakeGit V2 loader contract",
    },
}

PUBLIC_POLYGON_RPC = "https://polygon-rpc.com"


def encode_eth_call(address: str, selector: str) -> dict:
    """Build the JSON-RPC body for a read-only eth_call. Pure/offline."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": address, "data": selector}, "latest"],
    }


def decode_string_result(hex_result: str) -> str:
    """
    Decode an ABI-encoded `string` return value (the format Solidity getters
    use) into text. Pure/offline — this is what the unit tests verify.

    Layout: 0x | 32-byte offset | 32-byte length | UTF-8 bytes (padded).
    """
    h = hex_result[2:] if hex_result.startswith("0x") else hex_result
    if len(h) < 128:
        return ""
    raw = bytes.fromhex(h)
    # word 0 = offset (usually 0x20), word 1 = byte length
    length = int.from_bytes(raw[32:64], "big")
    data = raw[64:64 + length]
    return data.decode("utf-8", errors="replace")


def resolve_live(which: str = "v2", rpc_url: str = PUBLIC_POLYGON_RPC,
                 timeout: int = 15) -> str:
    """
    Perform a live, read-only lookup of the current C2 value. This makes ONE
    outbound HTTPS request to a public RPC node. It executes NO malware and
    sends NO traffic to the attacker. Network-gated on purpose: nothing here
    runs unless you explicitly call it.
    """
    if which not in CONTRACTS:
        raise ValueError(f"unknown contract key: {which!r}")
    c = CONTRACTS[which]
    body = json.dumps(encode_eth_call(c["address"], c["selector"])).encode()
    req = urllib.request.Request(
        rpc_url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    result = payload.get("result", "")
    return decode_string_result(result)


if __name__ == "__main__":
    # Offline self-test of the decoder (no network).
    sample = (
        "0x"
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000011"
        + "687474703a2f2f38392e3136392e31322e323431".ljust(64, "0")
    )
    print("decoded:", decode_string_result(sample))
