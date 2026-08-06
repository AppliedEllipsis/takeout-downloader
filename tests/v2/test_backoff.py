"""Deterministic tests for takeout2.backoff.

Network-free, clock-free, sleep-free. Every non-trivial value comes from an
injected ``rand`` or ``now``, so a failure here is always a policy bug and
never a flaky timing artefact.

The invariant that matters most: an auth reason must NEVER produce a wait.
Google allows 5 downloads per part, ever — sleeping on a dead cookie and then
re-requesting turns one wasted attempt into two.
"""
from __future__ import annotations

import calendar
from dataclasses import FrozenInstanceError

import pytest

from takeout2.backoff import (
    DEFAULT_BASE_S,
    DEFAULT_CAP_S,
    MAX_RATE_LIMIT_RETRIES,
    BackoffDecision,
    backoff_delay,
    decide,
    parse_retry_after,
)
from takeout2.contracts import AUTH_REASONS, ReasonCode

#: Fixed reference instant so HTTP-date maths never touches the wall clock.
#: 2026-10-21 07:26:00 GMT — two minutes before the sample date below.
NOW = float(calendar.timegm((2026, 10, 21, 7, 26, 0, 0, 0, 0)))
HTTP_DATE = "Wed, 21 Oct 2026 07:28:00 GMT"
PAST_DATE = "Wed, 21 Oct 2020 07:28:00 GMT"


def rand_at(value: float):
    """A deterministic stand-in for ``random.random``."""
    return lambda: value


class TestParseRetryAfter:
    def test_delta_seconds(self):
        assert parse_retry_after("120") == 120.0

    def test_delta_seconds_with_whitespace(self):
        assert parse_retry_after("  45  ") == 45.0

    def test_zero_is_zero_not_none(self):
        assert parse_retry_after("0") == 0.0

    def test_http_date_returns_delta_to_now(self):
        assert parse_retry_after(HTTP_DATE, now=NOW) == pytest.approx(120.0)

    def test_past_http_date_clamps_to_zero(self):
        assert parse_retry_after(PAST_DATE, now=NOW) == 0.0

    def test_negative_delta_seconds_clamps_to_zero(self):
        assert parse_retry_after("-30") == 0.0

    def test_garbage_is_none(self):
        assert parse_retry_after("soon-ish") is None
        assert parse_retry_after("!!!") is None

    def test_absent_is_none(self):
        assert parse_retry_after(None) is None

    def test_empty_string_is_none(self):
        assert parse_retry_after("") is None
        assert parse_retry_after("   ") is None


class TestBackoffDelay:
    def test_exponential_growth_sequence(self):
        """base * 2**(attempt-1), with rand=None cancelling jitter exactly."""
        delays = [backoff_delay(a, base=10.0, cap=10_000.0) for a in (1, 2, 3, 4, 5)]
        assert delays == [10.0, 20.0, 40.0, 80.0, 160.0]

    def test_saturates_at_cap(self):
        assert backoff_delay(1, base=DEFAULT_BASE_S, cap=DEFAULT_CAP_S) == 30.0
        for attempt in (6, 7, 20, 100):
            assert backoff_delay(attempt, base=DEFAULT_BASE_S,
                                 cap=DEFAULT_CAP_S) == DEFAULT_CAP_S

    def test_attempt_below_one_is_treated_as_first_attempt(self):
        assert backoff_delay(0, base=10.0, cap=10_000.0) == 10.0

    def test_jitter_lower_bound(self):
        """rand()==0.0 => the -jitter edge: raw * (1 - jitter)."""
        assert backoff_delay(2, base=10.0, cap=10_000.0, jitter=0.2,
                             rand=rand_at(0.0)) == pytest.approx(16.0)

    def test_jitter_upper_bound(self):
        """rand()==1.0 => the +jitter edge: raw * (1 + jitter)."""
        assert backoff_delay(2, base=10.0, cap=10_000.0, jitter=0.2,
                             rand=rand_at(1.0)) == pytest.approx(24.0)

    @pytest.mark.parametrize("draw", [0.0, 0.25, 0.5, 0.75, 1.0])
    def test_jitter_stays_within_window(self, draw):
        raw = 40.0  # base 10, attempt 3
        delay = backoff_delay(3, base=10.0, cap=10_000.0, jitter=0.2,
                              rand=rand_at(draw))
        assert raw * 0.8 <= delay <= raw * 1.2

    def test_jitter_never_exceeds_cap(self):
        """Cap is a hard ceiling: upward jitter must not push past it."""
        assert backoff_delay(9, base=30.0, cap=DEFAULT_CAP_S,
                             rand=rand_at(1.0)) == DEFAULT_CAP_S

    def test_zero_jitter_is_exact(self):
        assert backoff_delay(3, base=10.0, cap=1e9, jitter=0.0,
                             rand=rand_at(1.0)) == 40.0


class TestDecideTransient:
    def test_rate_limited_without_headers_uses_exponential(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=1)
        assert d.should_wait is True
        assert d.give_up is False
        assert d.source == "exponential"
        assert d.delay_s == DEFAULT_BASE_S

    def test_network_error_is_transient_too(self):
        """classify.py maps every 5xx (500/502/503/504) to NETWORK_ERROR."""
        d = decide(ReasonCode.NETWORK_ERROR, attempt=2)
        assert d.should_wait is True
        assert d.source == "exponential"
        assert d.delay_s == pytest.approx(DEFAULT_BASE_S * 2)

    def test_retry_after_beats_exponential(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=3,
                   headers={"Retry-After": "120"})
        assert d.source == "retry-after"
        assert d.delay_s == 120.0
        # Exponential for attempt 3 would have been 120s too only by accident;
        # prove the header is actually driving it with a distinctive value.
        d2 = decide(ReasonCode.RATE_LIMITED, attempt=1,
                    headers={"Retry-After": "77"})
        assert (d2.source, d2.delay_s) == ("retry-after", 77.0)

    def test_retry_after_header_lookup_is_case_insensitive(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=1,
                   headers={"retry-after": "61"})
        assert (d.source, d.delay_s) == ("retry-after", 61.0)

    def test_retry_after_http_date_is_honoured(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=1,
                   headers={"Retry-After": HTTP_DATE}, now=NOW)
        assert d.source == "retry-after"
        assert d.delay_s == pytest.approx(120.0)

    def test_absurd_retry_after_clamps_to_cap(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=1,
                   headers={"Retry-After": "99999"})
        assert d.source == "retry-after"
        assert d.delay_s == DEFAULT_CAP_S
        assert "clamped" in d.detail

    def test_unparseable_retry_after_falls_back_to_exponential(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=2,
                   headers={"Retry-After": "whenever"})
        assert d.source == "exponential"
        assert d.delay_s == pytest.approx(DEFAULT_BASE_S * 2)

    def test_past_retry_after_date_falls_back_to_exponential(self):
        """Retry-After of 0s is not a usable wait; do not hot-loop on it."""
        d = decide(ReasonCode.RATE_LIMITED, attempt=1,
                   headers={"Retry-After": PAST_DATE}, now=NOW)
        assert d.source == "exponential"
        assert d.should_wait is True
        assert d.delay_s > 0.0

    def test_injected_rand_makes_decide_deterministic(self):
        low = decide(ReasonCode.RATE_LIMITED, attempt=2, base=10.0, cap=1e9,
                     rand=rand_at(0.0))
        high = decide(ReasonCode.RATE_LIMITED, attempt=2, base=10.0, cap=1e9,
                      rand=rand_at(1.0))
        assert low.delay_s == pytest.approx(16.0)
        assert high.delay_s == pytest.approx(24.0)


class TestDecideGiveUpBoundary:
    def test_at_max_retries_still_waits(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=MAX_RATE_LIMIT_RETRIES)
        assert d.should_wait is True
        assert d.give_up is False

    def test_one_past_max_retries_gives_up(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=MAX_RATE_LIMIT_RETRIES + 1)
        assert d.give_up is True
        assert d.should_wait is False
        assert d.delay_s == 0.0
        assert d.source == "none"

    def test_custom_max_retries_boundary(self):
        assert decide(ReasonCode.NETWORK_ERROR, attempt=2, max_retries=2).should_wait is True
        assert decide(ReasonCode.NETWORK_ERROR, attempt=3, max_retries=2).give_up is True

    def test_give_up_ignores_retry_after(self):
        """Exhausted budget beats a server hint — the attempt is what is scarce."""
        d = decide(ReasonCode.RATE_LIMITED, attempt=99,
                   headers={"Retry-After": "10"})
        assert (d.should_wait, d.give_up, d.source) == (False, True, "none")

    def test_zero_max_retries_never_waits(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=1, max_retries=0)
        assert d.should_wait is False
        assert d.give_up is True


class TestDecideAuthNeverWaits:
    @pytest.mark.parametrize("reason", [ReasonCode.AUTH_REDIRECT, ReasonCode.AUTH_401])
    def test_auth_reasons_never_wait(self, reason):
        d = decide(reason, attempt=1)
        assert d.should_wait is False
        assert d.delay_s == 0.0
        assert d.source == "none"

    @pytest.mark.parametrize("reason", sorted(AUTH_REASONS, key=lambda r: r.value))
    def test_every_member_of_auth_reasons_is_covered(self, reason):
        """Guard against a new AUTH_REASONS member silently becoming waitable."""
        assert decide(reason, attempt=1).should_wait is False

    def test_auth_ignores_retry_after_header(self):
        """A dead cookie does not heal by waiting, whatever the server says."""
        d = decide(ReasonCode.AUTH_REDIRECT, attempt=1,
                   headers={"Retry-After": "60"})
        assert d.should_wait is False
        assert d.delay_s == 0.0
        assert d.source == "none"

    def test_auth_gives_up_so_caller_parks_for_cookie(self):
        assert decide(ReasonCode.AUTH_401, attempt=1).give_up is True


class TestDecideNonRetryable:
    @pytest.mark.parametrize("reason", [ReasonCode.OK_COMPLETE, ReasonCode.OK_PARTIAL])
    def test_ok_reasons_never_wait(self, reason):
        d = decide(reason, attempt=1)
        assert d.should_wait is False
        assert d.delay_s == 0.0
        assert d.source == "none"
        assert d.give_up is False

    @pytest.mark.parametrize("reason", [ReasonCode.LIMIT_EXCEEDED, ReasonCode.DISK_ERROR])
    def test_fatal_reasons_give_up_without_waiting(self, reason):
        d = decide(reason, attempt=1)
        assert (d.should_wait, d.give_up, d.source) == (False, True, "none")

    @pytest.mark.parametrize("reason", [ReasonCode.END_OF_RANGE,
                                        ReasonCode.NOT_FOUND,
                                        ReasonCode.ABORTED])
    def test_clean_stops_never_wait(self, reason):
        d = decide(reason, attempt=1)
        assert d.should_wait is False
        assert d.source == "none"

    def test_end_of_range_is_not_treated_as_auth(self):
        """v1's fatal conflation: END_OF_RANGE must not look like a dead cookie."""
        assert decide(ReasonCode.END_OF_RANGE, attempt=1).give_up is False


class TestDecidePurity:
    def test_decide_does_not_sleep(self, monkeypatch):
        def explode(_seconds):  # pragma: no cover - must never run
            raise AssertionError("backoff.decide must not sleep")

        monkeypatch.setattr("time.sleep", explode)
        d = decide(ReasonCode.RATE_LIMITED, attempt=1)
        assert d.should_wait is True

    def test_repeated_calls_are_identical_without_rand(self):
        a = decide(ReasonCode.RATE_LIMITED, attempt=2)
        b = decide(ReasonCode.RATE_LIMITED, attempt=2)
        assert a == b


class TestBackoffDecisionShape:
    def test_is_frozen(self):
        d = BackoffDecision(should_wait=True, delay_s=1.0, source="exponential",
                            give_up=False, detail="x")
        with pytest.raises(FrozenInstanceError):
            d.delay_s = 2.0  # type: ignore[misc]

    def test_delay_ms_helper(self):
        d = decide(ReasonCode.RATE_LIMITED, attempt=1,
                   headers={"Retry-After": "1.5"})
        assert d.delay_ms == 1500

    def test_detail_is_always_populated(self):
        for reason in ReasonCode:
            assert decide(reason, attempt=1).detail
