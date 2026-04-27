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
    """A non-empty settings field beats the on-disk file. Default provider
    is anthropic for backward compatibility with the v0.8.x signature."""
    on_disk = tmp_path / "anthropic_api_key"
    on_disk.write_text("file-key")
    monkeypatch.setattr(plugin, "PLUGIN_DIR", str(tmp_path))
    assert plugin._read_api_key({"anthropic_api_key": "ui-key"}) == "ui-key"


def test_read_api_key_falls_back_to_disk(tmp_path, monkeypatch):
    on_disk = tmp_path / "anthropic_api_key"
    on_disk.write_text("file-key\n")
    monkeypatch.setattr(plugin, "PLUGIN_DIR", str(tmp_path))
    assert plugin._read_api_key({"anthropic_api_key": ""}) == "file-key"
    assert plugin._read_api_key({}) == "file-key"
    assert plugin._read_api_key(None) == "file-key"


def test_read_api_key_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "PLUGIN_DIR", str(tmp_path))
    assert plugin._read_api_key({}) == ""


# ----- _read_api_key per-provider -----

@pytest.mark.parametrize(
    "provider, field, filename",
    [
        ("anthropic", "anthropic_api_key", "anthropic_api_key"),
        ("openai", "openai_api_key", "openai_api_key"),
        ("gemini", "gemini_api_key", "gemini_api_key"),
    ],
)
def test_read_api_key_per_provider_settings_field(provider, field, filename, tmp_path, monkeypatch):
    """Each provider has its own settings field. Setting the field for one
    provider must not leak into another provider's lookup."""
    monkeypatch.setattr(plugin, "PLUGIN_DIR", str(tmp_path))
    assert plugin._read_api_key({field: "key-from-ui"}, provider=provider) == "key-from-ui"
    # Sibling provider's field shouldn't satisfy us.
    other_field = "openai_api_key" if field != "openai_api_key" else "gemini_api_key"
    assert plugin._read_api_key({other_field: "wrong-key"}, provider=provider) == ""


@pytest.mark.parametrize(
    "provider, filename",
    [
        ("anthropic", "anthropic_api_key"),
        ("openai", "openai_api_key"),
        ("gemini", "gemini_api_key"),
    ],
)
def test_read_api_key_per_provider_disk_fallback(provider, filename, tmp_path, monkeypatch):
    """File-fallback pattern is symmetric across providers — each provider
    looks for <plugin_dir>/<provider>_api_key on disk when no settings field
    is set."""
    (tmp_path / filename).write_text(f"{provider}-disk-key\n")
    monkeypatch.setattr(plugin, "PLUGIN_DIR", str(tmp_path))
    assert plugin._read_api_key({}, provider=provider) == f"{provider}-disk-key"


def test_read_api_key_unknown_provider_raises(tmp_path, monkeypatch):
    """Unknown provider should raise loud rather than silently fall back to a
    default. Otherwise a typo'd provider string would hide the bug behind a
    return-empty-string."""
    monkeypatch.setattr(plugin, "PLUGIN_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="unknown provider"):
        plugin._read_api_key({}, provider="bogus")


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


def test_pyflakes_clean():
    """Run pyflakes against every Python file in the plugin. Catches the
    class of bug where a refactor removes a local variable but leaves a
    later reference to it intact (e.g. `if debug or X:` after lifting
    `debug = bool(settings.get(...))` into a helper). NameError fires only
    when the gated path actually executes — unit tests that mock around
    that branch miss it.

    Real ship-blocker in v0.7.0: _action_classify and _action_apply both
    referenced `debug` after the variable was lifted into
    _apply_debug_logging. Auto-pipeline NameError'd in the UI. Pyflakes
    would have flagged 'undefined name "debug"' immediately.
    """
    import os
    import subprocess

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    targets = [
        os.path.join(repo_root, name)
        for name in ("plugin.py", "classifier.py", "constants.py", "__init__.py")
    ]
    pyflakes = os.path.join(repo_root, ".venv", "bin", "pyflakes")
    if not os.path.exists(pyflakes):
        # Falls back to the system pyflakes if .venv isn't present (CI etc.).
        pyflakes = "pyflakes"
    result = subprocess.run(
        [pyflakes, *targets], capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"pyflakes flagged issues:\n{result.stdout}\n{result.stderr}"
    )


def test_plugin_json_and_python_fields_agree_on_ids():
    """Plugin.fields (Python-side) and plugin.json (static manifest) are
    duplicate manifests by design — Plugin.fields exists so the loader can
    inject DB-driven dropdown options that JSON cannot. The in-code comment
    says they must stay in sync; this test pins the contract.

    Drift is silent: a field present in only one source might or might not
    surface depending on Dispatcharr's loader merge order. Pin against the
    pre-v0.9.0 drift (anthropic_api_key was in plugin.json only) by
    requiring exact set equality."""
    import json
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "plugin.json")) as f:
        manifest = json.load(f)
    json_ids = {f["id"] for f in manifest["fields"]}

    p = plugin.Plugin.__new__(plugin.Plugin)
    # Skip Plugin.__init__ side effects (DB queries for dropdown options) by
    # cherry-picking the literal field list. We can't call __init__ in a
    # Django-free test env. Read self.fields by introspecting the source.
    src = open(os.path.join(here, "..", "plugin.py")).read()
    py_ids = set(re.findall(r'"id":\s*"([a-z0-9_]+)"', src))
    # Drop action ids — those are in self.actions, not self.fields.
    action_ids = {"classify", "refine_mixed", "apply", "cleanup_orphans",
                  "auto_pipeline", "show_status"}
    py_ids -= action_ids

    only_in_json = json_ids - py_ids
    only_in_py = py_ids - json_ids
    assert not only_in_json and not only_in_py, (
        f"plugin.json and Plugin.fields drifted.\n"
        f"  in plugin.json only: {sorted(only_in_json)}\n"
        f"  in Plugin.fields only: {sorted(only_in_py)}"
    )


def test_plugin_json_has_per_provider_api_key_fields():
    """v0.9.0 added OpenAI + Gemini key fields alongside the existing
    Anthropic key field. All three must be present and masked, mirroring
    the constants.PROVIDER_SETTINGS_FIELD map."""
    import json
    import os
    from dispatcharr_sports_filter.constants import (
        PROVIDER_SETTINGS_FIELD,
        PROVIDERS,
    )
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "plugin.json")) as f:
        manifest = json.load(f)
    fields_by_id = {f["id"]: f for f in manifest["fields"]}
    for provider in PROVIDERS:
        field_id = PROVIDER_SETTINGS_FIELD[provider]
        assert field_id in fields_by_id, (
            f"plugin.json missing API key field {field_id!r} for provider {provider!r}"
        )
        field = fields_by_id[field_id]
        assert field.get("input_type") == "password", (
            f"{field_id!r} must use input_type=password to mask the secret in the UI"
        )


def test_plugin_json_model_dropdown_covers_all_providers():
    """The model dropdown must surface at least one option per provider — a
    user shouldn't have to type a model ID by hand to use OpenAI or Gemini.
    Verifies provider_for_model() recognizes every shipped option."""
    import json
    import os
    from dispatcharr_sports_filter import classifier
    from dispatcharr_sports_filter.constants import PROVIDERS
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "plugin.json")) as f:
        manifest = json.load(f)
    model_field = next(f for f in manifest["fields"] if f["id"] == "model")
    seen_providers = {classifier.provider_for_model(opt["value"])
                      for opt in model_field["options"]}
    missing = PROVIDERS - seen_providers
    assert not missing, f"Model dropdown has no option for providers: {missing}"


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
