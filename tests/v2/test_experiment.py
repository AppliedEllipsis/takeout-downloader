"""Tests for takeout2.experiment — the empirical attempt-cost harness.

These tests run FULLY OFFLINE: every Google-facing dependency (cookie_source,
transport_factory, dl_scrape) is a fake, and the ledger uses an in-memory
SQLite connection. The real deployment would point the same interfaces at
takeout-download.usercontent.google.com on a THROWAWAY export.
"""
from __future__ import annotations

import sqlite3

import pytest

from takeout2.contracts import AttemptCost, PartPlan, ReasonCode
from takeout2.experiment import (ACTIONS, ThrowawayRequired, measure_action,
                                 run_study, write_findings)
from takeout2.ledger import AttemptLedger, BudgetExhausted

ARCHIVE = "j-coststudy"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeTransport:
    """A controlled-action transport: records the call, returns OK_COMPLETE."""

    def __init__(self, reason=ReasonCode.OK_COMPLETE, bytes_moved=0):
        self.reason = reason
        self.bytes_moved = bytes_moved
        self.calls = []

    def __call__(self, part, cookie_header=None):
        self.calls.append((part.idx, cookie_header))
        return self.reason, self.bytes_moved


class FakeTransportFactory:
    """transport_factory(action, part_idx) -> a transport bound to that action.

    If given a ``scrape`` oracle, each transport call advances that oracle's
    counter for the part — mirroring reality where the controlled action is
    what moves Google's counter and the scrape only reads it.
    """

    def __init__(self, reason=ReasonCode.OK_COMPLETE, bytes_moved=0,
                 scrape=None):
        self.reason = reason
        self.bytes_moved = bytes_moved
        self.scrape = scrape
        self.issued = []        # (action, part_idx) pairs handed out
        self.calls = []         # (action, part_idx, cookie_header) executed

    def __call__(self, action, part_idx):
        self.issued.append((action, part_idx))
        reason, bytes_moved = self.reason, self.bytes_moved
        calls = self.calls
        scrape = self.scrape

        def transport(part, cookie_header=None):
            calls.append((action, part_idx, cookie_header))
            if scrape is not None:
                scrape.counts[part_idx] = (
                    scrape.counts.get(part_idx, 0) + scrape.increment)
            return reason, bytes_moved

        return transport


class CounterScrape:
    """Fake dl_scrape: reads Google's manage-page counter for a part.

    The counter is advanced by the fake transports (one controlled action ==
    one bump of ``increment``), so a before/after pair straddling one action
    yields a delta of exactly ``increment`` — a configurable per-action cost.
    """

    def __init__(self, increment: int = 1):
        self.increment = increment
        self.counts: dict[int, int] = {}
        self.calls = 0

    def __call__(self, part_idx: int) -> int:
        self.calls += 1
        return self.counts.get(part_idx, 0)


def _ledger(budget=5, reserve=1):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return AttemptLedger(conn, budget=budget, reserve=reserve)


def _part(idx):
    return PartPlan(idx=idx, filename=f"takeout-{ARCHIVE}-{idx:03d}.zip")


# --------------------------------------------------------------------------
# measure_action
# --------------------------------------------------------------------------
class TestMeasureAction:
    def test_zero_delta_is_free(self):
        ledger = _ledger()
        transport = FakeTransport()
        cost = measure_action("probe", part=_part(0), cookie_header="ck",
                              transport=transport,
                              dl_scrape_before=3, dl_scrape_after=3,
                              ledger=ledger, archive_id=ARCHIVE)
        assert isinstance(cost, AttemptCost)
        assert cost.action == "probe"
        assert cost.observed_delta == 0
        assert cost.samples == 1
        assert cost.is_free

    def test_positive_delta_is_not_free(self):
        ledger = _ledger()
        transport = FakeTransport()
        cost = measure_action("abort", part=_part(0), cookie_header="ck",
                              transport=transport,
                              dl_scrape_before=3, dl_scrape_after=4,
                              ledger=ledger, archive_id=ARCHIVE)
        assert cost.observed_delta == 1
        assert not cost.is_free

    def test_runs_transport_and_commits_to_ledger(self):
        ledger = _ledger()
        transport = FakeTransport(bytes_moved=1_048_576)
        measure_action("abort", part=_part(0), cookie_header="ck",
                       transport=transport,
                       dl_scrape_before=0, dl_scrape_after=1,
                       ledger=ledger, archive_id=ARCHIVE)
        assert transport.calls == [(0, "ck")]          # exactly one action
        assert ledger.budget_for(ARCHIVE, 0).local_used == 1
        assert ledger.archive_totals(ARCHIVE)["bytes_moved"] == 1_048_576

    def test_records_post_action_counter_as_remote_ground_truth(self):
        ledger = _ledger()
        measure_action("resume", part=_part(0), cookie_header="ck",
                       transport=FakeTransport(),
                       dl_scrape_before=2, dl_scrape_after=3,
                       ledger=ledger, archive_id=ARCHIVE)
        assert ledger.remote_used(ARCHIVE, 0) == 3

    def test_probe_uses_probe_cost_class(self):
        ledger = _ledger()
        measure_action("probe", part=_part(0), cookie_header="ck",
                       transport=FakeTransport(),
                       dl_scrape_before=0, dl_scrape_after=0,
                       ledger=ledger, archive_id=ARCHIVE)
        totals = ledger.archive_totals(ARCHIVE)
        assert totals["probe_attempts"] == 1
        assert totals["attempts_charged"] == 1

    def test_byte_moving_actions_use_payload_class(self):
        ledger = _ledger()
        for action in ("abort", "resume", "full", "parallel"):
            measure_action(action, part=_part(0), cookie_header="ck",
                           transport=FakeTransport(),
                           dl_scrape_before=0, dl_scrape_after=1,
                           ledger=ledger, archive_id=ARCHIVE)
        # all four byte-moving actions charged as PAYLOAD, none as probe
        totals = ledger.archive_totals(ARCHIVE)
        assert totals["attempts_charged"] == 4
        assert totals["probe_attempts"] == 0

    def test_refuses_when_part_has_no_spendable_budget(self):
        ledger = _ledger()
        for _ in range(4):                    # spendable 4 used up (5 - reserve 1)
            ledger.reserve(ARCHIVE, 0).commit(ReasonCode.NETWORK_ERROR)
        with pytest.raises(BudgetExhausted):
            measure_action("full", part=_part(0), cookie_header="ck",
                           transport=FakeTransport(),
                           dl_scrape_before=5, dl_scrape_after=5,
                           ledger=ledger, archive_id=ARCHIVE)
        assert ledger.budget_for(ARCHIVE, 0).local_used == 4   # nothing extra spent

    def test_rejects_unknown_action(self):
        with pytest.raises(ValueError, match="unknown action"):
            measure_action("range-x", part=_part(0), cookie_header="ck",
                           transport=FakeTransport(),
                           dl_scrape_before=0, dl_scrape_after=0,
                           ledger=_ledger(), archive_id=ARCHIVE)


# --------------------------------------------------------------------------
# run_study
# --------------------------------------------------------------------------
class TestRunStudy:
    def test_study_records_per_action_samples_and_stats(self, tmp_path):
        ledger = _ledger()
        scrape = CounterScrape(increment=1)
        factory = FakeTransportFactory(scrape=scrape)
        results = run_study(
            ARCHIVE, samples=3,
            cookie_source=lambda: "ck", transport_factory=factory,
            dl_scrape=scrape, ledger=ledger,
            out_path=str(tmp_path / "ATTEMPT-COST-FINDINGS.md"),
            confirm_throwaway=True)

        assert results["archive_id"] == ARCHIVE
        assert results["samples"] == 3
        assert set(results["actions"]) == set(ACTIONS)

        for action in ACTIONS:
            stats = results["actions"][action]
            assert len(stats["samples"]) == 3
            assert all(isinstance(s, AttemptCost) and s.samples == 1
                       for s in stats["samples"])
            # each controlled action moved Google's counter by exactly 1
            assert stats["deltas"] == [1, 1, 1]
            assert stats["min"] == 1
            assert stats["median"] == 1
            assert stats["max"] == 1
            assert stats["skipped_exhausted"] == 0

        # every sample ran on a fresh, distinct part index
        all_idxs = [factory.issued[i][1] for i in range(len(factory.issued))]
        assert len(all_idxs) == len(set(all_idxs))
        assert all_idxs == list(range(15))

    def test_study_honors_configurable_per_action_cost(self, tmp_path):
        ledger = _ledger()
        scrape = CounterScrape(increment=2)
        results = run_study(
            ARCHIVE, actions=("resume",), samples=3,
            cookie_source=lambda: "ck",
            transport_factory=FakeTransportFactory(scrape=scrape),
            dl_scrape=scrape, ledger=ledger,
            out_path=str(tmp_path / "ATTEMPT-COST-FINDINGS.md"),
            confirm_throwaway=True)
        assert results["actions"]["resume"]["deltas"] == [2, 2, 2]
        assert results["actions"]["resume"]["median"] == 2

    def test_free_action_study(self, tmp_path):
        """A zero-delta action is reported FREE end to end."""
        ledger = _ledger()

        class FreeScrape(CounterScrape):
            def __call__(self, part_idx):
                self.calls += 1
                return 0          # Google's counter never moves

        scrape = FreeScrape()
        results = run_study(
            ARCHIVE, actions=("probe",), samples=3,
            cookie_source=lambda: "ck",
            transport_factory=FakeTransportFactory(scrape=scrape),
            dl_scrape=scrape, ledger=ledger,
            out_path=str(tmp_path / "ATTEMPT-COST-FINDINGS.md"),
            confirm_throwaway=True)
        stats = results["actions"]["probe"]
        assert stats["deltas"] == [0, 0, 0]
        assert stats["min"] == 0 and stats["median"] == 0 and stats["max"] == 0

    def test_refuses_without_confirm_throwaway(self, tmp_path):
        ledger = _ledger()
        with pytest.raises(ThrowawayRequired, match="[Tt]hrowaway"):
            run_study(
                ARCHIVE, samples=1,
                cookie_source=lambda: "ck",
                transport_factory=FakeTransportFactory(),
                dl_scrape=CounterScrape(), ledger=ledger,
                out_path=str(tmp_path / "ATTEMPT-COST-FINDINGS.md"))
        # nothing was spent, nothing was written
        assert ledger.archive_totals(ARCHIVE)["attempts_charged"] == 0
        assert not (tmp_path / "ATTEMPT-COST-FINDINGS.md").exists()

    def test_never_exceeds_budget_across_study(self, tmp_path):
        """The whole study must not push any part past its attempt budget."""
        ledger = _ledger()                  # budget 5, reserve 1
        scrape = CounterScrape(increment=1)
        results = run_study(
            ARCHIVE, samples=3,
            cookie_source=lambda: "ck",
            transport_factory=FakeTransportFactory(scrape=scrape),
            dl_scrape=scrape, ledger=ledger,
            out_path=str(tmp_path / "ATTEMPT-COST-FINDINGS.md"),
            confirm_throwaway=True)

        # one attempt reserved+committed per part; none exhausted, none at risk
        for idx in range(15):
            b = ledger.budget_for(ARCHIVE, idx)
            assert b.local_used == 1
            assert b.effective_used <= 5
            assert not b.exhausted
        assert ledger.parts_at_risk(ARCHIVE) == []
        for action in ACTIONS:
            assert results["actions"][action]["skipped_exhausted"] == 0

    def test_skips_parts_with_no_spendable_budget(self, tmp_path):
        """An exhausted part is skipped, never charged or crashed into."""
        ledger = _ledger()
        for idx in range(4):                # parts 0-3: spendable 0
            for _ in range(4):
                ledger.reserve(ARCHIVE, idx).commit(ReasonCode.LIMIT_EXCEEDED)
        scrape = CounterScrape(increment=1)
        results = run_study(
            ARCHIVE, actions=("probe",), samples=5,
            cookie_source=lambda: "ck",
            transport_factory=FakeTransportFactory(scrape=scrape),
            dl_scrape=scrape, ledger=ledger,
            out_path=str(tmp_path / "ATTEMPT-COST-FINDINGS.md"),
            confirm_throwaway=True)

        stats = results["actions"]["probe"]
        assert stats["skipped_exhausted"] == 4     # parts 0-3 refused
        assert stats["deltas"] == [1]              # only part 4 ran
        assert ledger.budget_for(ARCHIVE, 4).local_used == 1


# --------------------------------------------------------------------------
# write_findings
# --------------------------------------------------------------------------
class TestWriteFindings:
    def _results(self, deltas_by_action):
        actions = {}
        for action, deltas in deltas_by_action.items():
            m = max(deltas) if deltas else None
            actions[action] = {
                "samples": [], "deltas": deltas,
                "min": min(deltas) if deltas else None,
                "median": (sorted(deltas)[len(deltas) // 2]
                           if deltas else None),
                "max": m, "skipped_exhausted": 0,
            }
        return {"archive_id": ARCHIVE, "out_path": "docs/v2/ATTEMPT-COST-FINDINGS.md",
                "samples": 3, "actions": actions}

    def test_creates_report_with_table_and_footer(self, tmp_path):
        path = str(tmp_path / "ATTEMPT-COST-FINDINGS.md")
        out = write_findings(self._results(
            {"probe": [0, 0, 0], "abort": [1, 1, 1]}), path)
        assert out == path
        text = open(path, encoding="utf-8").read()

        assert "THROWAWAY EXPORT REQUIRED" in text          # header
        assert "| Action | Deltas | Median | Verdict |" in text
        assert "| probe | 0, 0, 0 | 0 | FREE |" in text     # 0 => FREE
        assert "| abort | 1, 1, 1 | 1 | COSTS 1+ |" in text  # else COSTS 1+
        assert "Conservative assumptions remain in force until a non-zero sample exists" in text  # footer

    def test_appends_study_sections_keeping_single_header(self, tmp_path):
        path = str(tmp_path / "ATTEMPT-COST-FINDINGS.md")
        write_findings(self._results({"probe": [0]}), path)
        write_findings(self._results({"full": [1, 1, 1]}), path)
        text = open(path, encoding="utf-8").read()

        assert text.count("THROWAWAY EXPORT REQUIRED") == 1   # header once
        assert text.count("## Study —") == 2                  # two sections
        assert text.count("| probe |") == 1
        assert text.count("| full |") == 1

    def test_returns_path_and_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "nested" / "dir" / "ATTEMPT-COST-FINDINGS.md")
        out = write_findings(self._results({"probe": [0]}), path)
        assert out == path
        assert (tmp_path / "nested" / "dir" / "ATTEMPT-COST-FINDINGS.md").exists()
