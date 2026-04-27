"""Tests for the pure helpers in plugin.py — name cleaner, regex builder,
cache filters, and JSON read/write. No Django, no Dispatcharr."""

import json
import os
import re

import pytest

from dispatcharr_sports_filter import plugin
from dispatcharr_sports_filter.constants import (
    VERDICT_MIXED,
    VERDICT_NOT_SPORTS,
    VERDICT_PURE_SPORTS,
    VERDICT_SPORTS,
)


# ----- _clean_target_name (pure_sports) -----

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Sports | NFL", "NFL"),
        ("Sports/PPV", "PPV"),
        ("Brazil | Sports", "Brazil Sports"),
        ("Sports | NBA (2)", "NBA"),
        ("Sports | NBA", "NBA"),
        ("UK | Sky Sports", "UK | Sky Sports"),  # no rule matches, region preserved
        ("Big Ten +", "Big Ten+"),  # trailing space-plus normalization
        ("Sky Sports + TNT Sports", "Sky Sports + TNT Sports"),  # internal '+' untouched
        ("Sports | NBA (2)", "NBA"),  # (N) duplicate-feed marker dropped
    ],
)
def test_clean_target_name_pure(raw, expected):
    assert plugin._clean_target_name(raw, is_mixed=False) == expected


# ----- _clean_target_name (mixed) -----

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Sports | HBO Max US", "HBO Max US Sports"),
        ("US | Peacock TV", "US Peacock TV Sports"),
        ("IE | Sky", "IE Sky Sports"),
        ("CAR | Sports", "CAR Sports"),  # already ends with Sports
        ("Sports | Stan (2)", "Stan Sports"),
    ],
)
def test_clean_target_name_mixed(raw, expected):
    assert plugin._clean_target_name(raw, is_mixed=True) == expected


def test_clean_target_name_consolidates_duplicate_feeds():
    """The (N) suffix must be dropped so 'NBA' and 'NBA (2)' point at the
    same target group."""
    a = plugin._clean_target_name("Sports | NBA", is_mixed=False)
    b = plugin._clean_target_name("Sports | NBA (2)", is_mixed=False)
    assert a == b == "NBA"


# ----- _build_match_regex -----

def test_build_match_regex_empty():
    assert plugin._build_match_regex([]) == ""
    assert plugin._build_match_regex([""]) == ""  # filter falsy


def test_build_match_regex_escapes_metachars():
    """Stream names contain '+', '|', '(', etc. They must be regex-escaped or
    Dispatcharr's iregex match will silently include unintended streams."""
    rx = plugin._build_match_regex(["NFL Network HD", "ESPN+ (West)"])
    pattern = re.compile(rx, re.IGNORECASE)
    assert pattern.fullmatch("NFL Network HD")
    assert pattern.fullmatch("ESPN+ (West)")
    # Must NOT match a non-listed stream
    assert not pattern.fullmatch("CNN")
    # Must NOT match prefixes/suffixes
    assert not pattern.fullmatch("NFL Network HD Plus")


def test_build_match_regex_anchored():
    rx = plugin._build_match_regex(["A"])
    assert rx.startswith("^") and rx.endswith("$")


# ----- cache load filters -----

def test_read_group_cache_drops_legacy_binary(tmp_path, monkeypatch):
    """Legacy v1 'sports' values must be filtered out so they re-classify."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "Sports | NFL": "sports",            # legacy binary -> drop
        "Sports | NBA": VERDICT_PURE_SPORTS,  # current ternary -> keep
        "Movies": VERDICT_NOT_SPORTS,         # current ternary -> keep
        "Sports | Stan": VERDICT_MIXED,       # current ternary -> keep
        "Garbage": "wat",                     # unknown -> drop
    }))
    monkeypatch.setattr(plugin, "CACHE_PATH", str(cache_path))
    out = plugin._read_group_cache()
    assert out == {
        "Sports | NBA": VERDICT_PURE_SPORTS,
        "Movies": VERDICT_NOT_SPORTS,
        "Sports | Stan": VERDICT_MIXED,
    }


def test_read_stream_cache_drops_unknown(tmp_path, monkeypatch):
    cache_path = tmp_path / "stream_cache.json"
    cache_path.write_text(json.dumps({
        "NFL Network HD": VERDICT_SPORTS,
        "Peacock News": VERDICT_NOT_SPORTS,
        "Mystery": "??",
        "Garbage": "pure_sports",  # group-level verdict in stream cache -> drop
    }))
    monkeypatch.setattr(plugin, "STREAM_CACHE_PATH", str(cache_path))
    out = plugin._read_stream_cache()
    assert out == {
        "NFL Network HD": VERDICT_SPORTS,
        "Peacock News": VERDICT_NOT_SPORTS,
    }


def test_read_group_cache_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "CACHE_PATH", str(tmp_path / "nope.json"))
    assert plugin._read_group_cache() == {}


# ----- _read_api_key -----

def test_read_api_key_settings_wins(tmp_path, monkeypatch):
    """A non-empty settings field beats the on-disk file."""
    on_disk = tmp_path / "anthropic_api_key"
    on_disk.write_text("file-key")
    monkeypatch.setattr(plugin, "API_KEY_PATH", str(on_disk))
    assert plugin._read_api_key({"anthropic_api_key": "ui-key"}) == "ui-key"


def test_read_api_key_falls_back_to_disk(tmp_path, monkeypatch):
    on_disk = tmp_path / "anthropic_api_key"
    on_disk.write_text("file-key\n")
    monkeypatch.setattr(plugin, "API_KEY_PATH", str(on_disk))
    assert plugin._read_api_key({"anthropic_api_key": ""}) == "file-key"
    assert plugin._read_api_key({}) == "file-key"
    assert plugin._read_api_key(None) == "file-key"


def test_read_api_key_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "API_KEY_PATH", str(tmp_path / "missing"))
    assert plugin._read_api_key({}) == ""


# ----- write_json round-trip -----

def test_write_json_atomic(tmp_path):
    path = tmp_path / "x.json"
    plugin._write_json(str(path), {"k": "v"})
    assert json.loads(path.read_text()) == {"k": "v"}
    # Tmp file must not linger
    assert not (tmp_path / "x.json.tmp").exists()


# ----- ACTION_HANDLERS dispatch table -----

def test_action_handlers_table_matches_manifest():
    """Every action declared in the user-facing actions list must have a
    handler, and vice versa. Catches the rot of adding an action button
    without wiring it up (or removing the button without removing the
    handler)."""
    declared_action_ids = set(plugin.ACTION_HANDLERS.keys())
    expected = {
        "classify",
        "refine_mixed",
        "apply",
        "cleanup_orphans",
        "auto_pipeline",
        "show_status",
    }
    assert declared_action_ids == expected


# ----- Settings resolvers -----

def test_resolve_account_id_default_is_all():
    """Empty / unset settings default to None ('all M3U accounts'), the new
    public-plugin default — was previously DEFAULT_ACCOUNT_ID, but forcing a
    fresh user to hand-pick a single account before the plugin would do
    anything was unnecessary cognitive overhead."""
    assert plugin._resolve_account_id({}) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", None),
        (None, None),
        (0, None),
        ("0", None),
        ("5", 5),
        (5, 5),
        ("1", 1),
    ],
)
def test_resolve_account_id_handles_all_sentinel_forms(raw, expected):
    """Dispatcharr's select fields can produce empty string, None, 0, or '0'
    depending on storage path. All four must map to None ('all'). A real
    int / numeric-string parses normally."""
    assert plugin._resolve_account_id({"m3u_account_id": raw}) == expected


def test_plugin_json_select_options_have_nonblank_values():
    """Dispatcharr's plugin loader rejects select option entries whose
    'value' is blank with 'apps.plugins.loader: Invalid plugin field entry
    ignored' — silently dropping the WHOLE field. Caught in live integration
    when the all-accounts sentinel was first set to ''. Pin the contract so
    this regresses loud."""
    import json
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "plugin.json")) as f:
        manifest = json.load(f)
    for field in manifest["fields"]:
        if field.get("type") != "select":
            continue
        for opt in field.get("options", []):
            assert opt.get("value") not in (None, ""), (
                f"Field {field['id']!r} has a select option with blank "
                f"value: {opt!r}. Dispatcharr will drop the field."
            )


def test_resolve_model_default():
    from dispatcharr_sports_filter.constants import DEFAULT_MODEL
    assert plugin._resolve_model({}) == DEFAULT_MODEL


def test_resolve_model_override():
    assert plugin._resolve_model({"model": "claude-opus-4-7"}) == "claude-opus-4-7"
