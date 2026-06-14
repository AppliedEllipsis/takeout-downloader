# `security/` — FakeGit / LuaJIT SmartLoader Defense Kit

> **ELI5:** Someone booby-trapped a "free program" on GitHub. This folder is
> the opposite — tools to *recognize*, *safely take apart*, and *regression-test
> against* that booby trap. Nothing here is malware; the test "samples" only
> print `BENIGN_TEST_MARKER`.

## Why this exists (provenance warning)

This repo is a hard fork of `Kalainilavann/takeout_downloader_script`. During
the public-release audit we found the **upstream was distributing malware** in
its `outscorn/*.zip` files: the FakeGit / SmartLoader loader chain, which
Microsoft Defender flags as `VirTool:Win32/Gelesz.A`. Full incident record is
in [`../audit_summary.md`](../audit_summary.md) and [`../NOTICE.md`](../NOTICE.md);
the deep technical write-up is in
[`../docs/THREAT_FAKEGIT_LUAJIT.md`](../docs/THREAT_FAKEGIT_LUAJIT.md).

**Treat the upstream repo as a hostile source.** Do not clone, run, or download
its archives on a machine you care about.

## What's here

| Module | Purpose | Touches network/OS? |
|--------|---------|---------------------|
| `obfuscation_lab.py` | The campaign's encoding layers (Lua `\NNN` escapes, custom-alphabet base64, `hex→XOR→base64url→AES-ECB` dead-drop chain), implemented over benign markers. Encode **and** decode, so you can prove you understand each layer. | No |
| `detector.py` | Pure-static scanner. Scores files/dirs for the loader chain and explains every hit. Has a CLI. | No |
| `c2_resolver.py` | Read-only blockchain lookup for the campaign's Polygon EtherHiding C2 contracts. Offline decoder is always available; live lookup is opt-in. | Only `resolve_live()` (opt-in) |
| `sandbox.py` | Capability-denied Lua harness (no `ffi`/`jit`/`socket`, OS calls stubbed) + documented containment model for dynamic analysis. | No (refuses to run if containment fails) |
| `fixture_generator.py` | Generates benign-but-structurally-realistic loader-chain fixtures at runtime for testing. Nothing malicious is committed. | No |

Tests: [`../tests/test_security.py`](../tests/test_security.py) — 18 cases.

## Quick start

```bash
# Scan a directory (e.g. an untrusted download you extracted in a VM)
python -m security.detector /path/to/suspicious/folder

# Self-check the encoding layers
python -m security.obfuscation_lab

# Confirm the sandbox is genuinely contained on this machine
python -m security.sandbox

# Offline-decode an ABI-encoded C2 string (no network)
python -m security.c2_resolver

# Run the test suite
pytest tests/test_security.py -v
```

## The detection model (what it keys on)

A single indicator is weak; the **combination** is the signal. The detector
scores each of these and the `loader_chain_verdict()` fires when ≥2 components
of the triad are present:

1. **Renamed runtime** — a PE named after a dev tool (`gcc.exe`, `luajit.exe`,
   `luad.exe`, `init.exe`, `vm_s390x.exe`) that embeds a `LuaJIT` banner and is
   far too small for what it claims to be (real GCC is tens of MB).
2. **Obfuscated script** — a `.txt`/`.lua` with thousands of `\NNN` decimal
   escapes, a single enormous line, the `getfenv`+`setmetatable`+`newproxy`
   VM-bootstrap trio, and the table-shuffle range markers (`1064`/`658`/`659`).
3. **Launcher** — a `.cmd`/`.bat` that does `start <runtime>.exe <script>.txt`,
   feeding a text file to an "executable."
4. **Bonus high-confidence hits** — the documented custom base64 alphabet, a
   known C2 contract address, or a known-bad SHA256.

## Cat-and-mouse: how this evolves and how to stay ahead

This family has shipped **16 obfuscator generations** and rotated runtimes
(24KB clean LuaJIT → 100KB trojanized → 878KB → 651KB `gcc.exe`). Static
hashes are obsolete the moment they're published. Design your detection for the
*structure*, and assume the attacker will adapt:

| Attacker move | Why hashes/keywords fail | Defender counter (in this kit / to add) |
|---------------|--------------------------|------------------------------------------|
| Recompile/repack runtime | New SHA256, 0/76 on VT | Key on *name+size+embedded-banner mismatch*, not hash (`scan_executable`) |
| New obfuscator generation | New byte patterns | Key on *behavioral structure* — escape density, VM trio, single-huge-line entropy (`scan_text_blob`) |
| Rename runtime (`gcc`→`clang`→`node`) | Name allowlist misses it | Match the *class* (dev-tool name + Lua banner + size mismatch), expand the name regex |
| Encrypt the script so escapes vanish | Escape-count heuristic drops to zero | Add an **entropy** gate (already in `shannon_entropy`); flag high-entropy blobs shipped next to a runtime+launcher |
| Move C2 off-chain / new contract | Address list misses it | EtherHiding *pattern* detection: any `eth_call`-shaped selector + contract string; monitor new contracts by deployer address |
| Drop the `.cmd`, use LNK/registry/scheduled task | Launcher scanner misses it | Add LNK/`.url`/autorun parsers; treat runtime+script *co-location* as enough |
| Split payload across many small files | Per-file score stays low | Add a *directory-level* aggregate score (co-location of runtime+blob even without launcher) |
| Polyglot (zip that's also an image) | Magic-byte check fooled | Validate full container structure, not just first bytes |
| Sign the binary / use a real signed LuaJIT | "It's signed, must be safe" | Signature ≠ intent; still flag the *chain*. Check publisher reputation, not just validity |

### Things to consider when extending this

- **False positives are the enemy of adoption.** The triad-of-2 rule exists so
  a lone `.cmd` or a legit LuaJIT install doesn't scream. Tune on your own clean
  tree (`test_detector_quiet_on_clean_tree` guards this).
- **Never raise the bar by executing.** Every capability you add should be
  static or read-only. Dynamic analysis belongs in `sandbox.py` under the
  containment contract, on a disposable VM with no network.
- **Detect the technique, report the indicator.** Each `Finding` carries a
  human-readable reason you can paste into an abuse report.
- **Assume EtherHiding spreads.** Blockchain-hosted C2 means takedowns don't
  work; the contract is immutable and the IP rotates. Resolve read-only, report
  the *contract*, and alert on the on-chain `updateDomain`/`updateData` calls.
- **Supply-chain hygiene is the real fix.** The attack only works because users
  download and run archives from untrusted repos. The strongest control is
  never executing untrusted code — see `../docs/THREAT_FAKEGIT_LUAJIT.md` §
  "Defensive guidance."

## Safety contract

- No real malware, no working stealer/loader, no functional obfuscated VM.
- All generated fixtures decode only to `print("BENIGN_TEST_MARKER")`.
- Network access is never implicit: `c2_resolver.resolve_live()` must be called
  explicitly; everything else is offline.
- The sandbox refuses to run if `ffi`, `jit`, or a real `socket` module is
  reachable.
