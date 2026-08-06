"""Tests for takeout2.contracts — the invariants everything else depends on."""
from __future__ import annotations

import pytest

from takeout2.contracts import (
    AUTH_REASONS, AccountIdentity, Confidence, CostClass, DL_COUNT_RE,
    IdentityRecord, LabelSource, PartPlan, ReasonCode, VerifyState,
    format_export_ts, parse_export_ts, sanitize_label, sanitize_segment,
)


class TestCostClass:
    def test_only_free_escapes_the_budget(self):
        assert CostClass.PAYLOAD.counts_against_budget
        # A probe is assumed to cost until measured otherwise.
        assert CostClass.PROBE.counts_against_budget
        assert not CostClass.FREE.counts_against_budget


class TestReasonCodeSemantics:
    def test_end_of_range_is_not_an_auth_failure(self):
        """v1's fatal conflation: HTML past the last part flipped the whole
        job to needs_cookie, causing the capture->discover->expire livelock."""
        assert ReasonCode.END_OF_RANGE not in AUTH_REASONS

    def test_real_auth_failures_are_auth_reasons(self):
        assert ReasonCode.AUTH_REDIRECT in AUTH_REASONS
        assert ReasonCode.AUTH_401 in AUTH_REASONS


class TestLabelProvenance:
    def test_ranking_is_strictly_ordered(self):
        order = [
            LabelSource.UNKNOWN,
            LabelSource.GAIA_FALLBACK,
            LabelSource.SCRAPED_LABEL,
            LabelSource.SCRAPED_EMAIL,
            LabelSource.OPERATOR_OVERRIDE,
        ]
        ranks = [s.rank for s in order]
        assert ranks == sorted(ranks), "provenance must be totally ordered"
        assert len(set(ranks)) == len(ranks), "no two sources may tie"

    def test_better_label_upgrades_but_worse_does_not(self):
        """The 2.8 TB bug: a scraped label replaced a gaia fallback and the
        job was orphaned. Upgrades must be allowed; downgrades must not."""
        gaia = AccountIdentity(gaia_user="1005482974000",
                               label_source=LabelSource.GAIA_FALLBACK)
        scraped = AccountIdentity(gaia_user="1005482974000",
                                  email="braincreation@gmail.com",
                                  label_source=LabelSource.SCRAPED_EMAIL)
        assert scraped.upgrades_over(gaia)
        assert not gaia.upgrades_over(scraped)

    def test_identity_with_no_prior_always_upgrades(self):
        ident = AccountIdentity(gaia_user="1", label_source=LabelSource.UNKNOWN)
        assert ident.upgrades_over(None)

    def test_confidence_tracks_source(self):
        assert AccountIdentity(gaia_user="1",
                               label_source=LabelSource.SCRAPED_EMAIL
                               ).confidence is Confidence.HIGH
        assert AccountIdentity(gaia_user="1",
                               label_source=LabelSource.GAIA_FALLBACK
                               ).confidence is Confidence.LOW


class TestFolderName:
    def test_prefers_label_then_email_then_gaia(self):
        assert AccountIdentity(gaia_user="42", label="BrainCreation",
                               email="x@y.com").folder_name() == "braincreation"
        assert AccountIdentity(gaia_user="42",
                               email="Some.User@gmail.com"
                               ).folder_name() == "some.user"
        assert AccountIdentity(gaia_user="1005482974000"
                               ).folder_name() == "gaia-1005482974000"

    def test_never_empty_even_with_junk_input(self):
        assert AccountIdentity(gaia_user="", label="///",
                               email="@@@").folder_name() == "unknown-account"

    def test_folder_name_is_path_safe(self):
        name = AccountIdentity(gaia_user="1",
                               label="../../etc/passwd").folder_name()
        assert "/" not in name and ".." not in name


class TestSanitizers:
    @pytest.mark.parametrize("raw,expected", [
        ("BrainCreation@gmail.com", "braincreation"),
        ("  Spaced Name  ", "spaced-name"),
        ("weird!!!chars", "weird-chars"),
        ("--leading--", "leading"),
        (None, ""),
    ])
    def test_sanitize_label(self, raw, expected):
        assert sanitize_label(raw) == expected

    def test_sanitize_segment_blocks_traversal(self):
        assert sanitize_segment("../../evil") == "evil"
        assert "/" not in sanitize_segment("a/b/c")


class TestExportTimestamp:
    def test_format(self):
        assert format_export_ts("20260616T040104Z") == "2026-06-16-04-01-04"

    def test_majority_wins_over_first_match(self):
        """A mixed scrape must not silently adopt whichever came first."""
        names = [
            "takeout-20260101T000000Z-9-001.zip",   # stray
            "takeout-20260616T040104Z-9-002.zip",
            "takeout-20260616T040104Z-9-003.zip",
        ]
        assert parse_export_ts(names) == "20260616T040104Z"

    def test_returns_none_when_absent(self):
        assert parse_export_ts(["random.zip"]) is None
        assert parse_export_ts([]) is None


class TestDlCountRegex:
    def test_extracts_googles_own_attempt_counter(self):
        text = ("takeout-20260616T040104Z-9-001.zip "
                "(Number of times already downloaded: 5)")
        m = DL_COUNT_RE.search(text)
        assert m and int(m.group(2)) == 5

    def test_scans_multiple_parts(self):
        text = (
            "takeout-20260616T040104Z-9-001.zip (Number of times already downloaded: 5)\n"
            "takeout-20260616T040104Z-9-002.zip (Number of times already downloaded: 0)\n"
        )
        found = {m.group(1)[-7:]: int(m.group(2))
                 for m in DL_COUNT_RE.finditer(text)}
        assert found == {"001.zip": 5, "002.zip": 0}


class TestBudgetArithmetic:
    def test_remote_count_outranks_local(self):
        """Google's counter is ground truth; our ledger is an estimate."""
        part = PartPlan(idx=0, dl_count_remote=4)
        assert part.remaining_attempts(budget=5, local_used=1) == 1

    def test_local_used_when_remote_missing(self):
        assert PartPlan(idx=0).remaining_attempts(budget=5, local_used=2) == 3

    def test_never_negative(self):
        part = PartPlan(idx=0, dl_count_remote=9)
        assert part.remaining_attempts(budget=5) == 0


class TestVerifyState:
    def test_ranking_makes_struct_stronger_than_size(self):
        assert VerifyState.STRUCT_OK.rank > VerifyState.SIZE_OK.rank
        assert VerifyState.HASH_OK.rank > VerifyState.STRUCT_OK.rank
        assert VerifyState.CORRUPT.rank < VerifyState.UNVERIFIED.rank


class TestIdentityRecord:
    def test_relative_dir_is_account_over_timestamp(self):
        rec = IdentityRecord(
            archive_id="abc123",
            export_raw="20260616T040104Z",
            account=AccountIdentity(gaia_user="1005482974000",
                                    email="braincreation@gmail.com",
                                    label_source=LabelSource.SCRAPED_EMAIL),
        )
        assert rec.relative_dir() == "braincreation/2026-06-16-04-01-04"
