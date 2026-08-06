"""Tests for takeout2.plan — zero-probe discovery."""
from __future__ import annotations

import pytest

from takeout2.contracts import PartPlan
from takeout2.plan import (plan_from_filenames, plan_from_payload,
                           plan_from_probes, plans_from_state, resolve_plan)

PAYLOAD = {
    "parts_expected": 62,
    "uris": {f"takeout-20260616T040104Z-9-{i:03d}.zip": f"https://dl/{i}"
             for i in range(62)},
    "sizes": {f"takeout-20260616T040104Z-9-{i:03d}.zip": 10 << 30
              for i in range(62)},
    "dl_counts": {f"takeout-20260616T040104Z-9-{i:03d}.zip": 0
                  for i in range(62)},
    "filenames": [f"takeout-20260616T040104Z-9-{i:03d}.zip" for i in range(62)],
}


class TestPayloadPlanning:
    def test_expected_parts_yields_zero_probe_plan(self):
        result = plan_from_payload(PAYLOAD)
        assert result.ok
        assert result.source == "PAYLOAD"
        assert result.cost_attempts == 0
        assert len(result.parts) == 62
        assert result.parts[0].url == "https://dl/0"

    def test_dl_counts_carried_into_plan(self):
        payload = dict(PAYLOAD, dl_counts={
            "takeout-20260616T040104Z-9-000.zip": 4})
        result = plan_from_payload(payload)
        assert result.parts[0].dl_count_remote == 4
        assert result.parts[1].dl_count_remote is None

    def test_no_metadata_yields_empty(self):
        result = plan_from_payload({})
        assert not result.ok
        assert result.cost_attempts == 0

    def test_ambiguous_timestamps_flagged(self):
        payload = dict(PAYLOAD, filenames=[
            "takeout-20260101T000000Z-9-000.zip",
            "takeout-20260616T040104Z-9-001.zip",
        ])
        result = plan_from_payload(payload)
        assert result.ambiguous

    def test_missing_sizes_are_tolerated(self):
        payload = dict(PAYLOAD, sizes={}, uris={})
        result = plan_from_payload(payload)
        assert result.ok
        assert all(p.size_expected is None for p in result.parts)


class TestFilenamePlanning:
    def test_embedded_indices(self):
        names = [
            "takeout-20260616T040104Z-9-002.zip",
            "takeout-20260616T040104Z-9-000.zip",
            "takeout-20260616T040104Z-9-001.zip",
        ]
        result = plan_from_filenames(names)
        assert result.ok and result.source == "FILENAMES"
        assert [p.idx for p in result.parts] == [0, 1, 2]

    def test_lexical_fallback(self):
        result = plan_from_filenames(["a.zip", "b.zip", "c.zip"])
        assert result.ok
        assert [p.idx for p in result.parts] == [0, 1, 2]


class TestStatePlanning:
    def test_uses_persisted_plan(self):
        class FakeStore:
            def list_parts(self, archive_id):
                return [type("R", (), dict(idx=i, filename=f"p{i}.zip",
                                           url=f"u{i}", size_expected=10))
                        for i in range(3)]
        result = plans_from_state(FakeStore(), "abc")
        assert result.ok and result.source == "STATE_DB"
        assert len(result.parts) == 3

    def test_empty_store_yields_nothing(self):
        class FakeStore:
            def list_parts(self, archive_id):
                return []
        result = plans_from_state(FakeStore(), "abc")
        assert not result.ok


class TestResolveLadder:
    def test_payload_wins_over_everything(self):
        class FakeStore:
            def list_parts(self, archive_id):
                return [type("R", (), dict(idx=0, filename="x.zip"))]

        result = resolve_plan(payload=PAYLOAD, store=FakeStore(),
                              archive_id="abc", allow_probes=True)
        assert result.source == "PAYLOAD"
        assert result.cost_attempts == 0

    def test_state_used_when_payload_is_empty(self):
        class FakeStore:
            def list_parts(self, archive_id):
                return [type("R", (), dict(idx=0, filename="x.zip",
                                            url="https://dl/0", size_expected=10))]
        result = resolve_plan(payload={}, store=FakeStore(), archive_id="abc")
        assert result.source == "STATE_DB"

    def test_probes_refused_without_opt_in(self):
        class EmptyStore:
            def list_parts(self, archive_id):
                return []
        result = resolve_plan(payload={}, store=EmptyStore(),
                              archive_id="abc", filenames=None, allow_probes=False,
                              ledger=object(), first_url="https://x")
        assert not result.ok

    def test_all_paths_empty_reports_actionable_error(self):
        class FakeStore:
            def list_parts(self, archive_id):
                return []
        result = resolve_plan(payload={}, store=FakeStore(), archive_id="abc")
        assert not result.ok
        assert "allow-discovery-probes" in result.message


class TestProbePath:
    """The opt-in bounded sweep, with every probe ledger-reserved."""

    def test_probes_are_charged_and_bounded(self):
        import sqlite3
        from takeout2.ledger import AttemptLedger
        from takeout2.contracts import ReasonCode

        ledger = AttemptLedger(sqlite3.connect(":memory:"), budget=5, reserve=1)

        def classify(first_url, idx):
            return ReasonCode.END_OF_RANGE if idx >= 3 else ReasonCode.OK_COMPLETE

        result = plan_from_probes(first_url="https://dl/0", max_probes=3,
                                  ledger=ledger, archive_id="abc",
                                  classify_fun=classify)
        assert result.source == "PROBES"
        assert result.probes_used == 3
        assert result.cost_attempts == 3
        totals = ledger.archive_totals("abc")
        assert totals["probe_attempts"] == 3
        assert totals["attempts_charged"] == 3

    def test_string_cost_class_works_via_ledger_coercion(self):
        """Cross-module seam: plan.py may pass 'PROBE' as a string."""
        import sqlite3
        from takeout2.ledger import AttemptLedger, BudgetExhausted
        from takeout2.contracts import CostClass

        ledger = AttemptLedger(sqlite3.connect(":memory:"), budget=2, reserve=0)
        # Burn the budget using the string form; exhaustion must still fire.
        ledger.reserve("abc", 0, "PROBE").commit("END_OF_RANGE")
        ledger.reserve("abc", 0, "PROBE").commit("END_OF_RANGE")
        try:
            ledger.reserve("abc", 0, CostClass.PROBE)
            raise AssertionError("should have raised BudgetExhausted")
        except BudgetExhausted:
            pass
