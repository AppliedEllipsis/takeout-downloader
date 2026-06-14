"""
security/ — Defensive analysis & detection kit for the FakeGit / LuaJIT
SmartLoader loader-chain malware family.

=============================================================================
ELI5 (explain like I'm 5)
=============================================================================
Someone put a booby-trapped "free program" on GitHub. It looks like a normal
download, but inside the zip is a fake `gcc.exe` (really a Lua engine) plus a
scrambled script. Double-clicking the included `Launch.cmd` quietly runs the
script, which phones a "secret address" stored on a blockchain, downloads a
password-stealer, and robs your browser logins and crypto wallets.

This folder is the OPPOSITE of that: tools to *recognize*, *take apart*, and
*test against* that trick — safely. Nothing here is malware. The "samples" we
generate only print a word like `BENIGN_TEST_MARKER`; they never touch the
network, the OS, or your files.

=============================================================================
WHY THIS EXISTS (provenance warning)
=============================================================================
This project (`AppliedEllipsis/takeout-downloader`) is a hard fork of the
upstream repo `Kalainilavann/takeout_downloader_script`. During the
public-release audit we discovered the **upstream repo was distributing
malware** in its `outscorn/*.zip` files — the FakeGit / SmartLoader loader
chain (Microsoft label `VirTool:Win32/Gelesz.A`). See `audit_summary.md` and
`NOTICE.md` for the full incident record.

That discovery is the reason this kit exists. The upstream is a
**compromised / malicious source** — treat anything from it as hostile. This
folder lets us (a) document exactly how that attack works, (b) detect it, and
(c) regression-test our own tooling against the vector so we never reship it.

=============================================================================
EXECUTIVE SUMMARY
=============================================================================
Modules:
  obfuscation_lab.py  Implements the campaign's encoding layers (Lua \\NNN
                      escapes, custom-alphabet base64, the hex->XOR->base64url
                      ->AES-ECB dead-drop chain) over BENIGN markers only.
                      Use to understand encoding and to build fixtures.
  detector.py         Pure-static scanner. Scores files/dirs for the loader
                      chain (renamed-LuaJIT runtime, obfuscated Lua, launcher
                      .cmd, blockchain-C2 strings, high entropy). Explains
                      every hit. Never executes anything.
  c2_resolver.py      READ-ONLY blockchain lookup. Resolves the campaign's
                      Polygon EtherHiding C2 contracts via public RPC eth_call.
                      No malware runs; it just reads a public smart-contract
                      value. Network is opt-in (off by default).
  sandbox.py          Capability-denied Lua harness + documented procedure for
                      safe dynamic analysis (no ffi/jit/socket, OS calls
                      stubbed). Explains the containment model.
  fixtures.py         Generates benign-but-structurally-realistic test samples
                      (fake loader chain, encoded blobs) at runtime. NOT
                      committed as files — built fresh for tests, scanned, then
                      thrown away.

Safety contract for the whole package:
  * No real malware, no working stealer/loader, no functional obfuscated VM.
  * Generated fixtures carry the literal token BENIGN_TEST_MARKER.
  * Network access is never implicit: c2_resolver requires allow_network=True.
"""

BENIGN_MARKER = "BENIGN_TEST_MARKER"

__all__ = ["BENIGN_MARKER"]
