"""Tests for the regex-only / LLM-free mode (issue #2, v0.7.0).

Pins the behavior contract for users who flip 'Use LLM' off in the
plugin settings: ambiguous groups must NOT trigger an LLM call (even
with an api_key configured), pre-existing 'mixed' cache entries must
survive the mode switch, and refine_mixed must return a friendly
ok-with-skip status rather than the hard 'no API key' error.
"""

from unittest.mock import patch

import pytest

from dispatcharr_sports_filter import classifier, plugin
from dispatcharr_sports_filter.constants import (
    VERDICT_MIXED,
    VERDICT_NOT_SPORTS,
    VERDICT_PURE_SPORTS,
    VERDICT_SPORTS,
)


# ----- classify_all_groups in regex-only mode -----

def test_enable_llm_false_skips_llm_even_with_key():
    """Critical contract: enable_llm=False must NOT call the LLM, even if a
    valid-looking api_key is configured. The user explicitly turned LLM
    off; honor that over the key being present."""
    groups = [("Sports | Peacock TV", ["channel one", "channel two"])]
    with patch.object(classifier, "classify_groups_with_llm") as mock_llm:
        results, new_only = classifier.classify_all_groups(
            api_key="sk-ant-fake-but-truthy",
            model="m",
            groups_with_samples=groups,
            cache={},
            enable_llm=False,
        )
        mock_llm.assert_not_called()
    assert results == {"Sports | Peacock TV": VERDICT_NOT_SPORTS}
    assert new_only == {"Sports | Peacock TV": VERDICT_NOT_SPORTS}


def test_enable_llm_false_preserves_existing_mixed_cache():
    """A user who built a cache with LLM ON, then flips LLM OFF, must NOT
    have their pre-classified 'mixed' entries dropped or re-classified.
    Cache check runs before the regex / LLM path."""
    cache = {
        "Sports | Peacock TV": VERDICT_MIXED,
        "US | Stan": VERDICT_PURE_SPORTS,
        "Movies | HBO": VERDICT_NOT_SPORTS,
    }
    groups = [
        ("Sports | Peacock TV", []),
        ("US | Stan", []),
        ("Movies | HBO", []),
    ]
    results, new_only = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache=cache, enable_llm=False,
    )
    # Mixed verdict survives despite LLM being off.
    assert results["Sports | Peacock TV"] == VERDICT_MIXED
    assert results["US | Stan"] == VERDICT_PURE_SPORTS
    assert results["Movies | HBO"] == VERDICT_NOT_SPORTS
    # Nothing was newly classified — all came from cache.
    assert new_only == {}


def test_enable_llm_false_resolves_via_regex_first():
    """Decisive regex matches still resolve at the regex layer in regex-only
    mode — only ambiguous-and-no-LLM cases fall to not_sports."""
    groups = [
        ("Sports | NFL", []),       # regex decisive: pure_sports
        ("Movies | HBO", []),       # regex decisive: not_sports
        ("Sports | Peacock TV", []),  # ambiguous -> not_sports under llm-off
    ]
    results, _ = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache={}, enable_llm=False,
    )
    assert results["Sports | NFL"] == VERDICT_PURE_SPORTS
    assert results["Movies | HBO"] == VERDICT_NOT_SPORTS
    assert results["Sports | Peacock TV"] == VERDICT_NOT_SPORTS


def test_enable_llm_false_respects_user_extras():
    """extra_allow_terms and extra_deny_terms are the user's tuning lever in
    regex-only mode — they must still take effect when LLM is off."""
    allow_extra = classifier.compile_user_terms("peacock")
    groups = [("Sports | Peacock TV", []), ("Sports | Stan", [])]
    results, _ = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache={},
        enable_llm=False, allow_extra_re=allow_extra,
    )
    # User's extra_allow recovers Peacock from the regex-only fail-closed default.
    assert results["Sports | Peacock TV"] == VERDICT_PURE_SPORTS
    # Stan still falls through to not_sports.
    assert results["Sports | Stan"] == VERDICT_NOT_SPORTS


def test_enable_llm_true_default_calls_llm_path():
    """Sanity-check that enable_llm defaults to True — i.e. omitting the kwarg
    keeps existing v0.6.0 behavior. Existing callers must not see a regression."""
    groups = [("Sports | Peacock TV", ["channel one"])]
    with patch.object(classifier, "classify_groups_with_llm",
                      return_value={"Sports | Peacock TV": VERDICT_MIXED}) as mock_llm:
        results, _ = classifier.classify_all_groups(
            api_key="sk-fake", model="m", groups_with_samples=groups, cache={},
        )
        mock_llm.assert_called_once()
    assert results["Sports | Peacock TV"] == VERDICT_MIXED


def test_enable_llm_true_no_key_still_fails_closed():
    """If LLM is enabled but no api_key is configured, ambiguous groups still
    fail-closed to not_sports (existing behavior, unchanged from v0.6.0)."""
    groups = [("Sports | Peacock TV", [])]
    results, _ = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache={}, enable_llm=True,
    )
    assert results["Sports | Peacock TV"] == VERDICT_NOT_SPORTS


# ----- _action_refine_mixed in regex-only mode -----

def test_refine_mixed_skips_gracefully_when_llm_disabled(tmp_path, monkeypatch):
    """Refine should return ok-with-skip status (NOT an error) when LLM is
    disabled. status='ok' so auto_pipeline doesn't bail; the message
    explains why nothing happened."""
    import json
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"Sports | Peacock TV": VERDICT_MIXED}))
    monkeypatch.setattr(plugin, "CACHE_PATH", str(cache_path))

    result = plugin._action_refine_mixed({
        "enable_llm": False,
        "m3u_account_id": "0",
    })
    assert result["status"] == "ok"
    assert result.get("skipped") is True
    assert result["mixed_groups"] == 1
    assert "LLM disabled" in result["message"]


def test_refine_mixed_no_mixed_groups_returns_early_regardless_of_llm(tmp_path, monkeypatch):
    """When the cache has no mixed entries, refine returns a 'nothing to do'
    status whether or not LLM is enabled. The mixed-empty path runs before
    the LLM check."""
    import json
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"Sports | NFL": VERDICT_PURE_SPORTS}))
    monkeypatch.setattr(plugin, "CACHE_PATH", str(cache_path))

    for enable_llm in (True, False):
        result = plugin._action_refine_mixed({
            "enable_llm": enable_llm,
            "m3u_account_id": "0",
        })
        assert result["status"] == "ok"
        assert "No groups marked 'mixed'" in result["message"]
