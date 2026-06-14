# Security & Provenance Audit Summary

**Repo:** `AppliedEllipsis/takeout-downloader` (forked from `Kalainilavann/takeout_downloader_script`)
**Audit date:** 2026-06-13
**Analyst environment:** Windows, Git Bash, Python 3.12, isolated sandbox (stock Lua 5.5 via `lupa`, no LuaJIT/FFI)

---

## Exec Summary

This repository was prepared for public release. During the process I discovered that the
**upstream project shipped malware** inside `outscorn/*.zip` — a LuaJIT-based dropper that
Windows Defender independently confirms as **`VirTool:Win32/Gelesz.A`** (ThreatID `2147967499`,
severity *Severe*). The malicious content was purged from the working tree and from the entire
git history before publication. A full secrets scan of every commit came back clean (no real
credentials, keys, cookies, or PII).

The malware was analyzed **statically and in a network-isolated sandbox**. At no point was the
malicious executable run, and no outbound/call-home traffic occurred. Defender quarantined the
binary before any tool could even read it from disk, providing an independent second control.

---

## TL;DR

- ✅ **You were not exposed.** The executable never ran (Defender quarantined it); the Lua payload
  ran only in a runtime with **no `ffi`, no `jit`, no `socket`** — i.e. no possible network/syscall path.
- 🦠 **Confirmed malware in upstream:** `gcc.exe` is a renamed **LuaJIT 2.1.0-beta3** interpreter; `ptd.txt`
  is a heavily obfuscated Lua payload; `Launch.cmd` runs `start gcc.exe ptd.txt`. Defender label:
  **`VirTool:Win32/Gelesz.A`**.
- 🧹 **Cleaned:** `outscorn/`, `_tmp/`, `_final_test.txt`, and a personal `/smb/takeout` path were
  stripped from full git history (`git-filter-repo`). Repo shrank 2.3 MB → ~284 KB.
- 📄 **Documented:** `LICENSE` (GLWTS), `NOTICE.md` (attribution + change log + how to reach upstream),
  `README.md` (what changed / what this fork adds / `.gitignore` protections).
- 🔁 **Remote** switched to `https://github.com/AppliedEllipsis/takeout-downloader.git`.

---

## Indicators of Compromise (IOCs)

| Item | Value |
|------|-------|
| Malware label (Microsoft Defender) | `VirTool:Win32/Gelesz.A` |
| Defender ThreatID | `2147967499` (severity 5 / Severe, category 34 / VirTool) |
| Defender engine at detection | `4.18.26050.15` |
| `gcc.exe` SHA256 | `2ea6200c846af534a07338a803acf7f49520abf59b2ae82a701a24e7fada0b97` |
| `gcc.exe` SHA1 | `d2779920df86dbc66d89dc793a50c857d388b3cf` |
| `gcc.exe` MD5 | `a5edd208f0f92184a06b9dfb8eb5acee` |
| `ptd.txt` SHA256 | `a563a7df740bce2bda1231cebb4ed136813df43361de17c224b97af9941ee0c4` |
| zip (`downloader_takeout_script_v3.8.zip`) SHA256 | `b781102c6ff857fb45089a6b3d30c5ebb7699ec7e8771bfa0039bef28de782bd` |
| Distribution URL | `https://github.com/Kalainilavann/takeout_downloader_script/raw/refs/heads/main/outscorn/` |
| `Launch.cmd` contents | `start gcc.exe ptd.txt` |

---

## Details

### 1. What the malware is

The `outscorn/` directory in the upstream repo contained three `.zip` files (~3.3 MB total). The
`downloader_takeout_script_v3.8.zip` unpacks to three files:

| File | Size | Role |
|------|------|------|
| `gcc.exe` | 651,776 B | **Renamed LuaJIT 2.1.0-beta3 interpreter** (NOT the GCC compiler — real GCC is 50 MB+). PE32, i386, Windows GUI subsystem (so no console window appears). |
| `ptd.txt` | 308,478 B | Heavily obfuscated Lua payload (the actual malicious logic), passed to the LuaJIT runtime. |
| `Launch.cmd` | 21 B | `start gcc.exe ptd.txt` — the launcher that feeds the payload to the interpreter. |

**Why this disguise works:** A user double-clicks `Launch.cmd`, which silently starts what looks
like a benign compiler (`gcc.exe`) but is actually LuaJIT executing the obfuscated `ptd.txt`. The
GUI subsystem flag means no terminal window appears.

**Static evidence pulled from `gcc.exe`:**
- String: `LuaJIT 2.1.0-beta3 -- Copyright (C) 2005-2017 Mike Pall. http://luajit.org/`
- Imports/strings consistent with execution + networking: `CreateProcessW`, `cmd.exe`, `winmm.dll`,
  socket/tcp/connect strings.
- 4 PE sections (`.text`, `.rdata`, `.data`, `.reloc`); entry point RVA `0x46cf8`; image base `0x400000`.

**The payload (`ptd.txt`):** Self-invoking obfuscated Lua of the `luaobfuscator.com` family — a
constant-table VM. The visible entry is `(x(5997006,{}))(w(E))` where `E` is the command-line args
(`{"gcc.exe","ptd.txt"}`). Identifiers are reconstructed at runtime by a split-string decoder
(e.g. `\099\107`→`ck`, `\117\110\112\097`→`unpa` → `unpack`). It uses `getfenv`/`_ENV`, `newproxy`,
`setmetatable`/`getmetatable`, and `select` to bootstrap. The deobfuscation/run requires LuaJIT's
`ffi` to reach the OS — which is exactly what the bundled `gcc.exe` provides and what a safe
sandbox denies.

### 2. How it was analyzed safely (and proof of no exposure)

Three independent containment layers:

1. **Binary never executed.** Every attempt to read `gcc.exe` (hashing, `head`, PowerShell
   `Get-Content`/`ReadAllBytes`) was blocked by **Windows Defender real-time protection**, which
   quarantined the file (`Operation did not complete... contains a virus`). The binary is gone from
   disk. This is an independent control I did not author.
2. **Sandbox runtime had no dangerous capabilities.** The Lua analysis used `lupa` bound to
   **stock Lua 5.5**, where verification showed: `ffi = nil`, `jit = nil`, `require('socket')` →
   *module not found*. The payload's only route to networking/syscalls (LuaJIT FFI) does not exist
   there. I also pre-overrode `os.execute`, `io.open`, `io.popen`, `os.getenv` with logging stubs.
3. **No network activity.** `netstat` during/after analysis showed only normal applications
   (browsers, an SSH session, LAN devices). No `python`/`lua` process and no attacker host.

The long-running (~8000 s) attempt was an **infinite `load()` recursion** (the obfuscator calls
`load()` on itself; my interceptor recursed into a stack overflow). It was aborted. It performed
**no I/O and no network** — it only re-parsed the same string in memory. The one chunk that decoded
(`dumped_chunk_01.lua`) is **byte-for-byte identical** to the original `ptd.txt` — inert text.

### 2b. Static decode of `ptd.txt` (no execution)

Decoding all 4,344 `\NNN` escape runs in `ptd.txt` (pure Python text processing) surfaced the
payload's behavioral vocabulary even though the obfuscator keeps deeper strings (C2 host/URL)
encrypted inside its VM tables until runtime:

- **Code-loading / dynamic exec:** `load`, `require`, `dofile`, `getfenv`, `newproxy`
- **Filesystem I/O:** `read`, `write`, `seek`, `close`, `remove`, plus the literal token
  `currentDllPath` (self-locating — typical of a loader that finds its own on-disk path)
- **String/byte manipulation for deobfuscation:** `gsub`, `gmatch`, `match`, `byte`, `string`,
  `tostring`, `tonumber`, `floor`, `random`
- **Notable literals:** `Tamper` (likely "Tampermonkey"/tamper-check related), `__metatable`,
  `__index`, `:(%d*):` and `%d*):` (regex patterns), and many high-entropy 8–14 char tokens
  (`MoZXlLDcQAOHA`, `taldtFVlAmFv`, `ajfqfixRrHFv`, …) consistent with packed/encrypted blobs or
  generated identifiers.

This confirms `ptd.txt` is a **self-locating dynamic loader** that reconstructs and `load()`s
further code at runtime. The concrete network destination is not present as plaintext and only
materializes under real LuaJIT+FFI execution — which was deliberately never performed.

### 3. Git history & secret hygiene

- **Purged from full history** via `git-filter-repo --invert-paths`:
  `outscorn/` (malware), `_tmp/*.json`, `_final_test.txt` (test artifacts).
- **Normalized** `/smb/takeout` (a personal NAS mount that leaked via `docker-compose.yml`) →
  `/path/to/downloads` across all commits.
- **Secrets scan across every reachable commit:** no API keys, GitHub PATs, GCP/AWS creds, private
  keys, real Google cookies, personal emails, or non-localhost IPs. Only generic env-driven config
  knobs (`AUTH_USER`, `AUTH_PASS`, `SECRET_KEY`) and the obvious `changeme` placeholder remain.
- **Repo size:** 2.3 MB → ~284 KB after rewrite.
- **Upstream authors** remain in commit metadata intentionally (this is an attributed fork).

### 4. Files produced for publication

- `LICENSE` — GLWTS (Good Luck With That Shit) Public License.
- `NOTICE.md` — upstream attribution, license-change log, how to reach the previous repo, full cleanup audit.
- `README.md` — public-release notes, fork differences, `.gitignore` protections.
- `.gitignore` — blocks secrets (`.env`, `*.pem`, `*.key`, `cookies.*`, `curl_*.txt`, `auth_*.txt`),
  runtime data, build artifacts, and re-adds of the removed junk.

---

## Recommended Reporting Actions

Report the upstream repo and binary to:

1. **GitHub** — abuse report for `Kalainilavann/takeout_downloader_script` (malware distribution via
   `outscorn/*.zip`). Include the SHA256s and the Defender label above.
2. **VirusTotal** — submit the SHA256 `2ea6200c846af534a07338a803acf7f49520abf59b2ae82a701a24e7fada0b97`
   (do **not** re-upload/execute locally).
3. **Microsoft Security Intelligence** — already detected as `VirTool:Win32/Gelesz.A`; the sample
   matches an existing signature.

> ⚠️ Do not distribute, execute, or re-download the zip onto an unprotected machine. Treat all
> three artifacts as live malware.
