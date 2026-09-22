"""The ledger: what we know, what we have, and what we have already paid for.

Three rules here are load-bearing, and each comes from a measurement or a
documented failure rather than taste.

1. **Never on a FUSE mount.** The live v1 deployment keeps a WAL-mode SQLite
   database on the rclone mount (`/opt/archives/google-takeout/state.db`), which
   is a corruption risk on a network filesystem. `open_ledger` refuses such a
   path outright. The check is a pure function over a mount table so it is
   testable without the host's real `/proc/mounts`.

2. **Minting is the scarce resource, not requesting.** Measured: a mint is `Δ1`,
   a `Range` resume is `Δ0`. So the ledger caches a minted URL per part and
   exposes `needs_mint()` — re-minting a part that already holds a valid URL is
   spending a scarce attempt for nothing.

3. **`dl_counts` is telemetry, never an invariant.** The manage-page counter gave
   `Δ2` and then `Δ0` for superficially identical actions before a cache hit was
   identified as the explanation. It is stored for the operator to read and is
   deliberately **not** consulted by any decision in this module.

Attempts are recorded per *kind* so a budget can tell the two apart.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from .errors import AutopilotError

__all__ = [
    "JOB_STATUSES",
    "PART_STATUSES",
    "AttemptKind",
    "LedgerError",
    "LedgerOnFuseMount",
    "fuse_mount_for",
    "inspect_ledger_path",
    "open_ledger",
    "Ledger",
]

JOB_STATUSES = (
    # NOTE: `incomplete` is here because `autopilot.run.RunOutcome.status` and this
    # vocabulary meet at exactly this point. A pass that ends with work remaining
    # leaves the job in a state meaning "more to do"; `pending` would imply "not
    # started", which is false. Found by `tests/v3/test_integration.py` — the
    # orchestrator wrote its own status straight into the ledger and the two lists
    # had silently disagreed. No unit test could see it, because `run.py` was
    # written after them.
    "pending", "scraping", "minting", "needs_reauth", "transferring",
    "verifying", "moving", "incomplete", "complete",
    "expired_unrecoverable", "quota_exceeded", "failed",
)
PART_STATUSES = ("pending", "active", "partial", "done", "failed", "budget_exhausted")


class AttemptKind:
    MINT = "mint"          # measured Δ1
    TRANSFER = "transfer"  # a fresh full GET — cost UNMEASURED, do not assume 0
    RESUME = "resume"      # measured Δ0


#: Names the scrape produces instead of a real part name.
#:
#: The scrape's filename comes from the redirector's basename, which is the literal
#: string `download` for EVERY part (measured 2026-09-19), so it carries no part
#: identity and must never displace a name that is real.
PLACEHOLDER_PART_NAMES = frozenset({"download", "download.zip"})


def carries_part_identity(name: str) -> bool:
    """Whether a filename carries part identity rather than being a placeholder.

    Pure, so the rule that decides when a recorded filename is preserved is testable
    without a database.

    **This predicate replaced one that destroyed data.** The previous rule was a regex
    matching only `takeout-<stamp>-N-NNN.zip`, and it gated the preserve-the-recorded-name
    check as `looks_like_part_filename(current)`. A part whose real name was
    `Louie 2018-057.mp4` therefore FAILED the test, the guard did not fire, and the
    scrape's placeholder `download` overwrote the real name. Measured 2026-09-22: a
    single re-scrape blanked **25 of the 62-export's 68 filenames** — every non-zip part —
    and reset them to `pending`. A guard that is right about zips and actively wrong about
    everything else is worse than no guard, because the comment above it promises that
    recorded names are safe.

    The rule is about the INCOMING value, not the stored one: a placeholder must never
    displace anything. Everything else is worth keeping, including names no pattern could
    have predicted — `.mp4`, `.mbox`, and whatever the next export type turns out to be.
    """
    n = (name or "").strip()
    if not n:
        return False
    if n.lower() in PLACEHOLDER_PART_NAMES:
        return False
    # Every real part name observed has an extension. A bare word is not one.
    return "." in n


def filename_aliases(name: str) -> list:
    """Every spelling of `name` that identifies the same part on disk.

    A ledger written before the percent-decoding fix holds
    `All%20mail%20Including%20Spam%20and%20Trash-002.mbox` while the file on disk is
    `All mail Including Spam and Trash-002.mbox`. Both name the same part. Without this,
    the 64-product export reports **18/19 and 13.58 GB "short" on a complete archive** —
    measured 2026-09-22 — because the report identifies parts by name, so the exact-size
    match never runs.

    Ordered most-specific first, so the stored spelling is preferred over a decoded
    guess. A caller must still verify by size: this says "the same name", never "the
    same bytes".
    """
    from urllib.parse import unquote

    n = (name or "").strip()
    if not n:
        return []
    out = [n]
    decoded = unquote(n)
    if decoded != n:
        out.append(decoded)
    return out


class LedgerError(AutopilotError):
    pass


class LedgerOnFuseMount(LedgerError):
    """Refused to open a ledger on a network-backed FUSE filesystem."""


# ---------------------------------------------------------------------------
# Mount safety (pure, so it is testable without the host)
# ---------------------------------------------------------------------------
def fuse_mount_for(path: str, mounts: Iterable[tuple[str, str]]) -> Optional[str]:
    """Return the FUSE mount point containing `path`, or None.

    `mounts` is an iterable of `(mount_point, fstype)` as read from
    `/proc/mounts`. The **longest** matching mount point wins, which is the
    correct semantics when mounts nest.
    """
    target = _normalize(path)
    # Find the LONGEST matching mount point whatever its type, then ask whether
    # that one is FUSE. A deeper local mount shadows a shallower FUSE one, so
    # filtering on FUSE-ness first would wrongly refuse a path that actually
    # lives on local storage (and vice versa).
    best: Optional[tuple[int, str, str]] = None
    for point, fstype in mounts:
        prefix = _normalize(point)
        if target == prefix or target.startswith(prefix.rstrip("/") + "/"):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), prefix, fstype)
    if best is not None and best[2].startswith("fuse"):
        return best[1]
    return None


def _normalize(path: str) -> str:
    """Lexically normalise a path **without** host filesystem semantics.

    Deliberately NOT `os.path.abspath`: on Windows that rewrites a POSIX path
    like `/opt/archives/state.db` into `C:/opt/archives/state.db`, which can
    never match a `/proc/mounts`-style mount point — so the FUSE guard would
    silently become a no-op on exactly the machine where it is developed.
    `tests/v3/test_ledger.py` caught that.
    """
    p = (path or "").replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def _read_mounts() -> list[tuple[str, str]]:
    """`/proc/mounts` on POSIX; empty elsewhere (the check degrades to a no-op)."""
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            out = []
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    out.append((parts[1], parts[2]))
            return out
    except OSError:
        return []


def inspect_ledger_path(path: str, mounts: Optional[Iterable[tuple[str, str]]] = None) -> None:
    """Raise `LedgerOnFuseMount` if `path` would live on a FUSE filesystem."""
    table = list(mounts) if mounts is not None else _read_mounts()
    hit = fuse_mount_for(path, table)
    if hit:
        raise LedgerOnFuseMount(
            f"refusing to put the ledger under the FUSE mount {hit!r} "
            f"(path={path!r}) — SQLite WAL on a network filesystem risks "
            "corrupting the only record of what has been paid for"
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    archive_id  TEXT PRIMARY KEY,
    account     TEXT,
    status      TEXT NOT NULL,
    output_dir  TEXT,
    expiry_at   TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parts (
    archive_id   TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    filename     TEXT,
    size_expected INTEGER,
    size_on_disk  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    verify_state  TEXT,
    -- minted-URL cache: the point of the ledger
    minted_url    TEXT,
    minted_at     TEXT,
    etag          TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    -- telemetry only. NEVER read by a decision in this module.
    dl_count_seen INTEGER,
    error         TEXT,
    PRIMARY KEY (archive_id, idx)
);

CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id  TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    outcome     TEXT,
    bytes_moved INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS attempts_by_part ON attempts (archive_id, idx);
"""


@dataclass
class PartRow:
    idx: int
    filename: Optional[str]
    size_expected: Optional[int]
    size_on_disk: int
    status: str
    minted_url: Optional[str]
    attempts: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_ledger(path: str, mounts: Optional[Iterable[tuple[str, str]]] = None) -> "Ledger":
    """Open (creating if needed) a ledger, refusing FUSE-backed paths."""
    inspect_ledger_path(path, mounts)
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return Ledger(conn)


class Ledger:
    """Thin, explicit SQLite wrapper. No ORM, no magic."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def close(self) -> None:
        self.conn.close()

    # -- jobs ---------------------------------------------------------------
    def upsert_job(self, archive_id: str, *, account: Optional[str] = None,
                   output_dir: Optional[str] = None,
                   expiry_at: Optional[str] = None) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO jobs (archive_id, account, status, output_dir, expiry_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(archive_id) DO UPDATE SET "
            " account=COALESCE(excluded.account, jobs.account),"
            " output_dir=COALESCE(excluded.output_dir, jobs.output_dir),"
            " expiry_at=COALESCE(excluded.expiry_at, jobs.expiry_at),"
            " updated_at=excluded.updated_at",
            (archive_id, account, "pending", output_dir, expiry_at, now, now),
        )
        self.conn.commit()

    def set_job_status(self, archive_id: str, status: str, *,
                       error: Optional[str] = None) -> None:
        if status not in JOB_STATUSES:
            raise LedgerError(f"unknown job status {status!r}; expected one of {JOB_STATUSES}")
        self.conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=? WHERE archive_id=?",
            (status, error, _now(), archive_id),
        )
        self.conn.commit()

    def job(self, archive_id: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM jobs WHERE archive_id=?", (archive_id,))
        return cur.fetchone()

    # -- parts --------------------------------------------------------------
    def upsert_parts(self, archive_id: str,
                     parts: Iterable[tuple[int, Optional[str], Optional[int]]]) -> int:
        """Seed parts from a scrape as `(idx, filename, size_expected)`.

        Existing rows keep their progress: only the descriptive fields are
        refreshed, so re-scraping cannot wipe a partial download's state.
        """
        n = 0
        for idx, filename, size in parts:
            # The scrape's filename comes from the redirector's basename, which is
            # the literal string `download` for EVERY part (measured 2026-09-19) —
            # so it carries no part identity and must never replace a name that is
            # real. `set_part_filename()` records the real one after minting.
            #
            # The test is applied to the INCOMING value, because the stored name is the
            # thing worth protecting. It used to be applied to the STORED value through
            # a zip-only regex, which meant a recorded `Louie 2018-057.mp4` did not look
            # "real", the guard did not fire, and the placeholder overwrote it — 25 of
            # 68 filenames in one re-scrape. See `carries_part_identity`.
            incoming = filename or None
            existing = self.part(archive_id, idx)
            if existing is not None:
                current = (existing["filename"] or "").strip()
                if current and not carries_part_identity(incoming or ""):
                    incoming = current
            self.conn.execute(
                "INSERT INTO parts (archive_id, idx, filename, size_expected, status) "
                "VALUES (?,?,?,?,'pending') "
                "ON CONFLICT(archive_id, idx) DO UPDATE SET "
                " filename=COALESCE(excluded.filename, parts.filename),"
                " size_expected=COALESCE(excluded.size_expected, parts.size_expected)",
                (archive_id, idx, incoming, size),
            )
            n += 1
        self.conn.commit()
        return n

    def part(self, archive_id: str, idx: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM parts WHERE archive_id=? AND idx=?", (archive_id, idx))
        return cur.fetchone()

    def parts(self, archive_id: str) -> list[PartRow]:
        cur = self.conn.execute(
            "SELECT idx, filename, size_expected, size_on_disk, status, minted_url, attempts"
            " FROM parts WHERE archive_id=? ORDER BY idx", (archive_id,))
        return [PartRow(**dict(r)) for r in cur.fetchall()]

    def set_part_status(self, archive_id: str, idx: int, status: str,
                        *, error: Optional[str] = None) -> None:
        if status not in PART_STATUSES:
            raise LedgerError(f"unknown part status {status!r}; expected {PART_STATUSES}")
        self.conn.execute(
            "UPDATE parts SET status=?, error=? WHERE archive_id=? AND idx=?",
            (status, error, archive_id, idx),
        )
        self.conn.commit()

    def set_part_filename(self, archive_id: str, idx: int, filename: str) -> None:
        """Record the part's REAL filename once it is known.

        The scrape can only see the redirector path, whose basename is the
        literal string `download` for every part of every export — measured
        2026-09-19. The true name only becomes knowable after minting, when the
        file host's URL exposes it, so it is written here rather than at upsert
        time. Without this the ledger would describe every part of a multi-part
        export by the same name.
        """
        if not filename:
            return
        self.conn.execute(
            "UPDATE parts SET filename=? WHERE archive_id=? AND idx=?",
            (filename, archive_id, idx),
        )
        self.conn.commit()

    # -- the minted-URL cache (rule 2) --------------------------------------
    def record_mint(self, archive_id: str, idx: int, url: str,
                    *, etag: Optional[str] = None) -> None:
        """Cache a minted URL and book the attempt it cost."""
        now = _now()
        self.conn.execute(
            "UPDATE parts SET minted_url=?, minted_at=?, etag=COALESCE(?, etag),"
            " attempts=attempts+1 WHERE archive_id=? AND idx=?",
            (url, now, etag, archive_id, idx),
        )
        self._attempt(archive_id, idx, AttemptKind.MINT, "ok", 0, "minted")
        self.conn.commit()

    def minted_url(self, archive_id: str, idx: int) -> Optional[str]:
        row = self.part(archive_id, idx)
        return row["minted_url"] if row else None

    def needs_mint(self, archive_id: str, idx: int) -> bool:
        """False when a minted URL is already cached.

        Re-minting is `Δ1` for nothing, so the scheduler should ask this before
        touching the browser.
        """
        return not self.minted_url(archive_id, idx)

    def clear_mint(self, archive_id: str, idx: int) -> None:
        """Drop a cached URL (e.g. after the file host rejected it)."""
        self.conn.execute(
            "UPDATE parts SET minted_url=NULL, minted_at=NULL WHERE archive_id=? AND idx=?",
            (archive_id, idx),
        )
        self.conn.commit()

    # -- transfer accounting -------------------------------------------------
    def record_transfer(self, archive_id: str, idx: int, kind: str, outcome: str,
                        *, bytes_moved: int = 0, size_on_disk: Optional[int] = None,
                        etag: Optional[str] = None) -> None:
        if kind not in (AttemptKind.TRANSFER, AttemptKind.RESUME):
            raise LedgerError(f"kind must be transfer/resume, not {kind!r}")
        sets = ["attempts=attempts+1"]
        args: list = []
        if size_on_disk is not None:
            sets.append("size_on_disk=?")
            args.append(size_on_disk)
        if etag:
            sets.append("etag=?")
            args.append(etag)
        args.extend([archive_id, idx])
        self.conn.execute(
            f"UPDATE parts SET {', '.join(sets)} WHERE archive_id=? AND idx=?", args)
        self._attempt(archive_id, idx, kind, outcome, bytes_moved, None)
        self.conn.commit()

    def record_failed_transfer(self, archive_id: str, idx: int, exc: Exception) -> None:
        """Book a transfer attempt that FAILED.

        Fixed 2026-09-19. `record_transfer` sits *after* the try/except in
        `run_once`, so every failure path skipped it — meaning a failed file-host
        GET made a real HTTP request and left no `attempts` row and no counter
        bump. The report then printed `transfer 0` for runs that had genuinely
        contacted Google, and the attempt count is the only budget signal here.

        The kind is recorded as `transfer` rather than `resume` deliberately: a
        request that failed tells us nothing about whether it was a resume, and
        claiming the cheap kind would understate the cost.
        """
        message = f"{type(exc).__name__}: {exc}"
        self.conn.execute(
            "UPDATE parts SET attempts=attempts+1 WHERE archive_id=? AND idx=?",
            (archive_id, idx),
        )
        self._attempt(archive_id, idx, AttemptKind.TRANSFER, "failed", 0, message[:200])
        self.conn.commit()

    def failed_transfer_count(self, archive_id: str, idx: int) -> int:
        """How many transfer attempts for this part have failed.

        Used to bound how long a cached minted URL is retried before it is
        discarded: re-minting costs Δ1 and an export allows only 5 attempts in
        total, so one transient blip must not trigger a fresh mint — but neither
        may a genuinely dead URL be retried forever.
        """
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE archive_id=? AND idx=? "
            "AND kind=? AND outcome='failed'",
            (archive_id, idx, AttemptKind.TRANSFER),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def observe_dl_count(self, archive_id: str, idx: int, value: int) -> None:
        """Record the manage-page counter. Telemetry only — rule 3.

        Nothing in this module reads this column, by design.
        """
        self.conn.execute(
            "UPDATE parts SET dl_count_seen=? WHERE archive_id=? AND idx=?",
            (value, archive_id, idx),
        )
        self.conn.commit()

    def _attempt(self, archive_id: str, idx: int, kind: str, outcome: str,
                 bytes_moved: int, note: Optional[str]) -> None:
        self.conn.execute(
            "INSERT INTO attempts (archive_id, idx, kind, outcome, bytes_moved, note, at)"
            " VALUES (?,?,?,?,?,?,?)",
            (archive_id, idx, kind, outcome, bytes_moved, note, _now()),
        )

    # -- reporting -----------------------------------------------------------
    def attempts_by_kind(self, archive_id: str) -> dict:
        cur = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM attempts WHERE archive_id=? GROUP BY kind",
            (archive_id,))
        return {r["kind"]: r["n"] for r in cur.fetchall()}

    def attempt_kinds_by_part(self, archive_id: str) -> dict:
        """`idx -> set(attempt kinds)`, for the report's zero-bytes warning.

        That warning exists for the v2 signature — a TRANSFER booked without a request
        ever being sent. A MINT also books an attempt and moves zero bytes, entirely
        legitimately, so a bare count cannot distinguish the two. Observed live
        2026-09-21: a 5-part `--mint-only` run warned about every single part.
        """
        cur = self.conn.execute(
            "SELECT idx, kind FROM attempts WHERE archive_id=?", (archive_id,))
        out: dict = {}
        for row in cur.fetchall():
            out.setdefault(int(row["idx"]), set()).add(row["kind"])
        return out

    def summary(self, archive_id: str) -> dict:
        rows = self.parts(archive_id)
        by_status: dict[str, int] = {}
        for p in rows:
            by_status[p.status] = by_status.get(p.status, 0) + 1
        done = by_status.get("done", 0)
        return {
            "archive_id": archive_id,
            "parts": len(rows),
            "done": done,
            "remaining": len(rows) - done,
            "by_status": by_status,
            "attempts": self.attempts_by_kind(archive_id),
        }
