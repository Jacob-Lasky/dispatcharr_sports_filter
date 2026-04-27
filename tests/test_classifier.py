"""Tests for the pure functions in classifier.py — regex pre-filter, JSON
extraction, and verdict normalization. No network, no Dispatcharr."""

import pytest

from dispatcharr_sports_filter import classifier
from dispatcharr_sports_filter.constants import (
    VERDICT_MIXED,
    VERDICT_NOT_SPORTS,
    VERDICT_PURE_SPORTS,
    VERDICT_SPORTS,
)


# ----- regex_classify -----

@pytest.mark.parametrize(
    "name, expected",
    [
        # Decisive sports leagues / networks
        ("Sports | NFL", VERDICT_PURE_SPORTS),
        ("ESPN", VERDICT_PURE_SPORTS),
        ("Sky Sports Premier League", VERDICT_PURE_SPORTS),
        ("Big Ten Network", VERDICT_PURE_SPORTS),
        ("FloSports", VERDICT_PURE_SPORTS),
        ("F1 TV", VERDICT_PURE_SPORTS),
        ("UFC PPV", VERDICT_PURE_SPORTS),
        # Decisive non-sports
        ("Movies | HBO", VERDICT_NOT_SPORTS),
        ("Religious | Gospel", VERDICT_NOT_SPORTS),
        ("Adult XXX", VERDICT_NOT_SPORTS),
        ("Kids | Disney", VERDICT_NOT_SPORTS),
        ("News | CNN", VERDICT_NOT_SPORTS),
        # Ambiguous: defer to LLM
        ("Sports | Peacock TV", None),  # bare 'sports' is intentionally NOT in ALLOW_RE
        ("US | Peacock TV", None),
        ("Colombia | TV", None),
        # Conflict: ALLOW + DENY both match -> defer
        ("ESPN Documentaries", None),
    ],
)
def test_regex_classify(name, expected):
    assert classifier.regex_classify(name) == expected


def test_regex_does_not_match_bare_sport_keyword():
    """Critical: bare 'sport'/'sports' must NOT trigger pure_sports — bouquet
    names like 'Sports | Peacock TV' need to defer to the LLM."""
    assert classifier.regex_classify("Sports | Whatever") is None
    assert classifier.regex_classify("Generic Sports Channel") is None


# ----- _extract_json -----

def test_extract_json_plain():
    assert classifier._extract_json('{"a": "b"}') == {"a": "b"}


def test_extract_json_fenced_with_lang():
    raw = '```json\n{"k": "v"}\n```'
    assert classifier._extract_json(raw) == {"k": "v"}


def test_extract_json_fenced_no_lang():
    raw = "```\n{\"k\": \"v\"}\n```"
    assert classifier._extract_json(raw) == {"k": "v"}


def test_extract_json_with_leading_prose():
    raw = 'Here is your answer: {"NFL": "pure_sports"} hope that helps.'
    assert classifier._extract_json(raw) == {"NFL": "pure_sports"}


def test_extract_json_invalid_raises():
    with pytest.raises(Exception):
        classifier._extract_json("not json at all")


# ----- _normalize_group_verdict -----

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("pure_sports", VERDICT_PURE_SPORTS),
        ("PURE_SPORTS", VERDICT_PURE_SPORTS),
        ("pure-sports", VERDICT_PURE_SPORTS),
        ("pure sports", VERDICT_PURE_SPORTS),
        ("puresports", VERDICT_PURE_SPORTS),
        ("mixed", VERDICT_MIXED),
        ("MIXED", VERDICT_MIXED),
        ("not_sports", VERDICT_NOT_SPORTS),
        ("anything else", VERDICT_NOT_SPORTS),
        ("", VERDICT_NOT_SPORTS),
        (None, VERDICT_NOT_SPORTS),
        (42, VERDICT_NOT_SPORTS),
    ],
)
def test_normalize_group_verdict(raw, expected):
    assert classifier._normalize_group_verdict(raw) == expected


# ----- _normalize_stream_verdict -----

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("sports", VERDICT_SPORTS),
        ("SPORTS", VERDICT_SPORTS),
        (" sports ", VERDICT_SPORTS),
        ("not_sports", VERDICT_NOT_SPORTS),
        ("anything", VERDICT_NOT_SPORTS),
        ("", VERDICT_NOT_SPORTS),
        (None, VERDICT_NOT_SPORTS),
    ],
)
def test_normalize_stream_verdict(raw, expected):
    assert classifier._normalize_stream_verdict(raw) == expected


# ----- classify_all_groups (cache + regex paths, no LLM) -----

def test_classify_all_groups_uses_cache_first():
    cache = {"NFL": VERDICT_PURE_SPORTS, "Movies": VERDICT_NOT_SPORTS}
    groups = [("NFL", []), ("Movies", [])]
    results, new_only = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache=cache,
    )
    assert results == {"NFL": VERDICT_PURE_SPORTS, "Movies": VERDICT_NOT_SPORTS}
    assert new_only == {}


def test_classify_all_groups_uses_regex_when_decisive():
    groups = [("ESPN HD", []), ("CNN International", [])]
    results, new_only = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache={},
    )
    assert results["ESPN HD"] == VERDICT_PURE_SPORTS
    assert results["CNN International"] == VERDICT_NOT_SPORTS
    assert new_only == results


def test_classify_all_groups_no_api_key_falls_back_to_not_sports():
    """Ambiguous groups with no API key must fail-closed to not_sports."""
    groups = [("Sports | Peacock", ["channel one"])]
    results, new_only = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache={},
    )
    assert results == {"Sports | Peacock": VERDICT_NOT_SPORTS}
    assert new_only == {"Sports | Peacock": VERDICT_NOT_SPORTS}


def test_classify_all_groups_drops_unknown_cache_values():
    """A cache entry with an unrecognized verdict (e.g. legacy 'sports') should
    NOT be honored. classify_all_groups treats it as missing and re-classifies."""
    cache = {"NFL": "sports"}  # legacy v1 binary verdict
    groups = [("NFL", [])]
    results, _ = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache=cache,
    )
    # NFL re-classified via regex -> pure_sports
    assert results["NFL"] == VERDICT_PURE_SPORTS
