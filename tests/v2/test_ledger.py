"""Tests for takeout2.ledger — the attempt-budget gate.

These tests encode the safety properties that protect a multi-terabyte export
from being permanently burned by over-spending Google's ~5-download ceiling.
"""
from __future__ import annotations

import sqlite3

import pytest

from takeout2.contracts import CostClass, ReasonCode
from takeout2.ledger import AttemptLedger, BudgetExhausted

ARCHIVE = "j-abc123"


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return AttemptLedger(conn, budget=5, reserve=1)


class TestBasicAccounting:
    def test_fresh_part_has_full_budget(self, ledger):
        b = ledger.budget_for(ARCHIVE, 0)
        assert b.local_used == 0
        assert b.remaining == 5
        assert b.spendable == 4       # reserve of 1 held back
        assert not b.exhausted

    def test_committing_charges_one_attempt(self, ledger):
        r = ledger.reserve(ARCHIVE, 0)
        r.commit(ReasonCode.OK_COMPLETE, bytes_moved=10_737_418_240)
        assert ledger.budget_for(ARCHIVE, 0).local_used == 1

    def test_free_requests_never_charge(self, ledger):
        r = ledger.reserve(ARCHIVE, 0, CostClass.FREE)
        r.commit(ReasonCode.OK_COMPLETE)
        assert ledger.budget_for(ARCHIVE, 0).local_used == 0

    def test_probe_is_charged_conservatively(self, ledger):
        """Until measured, assume a probe costs a real attempt."""
        r = ledger.reserve(ARCHIVE, 0, CostClass.PROBE)
        r.commit(ReasonCode.END_OF_RANGE)
        assert ledger.budget_for(ARCHIVE, 0).local_used == 1

    def test_release_does_not_charge(self, ledger):
        r = ledger.reserve(ARCHIVE, 0)
        r.release("aborted during pre-flight, never sent")
        assert ledger.budget_for(ARCHIVE, 0).local_used == 0


class TestBudgetEnforcement:
    def test_refuses_once_reserve_is_reached(self, ledger):
        for _ in range(4):                      # budget 5 - reserve 1
            ledger.reserve(ARCHIVE, 0).commit(ReasonCode.NETWORK_ERROR)
        assert ledger.budget_for(ARCHIVE, 0).exhausted
        with pytest.raises(BudgetExhausted) as exc:
            ledger.reserve(ARCHIVE, 0)
        assert "Refusing to spend another attempt" in str(exc.value)

    def test_force_overrides_the_ceiling(self, ledger):
        for _ in range(4):
            ledger.reserve(ARCHIVE, 0).commit(ReasonCode.NETWORK_ERROR)
        r = ledger.reserve(ARCHIVE, 0, force=True)   # operator accepted the risk
        r.commit(ReasonCode.OK_COMPLETE)
        assert ledger.budget_for(ARCHIVE, 0).local_used == 5

    def test_budget_is_per_part_not_per_archive(self, ledger):
        for _ in range(4):
            ledger.reserve(ARCHIVE, 0).commit(ReasonCode.NETWORK_ERROR)
        assert ledger.budget_for(ARCHIVE, 0).exhausted
        assert not ledger.budget_for(ARCHIVE, 1).exhausted   # untouched part

    def test_free_class_bypasses_exhaustion(self, ledger):
        for _ in range(5):
            ledger.reserve(ARCHIVE, 0, force=True).commit(ReasonCode.NETWORK_ERROR)
        # Scraping/local work must still be possible on an exhausted part.
        ledger.reserve(ARCHIVE, 0, CostClass.FREE).commit(ReasonCode.OK_COMPLETE)


class TestRemoteGroundTruth:
    def test_google_count_outranks_local(self, ledger):
        """Google sees attempts we never made (browser downloads, other hosts)."""
        ledger.observe_remote(ARCHIVE, 3, 4)
        b = ledger.budget_for(ARCHIVE, 3)
        assert b.local_used == 0
        assert b.effective_used == 4
        assert b.remaining == 1
        assert b.exhausted            # only the reserve remains

    def test_reserve_refuses_based_on_remote_count(self, ledger):
        ledger.observe_remote(ARCHIVE, 3, 5)
        with pytest.raises(BudgetExhausted):
            ledger.reserve(ARCHIVE, 3)

    def test_local_wins_when_higher(self, ledger):
        ledger.observe_remote(ARCHIVE, 0, 1)
        for _ in range(3):
            ledger.reserve(ARCHIVE, 0).commit(ReasonCode.NETWORK_ERROR)
        assert ledger.budget_for(ARCHIVE, 0).effective_used == 3

    def test_observation_is_idempotent_upsert(self, ledger):
        ledger.observe_remote(ARCHIVE, 0, 2)
        ledger.observe_remote(ARCHIVE, 0, 3)
        assert ledger.remote_used(ARCHIVE, 0) == 3


class TestCrashRecovery:
    def test_orphan_reservations_are_assumed_consumed(self, ledger):
        """Fail closed: a crash mid-request must not under-count."""
        ledger.reserve(ARCHIVE, 0)        # never settled — simulates a crash
        assert ledger.reconcile_orphans() == 1
        assert ledger.budget_for(ARCHIVE, 0).local_used == 1

    def test_reconcile_is_idempotent(self, ledger):
        ledger.reserve(ARCHIVE, 0)
        ledger.reconcile_orphans()
        assert ledger.reconcile_orphans() == 0

    def test_context_manager_autosettles_on_exception(self, ledger):
        with pytest.raises(ValueError):
            with ledger.attempt(ARCHIVE, 0):
                raise ValueError("connection died mid-stream")
        assert ledger.budget_for(ARCHIVE, 0).local_used == 1

    def test_context_manager_commit_is_respected(self, ledger):
        with ledger.attempt(ARCHIVE, 0) as r:
            r.commit(ReasonCode.OK_COMPLETE, bytes_moved=1234)
        assert ledger.archive_totals(ARCHIVE)["bytes_moved"] == 1234

    def test_double_settle_is_rejected(self, ledger):
        r = ledger.reserve(ARCHIVE, 0)
        r.commit(ReasonCode.OK_COMPLETE)
        with pytest.raises(RuntimeError, match="already settled"):
            r.commit(ReasonCode.OK_COMPLETE)


class TestReporting:
    def test_archive_totals(self, ledger):
        ledger.reserve(ARCHIVE, 0).commit(ReasonCode.OK_COMPLETE, bytes_moved=100)
        ledger.reserve(ARCHIVE, 1, CostClass.PROBE).commit(ReasonCode.END_OF_RANGE)
        ledger.reserve(ARCHIVE, 2, CostClass.FREE).commit(ReasonCode.OK_COMPLETE)
        totals = ledger.archive_totals(ARCHIVE)
        assert totals["attempts_charged"] == 2      # FREE excluded
        assert totals["probe_attempts"] == 1
        assert totals["bytes_moved"] == 100

    def test_parts_at_risk_surfaces_the_dangerous_ones(self, ledger):
        ledger.observe_remote(ARCHIVE, 7, 4)     # 1 left -> at risk
        ledger.observe_remote(ARCHIVE, 8, 0)     # healthy
        at_risk = {b.idx for b in ledger.parts_at_risk(ARCHIVE)}
        assert at_risk == {7}

    def test_persistence_across_connections(self, tmp_path):
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        first = AttemptLedger(conn, budget=5, reserve=1)
        first.reserve(ARCHIVE, 0).commit(ReasonCode.OK_COMPLETE)
        conn.close()

        second = AttemptLedger(sqlite3.connect(db), budget=5, reserve=1)
        assert second.budget_for(ARCHIVE, 0).local_used == 1
