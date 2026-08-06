"""AttemptLedger — the gate every Google-bound request must pass through.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §1.

The central rule of this project::

    No module may call requests/httpx/curl/aria2c against a Google host
    without first obtaining a Reservation from AttemptLedger.reserve().

Why a ledger and not a counter? Because the process can die mid-download. A
plain counter incremented after the fact would under-count and let us exceed
Google's ~5-download ceiling, permanently burning an archive that takes days
to regenerate. So we write the intent to durable storage BEFORE the request,
and reconcile on startup with a fail-closed assumption: a reservation that was
never settled is assumed to have reached Google.

Ground truth: Google's own "Number of times already downloaded: N" counter,
scraped free from the manage page, outranks our local estimate. See
``docs/v2/01-IDENTITY-AND-SCRAPE.md`` §8.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

from .contracts import DEFAULTS, CostClass, ReasonCode

__all__ = ["BudgetExhausted", "Reservation", "PartBudget", "AttemptLedger"]


def _coerce_cost(value) -> CostClass:
    """Accept either a CostClass or its string value (defensive)."""
    if isinstance(value, CostClass):
        return value
    try:
        return CostClass(value)
    except ValueError:
        raise ValueError(f"unknown cost class {value!r}") from None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BudgetExhausted(RuntimeError):
    """Raised when a reservation would exceed the per-part attempt ceiling."""

    def __init__(self, archive_id: str, idx: int, used: int, budget: int, reserve: int):
        self.archive_id, self.idx = archive_id, idx
        self.used, self.budget, self.reserve = used, budget, reserve
        super().__init__(
            f"part {idx} of archive {archive_id[:12]}: {used}/{budget} attempts used "
            f"(reserve={reserve}). Refusing to spend another attempt. "
            f"Use force=True only if you accept possibly burning the archive."
        )


@dataclass
class Reservation:
    """A durable intent-to-request. Settle it exactly once."""

    id: int
    archive_id: str
    idx: int
    cost_class: CostClass
    _ledger: "AttemptLedger"
    _settled: bool = False

    @property
    def settled(self) -> bool:
        return self._settled

    def commit(self, outcome: ReasonCode, bytes_moved: int = 0,
               http_status: Optional[int] = None, note: str = "") -> None:
        """Record the real outcome of the request."""
        if self._settled:
            raise RuntimeError(f"reservation {self.id} already settled")
        self._ledger._settle(self.id, outcome, bytes_moved, http_status, note)
        self._settled = True

    def release(self, note: str = "not sent") -> None:
        """Cancel a reservation for a request that was NEVER sent.

        Only legitimate when we are certain no packet reached Google — e.g. we
        aborted during pre-flight. If in doubt, commit with the real outcome
        instead; over-counting is safe, under-counting is not.
        """
        if self._settled:
            return
        self._ledger._release(self.id, note)
        self._settled = True

    def __enter__(self) -> "Reservation":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._settled:
            # Fail closed: an exception mid-request means it may well have
            # reached Google. Count it.
            self._ledger._settle(
                self.id,
                ReasonCode.ABORTED if exc_type is None else ReasonCode.NETWORK_ERROR,
                0, None, f"auto-settled on exit: {exc_type.__name__ if exc_type else 'no commit'}",
            )
            self._settled = True
        return False


@dataclass(frozen=True)
class PartBudget:
    """A part's attempt accounting, blending local ledger and Google's count."""

    archive_id: str
    idx: int
    local_used: int
    remote_used: Optional[int]
    budget: int
    reserve: int

    @property
    def effective_used(self) -> int:
        """Google's count wins when higher — it sees attempts we never made."""
        return max(self.local_used, self.remote_used or 0)

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.effective_used)

    @property
    def spendable(self) -> int:
        """Attempts we will spend without an explicit override."""
        return max(0, self.remaining - self.reserve)

    @property
    def exhausted(self) -> bool:
        return self.spendable <= 0

    @property
    def at_risk(self) -> bool:
        return self.remaining <= self.reserve + 1


class AttemptLedger:
    """Durable, thread-safe attempt accounting backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection,
                 budget: int = DEFAULTS["ATTEMPT_BUDGET"],
                 reserve: int = DEFAULTS["BUDGET_RESERVE"]):
        self._conn = conn
        self._budget = int(budget)
        self._reserve = int(reserve)
        self._lock = threading.RLock()
        self._ensure_schema()

    # -- schema -----------------------------------------------------------
    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA busy_timeout=10000;

                CREATE TABLE IF NOT EXISTS attempt (
                  id            INTEGER PRIMARY KEY AUTOINCREMENT,
                  archive_id    TEXT NOT NULL,
                  idx           INTEGER NOT NULL,
                  cost_class    TEXT NOT NULL,
                  reserved_at   TEXT NOT NULL,
                  settled_at    TEXT,
                  outcome       TEXT,
                  bytes_moved   INTEGER NOT NULL DEFAULT 0,
                  http_status   INTEGER,
                  note          TEXT
                );
                CREATE INDEX IF NOT EXISTS attempt_part
                  ON attempt(archive_id, idx);

                CREATE TABLE IF NOT EXISTS remote_count (
                  archive_id  TEXT NOT NULL,
                  idx         INTEGER NOT NULL,
                  count       INTEGER NOT NULL,
                  observed_at TEXT NOT NULL,
                  PRIMARY KEY (archive_id, idx)
                );
                """
            )
            self._conn.commit()

    # -- counting ---------------------------------------------------------
    def local_used(self, archive_id: str, idx: int) -> int:
        """Attempts charged locally: anything not FREE and not released."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) FROM attempt
                 WHERE archive_id = ? AND idx = ?
                   AND cost_class != ?
                   AND (settled_at IS NULL OR outcome IS NOT 'RELEASED')
                """,
                (archive_id, idx, CostClass.FREE.value),
            ).fetchone()
            return int(row[0] or 0)

    def remote_used(self, archive_id: str, idx: int) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM remote_count WHERE archive_id=? AND idx=?",
                (archive_id, idx),
            ).fetchone()
            return None if row is None else int(row[0])

    def budget_for(self, archive_id: str, idx: int) -> PartBudget:
        return PartBudget(
            archive_id=archive_id, idx=idx,
            local_used=self.local_used(archive_id, idx),
            remote_used=self.remote_used(archive_id, idx),
            budget=self._budget, reserve=self._reserve,
        )

    def observe_remote(self, archive_id: str, idx: int, count: int) -> None:
        """Record Google's own counter for this part (free, from the scrape)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO remote_count(archive_id, idx, count, observed_at)
                VALUES (?,?,?,?)
                ON CONFLICT(archive_id, idx) DO UPDATE SET
                  count=excluded.count, observed_at=excluded.observed_at
                """,
                (archive_id, idx, int(count), _utcnow()),
            )
            self._conn.commit()

    # -- reserve / settle --------------------------------------------------
    def reserve(self, archive_id: str, idx: int,
                cost_class=CostClass.PAYLOAD,
                force: bool = False, note: str = "") -> Reservation:
        """Reserve an attempt BEFORE issuing the request.

        Raises ``BudgetExhausted`` unless ``force=True``.
        """
        cost_class = _coerce_cost(cost_class)
        with self._lock:
            if cost_class.counts_against_budget and not force:
                budget = self.budget_for(archive_id, idx)
                if budget.exhausted:
                    raise BudgetExhausted(archive_id, idx, budget.effective_used,
                                          self._budget, self._reserve)
            cur = self._conn.execute(
                """INSERT INTO attempt(archive_id, idx, cost_class, reserved_at, note)
                   VALUES (?,?,?,?,?)""",
                (archive_id, idx, cost_class.value, _utcnow(), note or None),
            )
            self._conn.commit()
            return Reservation(id=int(cur.lastrowid), archive_id=archive_id,
                               idx=idx, cost_class=cost_class, _ledger=self)

    def _settle(self, attempt_id: int, outcome: ReasonCode, bytes_moved: int,
                http_status: Optional[int], note: str) -> None:
        if not isinstance(outcome, ReasonCode):
            outcome = ReasonCode(outcome)
        with self._lock:
            self._conn.execute(
                """UPDATE attempt
                      SET settled_at=?, outcome=?, bytes_moved=?, http_status=?,
                          note=COALESCE(NULLIF(?,''), note)
                    WHERE id=?""",
                (_utcnow(), outcome.value, int(bytes_moved), http_status, note, attempt_id),
            )
            self._conn.commit()

    def _release(self, attempt_id: int, note: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE attempt
                      SET settled_at=?, outcome='RELEASED', note=?
                    WHERE id=?""",
                (_utcnow(), note, attempt_id),
            )
            self._conn.commit()

    @contextmanager
    def attempt(self, archive_id: str, idx: int,
                cost_class=CostClass.PAYLOAD,
                force: bool = False, note: str = "") -> Iterator[Reservation]:
        """Context-manager form; auto-settles if the body forgets to commit."""
        reservation = self.reserve(archive_id, idx, cost_class, force, note)
        with reservation as r:
            yield r

    # -- crash recovery ----------------------------------------------------
    def reconcile_orphans(self) -> int:
        """Settle reservations left dangling by a crash. FAIL CLOSED.

        We cannot know whether an unsettled request reached Google, so we
        assume it did. Over-counting costs us a retry; under-counting can
        permanently burn an archive.
        """
        with self._lock:
            cur = self._conn.execute(
                """UPDATE attempt
                      SET settled_at=?, outcome=?,
                          note=COALESCE(note,'') || ' [orphan: assumed consumed]'
                    WHERE settled_at IS NULL""",
                (_utcnow(), ReasonCode.ABORTED.value),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    # -- reporting ---------------------------------------------------------
    def archive_totals(self, archive_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(bytes_moved),0)
                     FROM attempt
                    WHERE archive_id=? AND cost_class!=?""",
                (archive_id, CostClass.FREE.value),
            ).fetchone()
            spent_probes = self._conn.execute(
                """SELECT COUNT(*) FROM attempt
                    WHERE archive_id=? AND cost_class=?""",
                (archive_id, CostClass.PROBE.value),
            ).fetchone()[0]
        return {
            "attempts_charged": int(row[0] or 0),
            "bytes_moved": int(row[1] or 0),
            "probe_attempts": int(spent_probes or 0),
        }

    def parts_at_risk(self, archive_id: str) -> list[PartBudget]:
        """Every part with at most reserve+1 attempts left."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT DISTINCT idx FROM (
                       SELECT idx FROM attempt WHERE archive_id=?
                       UNION SELECT idx FROM remote_count WHERE archive_id=?
                   ) ORDER BY idx""",
                (archive_id, archive_id),
            ).fetchall()
        budgets = [self.budget_for(archive_id, int(r[0])) for r in rows]
        return [b for b in budgets if b.at_risk]
