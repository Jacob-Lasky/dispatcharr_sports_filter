"""Tests for multi-provider LLM plumbing (issue #1, v0.9.0).

Pins the contract that:
  - The provider is inferred from the model ID prefix and only from the
    model ID prefix. No separate provider field exists, so a model and key
    set in the UI must always agree.
  - Each provider's request goes to the right URL with the right auth
    header and the right body shape.
  - Each provider's response is parsed correctly — text comes out, token
    counts populate the same in=/out= log line regardless of source.
  - The dispatcher fails-closed (returns None) on any network or parse
    error rather than raising — callers depend on this to fall back to
    not_sports.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from dispatcharr_sports_filter import classifier
from dispatcharr_sports_filter.constants import (
    PROVIDER_ANTHROPIC,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
)


# ----- provider_for_model -----

@pytest.mark.parametrize(
    "model, expected",
    [
        # Anthropic
        ("claude-haiku-4-5", PROVIDER_ANTHROPIC),
        ("claude-opus-4-7", PROVIDER_ANTHROPIC),
        ("claude-sonnet-4-6", PROVIDER_ANTHROPIC),
        # OpenAI families
        ("gpt-4o-mini", PROVIDER_OPENAI),
        ("gpt-4.1", PROVIDER_OPENAI),
        ("gpt-4o", PROVIDER_OPENAI),
        ("o1-preview", PROVIDER_OPENAI),
        ("o3-mini", PROVIDER_OPENAI),
        ("o4-mini", PROVIDER_OPENAI),
        # Gemini
        ("gemini-2.0-flash", PROVIDER_GEMINI),
        ("gemini-2.5-pro", PROVIDER_GEMINI),
        # Case + whitespace tolerance
        ("CLAUDE-haiku-4-5", PROVIDER_ANTHROPIC),
        ("  gpt-4o-mini  ", PROVIDER_OPENAI),
    ],
)
def test_provider_for_model(model, expected):
    assert classifier.provider_for_model(model) == expected


@pytest.mark.parametrize("bogus", ["", None, "totally-made-up-model", "llama-3"])
def test_provider_for_model_fallback_is_anthropic(bogus):
    """Unknown / empty model names fall back to anthropic — the plugin shipped
    Anthropic-only through 0.8.x, so an unrecognized prefix is more likely a
    Claude alias the prefix table doesn't know about than a wholly new
    provider. Documenting the fallback here so a future re-think happens
    intentionally rather than as a silent change."""
    assert classifier.provider_for_model(bogus) == PROVIDER_ANTHROPIC


# ----- _build_request shape -----

def test_build_request_anthropic_shape():
    req = classifier._build_request(
        PROVIDER_ANTHROPIC, "sk-ant-test", "claude-haiku-4-5", "sys-prompt", "user-prompt",
    )
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.get_method() == "POST"
    # Header lookups are case-insensitive in urllib
    assert req.get_header("X-api-key") == "sk-ant-test"
    assert req.get_header("Anthropic-version") == "2023-06-01"
    body = json.loads(req.data)
    assert body["model"] == "claude-haiku-4-5"
    assert body["system"] == "sys-prompt"  # Anthropic-specific top-level system
    assert body["messages"] == [{"role": "user", "content": "user-prompt"}]
    assert body["max_tokens"] == 4096


def test_build_request_openai_shape():
    req = classifier._build_request(
        PROVIDER_OPENAI, "sk-test", "gpt-4o-mini", "sys-prompt", "user-prompt",
    )
    assert req.full_url == "https://api.openai.com/v1/chat/completions"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer sk-test"
    body = json.loads(req.data)
    assert body["model"] == "gpt-4o-mini"
    # OpenAI puts system prompt as the first message with role=system, not top-level.
    assert body["messages"] == [
        {"role": "system", "content": "sys-prompt"},
        {"role": "user", "content": "user-prompt"},
    ]
    # response_format=json_object is a deliberate ask for strict JSON output.
    assert body["response_format"] == {"type": "json_object"}


def test_build_request_gemini_shape():
    req = classifier._build_request(
        PROVIDER_GEMINI, "gem-test", "gemini-2.0-flash", "sys-prompt", "user-prompt",
    )
    # Gemini auth is via ?key= query string, not a header. Path includes the model.
    assert req.full_url.startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key="
    )
    assert "gem-test" in req.full_url
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") is None  # no auth header for Gemini
    body = json.loads(req.data)
    assert body["systemInstruction"] == {"parts": [{"text": "sys-prompt"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "user-prompt"}]}]
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_build_request_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        classifier._build_request("madeup", "k", "m", "s", "u")


# ----- _parse_response shape -----

def test_parse_response_anthropic():
    raw = json.dumps({
        "content": [{"type": "text", "text": '{"NFL": "pure_sports"}'}],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    })
    text, in_t, out_t = classifier._parse_response(PROVIDER_ANTHROPIC, raw)
    assert text == '{"NFL": "pure_sports"}'
    assert in_t == 100 and out_t == 20


def test_parse_response_anthropic_skips_non_text_blocks():
    """Anthropic can return mixed content blocks (text, tool_use). Only text
    blocks should be concatenated."""
    raw = json.dumps({
        "content": [
            {"type": "tool_use", "name": "calculator", "input": {}},
            {"type": "text", "text": "hello"},
            {"type": "text", "text": " world"},
        ],
        "usage": {},
    })
    text, _, _ = classifier._parse_response(PROVIDER_ANTHROPIC, raw)
    assert text == "hello world"


def test_parse_response_openai():
    raw = json.dumps({
        "choices": [{"message": {"content": '{"NFL": "pure_sports"}'}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    })
    text, in_t, out_t = classifier._parse_response(PROVIDER_OPENAI, raw)
    assert text == '{"NFL": "pure_sports"}'
    assert in_t == 50 and out_t == 10


def test_parse_response_openai_empty_choices():
    """OpenAI safety/refusal can produce an empty choices array. Don't index-
    error on that — return empty text and let _extract_json fail naturally."""
    raw = json.dumps({"choices": [], "usage": {}})
    text, _, _ = classifier._parse_response(PROVIDER_OPENAI, raw)
    assert text == ""


def test_parse_response_gemini():
    raw = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": '{"NFL": "pure_sports"}'}]},
        }],
        "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 12},
    })
    text, in_t, out_t = classifier._parse_response(PROVIDER_GEMINI, raw)
    assert text == '{"NFL": "pure_sports"}'
    assert in_t == 80 and out_t == 12


def test_parse_response_gemini_no_candidates():
    """Gemini returns no candidates when the prompt is filtered. Don't index-
    error — return empty text."""
    raw = json.dumps({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    text, _, _ = classifier._parse_response(PROVIDER_GEMINI, raw)
    assert text == ""


def test_parse_response_missing_usage_returns_none_tokens():
    """Token counts are best-effort. A response with no usage block yields
    (None, None) and the log line shows in=None out=None — not a crash."""
    raw = json.dumps({"content": [{"type": "text", "text": "{}"}]})
    text, in_t, out_t = classifier._parse_response(PROVIDER_ANTHROPIC, raw)
    assert text == "{}"
    assert in_t is None and out_t is None


# ----- _post_llm end-to-end with mocked urlopen -----

class _FakeResponse:
    """Mimics the context-manager-yielding object urlopen returns."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _stub_urlopen_with(body: dict):
    """Returns a mock urlopen-callable that yields the given body for any
    request. Captures the Request it was called with so tests can assert
    request shape."""
    captured = {}

    def stub(req, timeout=None):
        captured["request"] = req
        captured["timeout"] = timeout
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    return stub, captured


def test_post_llm_anthropic_round_trip():
    stub, captured = _stub_urlopen_with({
        "content": [{"type": "text", "text": '{"NFL": "pure_sports"}'}],
        "usage": {"input_tokens": 5, "output_tokens": 1},
    })
    with patch.object(classifier.urllib.request, "urlopen", stub):
        out = classifier._post_llm("sk-ant", "claude-haiku-4-5", "sys", "user")
    assert out == {"NFL": "pure_sports"}
    assert captured["request"].full_url == "https://api.anthropic.com/v1/messages"


def test_post_llm_openai_round_trip():
    stub, captured = _stub_urlopen_with({
        "choices": [{"message": {"content": '{"NBA": "pure_sports"}'}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    })
    with patch.object(classifier.urllib.request, "urlopen", stub):
        out = classifier._post_llm("sk-oai", "gpt-4o-mini", "sys", "user")
    assert out == {"NBA": "pure_sports"}
    assert captured["request"].full_url == "https://api.openai.com/v1/chat/completions"


def test_post_llm_gemini_round_trip():
    stub, captured = _stub_urlopen_with({
        "candidates": [{"content": {"parts": [{"text": '{"MLB": "pure_sports"}'}]}}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
    })
    with patch.object(classifier.urllib.request, "urlopen", stub):
        out = classifier._post_llm("gem-key", "gemini-2.0-flash", "sys", "user")
    assert out == {"MLB": "pure_sports"}
    assert "generativelanguage.googleapis.com" in captured["request"].full_url


def test_post_llm_returns_none_on_network_error():
    """Any urlopen failure must yield None so the caller fail-closes to
    not_sports for every group in the batch. Re-raising would crash the
    whole classify run for one transient connection blip."""
    def boom(req, timeout=None):
        raise OSError("network down")

    with patch.object(classifier.urllib.request, "urlopen", boom):
        out = classifier._post_llm("sk", "claude-haiku-4-5", "sys", "user")
    assert out is None


def test_post_llm_returns_none_on_parse_error():
    """A malformed response (e.g. HTML error page from a misconfigured proxy)
    must yield None, not raise."""
    def stub(req, timeout=None):
        return _FakeResponse(b"<html>500 Internal Server Error</html>")

    with patch.object(classifier.urllib.request, "urlopen", stub):
        out = classifier._post_llm("sk", "claude-haiku-4-5", "sys", "user")
    assert out is None


def test_post_llm_log_format_is_consistent_across_providers(caplog):
    """The 'in=X out=Y' log shape must be identical across providers so
    grep/dashboard tooling doesn't need to special-case each one. Provider
    name appears as the leading token."""
    import logging

    cases = [
        ("claude-haiku-4-5", PROVIDER_ANTHROPIC, {
            "content": [{"type": "text", "text": "{}"}],
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }),
        ("gpt-4o-mini", PROVIDER_OPENAI, {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }),
        ("gemini-2.0-flash", PROVIDER_GEMINI, {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
        }),
    ]
    for model, provider, body in cases:
        caplog.clear()
        stub, _ = _stub_urlopen_with(body)
        with patch.object(classifier.urllib.request, "urlopen", stub):
            with caplog.at_level(logging.INFO):
                classifier._post_llm("k", model, "sys", "user")
        # Expect "[sports_filter] <provider> call <elapsed>s in=5 out=1"
        msgs = [r.message for r in caplog.records]
        assert any(provider in m and "in=5" in m and "out=1" in m for m in msgs), (
            f"missing/incorrect log line for {provider}: {msgs}"
        )


# ----- classify_groups_with_llm integrates with new dispatcher -----

def test_classify_groups_with_llm_dispatches_via_post_llm():
    """The high-level classify_groups_with_llm should not call _post_claude
    (which no longer exists). Mock _post_llm and verify it's the dispatch
    target."""
    assert not hasattr(classifier, "_post_claude"), (
        "_post_claude was renamed to _post_llm in v0.9.0; remove the legacy "
        "name so it can't be called by stale code paths."
    )
    with patch.object(classifier, "_post_llm",
                      return_value={"Sports | Peacock TV": "mixed"}) as mock_post:
        out = classifier.classify_groups_with_llm(
            api_key="k", model="claude-haiku-4-5",
            groups_with_samples=[("Sports | Peacock TV", ["a", "b"])],
        )
        mock_post.assert_called_once()
    assert out == {"Sports | Peacock TV": "mixed"}
