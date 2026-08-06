"""JobStore — durable job/part state and the event log.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §2.2 and
``docs/v2/01-IDENTITY-AND-SCRAPE.md`` §4.

Two properties matter more than anything else here:

1. **``archive_id`` is the only job key.** v1 keyed resume on the
   label-derived output directory. When the scraped label improved from
   ``gaia-1005482974000`` to ``braincreation`` the directory changed, the
   manager failed to recognise 2.8 TB already on disk, and began
   re-downloading everything. Identity is display metadata; it may be
   *upgraded*, and the folder is renamed underneath a job that never moves.

2. **Every mutation emits an event.** ``event.seq`` is a monotonic cursor, so
   an SSE client that disconnects can resume with ``?since=<seq>`` and miss
   nothing. v1 used a bounded 256-event queue that silently dropped events.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .contracts import (AccountIdentity, IdentityRecord, JobStatus, LabelSource,
                        PartStatus, VerifyState)

__all__ = ["JobStore", "JobRow", "PartRow", "Event", "SCHEMA"]

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS job (
  archive_id      TEXT PRIMARY KEY,
  account_label   TEXT NOT NULL,
  label_source    TEXT NOT NULL DEFAULT 'UNKNOWN',
  account_email   TEXT,
  gaia_user       TEXT,
  authuser        TEXT,
  export_ts       TEXT NOT NULL,
  export_raw      TEXT,
  output_dir      TEXT NOT NULL,
  parts_expected  INTEGER,
  status          TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  finished_at     TEXT,
  last_error      TEXT,
  attempt_budget  INTEGER NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS part (
  archive_id      TEXT NOT NULL,
  idx             INTEGER NOT NULL,
  filename        TEXT,
  url             TEXT,
  size_expected   INTEGER,
  size_on_disk    INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL,
  verify_state    TEXT NOT NULL DEFAULT 'UNVERIFIED',
  attempts_used   INTEGER NOT NULL DEFAULT 0,
  sha256          TEXT,
  first_started   TEXT,
  last_activity   TEXT,
  completed_at    TEXT,
  last_error      TEXT,
  PRIMARY KEY (archive_id, idx)
);

CREATE TABLE IF NOT EXISTS event (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_id      TEXT,
  ts              TEXT NOT NULL,
  kind            TEXT NOT NULL,
  payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_archive ON event(archive_id, seq);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobRow:
    archive_id: str
    account_label: str
    label_source: LabelSource
    export_ts: str
    output_dir: str
    status: JobStatus
    parts_expected: Optional[int] = None
    account_email: Optional[str] = None
    gaia_user: Optional[str] = None
    authuser: Optional[str] = None
    export_raw: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None
    attempt_budget: int = 5

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "JobRow":
        return cls(
            archive_id=row["archive_id"],
            account_label=row["account_label"],
            label_source=LabelSource(row["label_source"]),
            export_ts=row["export_ts"],
            output_dir=row["output_dir"],
            status=JobStatus(row["status"]),
            parts_expected=row["parts_expected"],
            account_email=row["account_email"],
            gaia_user=row["gaia_user"],
            authuser=row["authuser"],
            export_raw=row["export_raw"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
            last_error=row["last_error"],
            attempt_budget=row["attempt_budget"],
        )


@dataclass
class PartRow:
    archive_id: str
    idx: int
    status: PartStatus
    verify_state: VerifyState = VerifyState.UNVERIFIED
    filename: Optional[str] = None
    url: Optional[str] = None
    size_expected: Optional[int] = None
    size_on_disk: int = 0
    attempts_used: int = 0
    sha256: Optional[str] = None
    last_error: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PartRow":
        return cls(
            archive_id=row["archive_id"], idx=row["idx"],
            status=PartStatus(row["status"]),
            verify_state=VerifyState(row["verify_state"]),
            filename=row["filename"], url=row["url"],
            size_expected=row["size_expected"], size_on_disk=row["size_on_disk"],
            attempts_used=row["attempts_used"], sha256=row["sha256"],
            last_error=row["last_error"],
        )

    @property
    def remaining_bytes(self) -> Optional[int]:
        if self.size_expected is None:
            return None
        return max(0, self.size_expected - self.size_on_disk)


@dataclass
class Event:
    seq: int
    archive_id: Optional[str]
    ts: str
    kind: str
    data: dict

    def to_sse(self) -> str:
        body = json.dumps({"seq": self.seq, "ts": self.ts, "kind": self.kind,
                           "archive_id": self.archive_id, "data": self.data})
        return f"id: {self.seq}\nevent: {self.kind}\ndata: {body}\n\n"


class JobStore:
    """Thread-safe SQLite-backed job/part state with an append-only event log."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @classmethod
    def open(cls, path: str) -> "JobStore":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        return cls(sqlite3.connect(path, check_same_thread=False))

    # -- events ------------------------------------------------------------
    def emit(self, kind: str, archive_id: Optional[str] = None, **data: Any) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO event(archive_id, ts, kind, payload_json) VALUES (?,?,?,?)",
                (archive_id, _utcnow(), kind, json.dumps(data, default=str)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def events_since(self, seq: int = 0, archive_id: Optional[str] = None,
                     limit: int = 500) -> list[Event]:
        """Replay events after ``seq`` — this is what makes SSE resumable."""
        sql = "SELECT * FROM event WHERE seq > ?"
        args: list[Any] = [seq]
        if archive_id:
            sql += " AND archive_id = ?"
            args.append(archive_id)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Event(seq=r["seq"], archive_id=r["archive_id"], ts=r["ts"],
                      kind=r["kind"], data=json.loads(r["payload_json"]))
                for r in rows]

    def latest_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(seq),0) FROM event").fetchone()
        return int(row[0])

    # -- jobs --------------------------------------------------------------
    def upsert_job(self, identity: IdentityRecord, output_dir: str,
                   status: JobStatus = JobStatus.DISCOVERING,
                   attempt_budget: int = 5) -> JobRow:
        """Create a job, or return the existing one for this archive_id.

        Never creates a second job for an archive_id that already exists —
        that was v1's orphaning bug.
        """
        existing = self.get_job(identity.archive_id)
        if existing is not None:
            self.maybe_upgrade_identity(identity)
            return self.get_job(identity.archive_id)  # type: ignore[return-value]

        now = _utcnow()
        acct = identity.account
        with self._lock:
            self._conn.execute(
                """INSERT INTO job(archive_id, account_label, label_source,
                       account_email, gaia_user, authuser, export_ts, export_raw,
                       output_dir, parts_expected, status, created_at, updated_at,
                       attempt_budget)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity.archive_id, acct.folder_name(), acct.label_source.value,
                 acct.email, acct.gaia_user, acct.authuser, identity.export_ts,
                 identity.export_raw, output_dir, identity.parts_expected,
                 status.value, now, now, attempt_budget),
            )
            self._conn.commit()
        self.emit("job_created", identity.archive_id,
                  label=acct.folder_name(), export_ts=identity.export_ts,
                  parts_expected=identity.parts_expected)
        return self.get_job(identity.archive_id)  # type: ignore[return-value]

    def get_job(self, archive_id: str) -> Optional[JobRow]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM job WHERE archive_id=?", (archive_id,)).fetchone()
        return JobRow.from_row(row) if row else None

    def list_jobs(self, limit: int = 50, offset: int = 0) -> list[JobRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
        return [JobRow.from_row(r) for r in rows]

    def set_job_status(self, archive_id: str, status: JobStatus,
                       error: Optional[str] = None) -> None:
        finished = _utcnow() if status in (JobStatus.COMPLETE, JobStatus.FAILED) else None
        with self._lock:
            self._conn.execute(
                """UPDATE job SET status=?, updated_at=?,
                       finished_at=COALESCE(?, finished_at),
                       last_error=COALESCE(?, last_error)
                   WHERE archive_id=?""",
                (status.value, _utcnow(), finished, error, archive_id),
            )
            self._conn.commit()
        self.emit("job_status", archive_id, status=status.value, error=error)

    # -- identity upgrade --------------------------------------------------
    def maybe_upgrade_identity(self, identity: IdentityRecord,
                               rename: bool = True) -> bool:
        """Adopt a better label; ignore an equal or worse one.

        Returns True when an upgrade happened. The job row is never recreated
        and no bytes are moved between filesystems: only the directory entry
        is renamed, which is instantaneous on the same mount.
        """
        job = self.get_job(identity.archive_id)
        if job is None:
            return False

        current = AccountIdentity(
            gaia_user=job.gaia_user or "", authuser=job.authuser or "0",
            email=job.account_email, label=job.account_label,
            label_source=job.label_source,
        )
        if not identity.account.upgrades_over(current):
            return False

        new_label = identity.account.folder_name()
        old_dir = job.output_dir
        new_dir = old_dir
        if rename and os.path.basename(os.path.dirname(old_dir)) != new_label:
            candidate = os.path.join(
                os.path.dirname(os.path.dirname(old_dir)), new_label,
                os.path.basename(old_dir))
            if self._safe_rename(old_dir, candidate):
                new_dir = candidate

        with self._lock:
            self._conn.execute(
                """UPDATE job SET account_label=?, label_source=?, account_email=?,
                       output_dir=?, updated_at=? WHERE archive_id=?""",
                (new_label, identity.account.label_source.value,
                 identity.account.email or job.account_email, new_dir,
                 _utcnow(), identity.archive_id),
            )
            self._conn.commit()
        self.emit("identity_upgraded", identity.archive_id,
                  from_label=job.account_label, to_label=new_label,
                  from_source=job.label_source.value,
                  to_source=identity.account.label_source.value,
                  old_dir=old_dir, new_dir=new_dir)
        return True

    @staticmethod
    def _safe_rename(old_dir: str, new_dir: str) -> bool:
        """Rename only when safe; never move bytes across filesystems."""
        if old_dir == new_dir or not os.path.isdir(old_dir):
            return False
        if os.path.exists(new_dir):
            return False
        try:
            if os.stat(old_dir).st_dev != os.stat(
                    os.path.dirname(os.path.dirname(new_dir)) or ".").st_dev:
                return False  # different mount: leave the terabytes in place
        except OSError:
            return False
        try:
            os.makedirs(os.path.dirname(new_dir), exist_ok=True)
            os.rename(old_dir, new_dir)
        except OSError:
            return False
        # The rename has already succeeded; a failed durability fsync must not
        # be reported as a failed rename or the DB would keep the stale path
        # while the bytes live at the new one. Windows cannot fsync a
        # directory handle opened this way, so this is best-effort by design.
        try:
            fd = os.open(os.path.dirname(new_dir), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, AttributeError, PermissionError):
            pass
        return True

    # -- parts -------------------------------------------------------------
    def upsert_parts(self, archive_id: str, parts: Iterable) -> int:
        """Seed the part table from a PartPlan iterable. Idempotent."""
        count = 0
        with self._lock:
            for plan in parts:
                self._conn.execute(
                    """INSERT INTO part(archive_id, idx, filename, url,
                           size_expected, status)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(archive_id, idx) DO UPDATE SET
                           filename=COALESCE(excluded.filename, part.filename),
                           url=COALESCE(excluded.url, part.url),
                           size_expected=COALESCE(excluded.size_expected,
                                                  part.size_expected)""",
                    (archive_id, plan.idx, plan.filename, plan.url,
                     plan.size_expected, PartStatus.PENDING.value),
                )
                count += 1
            self._conn.commit()
        return count

    def get_part(self, archive_id: str, idx: int) -> Optional[PartRow]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM part WHERE archive_id=? AND idx=?",
                (archive_id, idx)).fetchone()
        return PartRow.from_row(row) if row else None

    def list_parts(self, archive_id: str, status: Optional[PartStatus] = None,
                   limit: int = 1000, offset: int = 0) -> list[PartRow]:
        sql = "SELECT * FROM part WHERE archive_id=?"
        args: list[Any] = [archive_id]
        if status is not None:
            sql += " AND status=?"
            args.append(status.value)
        sql += " ORDER BY idx LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [PartRow.from_row(r) for r in rows]

    def update_part(self, archive_id: str, idx: int, *,
                    status: Optional[PartStatus] = None,
                    verify_state: Optional[VerifyState] = None,
                    size_on_disk: Optional[int] = None,
                    size_expected: Optional[int] = None,
                    filename: Optional[str] = None,
                    sha256: Optional[str] = None,
                    error: Optional[str] = None,
                    bump_attempts: bool = False,
                    quiet: bool = False) -> None:
        sets, args = ["last_activity=?"], [_utcnow()]
        if status is not None:
            sets.append("status=?"); args.append(status.value)
            if status is PartStatus.ACTIVE:
                sets.append("first_started=COALESCE(first_started, ?)")
                args.append(_utcnow())
            if status is PartStatus.DONE:
                sets.append("completed_at=?"); args.append(_utcnow())
        if verify_state is not None:
            sets.append("verify_state=?"); args.append(verify_state.value)
        if size_on_disk is not None:
            sets.append("size_on_disk=?"); args.append(int(size_on_disk))
        if size_expected is not None:
            sets.append("size_expected=?"); args.append(int(size_expected))
        if filename is not None:
            sets.append("filename=?"); args.append(filename)
        if sha256 is not None:
            sets.append("sha256=?"); args.append(sha256)
        if error is not None:
            sets.append("last_error=?"); args.append(error)
        if bump_attempts:
            sets.append("attempts_used=attempts_used+1")

        args += [archive_id, idx]
        with self._lock:
            self._conn.execute(
                f"UPDATE part SET {', '.join(sets)} WHERE archive_id=? AND idx=?",
                args)
            self._conn.commit()

        # Byte-level progress is high-frequency; callers pass quiet=True to
        # avoid writing an event per chunk.
        if not quiet:
            payload = {"idx": idx}
            if status is not None:
                payload["status"] = status.value
            if verify_state is not None:
                payload["verify_state"] = verify_state.value
            if size_on_disk is not None:
                payload["size_on_disk"] = size_on_disk
            if error is not None:
                payload["error"] = error
            self.emit("part_update", archive_id, **payload)

    # -- aggregates --------------------------------------------------------
    def job_totals(self, archive_id: str) -> dict:
        """One grouped query — never an O(n) python loop over parts."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, COUNT(*) n,
                          COALESCE(SUM(size_on_disk),0) done,
                          COALESCE(SUM(size_expected),0) total
                     FROM part WHERE archive_id=? GROUP BY status""",
                (archive_id,)).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        return {
            "parts_total": sum(by_status.values()),
            "parts_done": by_status.get(PartStatus.DONE.value, 0),
            "parts_active": by_status.get(PartStatus.ACTIVE.value, 0),
            "parts_partial": by_status.get(PartStatus.PARTIAL.value, 0),
            "parts_failed": by_status.get(PartStatus.FAILED.value, 0),
            "parts_exhausted": by_status.get(PartStatus.BUDGET_EXHAUSTED.value, 0),
            "bytes_done": sum(r["done"] for r in rows),
            "bytes_total": sum(r["total"] for r in rows),
            "by_status": by_status,
        }

    # -- restart recovery --------------------------------------------------
    def recover(self) -> list[str]:
        """Re-home jobs interrupted by a restart.

        Anything mid-flight becomes NEEDS_COOKIE: the process died, so any
        cookie we held is long dead. Terminal jobs are left alone.
        """
        moved = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT archive_id FROM job WHERE status IN (?,?,?)",
                (JobStatus.DOWNLOADING.value, JobStatus.DISCOVERING.value,
                 JobStatus.VERIFYING.value)).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE job SET status=?, updated_at=? WHERE archive_id=?",
                    (JobStatus.NEEDS_COOKIE.value, _utcnow(), row["archive_id"]))
                moved.append(row["archive_id"])
            # An ACTIVE part was mid-stream; its bytes are on disk, so it is
            # resumable rather than failed.
            self._conn.execute(
                "UPDATE part SET status=? WHERE status=?",
                (PartStatus.PARTIAL.value, PartStatus.ACTIVE.value))
            self._conn.commit()
        for archive_id in moved:
            self.emit("job_recovered", archive_id, new_status=JobStatus.NEEDS_COOKIE.value)
        return moved
