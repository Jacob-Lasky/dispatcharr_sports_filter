"""
Two-stage classifier for IPTV M3U groups, plus per-stream classifier for mixed groups.

Group-level (3-bucket):
  Stage 1 (regex): match decisive league/network names -> pure_sports / not_sports.
                   Generic "sport"/"sports" word match removed deliberately so that
                   bouquet groups like "Sports | Peacock" defer to the LLM (which
                   can identify them as 'mixed' from sample channel names).
  Stage 2 (LLM):   batched call returning pure_sports / mixed / not_sports.

Stream-level (binary, only run for groups marked 'mixed'):
  LLM call:        for each (stream_name, group_context, ...), return sports / not_sports.

Cache values persisted by the plugin:
  - cache.json:        {group_name: 'pure_sports'|'mixed'|'not_sports'}
  - stream_cache.json: {stream_name: 'sports'|'not_sports'}

Note: legacy v1 cache values 'sports' (binary) get dropped on load by the plugin so
they re-flow through this ternary classifier.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

from .constants import (
    GROUP_VERDICTS,
    LOGGER_NAME,
    MODEL_PREFIX_PROVIDER,
    PROVIDER_ANTHROPIC,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    VERDICT_MIXED,
    VERDICT_NOT_SPORTS,
    VERDICT_PURE_SPORTS,
    VERDICT_SPORTS,
)

logger = logging.getLogger(f"{LOGGER_NAME}.classifier")

# ALLOW_RE matches ONLY decisive sport-league / sport-network keywords.
# We removed bare 'sport'/'sports' so that ambiguous bouquet names defer to the LLM,
# which can decide between pure_sports and mixed using sample channel content.
# Note on \b boundary anchoring:
# Each alternation is wrapped in \b...\b, which forces a word-boundary at both
# ends. That means a stem like 'documentar' will NOT match 'documentary' or
# 'documentaries' (the boundary fails between 'r' and 'y'/'i'). The same trap
# kills any pluralization for 'sport' tokens. So every term that needs a
# plural-y/plural-s/plural-ies suffix must spell it explicitly with a `?`
# group. Earlier versions of this file shipped 'documentar' and 'religi' as
# silently-dead tokens.
ALLOW_RE = re.compile(
    # Most tokens use the standard \b...\b sandwich. The trailing anchor on
    # 'sec\+' must be (?!\w) instead of \b — a literal '+' is not a word
    # char, so \b after it fails at end-of-string or before whitespace,
    # silently killing the match. Same trap that hit 'documentar' / 'religi'
    # at the leading end. See compile_user_terms() for the same fix in the
    # user-supplied-terms builder.
    r"\b("
    r"nfl|nba|mlb|nhl|nascar|mls|epl|efl|laliga|bundesliga|seriea|"
    r"ucl|uefa|fifa|conmebol|copa|"
    r"ufc|wwe|aew|boxing|mma|"
    r"f1|formula\s*1|motogp|indycar|wrc|"
    r"tennis|atp|wta|"
    r"golf|pga|liv\s*golf|"
    r"cricket|ipl|"
    r"rugby|nrl|afl|"
    r"olympic|"
    r"espn|fox\s*sports?|nbcsn|tnt\s*sports?|sky\s*sports?|bein|dazn|willow|"
    r"flosports?|"
    r"horse\s*rac|darts|snooker|"
    r"acc\s*extra|big\s*ten|big12|pac-?12"
    r")\b"
    r"|\bsec\+(?!\w)",
    re.IGNORECASE,
)

DENY_RE = re.compile(
    r"\b("
    r"vod|movies?|cinema|hbo\s*movies?|"
    r"series|tv\s*shows?|sitcoms?|drama|"
    r"24/?7|"
    r"kids|cartoons?|disney|nickelodeon|"
    r"news|cnn|fox\s*news|msnbc|bbc\s*news|"
    r"documentar(y|ies)|history\s*channel|nat\s*geo|"
    r"music|mtv|vh1|radio|sirius|"
    r"porn|xxx|adult|nsfw|playboy|"
    r"religio(us|n)|gospel|cooking|home\s*shop|qvc|hsn|"
    r"weather|game\s*show|reality"
    r")\b",
    re.IGNORECASE,
)


def compile_user_terms(extra_terms: str) -> Optional[re.Pattern]:
    """Compile a comma- or newline-separated list of user-supplied terms into
    a case-insensitive regex anchored to word-boundary-equivalents on both
    sides. Each term is regex-ESCAPED so a non-power-user can list plain
    words ('flosports', 'horse racing') without learning regex syntax.

    Returns None when the input has no usable terms — callers treat None as
    "no extension"; do not coerce to an empty pattern that would match
    everything.

    Trailing anchor uses (?!\\w) instead of \\b so a term ending in a
    non-word char (e.g. 'sec+') still matches at end-of-string. Plain \\b
    requires a word/non-word transition, which silently fails when the term
    already ends in punctuation.
    """
    if not extra_terms:
        return None
    terms = [t.strip() for t in re.split(r"[,\n]", extra_terms) if t.strip()]
    if not terms:
        return None
    alt = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"(?<!\w)({alt})(?!\w)", re.IGNORECASE)


def regex_classify(
    name: str,
    *,
    allow_re: re.Pattern = ALLOW_RE,
    deny_re: re.Pattern = DENY_RE,
    allow_extra_re: Optional[re.Pattern] = None,
    deny_extra_re: Optional[re.Pattern] = None,
) -> Optional[str]:
    """Return pure_sports / not_sports if regex is decisive, else None.

    User-supplied extension regexes (from settings) are OR'd with the built-in
    base patterns. A name matched ONLY by the user's deny list with no allow
    hit becomes not_sports — this is how a public user demotes 'flosports'
    or 'darts' without forking the plugin.
    """
    has_allow = bool(allow_re.search(name)) or bool(allow_extra_re and allow_extra_re.search(name))
    has_deny = bool(deny_re.search(name)) or bool(deny_extra_re and deny_extra_re.search(name))
    if has_deny and not has_allow:
        return VERDICT_NOT_SPORTS
    if has_allow:
        if has_deny:
            return None  # ambiguous, defer
        return VERDICT_PURE_SPORTS
    return None


def provider_for_model(model: str) -> str:
    """Infer provider from the model ID prefix. Falls back to Anthropic for
    historical compatibility — the plugin shipped Anthropic-only through 0.8.x,
    so an unrecognized prefix is more likely a Claude alias the prefix table
    doesn't know about than a wholly new provider.
    """
    m = (model or "").lower().strip()
    for prefix, provider in MODEL_PREFIX_PROVIDER:
        if m.startswith(prefix):
            return provider
    return PROVIDER_ANTHROPIC


def _build_request(provider: str, api_key: str, model: str, system: str, user: str) -> urllib.request.Request:
    """Build the provider-specific HTTP request. Each branch below handles the
    three things that vary across providers: URL, auth header, and body shape
    (system-prompt placement in particular). Stdlib urllib only — keeping the
    plugin pip-install-free is a deliberate constraint.
    """
    if provider == PROVIDER_ANTHROPIC:
        body = json.dumps({
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")
        return urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
    if provider == PROVIDER_OPENAI:
        body = json.dumps({
            "model": model,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode("utf-8")
        return urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
    if provider == PROVIDER_GEMINI:
        # Gemini auth is via query string ?key=, NOT a header. Path includes
        # the model ID. systemInstruction is a top-level peer of contents,
        # not a message role.
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
            },
        }).encode("utf-8")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model, safe='')}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
        )
        return urllib.request.Request(
            url,
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
    raise ValueError(f"unknown provider: {provider!r}")


def _parse_response(provider: str, raw: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Decode a provider response into (text, input_tokens, output_tokens).
    text is the raw model-output string (still needs _extract_json to find the
    JSON object); token counts are best-effort — a provider that omits usage
    metadata yields (None, None) and the log line just shows in=None out=None.
    """
    data = json.loads(raw)
    if provider == PROVIDER_ANTHROPIC:
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        usage = data.get("usage", {})
        return text, usage.get("input_tokens"), usage.get("output_tokens")
    if provider == PROVIDER_OPENAI:
        choices = data.get("choices") or []
        text = (choices[0].get("message", {}) or {}).get("content", "") if choices else ""
        usage = data.get("usage", {})
        return text, usage.get("prompt_tokens"), usage.get("completion_tokens")
    if provider == PROVIDER_GEMINI:
        cands = data.get("candidates") or []
        if cands:
            parts = (cands[0].get("content", {}) or {}).get("parts", []) or []
            text = "".join(p.get("text", "") for p in parts)
        else:
            text = ""
        usage = data.get("usageMetadata", {})
        return text, usage.get("promptTokenCount"), usage.get("candidatesTokenCount")
    raise ValueError(f"unknown provider: {provider!r}")


def _post_llm(api_key: str, model: str, system: str, user: str, timeout: int = 60) -> Optional[Dict[str, str]]:
    """Single LLM call dispatching to the right provider based on the model
    prefix. Returns parsed JSON dict (the model's verdict mapping) or None on
    any network/parse failure. Failure is logged but not raised — callers
    fail-closed to not_sports for any group the LLM didn't return.
    """
    provider = provider_for_model(model)
    req = _build_request(provider, api_key, model, system, user)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        logger.error("[sports_filter] %s API call failed: %s", provider, e)
        return None
    elapsed = time.time() - t0
    try:
        text, in_tokens, out_tokens = _parse_response(provider, raw)
        parsed = _extract_json(text)
        logger.info(
            "[sports_filter] %s call %.1fs in=%s out=%s",
            provider, elapsed, in_tokens, out_tokens,
        )
        return parsed
    except Exception as e:
        logger.error(
            "[sports_filter] Failed to parse %s response: %s ; raw=%s",
            provider, e, raw[:500],
        )
        return None


def _extract_json(text: str) -> Dict[str, str]:
    """Pull a JSON object out of model output, tolerant to fenced code blocks."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _normalize_group_verdict(v: object) -> str:
    s = str(v).lower().strip().replace("-", "_").replace(" ", "_")
    if s in (VERDICT_PURE_SPORTS, "puresports"):
        return VERDICT_PURE_SPORTS
    if s == VERDICT_MIXED:
        return VERDICT_MIXED
    return VERDICT_NOT_SPORTS


def _normalize_stream_verdict(v: object) -> str:
    s = str(v).lower().strip()
    return VERDICT_SPORTS if s == VERDICT_SPORTS else VERDICT_NOT_SPORTS


# ----- Group-level classification (ternary) -----

GROUP_SYSTEM_PROMPT = (
    "You classify IPTV channel groups into one of three categories:\n"
    "  - pure_sports: nearly every channel in the group is a sports channel "
    "(live sports events, sports networks, sports-themed). Examples: NFL, NBA, "
    "ESPN+, Sky Sports, F1, UFC, golf, tennis-only bouquets.\n"
    "  - mixed: the group is a STREAMING SERVICE BOUQUET or REGIONAL PACK that "
    "contains a mix of sports and non-sports channels. Examples: 'US | Peacock TV' "
    "(has NFL Channel + Premier League TV alongside news / shows / movies), "
    "'Sports | Paramount+', 'Sports | HBO Max US', 'Sports | Max'. Look at "
    "sample_channels: if you see news, kids, lifestyle, talk shows mixed with "
    "some sports -> 'mixed'.\n"
    "  - not_sports: movies, VOD, series, news, kids, music, religious, adult, "
    "regional general-entertainment bouquets (e.g. 'Colombia | TV'), "
    "international/news/lifestyle channels with NO sports content.\n"
    "Decision rule: if >90% of sample_channels look sports-related -> pure_sports. "
    "If 10-90% sports -> mixed. If <10% sports -> not_sports.\n"
    "Output ONLY a JSON object mapping each input group name to one of "
    "'pure_sports', 'mixed', or 'not_sports'. No prose, no markdown."
)


def _augment_prompt(base: str, extra_hints: str) -> str:
    """Append user-supplied hints to a system prompt, separated for legibility."""
    if not extra_hints or not extra_hints.strip():
        return base
    return base + "\n\nAdditional user instructions:\n" + extra_hints.strip()


def classify_groups_with_llm(
    api_key: str,
    model: str,
    groups_with_samples: List[Tuple[str, List[str]]],
    batch_size: int = 30,
    timeout: int = 60,
    extra_hints: str = "",
) -> Dict[str, str]:
    """Classify (group_name, [sample_streams]) -> {group_name: pure_sports/mixed/not_sports}."""
    out: Dict[str, str] = {}
    if not groups_with_samples:
        return out
    system_prompt = _augment_prompt(GROUP_SYSTEM_PROMPT, extra_hints)
    for i in range(0, len(groups_with_samples), batch_size):
        batch = groups_with_samples[i : i + batch_size]
        payload = [{"group": n, "sample_channels": [s for s in samples[:10] if s]} for n, samples in batch]
        user = "Classify each of these groups. Return JSON only.\n\n" + json.dumps(payload, ensure_ascii=False)
        parsed = _post_llm(api_key, model, system_prompt, user, timeout) or {}
        # Fail-closed: anything missing or unparseable defaults to not_sports
        for name, _ in batch:
            out[name] = _normalize_group_verdict(parsed.get(name, VERDICT_NOT_SPORTS))
    return out


def classify_all_groups(
    api_key: str,
    model: str,
    groups_with_samples: Iterable[Tuple[str, List[str]]],
    cache: Dict[str, str],
    *,
    allow_extra_re: Optional[re.Pattern] = None,
    deny_extra_re: Optional[re.Pattern] = None,
    extra_hints: str = "",
    enable_llm: bool = True,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Returns (results, new_only).
      - results:  {group_name: 'pure_sports'|'mixed'|'not_sports'} for ALL inputs
      - new_only: subset of results that were just classified this run

    enable_llm=False is the regex-only / public-user mode: ambiguous groups
    that the regex pre-filter cannot resolve fail-closed to not_sports
    instead of calling the LLM, even if an api_key is configured. The user
    recovers bouquets they care about via extra_allow_terms.
    """
    results: Dict[str, str] = {}
    new_only: Dict[str, str] = {}
    needs_llm: List[Tuple[str, List[str]]] = []

    for name, samples in groups_with_samples:
        if name in cache and cache[name] in GROUP_VERDICTS:
            results[name] = cache[name]
            continue
        verdict = regex_classify(
            name, allow_extra_re=allow_extra_re, deny_extra_re=deny_extra_re,
        )
        if verdict:
            results[name] = verdict
            new_only[name] = verdict
            continue
        needs_llm.append((name, samples))

    if needs_llm:
        can_llm = enable_llm and bool(api_key)
        if not can_llm:
            if not enable_llm:
                logger.info(
                    "[sports_filter] LLM disabled; %d ambiguous groups -> not_sports (use extra_allow_terms to recover)",
                    len(needs_llm),
                )
            else:
                logger.warning(
                    "[sports_filter] %d groups need LLM but no API key; defaulting to not_sports",
                    len(needs_llm),
                )
            for name, _ in needs_llm:
                results[name] = VERDICT_NOT_SPORTS
                new_only[name] = VERDICT_NOT_SPORTS
        else:
            llm_results = classify_groups_with_llm(
                api_key, model, needs_llm, extra_hints=extra_hints,
            )
            # Iterate the requested set, not the LLM's response keys, so an extra
            # hallucinated group name in the JSON can't sneak into the cache.
            for name, _ in needs_llm:
                verdict = llm_results.get(name, VERDICT_NOT_SPORTS)
                results[name] = verdict
                new_only[name] = verdict

    return results, new_only


# ----- Stream-level classification (binary, for mixed groups) -----

STREAM_SYSTEM_PROMPT = (
    "You classify individual IPTV streams as either 'sports' or 'not_sports'.\n"
    "Sports = live-sports, sports-network, league-dedicated, sports-talk, or "
    "sports-themed channels (e.g. 'NFL Channel', 'Premier League TV', 'GolfPass', "
    "'NBC Sports NOW', 'TEAM USA TV', 'F1 TV').\n"
    "Not sports = news, kids, movies, lifestyle, music, religious, talk shows, "
    "documentaries, reality, weather, shopping, regional/local TV, automotive "
    "lifestyle (e.g. 'Top Gear' is car entertainment, not motorsport coverage), "
    "general entertainment.\n"
    "Each input has the stream name and (optionally) the group context. Use the "
    "stream name as the primary signal; group context only helps disambiguate.\n"
    "Output ONLY a JSON object mapping each input stream name to 'sports' or "
    "'not_sports'. No prose, no markdown."
)


def classify_streams_with_llm(
    api_key: str,
    model: str,
    streams_with_context: List[Tuple[str, str]],
    batch_size: int = 50,
    timeout: int = 60,
    extra_hints: str = "",
) -> Dict[str, str]:
    """Classify [(stream_name, group_context)] -> {stream_name: sports/not_sports}."""
    out: Dict[str, str] = {}
    if not streams_with_context:
        return out
    system_prompt = _augment_prompt(STREAM_SYSTEM_PROMPT, extra_hints)
    for i in range(0, len(streams_with_context), batch_size):
        batch = streams_with_context[i : i + batch_size]
        payload = [{"stream": n, "in_group": g} for n, g in batch]
        user = "Classify each stream. Return JSON only.\n\n" + json.dumps(payload, ensure_ascii=False)
        parsed = _post_llm(api_key, model, system_prompt, user, timeout) or {}
        for name, _ in batch:
            out[name] = _normalize_stream_verdict(parsed.get(name, VERDICT_NOT_SPORTS))
    return out
