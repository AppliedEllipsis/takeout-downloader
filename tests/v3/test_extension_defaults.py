"""The extension's tab-flood spawner must FAIL CLOSED, and stay that way.

Failure mode 1.9, measured: while any v2 job sat in `needs_cookie`, the
extension's one-minute alarm opened another browser tab, every minute, for about
three months. Peak observed: **314 tabs, memory 0 -> 4.7 GB**.

The live instance was fixed by setting `chrome.storage.local.autoRecapture =
false`. **Storage does not survive a profile reset.** The code default was still
`true`, so a fresh profile would rebuild the flood — and two config-merge sites
used `d.autoRecapture !== false`, which treats *missing* as ENABLED and would
re-arm the spawner from any merge that omitted the key.

There is no JS test harness in this repo, so this asserts the source directly.
It is textual on purpose: deterministic, offline, and it fails loudly if anyone
restores a fail-open form.

v3 does not need this path at all — it mints over CDP — so there is no cost to
keeping the switch off by default.
"""
import re
from pathlib import Path

BACKGROUND = Path(__file__).resolve().parents[2] / "helpers" / "background.js"


def _src() -> str:
    assert BACKGROUND.exists(), f"extension source missing at {BACKGROUND}"
    return BACKGROUND.read_text(encoding="utf-8")


def test_the_default_is_off():
    """A fresh profile must not re-arm the spawner."""
    src = _src()
    m = re.search(r"autoRecapture:\s*(true|false)", src)
    assert m, "could not find the autoRecapture default"
    assert m.group(1) == "false", (
        "DEFAULTS.autoRecapture must be false: a `true` default rebuilds the "
        "3-month tab flood on any profile reset")


def test_no_fail_open_merge_anywhere():
    """`!== false` treats undefined as ENABLED. Every site must require `=== true`."""
    src = _src()
    offenders = [ln for ln in src.splitlines()
                 if "autoRecapture" in ln
                 and "!==" in ln
                 and "false" in ln
                 and not ln.strip().startswith("//")]
    assert not offenders, f"fail-open autoRecapture merge(s): {offenders}"


def test_the_alarm_is_not_created_unconditionally():
    """Creating the alarm unconditionally kept polling the manager every minute
    forever, so a single bug in the early-return would resume the flood."""
    src = _src()
    calls = [ln.strip() for ln in src.splitlines()
             if "chrome.alarms.create(RECAPTURE_ALARM" in ln
             and not ln.strip().startswith("//")]
    assert calls, "the recapture alarm is no longer created anywhere"
    assert len(calls) == 1, f"expected exactly one creation site, got {calls}"


def test_the_poll_requires_an_explicit_opt_in():
    """The early-return is the last line of defence, so it must be strict."""
    src = _src()
    m = re.search(r"async function pollRecapturePending\(\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "pollRecapturePending not found"
    body = m.group(1)
    assert "s.autoRecapture !== true" in body, (
        "the poll must return unless autoRecapture is explicitly true; a falsy "
        "check lets a missing value through in some coercion paths")
    assert "if (!s.autoRecapture)" not in body

    assert "autoRecapture" in body
    assert "return" in body


def test_the_spawner_still_exists_rather_than_being_silently_deleted():
    """Guard against the opposite failure: someone removing the feature entirely
    without noticing it is referenced by the popup and options UI."""
    src = _src()
    assert "triggerRecapture" in src
    assert "RECAPTURE_ALARM" in src
