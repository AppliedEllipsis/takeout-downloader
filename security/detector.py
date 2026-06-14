"""
detector.py — static, signature- and heuristic-based scanner for the
FakeGit / LuaJIT SmartLoader loader-chain pattern (and the general class of
"trusted-runtime + obfuscated-script + launcher" droppers).

=============================================================================
ELI5
=============================================================================
Bad guys hide a fake program that looks like a normal tool (like "gcc.exe")
next to a scrambled script and a tiny "click me" launcher. This scanner walks
a folder and points at the warning signs — without ever running anything. It
just reads files and explains, in plain English, why each one looks dangerous.

=============================================================================
EXECUTIVE SUMMARY
=============================================================================
Pure static analysis. Reads bytes, scores them, never executes, never loads a
Lua VM, never touches the network. Safe to point at a freshly-downloaded
archive.

Detects, by STRUCTURE (so it survives new obfuscator generations that are
0/76 on VirusTotal):
  * Known-bad SHA256 (high confidence, brittle).
  * A renamed dev-tool EXE (gcc/luajit/compiler/init/vm_*) that is actually a
    LuaJIT runtime, identified by the embedded LuaJIT copyright string and a
    suspiciously small size for the name it claims.
  * A launcher (.cmd/.bat) that does `start <exe> <textfile>`.
  * An obfuscated-Lua text blob: huge single line, thousands of \\NNN decimal
    escapes, high entropy, the campaign's table-shuffle range markers.
  * The campaign's custom base64 alphabet and blockchain C2 contract strings.

Output: a list of Finding(path, severity, score, reasons[]). Every reason is
citable in an abuse report.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .obfuscation_lab import CAMPAIGN_CUSTOM_ALPHABET

# --------------------------------------------------------------------------- #
# Known IOCs (published threat intel). Hash hits are high-confidence but
# brittle; the heuristics below are what catch unseen variants.
# --------------------------------------------------------------------------- #
KNOWN_BAD_SHA256 = {
    "2ea6200c846af534a07338a803acf7f49520abf59b2ae82a701a24e7fada0b97":
        "FakeGit loader runtime (renamed LuaJIT), VirTool:Win32/Gelesz.A",
    "a563a7df740bce2bda1231cebb4ed136813df43361de17c224b97af9941ee0c4":
        "FakeGit obfuscated Lua payload (ptd.txt)",
}

# Blockchain C2 contracts (Polygon) documented for this campaign.
KNOWN_C2_CONTRACTS = {
    "0xd68910ED4D4A5A9bAdF9ec95604CAE0f3378479B": "FakeGit V1 loader contract",
    "0x2cbd2464dd749f5c0034fc9cddc6db2d53dea400": "FakeGit V1 alternate contract",
    "0x1823A9a0Ec8e0C25dD957D0841e3D41a4474bAdc": "FakeGit V2 loader contract",
}

# EXE names the campaign reuses for the renamed LuaJIT runtime.
SUSPICIOUS_RUNTIME_NAMES = {
    "gcc.exe", "luajit.exe", "luad.exe", "compiler.exe",
    "init.exe", "vm_s390x.exe", "g++.exe", "clang.exe",
}

LUAJIT_MARKER = b"LuaJIT"
LUA_COPYRIGHT = b"Mike Pall"

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class Finding:
    path: str
    severity: str
    score: int
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"[{self.severity.upper():8}] score={self.score:<3} {self.path}\n" + \
            "\n".join(f"        - {r}" for r in self.reasons)


# --------------------------------------------------------------------------- #
# Primitive helpers
# --------------------------------------------------------------------------- #
def shannon_entropy(data: bytes) -> float:
    """Bits per byte (0..8). High entropy => compressed/encrypted/encoded."""
    if not data:
        return 0.0
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    n = len(data)
    ent = 0.0
    for c in freq:
        if c:
            p = c / n
            ent -= p * math.log2(p)
    return ent


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_pe(data: bytes) -> bool:
    return len(data) >= 2 and data[:2] == b"MZ"


# --------------------------------------------------------------------------- #
# Per-file scanners
# --------------------------------------------------------------------------- #
def scan_executable(path: Path, data: bytes) -> Finding | None:
    name = path.name.lower()
    reasons: list[str] = []
    score = 0

    digest = hashlib.sha256(data).hexdigest()
    if digest in KNOWN_BAD_SHA256:
        return Finding(str(path), "critical", 100,
                       [f"SHA256 matches known IOC: {KNOWN_BAD_SHA256[digest]}"])

    if not _is_pe(data):
        return None

    has_luajit = LUAJIT_MARKER in data or LUA_COPYRIGHT in data

    if name in SUSPICIOUS_RUNTIME_NAMES and has_luajit:
        score += 70
        reasons.append(
            f"PE named '{path.name}' (claims to be a dev tool) but embeds a "
            f"LuaJIT runtime — classic renamed-interpreter loader."
        )
    elif has_luajit and name not in ("luajit.exe",):
        score += 40
        reasons.append(
            f"PE embeds a LuaJIT runtime but is named '{path.name}', not luajit.exe."
        )

    # gcc that isn't gcc: real GCC driver is large + links many libs.
    if name == "gcc.exe" and len(data) < 5 * 1024 * 1024:
        score += 25
        reasons.append(
            f"'gcc.exe' is only {len(data):,} bytes; the real GCC driver is "
            f"tens of MB. Size/name mismatch."
        )

    if score == 0:
        return None
    sev = "critical" if score >= 80 else "high" if score >= 50 else "medium"
    return Finding(str(path), sev, min(score, 100), reasons)


def scan_launcher(path: Path, data: bytes) -> Finding | None:
    if path.suffix.lower() not in (".cmd", ".bat"):
        return None
    text = data.decode("utf-8", "replace").lower()
    reasons: list[str] = []
    score = 0

    m = re.search(r"start\s+([^\s]+\.exe)\s+([^\s]+\.(txt|lua|dat|conf))", text)
    if m:
        score += 60
        reasons.append(
            f"Launcher runs `start {m.group(1)} {m.group(2)}` — feeds a text/script "
            f"file to an EXE (interpreter-loader pattern)."
        )
    if re.search(r"\b(gcc|luajit|luad|compiler|init|vm_\w+)\.exe\b", text):
        score += 20
        reasons.append("Launcher invokes a dev-tool-named EXE that is likely a renamed runtime.")
    if score == 0:
        return None
    sev = "high" if score >= 60 else "medium"
    return Finding(str(path), sev, min(score, 100), reasons)


def scan_text_blob(path: Path, data: bytes) -> Finding | None:
    digest = hashlib.sha256(data).hexdigest()
    if digest in KNOWN_BAD_SHA256:
        return Finding(str(path), "critical", 100,
                       [f"SHA256 matches known IOC: {KNOWN_BAD_SHA256[digest]}"])

    # Only consider mostly-text files.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None

    reasons: list[str] = []
    score = 0

    escapes = len(re.findall(r"\\\d{1,3}", text))
    if escapes >= 500:
        score += 35
        reasons.append(f"{escapes:,} Lua \\NNN decimal escapes — heavy string obfuscation.")

    lines = text.split("\n")
    longest = max((len(l) for l in lines), default=0)
    if longest >= 50_000:
        score += 25
        reasons.append(f"Single line of {longest:,} chars — minified/obfuscated blob.")

    if "getfenv" in text and "setmetatable" in text and "newproxy" in text:
        score += 20
        reasons.append("Lua VM-bootstrap trio present: getfenv + setmetatable + newproxy.")

    # Campaign table-shuffle range markers.
    markers = [m for m in ("1064", "658", "659", "1078") if m in text]
    if len(markers) >= 2:
        score += 15
        reasons.append(f"Obfuscator table-shuffle range markers present: {markers}.")

    if CAMPAIGN_CUSTOM_ALPHABET in text:
        score += 40
        reasons.append("Embeds the campaign's documented custom base64 alphabet.")

    for addr, label in KNOWN_C2_CONTRACTS.items():
        if addr.lower() in text.lower():
            score += 50
            reasons.append(f"Contains known C2 contract address ({label}).")

    if score == 0:
        return None
    sev = "critical" if score >= 80 else "high" if score >= 50 else "medium" if score >= 30 else "low"
    return Finding(str(path), sev, min(score, 100), reasons)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
SCANNERS = (scan_executable, scan_launcher, scan_text_blob)
MAX_READ = 8 * 1024 * 1024  # cap per-file read; obfuscated blobs are < 1MB


def scan_file(path: Path) -> list[Finding]:
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_READ)
    except (OSError, PermissionError) as e:
        # Defender may quarantine on read — that itself is a strong signal.
        return [Finding(str(path), "high", 60,
                        [f"Unreadable ({e.__class__.__name__}) — possible AV quarantine."])]
    out: list[Finding] = []
    for scanner in SCANNERS:
        f = scanner(path, data)
        if f:
            out.append(f)
    return out


def scan_dir(root: str | Path) -> list[Finding]:
    """Walk a directory and return findings sorted by severity then score."""
    root = Path(root)
    findings: list[Finding] = []
    for p in root.rglob("*"):
        if p.is_file():
            findings.extend(scan_file(p))
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.score), reverse=True)
    return findings


def loader_chain_verdict(findings: Iterable[Finding]) -> tuple[bool, str]:
    """
    Decide if a directory contains the full loader-chain triad
    (runtime + launcher + obfuscated blob). The combination is far more
    damning than any single file.
    """
    kinds = {"runtime": False, "launcher": False, "blob": False}
    for f in findings:
        joined = " ".join(f.reasons).lower()
        if "luajit runtime" in joined or "renamed-interpreter" in joined:
            kinds["runtime"] = True
        if "launcher runs" in joined:
            kinds["launcher"] = True
        if "obfusc" in joined or "decimal escapes" in joined or "custom base64" in joined:
            kinds["blob"] = True
    present = [k for k, v in kinds.items() if v]
    if len(present) >= 2:
        return True, f"LOADER-CHAIN MATCH: {', '.join(present)} components present."
    return False, "No full loader-chain triad detected."


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    fs = scan_dir(target)
    for f in fs:
        print(f)
    print()
    matched, verdict = loader_chain_verdict(fs)
    print(verdict)
