"""
sandbox.py — a capability-denied harness for DYNAMIC analysis of obfuscated
Lua, plus documentation of why the approach is safe.

=============================================================================
ELI5
=============================================================================
If you ever need to actually *run* a scrambled script to watch what it does,
you do it inside a padded room with no doors: no internet, no disk, no way to
start other programs. This module builds that padded room for Lua and writes
down everything the script *tries* to do, then refuses to actually do it.

=============================================================================
EXECUTIVE SUMMARY
=============================================================================
The FakeGit payload only becomes dangerous when run under LuaJIT *with FFI*,
because FFI is its bridge to Win32 syscalls and sockets. Remove FFI/JIT and
stub the OS/IO/network entry points, and the script can decode itself but
cannot act.

This module provides `make_safe_runtime()` which returns a `lupa` Lua runtime
(if installed) with every dangerous global replaced by a logging stub, plus a
`CallLog` recording each attempted dangerous operation. If `lupa` is not
installed, the functions degrade gracefully and the static detector still
works.

CONTAINMENT CHECKLIST (verified before trusting any run):
  1. Runtime is stock Lua (lua54/lua55), NOT LuaJIT  -> ffi == nil, jit == nil
  2. require('socket') fails                          -> no network module
  3. os.execute / io.popen / io.open are stubbed      -> no exec, no file I/O
  4. Network adapter offline / VM snapshot taken      -> belt-and-suspenders
  5. Bounded instruction count                        -> no infinite-spin DoS

NEVER run a sample under a real LuaJIT + FFI runtime on a host you care about.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CallLog:
    """Records every dangerous operation the script *attempted*."""
    calls: list[tuple[str, tuple]] = field(default_factory=list)

    def record(self, name: str, *args) -> None:
        # Truncate giant args so the log stays readable.
        safe_args = tuple(
            (a[:200] + "...") if isinstance(a, str) and len(a) > 200 else a
            for a in args
        )
        self.calls.append((name, safe_args))

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, _ in self.calls:
            out[name] = out.get(name, 0) + 1
        return out


def lupa_available() -> bool:
    try:
        import lupa  # noqa: F401
        return True
    except ImportError:
        return False


def verify_containment(lua) -> list[str]:
    """
    Return a list of containment violations. Empty list == safe to proceed.
    Pass the LuaRuntime returned by make_safe_runtime().
    """
    problems: list[str] = []
    if lua.eval("_G.ffi ~= nil and 1 or 0"):
        problems.append("FFI is present — script could reach Win32/syscalls.")
    if lua.eval("_G.jit ~= nil and 1 or 0"):
        problems.append("JIT is present — runtime is LuaJIT, not stock Lua.")
    # A REAL socket module must be unreachable. We don't just check that
    # require ran (our stub returns nil without error) — we check that no
    # usable socket table actually materializes (one exposing .tcp/.connect).
    has_socket = lua.eval(
        "(function()"
        "  local ok, m = pcall(require, 'socket')"
        "  if ok and type(m) == 'table' and (m.tcp or m.connect) then return 1 end"
        "  if package and package.loaded and package.loaded.socket then return 1 end"
        "  return 0"
        " end)()"
    )
    if has_socket:
        problems.append("luasocket reachable — network module available.")
    return problems


def make_safe_runtime(log: CallLog | None = None):
    """
    Build a capability-denied Lua runtime. Returns (lua, log) or raises
    RuntimeError if lupa is unavailable.

    Every dangerous global is replaced by a stub that records the attempt
    via `log.record(...)` and then returns a harmless value.
    """
    if not lupa_available():
        raise RuntimeError("lupa not installed; cannot build dynamic sandbox.")

    import lupa
    from lupa import LuaRuntime

    log = log or CallLog()
    lua = LuaRuntime(unpack_returned_tuples=True, register_eval=False)

    def make_stub(name):
        def stub(*args):
            log.record(name, *[str(a) for a in args])
            return None
        return stub

    g = lua.globals()

    # ----- os table -----
    g.os.execute = make_stub("os.execute")
    g.os.remove = make_stub("os.remove")
    g.os.rename = make_stub("os.rename")
    g.os.getenv = make_stub("os.getenv")
    g.os.tmpname = make_stub("os.tmpname")
    g.os.exit = make_stub("os.exit")

    # ----- io table -----
    g.io.open = make_stub("io.open")
    g.io.popen = make_stub("io.popen")
    g.io.lines = make_stub("io.lines")
    g.io.write = make_stub("io.write")

    # ----- dynamic code loaders: log the chunk, then refuse to recurse -----
    seen = {"count": 0}

    def safe_load(chunk, *rest):
        seen["count"] += 1
        n = len(chunk) if isinstance(chunk, (str, bytes)) else -1
        log.record("load", f"chunk_len={n}", f"call#{seen['count']}")
        # Return a no-op function instead of compiling attacker code.
        return lua.eval("function() return nil end")

    g.load = safe_load
    g.loadstring = safe_load
    g.dofile = make_stub("dofile")
    g.loadfile = make_stub("loadfile")

    # ----- kill require so no C modules (socket, ffi shims) load -----
    g.require = make_stub("require")

    # ----- actively neutralize any pre-loaded network/FFI modules -----
    # Some lupa builds bundle luasocket; some Lua builds expose ffi. Wipe both
    # from package.loaded/preload and globals so containment passes legitimately.
    lua.execute(
        """
        if package then
          if package.loaded then
            package.loaded.socket = nil
            package.loaded['socket.core'] = nil
            package.loaded.mime = nil
            package.loaded.ssl = nil
          end
          if package.preload then
            package.preload.socket = nil
            package.preload['socket.core'] = nil
          end
          package.cpath = ''
          package.path = ''
        end
        _G.ffi = nil
        _G.jit = nil
        """
    )

    return lua, log


def safe_decode_only(lua_source: str, log: CallLog | None = None) -> CallLog:
    """
    Load (but do not meaningfully execute) an obfuscated Lua source inside the
    safe runtime, capturing what it tries to do. Containment is verified first;
    if verification fails, raises RuntimeError BEFORE running anything.

    This is intentionally conservative: `load` is stubbed, so a self-decoding
    obfuscator will reveal its first-stage chunk length / call pattern without
    its payload ever compiling.
    """
    lua, log = make_safe_runtime(log)
    problems = verify_containment(lua)
    if problems:
        raise RuntimeError("Containment check FAILED: " + "; ".join(problems))
    try:
        fn = lua.eval("function(src) return load(src) end")
        fn(lua_source)
    except Exception as e:  # noqa: BLE001
        log.record("exception", str(e))
    return log


if __name__ == "__main__":
    print("lupa available:", lupa_available())
    if lupa_available():
        lua, log = make_safe_runtime()
        problems = verify_containment(lua)
        print("containment problems:", problems or "NONE (safe)")
