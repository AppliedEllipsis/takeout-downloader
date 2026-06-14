# Threat Dossier: FakeGit / LuaJIT SmartLoader (and the loader-chain class)

> **Why this document exists.** This repository is a fork of an upstream project
> (`Kalainilavann/takeout_downloader_script`) that **shipped live malware** in its
> `outscorn/*.zip` files. During public-release prep we found, analyzed, and purged
> it (see [`../audit_summary.md`](../audit_summary.md) and [`../NOTICE.md`](../NOTICE.md)).
> This dossier turns that incident into reusable defensive knowledge and a safe,
> unit-tested lab (`../security/`) so you can recognize, decode, detect, and
> regression-test against this entire class of attack — **without ever handling
> live malware**.

---

## ELI5 (read this first)

Imagine someone mails you a "free game." Inside the box is a **brand-name games
console** (everyone trusts it) and a **scrambled instruction card**. You plug the
card into the console and it secretly tells the console to unlock your front door
and copy your house keys. The console is innocent — it just follows cards. The
trick is: the bad guys never had to build a suspicious robot; they reused a trusted
machine and hid the evil part as an unreadable card.

In this attack:
- the **trusted console** = a real `LuaJIT` interpreter renamed to `gcc.exe`,
- the **scrambled card** = `ptd.txt` (obfuscated Lua),
- the **"plug it in"** = `Launch.cmd` running `start gcc.exe ptd.txt`,
- the **front-door address** is looked up from a **public blockchain** so the
  criminals can change it any time without re-mailing the box.

Antivirus often trusts the console (it's legitimately signed / known-good) and
can't read the card (it's encrypted), so the box slips through.

---

## Executive summary

**Pattern name:** trusted-runtime loader chain (a.k.a. "FakeGit", "LuaJIT
SmartLoader", "living-off-trusted-binaries"). Microsoft Defender labels the
sample we found `VirTool:Win32/Gelesz.A`.

**Kill chain:**

1. **Lure** — a GitHub repo with an AI-generated README impersonating a useful
   tool (here: a Google Takeout downloader). Every link points at one ZIP.
2. **Delivery** — ZIP contains a renamed but **genuine** LuaJIT runtime, an
   obfuscated Lua script, and a `Launch.cmd`.
3. **Execution** — user runs `Launch.cmd`; the trusted EXE interprets the script.
   The console window is hidden (`ShowWindow(SW_HIDE)`).
4. **C2 resolution** — the script reads its command-and-control address from a
   **Polygon smart contract** (EtherHiding), so infrastructure rotates without a
   new binary.
5. **Second stage** — it downloads an encrypted blob from a **GitHub dead drop**
   and peels a 4-layer chain (`hex → XOR → base64url → AES-ECB`).
6. **Payload** — an information stealer (StealC / Lumma / Rhadamanthys class):
   browser credentials, cookies, crypto wallets, screenshots; some variants add a
   crypto-clipper. Capabilities include process injection / PE hollowing.

**Why it evades detection:** the dangerous logic is *data*, not code-on-disk. The
on-disk EXE is a clean, well-known interpreter (often 0–1/72 on VirusTotal). The
script is encrypted text. The C2 lives off-host on a blockchain. The payload
arrives only at runtime from a reputable domain (github.com).

---

## 1. Anatomy of the loader chain

```
victim-repo.zip
└── innkeeper/                     ← single oddly-named "rare English word" dir
    ├── gcc.exe                    ← genuine LuaJIT 2.1, renamed (651,776 B here)
    ├── ptd.txt                    ← obfuscated Lua payload (~300 KB)
    └── Launch.cmd                 ← `start gcc.exe ptd.txt`
```

| Component | Real-world trait | Why it matters to a defender |
|-----------|------------------|------------------------------|
| Renamed runtime | `gcc.exe`, `luad.exe`, `luajit.exe`, `init.exe`, `vm_s390x.exe`. **Size/name mismatch** (a 650 KB "gcc.exe" — the real GCC driver is tens of MB). | Name claims a dev tool; bytes are an interpreter. The mismatch is a high-signal heuristic. |
| Obfuscated script | Megabytes of Lua `\NNN` decimal escapes; a constant-table VM; `getfenv`/`setmetatable`/`newproxy` bootstrap. | Encrypted text → AV can't pattern-match. But the *texture* (escape density, VM trio, single huge line) is detectable. |
| Launcher | `start <runtime> <script>` with a hidden window. | The `EXE + text-file` invocation pattern is itself the signature. |

The **triad together** is far more damning than any single file. Our detector
scores the combination (`loader_chain_verdict`).

---

## 2. How the obfuscation/encoding works (and how to peel it)

All implemented over **benign markers only** in
[`../security/obfuscation_lab.py`](../security/obfuscation_lab.py). Each layer is
reversible and unit-tested.

### Layer 1 — Lua decimal escapes
`"A"` → `"\065"`. Trivial, but applied to *every* character it turns readable
source into a wall of digits. **Peel:** regex `\\(\d{1,3})` → `bytes(...)`.

### Layer 2 — custom-alphabet base64
Standard base64 but with a **scrambled alphabet**. The campaign's documented
alphabet is:
```
L07ker/2Jn4Z+b6y8hfXYVpTiDCvQKc3qRjUEBdo9MzHxtGNgWuA1FSmlaPO5wIs
```
**Peel:** build the inverse map from custom→standard, then standard base64-decode.
(We keep the real alphabet as a *recognition constant* only; our encoder uses a
separate benign demo alphabet so we never normalize against the live one.)

### Layer 3 — repeating-key XOR
Cheap, symmetric, leaves no obvious header. Often the outer "skin" over a base64
blob. **Peel:** XOR again with the same key.

### Layer 4 — the dead-drop chain `hex → XOR → base64url → AES-ECB`
The documented second-stage decryption. To *produce* a blob you reverse it:
`AES-ECB encrypt → base64url → XOR → hex`. **Peel (what the loader does):**
`unhex → XOR → base64url-decode → AES-ECB decrypt → PKCS7 unpad`.

> **Encoding ≠ encryption.** Layers 1–3 are *obfuscation* (no real secret; the
> key is in the sample). Only Layer 4's AES is true encryption — and even then the
> key ships with the loader, because it must self-decrypt. This is the attacker's
> fundamental weakness: **everything needed to decode is reachable**, so a patient
> analyst (or a contained sandbox) always wins eventually.

---

## 3. EtherHiding: blockchain-hosted C2

Instead of a hardcoded domain, the loader calls a **getter on a smart contract**
(`getDomain()` selector `0xb68d1809`, or `getData()` selector `0x3bc5de30`). The
operator updates the stored IP with on-chain transactions; the binary never
changes.

- Reading the value is a **public, free, harmless** `eth_call` — no transaction,
  no gas, no malware execution. See
  [`../security/c2_resolver.py`](../security/c2_resolver.py).
- The ABI-encoded `string` return is `offset(32) | length(32) | utf8 bytes`. Our
  `decode_string_result()` decodes it offline (unit-tested).
- **Why attackers love it:** takedown-resistant (no registrar/host to seize),
  cheap rotation, no binary churn.
- **Why it helps defenders:** the C2 history is **public and immutable**. You can
  read every IP the operator ever set, with timestamps, straight off the chain.

Documented contracts (Polygon Mainnet) are listed in `c2_resolver.CONTRACTS`.

---

## 4. Detecting it (structure over signatures)

Hash blocklists are brittle — the campaign iterated through **16 obfuscator
generations** and several payload hashes sit at **0/72** on VirusTotal. So the
detector in [`../security/detector.py`](../security/detector.py) scores
**structure and behavior**, not just hashes:

| Heuristic | Why it generalizes |
|-----------|--------------------|
| PE whose name claims a dev tool but body contains `LuaJIT` strings | Renamed-interpreter tell; survives renames across variants. |
| Dev-tool EXE far smaller than the real tool | Size/name mismatch is intrinsic to the disguise. |
| Launcher `start x.exe y.txt` feeding text to an EXE | The loader invocation itself. |
| High density of `\NNN` escapes / single 50k+ char line | Obfuscation texture, not a specific string. |
| `getfenv` + `setmetatable` + `newproxy` together | Lua VM bootstrap trio. |
| Table-shuffle range markers (`1064`, `658`, `659`) | This family's VM constants. |
| Embedded custom base64 alphabet | Exact campaign artifact. |
| Known C2 contract address present | Direct IOC. |
| **File unreadable (AV quarantine on read)** | Defender already flagged it — treat as strong signal. |
| Triad present in one dir | Combination verdict. |

Run it:
```bash
python -m security.detector /path/to/suspicious/folder
```

---

## 5. Analyzing it safely (the padded room)

If you must *run* an obfuscated script to watch it self-decode, use a
**capability-denied** runtime. See
[`../security/sandbox.py`](../security/sandbox.py).

Containment checklist (the harness verifies items 1–3 before running anything):

1. **Stock Lua, not LuaJIT** → `ffi == nil`, `jit == nil`. FFI is the *only*
   bridge to Win32 syscalls/sockets; without it the script can decode but not act.
2. **No network module** → `require('socket')` yields no usable module; we also
   wipe `package.loaded/preload` socket entries and null `cpath`/`path`.
3. **OS/IO/loaders stubbed** → `os.execute`, `io.open`, `io.popen`, `dofile`,
   `loadfile`, and `load` are replaced with logging stubs. A self-recursing
   obfuscator reveals its chunk pattern without its payload ever compiling.
4. **Belt-and-suspenders (host level):** offline VM, fresh snapshot, no creds in
   the VM, network adapter disabled.

> **Hard rule:** never run a sample under real LuaJIT + FFI on a host you care
> about. In our incident analysis, Windows Defender quarantined the EXE on read
> before we could even hash it — an independent control that confirms execution
> never happened.

---

## 6. Reproduce it for your own tests (safe fixtures)

[`../security/fixture_generator.py`](../security/fixture_generator.py) builds
**benign, structurally-realistic** loader chains at test time (never committed):

- a fake-PE "runtime" (starts with `MZ`, contains a `LuaJIT` string, but is inert
  text — not an executable),
- an obfuscated "script" with production-scale escape density + the VM trio that
  *decodes only to* `print("BENIGN_TEST_MARKER")`,
- a `Launch.cmd` with a real `start runtime.exe ptd.txt` line.

```python
from security import fixture_generator as fix, detector
manifest = fix.write_fixture_tree("/tmp/lab")      # loose files
zip_bytes = fix.build_fixture_zip("luad.exe", "innkeeper")  # in-memory zip
findings = detector.scan_dir("/tmp/lab")
print(detector.loader_chain_verdict(findings))     # (True, 'LOADER-CHAIN MATCH: ...')
```

The test suite ([`../tests/test_security.py`](../tests/test_security.py),
18 tests) asserts: every layer round-trips, the detector fires on the triad and
on each documented runtime name, it stays quiet on clean trees, the dead-drop
chain decodes, and the sandbox is genuinely contained.

```bash
pytest tests/test_security.py -v
```

---

## 7. The cat-and-mouse game (and how to stay ahead)

Detection and evasion co-evolve. What you build today degrades; plan for it.

### How attackers will adapt to each detection you add
| You detect… | They respond with… | Your counter-move |
|-------------|--------------------|-------------------|
| Name `gcc.exe` | Rotate names (`vm_s390x.exe`, random) | Match on *body* (LuaJIT strings) + size/name mismatch, not name lists. |
| LuaJIT strings in the EXE | Strip/encrypt the banner, pack with UPC/themida | Entropy + import-table analysis; flag packed dev-tool-named EXEs. |
| `\NNN` escape density | Switch encodings (hex tables, bytecode-only) | Generic high-entropy-text + Lua-VM-shape heuristics; bytecode magic (`\x1bLua`). |
| The custom base64 alphabet | Generate a new alphabet per build | Detect *any* 64-unique-char permutation constant near decode loops. |
| `getfenv/setmetatable/newproxy` trio | Reorder / alias via `_ENV`, indirect calls | Behavioral sandbox: watch for self-`load()` and big constant tables. |
| Known C2 contract address | Deploy new contracts / chains | Detect the *technique*: `eth_call` selectors, RPC endpoints, hex-IP decode. |
| Hash blocklist | Recompile (hash changes every build) | Never rely on hashes alone; they are the weakest tier. |
| `Launch.cmd` pattern | `.lnk`, `.vbs`, registry run-keys, scheduled tasks, ClickFix/PowerShell | Broaden launcher coverage; watch parent→child (`*.exe` spawned by `cmd`/`wscript`). |

### Structural truths that *don't* change (anchor your detection here)
- The runtime must be a **real interpreter** → it carries interpreter
  fingerprints no matter the filename.
- The loader must **self-decrypt** → the key is always present in the sample.
- It must **reach the network** for the second stage → FFI/socket use is
  observable in a sandbox; deny it and the chain stalls.
- EtherHiding C2 is **public and immutable** → free intel for defenders.
- The disguise needs a **trusted-looking name** → name/identity mismatches recur.

### Things to consider / further improvements
- **Behavioral > static.** Add an instrumented dynamic pass (our sandbox) to the
  pipeline; record attempted `load()`/`os`/`io`/`require` calls as features.
- **Parent-child process telemetry.** `cmd.exe → gcc.exe → cmd.exe` or an
  interpreter spawning `powershell` is a louder signal than any file content.
- **Network egress policy.** Alert on processes resolving C2 via public RPC nodes
  (`polygon-rpc.com`, Infura, Alchemy) shortly after launch.
- **Reputation + provenance.** New GitHub account, single repo, AI-generated
  README, every link → one ZIP, "enable Developer Mode / load unpacked" install
  instructions = classic FakeGit lure. Weight repo metadata.
- **Hunt signals at scale.** The campaign's rare-word inner-dir names and reused
  inner-file hashes (identical across unrelated lures) are strong pivot points.
- **Supply-chain hygiene for *your* repos.** CI step that runs
  `security.detector` over the tree and **fails the build** on a loader-chain
  verdict; pre-commit hook; branch protection; treat committed binaries/zips as
  suspect by default.
- **Don't trust "it's just data."** Text files, images, and blockchains are all
  viable payload carriers. Decode boundaries are where you inspect.
- **Assume key-ship.** Because self-decrypting malware must carry its keys,
  *full* offline decryption is always theoretically possible — invest in
  automated multi-layer peelers.
- **Watch the trust transfer.** The whole attack is a chain of "trust this
  because the previous thing was trusted." Break any link (untrusted source,
  denied capability, inspected decode point) and it collapses.

---

## 8. Incident-specific facts (this repo's upstream)

- **Upstream:** `Kalainilavann/takeout_downloader_script` shipped the malware in
  `outscorn/*.zip`. Treat that repo and those archives as hostile.
- **Sample (v3.8 zip):** SHA256 `b781102c6ff857fb45089a6b3d30c5ebb7699ec7e8771bfa0039bef28de782bd`
  - `gcc.exe` SHA256 `2ea6200c846af534a07338a803acf7f49520abf59b2ae82a701a24e7fada0b97` (Defender: `VirTool:Win32/Gelesz.A`)
  - `ptd.txt` SHA256 `a563a7df740bce2bda1231cebb4ed136813df43361de17c224b97af9941ee0c4`
- **Disposition:** purged from working tree and full git history; never present in
  this fork's published history. Full IOC table in
  [`../audit_summary.md`](../audit_summary.md).

### Reporting checklist
1. GitHub abuse report for the upstream repo (malware distribution).
2. VirusTotal: submit by **hash** (do not re-upload/execute locally).
3. Microsoft Security Intelligence (already matches `VirTool:Win32/Gelesz.A`).
4. Preserve hashes + Defender detection metadata as evidence.

---

## References (for further reading)
- Derp / Kirk — "FakeGit: LuaJIT malware distributed via GitHub at scale"
- Intellibron — "Lua-JIT SmartLoader: Analyzing the GitHub Campaign Delivering Stealer"
- ESET — "Gelsemium: When threat actors go gardening"
- Morphisec — "Not All Fun and Games: Lua Malware Targets Educational Sector"
- MITRE ATT&CK — EtherHiding (T1102 variants), Obfuscated Files (T1027),
  Masquerading (T1036), Process Injection (T1055).
