"""Tests for the auto_pipeline_schedule field (issue: scheduler redesign,
v0.8.0). Replaces the prior auto_pipeline_hour + auto_pipeline_minute pair
with a single comma-separated clock-times string.
"""

from datetime import datetime

import pytest

from dispatcharr_sports_filter import plugin


# ----- _parse_schedule -----

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Default fallback
        ("", [(3, 0)]),
        ("   ", [(3, 0)]),
        (None, [(3, 0)]),
        # 4-digit HHMM
        ("0300", [(3, 0)]),
        ("1830", [(18, 30)]),
        ("0000", [(0, 0)]),
        ("2359", [(23, 59)]),
        # HH:MM
        ("03:00", [(3, 0)]),
        ("18:30", [(18, 30)]),
        # Bare hours
        ("3", [(3, 0)]),
        ("03", [(3, 0)]),
        ("23", [(23, 0)]),
        # 3-digit (h hh)
        ("300", [(3, 0)]),
        ("930", [(9, 30)]),
        # Multiple, sorted ascending, deduped
        ("1800,0600,1200,0000", [(0, 0), (6, 0), (12, 0), (18, 0)]),
        ("0300, 0300, 03:00", [(3, 0)]),  # duplicates collapsed
        # Whitespace tolerance
        ("0300 , 0900 , 1500", [(3, 0), (9, 0), (15, 0)]),
        ("0300\n0900\n1500", [(3, 0), (9, 0), (15, 0)]),
        # Mixed forms in same string
        ("0300, 12:00, 18", [(3, 0), (12, 0), (18, 0)]),
    ],
)
def test_parse_schedule_valid(raw, expected):
    assert plugin._parse_schedule(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "abc",        # not numeric
        "2500",       # hour out of range
        "0360",       # minute out of range
        "12345",      # too many digits
        "-300",       # negative -> non-numeric after strip
    ],
)
def test_parse_schedule_invalid_entries_fall_back_to_default(raw):
    """When EVERY entry is invalid, parser logs a warning and falls back to
    the default 0300 rather than returning an empty list (which would
    starve the scheduler)."""
    assert plugin._parse_schedule(raw) == [(3, 0)]


def test_parse_schedule_skips_invalid_keeps_valid():
    """A mix of valid and invalid entries: invalid ones get logged + dropped,
    valid ones survive. User who fat-fingers one entry doesn't lose the rest."""
    result = plugin._parse_schedule("0300, abc, 1800, 2500")
    assert result == [(3, 0), (18, 0)]


# ----- _next_firing -----

def _at(h, m, *, day=15):
    """Helper: build a datetime on 2026-04-{day} at h:m."""
    return datetime(2026, 4, day, h, m)


def test_next_firing_returns_today_if_time_still_ahead():
    now = _at(2, 0)
    schedule = [(3, 0)]
    assert plugin._next_firing(now, schedule) == _at(3, 0)


def test_next_firing_strictly_after_now_skips_exact_match():
    """If now == a scheduled time exactly, the function picks the NEXT one
    (or wraps to tomorrow). This prevents a tight loop where the scheduler
    fires, computes the next firing as 'right now', fires again."""
    now = _at(3, 0)
    schedule = [(3, 0)]
    # All today's times are <= now, so wrap to tomorrow.
    assert plugin._next_firing(now, schedule) == _at(3, 0, day=16)


def test_next_firing_picks_earliest_future_in_multi_time_schedule():
    now = _at(7, 30)
    schedule = [(0, 0), (6, 0), (12, 0), (18, 0)]
    assert plugin._next_firing(now, schedule) == _at(12, 0)


def test_next_firing_wraps_to_tomorrow_when_all_times_past():
    now = _at(23, 30)
    schedule = [(0, 0), (6, 0), (12, 0), (18, 0)]
    # Earliest tomorrow: 00:00 next day
    assert plugin._next_firing(now, schedule) == _at(0, 0, day=16)


def test_next_firing_wraps_to_tomorrow_picks_first_in_sorted_schedule():
    """Schedule passed in arbitrary order — function relies on caller
    (_parse_schedule) for sort. We test with sorted input only since
    that's the contract."""
    now = _at(23, 0)
    schedule = sorted([(3, 0), (15, 0)])
    assert plugin._next_firing(now, schedule) == _at(3, 0, day=16)


def test_next_firing_zeroes_seconds_and_microseconds():
    """The returned datetime is anchored to a clock minute, not whatever
    fractional time `now` happened to be. Otherwise sleep_s computation
    would carry the fractional offset into the next iteration."""
    now = datetime(2026, 4, 15, 2, 30, 45, 123456)
    schedule = [(3, 0)]
    target = plugin._next_firing(now, schedule)
    assert target.second == 0
    assert target.microsecond == 0
    assert target == _at(3, 0)


# ----- field manifest contract -----

def test_plugin_json_replaced_hour_minute_with_schedule():
    """Field schema sanity: the old auto_pipeline_hour and
    auto_pipeline_minute fields are gone; auto_pipeline_schedule is
    present. Catches accidental re-introduction during refactors."""
    import json
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "plugin.json")) as f:
        manifest = json.load(f)
    field_ids = {f["id"] for f in manifest["fields"]}
    assert "auto_pipeline_schedule" in field_ids
    assert "auto_pipeline_hour" not in field_ids
    assert "auto_pipeline_minute" not in field_ids
