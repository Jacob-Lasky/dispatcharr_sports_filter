"""Tests for the user-facing extension settings introduced in 0.6.0.

These let a downstream user (not Jake) tune the plugin via the Dispatcharr
plugin UI without forking the regex tables or the system prompts.
"""

import re

import pytest

from dispatcharr_sports_filter import classifier, plugin
from dispatcharr_sports_filter.constants import (
    DEFAULT_STRIP_PREFIXES,
    VERDICT_NOT_SPORTS,
    VERDICT_PURE_SPORTS,
)


# ----- compile_user_terms -----

def test_compile_user_terms_empty_returns_none():
    """None / empty string / whitespace must NOT compile to a regex that matches
    the empty string. Callers rely on None to mean 'no extension'."""
    assert classifier.compile_user_terms("") is None
    assert classifier.compile_user_terms("   ") is None
    assert classifier.compile_user_terms(None) is None
    assert classifier.compile_user_terms(",,,\n\n") is None


def test_compile_user_terms_comma_separated():
    rx = classifier.compile_user_terms("flosports, darts, snooker")
    assert rx is not None
    assert rx.search("FloSports HD")
    assert rx.search("Sky Darts")
    assert rx.search("Snooker UK")
    assert not rx.search("Football")


def test_compile_user_terms_newline_separated():
    rx = classifier.compile_user_terms("padel\nlacrosse\nbeach volleyball")
    assert rx is not None
    assert rx.search("Padel TV")
    assert rx.search("College Lacrosse")
    assert rx.search("Beach Volleyball Pro")


def test_compile_user_terms_escapes_metachars():
    """A user typing 'sec+' (a real conference name) should not be interpreted
    as 'one or more s, e, c'. Each term is regex-escaped, AND the trailing
    anchor uses (?!\\w) so the literal '+' at end-of-string still matches
    (plain \\b would fail because '+' is not a word char)."""
    rx = classifier.compile_user_terms("sec+")
    assert rx is not None
    assert rx.search("sec+")
    assert rx.search("SEC+ Football")
    assert rx.search("foo SEC+ bar")
    # Escaped: 'seccc' must NOT match (would have if + were the regex
    # quantifier instead of a literal).
    assert not rx.search("seccc")
    # Word-boundary on either side: 'sec+x' must not match (next char is word).
    assert not rx.search("sec+x")


def test_compile_user_terms_word_boundary():
    """Terms anchor to word boundaries — 'horse' should not match 'horseradish'."""
    rx = classifier.compile_user_terms("horse")
    assert rx is not None
    assert rx.search("horse racing")
    assert not rx.search("horseradish")


# ----- regex_classify with extras -----

def test_regex_classify_extra_deny_demotes_unwanted_sports():
    """The most common public-user case: demote a built-in sports term to
    not_sports. FloSports is in ALLOW_RE; user's deny extension wins via
    the 'has_deny + has_allow -> ambiguous (None)' path."""
    deny_extra = classifier.compile_user_terms("flosports")
    # Without extras, 'FloSports HD' -> pure_sports.
    assert classifier.regex_classify("FloSports HD") == VERDICT_PURE_SPORTS
    # With user's deny extension: ambiguous, defer to LLM (NOT pure_sports).
    assert classifier.regex_classify("FloSports HD", deny_extra_re=deny_extra) is None


def test_regex_classify_extra_deny_alone_classifies_not_sports():
    """A name matched ONLY by the user's deny extension (no allow hit) becomes
    not_sports, just like the built-in DENY_RE."""
    deny_extra = classifier.compile_user_terms("fishing, hunting")
    # 'Fishing TV' has no allow hit and no built-in deny hit.
    assert classifier.regex_classify("Fishing TV") is None
    # With user deny: now classified as not_sports.
    assert classifier.regex_classify("Fishing TV", deny_extra_re=deny_extra) == VERDICT_NOT_SPORTS


def test_regex_classify_extra_allow_promotes_niche_sports():
    """User can teach the regex about their favorite niche sports."""
    allow_extra = classifier.compile_user_terms("padel, lacrosse")
    assert classifier.regex_classify("Padel Premier") is None
    assert classifier.regex_classify("Padel Premier", allow_extra_re=allow_extra) == VERDICT_PURE_SPORTS


def test_regex_classify_no_extras_unchanged():
    """When no extras are passed, behavior matches the original regex."""
    assert classifier.regex_classify("Sports | NFL") == VERDICT_PURE_SPORTS
    assert classifier.regex_classify("Movies | HBO") == VERDICT_NOT_SPORTS
    assert classifier.regex_classify("US | Peacock TV") is None


# ----- _augment_prompt -----

def test_augment_prompt_no_hints_unchanged():
    base = "BASE"
    assert classifier._augment_prompt(base, "") == base
    assert classifier._augment_prompt(base, "   ") == base
    assert classifier._augment_prompt(base, None) == base


def test_augment_prompt_appends_hints():
    base = "BASE"
    out = classifier._augment_prompt(base, "Treat motorsport docs as sports.")
    assert out.startswith("BASE")
    assert "Additional user instructions" in out
    assert "motorsport docs" in out


# ----- _parse_strip_prefixes -----

def test_parse_strip_prefixes_default_when_empty():
    assert plugin._parse_strip_prefixes(None) == list(DEFAULT_STRIP_PREFIXES)
    assert plugin._parse_strip_prefixes("") == list(DEFAULT_STRIP_PREFIXES)
    assert plugin._parse_strip_prefixes("   ") == list(DEFAULT_STRIP_PREFIXES)


def test_parse_strip_prefixes_comma_separated():
    assert plugin._parse_strip_prefixes("Sports |, Sports/, SP-") == ["Sports |", "Sports/", "SP-"]


def test_parse_strip_prefixes_preserves_internal_pipes():
    """A prefix like 'SPRT|' contains a pipe. Splitting must NOT use pipe."""
    assert plugin._parse_strip_prefixes("SPRT|, SP-") == ["SPRT|", "SP-"]


def test_parse_strip_prefixes_strips_whitespace_around_entries():
    assert plugin._parse_strip_prefixes("  Sports |  ,   SP-  ") == ["Sports |", "SP-"]


# ----- _clean_target_name with custom config -----

def test_clean_target_name_custom_strip_prefix():
    """A provider that uses 'SP-' instead of 'Sports |' should produce the
    same shape of output once the user configures the prefix."""
    out = plugin._clean_target_name("SP-NFL", strip_prefixes=["SP-"])
    assert out == "NFL"


def test_clean_target_name_custom_prefix_case_insensitive():
    out = plugin._clean_target_name("sports | NFL", strip_prefixes=["Sports |"])
    assert out == "NFL"


def test_clean_target_name_no_prefixes_keeps_name():
    """Empty strip_prefixes list disables prefix stripping."""
    out = plugin._clean_target_name("Sports | NFL", strip_prefixes=[])
    # Without prefix strip, the input has no other rule that matches at the
    # head, so the name stays as-is (modulo whitespace normalization).
    assert out == "Sports | NFL"


def test_clean_target_name_mixed_without_sports_suffix():
    """User who prefers bouquet name verbatim turns suffix off."""
    out = plugin._clean_target_name(
        "US | Peacock TV", is_mixed=True, add_sports_suffix=False,
    )
    assert out == "US Peacock TV"


def test_clean_target_name_mixed_with_sports_suffix_default():
    out = plugin._clean_target_name("US | Peacock TV", is_mixed=True)
    assert out == "US Peacock TV Sports"


def test_clean_target_name_first_matching_prefix_wins():
    """When multiple prefixes match (rare, mostly nesting), only the first
    one in the configured list is stripped per call."""
    # 'Sports |' matches before 'Sports/'. The result drops 'Sports |' once.
    out = plugin._clean_target_name(
        "Sports | NFL", strip_prefixes=["Sports |", "Sports/"],
    )
    assert out == "NFL"


# ----- end-to-end: classify_all_groups with user extensions -----

def test_classify_all_groups_threads_user_terms_through():
    """A user with deny_extra of 'flosports' should see FloSports defer
    rather than classify as pure_sports — even though the built-in regex
    would normally handle it."""
    deny_extra = classifier.compile_user_terms("flosports")
    groups = [("FloSports HD", []), ("ESPN", [])]
    results, _ = classifier.classify_all_groups(
        api_key="", model="m", groups_with_samples=groups, cache={},
        deny_extra_re=deny_extra,
    )
    # FloSports: regex returns None (ambiguous), no API key -> not_sports fallback.
    assert results["FloSports HD"] == VERDICT_NOT_SPORTS
    # ESPN: still resolves at regex layer.
    assert results["ESPN"] == VERDICT_PURE_SPORTS
