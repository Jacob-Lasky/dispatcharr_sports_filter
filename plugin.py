"""
Dispatcharr Sports-Only Group Filter — three-bucket group classification +
per-stream filtering for mixed bouquets + auto-rename of source groups via
the built-in group_override mechanism. Version is the source of truth in
plugin.json and the Plugin class self.version.

Pipeline:
  1) classify        -> read enabled groups, classify each as
                        pure_sports / mixed / not_sports (regex + LLM —
                        Anthropic / OpenAI / Google, picked by model prefix).
                        Writes cache.json. No DB writes.
  2) refine_mixed    -> for groups marked 'mixed', classify each stream within
                        the group as sports/not_sports. Writes stream_cache.json.
                        No DB writes.
  3) apply           -> writes ChannelGroupM3UAccount based on cache + stream_cache:
                          pure_sports -> auto_channel_sync=True, profile assigned,
                                         optional group_override to a clean target group
                          mixed       -> same as pure_sports + name_match_regex
                                         built from sports-classified streams
                          not_sports  -> auto_channel_sync=False
                                         (and enabled=False if also_unselect_not_sports)

Files:
  - cache.json:        {group_name: 'pure_sports'|'mixed'|'not_sports'}
  - stream_cache.json: {stream_name: 'sports'|'not_sports'}
  - anthropic_api_key / openai_api_key / gemini_api_key:
                       per-provider API key on disk (chmod 600). The plugin
                       picks the file matching the chosen model's provider
                       (claude-* -> anthropic, gpt-* -> openai, gemini-* -> gemini).

Legacy v1 cache values ('sports' / 'not_sports' binary) — 'sports' entries are
silently dropped on load so they re-flow through the ternary classifier; 'not_sports'
entries are still valid and kept.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_MODEL,
    DEFAULT_PROFILE_ID,
    DEFAULT_SAMPLES_PER_GROUP,
    DEFAULT_SCHEDULE_STRING,
    DEFAULT_SCHEDULE_TIMES,
    DEFAULT_STRIP_PREFIXES,
    GROUP_VERDICTS,
    LOGGER_NAME,
    PROVIDER_KEY_FILE,
    PROVIDER_SETTINGS_FIELD,
    PROVIDERS,
    SCHEDULER_LOCK_KEY,
    SCHEDULER_LOCK_TTL_S,
    STREAM_VERDICTS,
    VERDICT_MIXED,
    VERDICT_NOT_SPORTS,
    VERDICT_PURE_SPORTS,
    VERDICT_SPORTS,
)

PLUGIN_VERSION = "0.9.0"

logger = logging.getLogger(LOGGER_NAME)

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(PLUGIN_DIR, "cache.json")
STREAM_CACHE_PATH = os.path.join(PLUGIN_DIR, "stream_cache.json")


# ---------- File helpers ----------

def _api_key_path(provider: str) -> str:
    """Resolve the on-disk fallback path for a provider's API key. Wraps the
    PLUGIN_DIR + filename join so tests can monkeypatch PLUGIN_DIR or the
    PROVIDER_KEY_FILE map without each test re-deriving the path."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    return os.path.join(PLUGIN_DIR, PROVIDER_KEY_FILE[provider])


def _read_api_key(settings: Optional[Dict[str, Any]] = None, provider: str = "anthropic") -> str:
    """Resolve the API key for a given LLM provider. Settings field wins (typed
    in plugin UI, masked via input_type=password); falls back to
    <plugin_dir>/<provider>_api_key on disk (chmod 600) for users who'd rather
    not paste a key into the DB.

    The on-disk pattern existed before multi-provider support landed. We
    extend it symmetrically — `anthropic_api_key`, `openai_api_key`,
    `gemini_api_key` — so a user with multiple providers configured can
    switch models freely without re-entering keys in the UI.
    """
    field_name = PROVIDER_SETTINGS_FIELD.get(provider)
    if field_name is None:
        raise ValueError(f"unknown provider: {provider!r}")
    if settings:
        v = settings.get(field_name) or ""
        if isinstance(v, str) and v.strip():
            return v.strip()
    path = _api_key_path(provider)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("[sports_filter] No %s API key in settings nor at %s", provider, path)
        return ""


def _read_json(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error("[sports_filter] Read failed %s (%s); starting fresh", path, e)
        return {}


def _write_json(path: str, data: Dict[str, str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def _read_group_cache() -> Dict[str, str]:
    """Read group cache, dropping legacy v1 'sports' verdicts so they re-classify."""
    raw = _read_json(CACHE_PATH)
    return {k: v for k, v in raw.items() if v in GROUP_VERDICTS}


def _read_stream_cache() -> Dict[str, str]:
    raw = _read_json(STREAM_CACHE_PATH)
    return {k: v for k, v in raw.items() if v in STREAM_VERDICTS}


# ---------- Group rename helpers ----------

def _parse_strip_prefixes(raw: Optional[str]) -> List[str]:
    """Parse the comma- or newline-separated strip_prefixes setting into a
    list of literal prefix strings. None / empty falls back to the default
    AliceXC-style prefixes ('Sports |', 'Sports/'). Each prefix is matched
    case-insensitively at the start of the group name.
    """
    if raw is None or not str(raw).strip():
        return list(DEFAULT_STRIP_PREFIXES)
    parts = [p.strip() for p in re.split(r"[,\n]", str(raw)) if p.strip()]
    return parts or list(DEFAULT_STRIP_PREFIXES)


def _clean_target_name(
    group_name: str,
    is_mixed: bool = False,
    *,
    strip_prefixes: Optional[List[str]] = None,
    add_sports_suffix: bool = True,
) -> str:
    """
    Generate the override-target group name. Used to give auto-created channels a
    cleaner home group than the M3U source name.

    Configuration knobs:
      strip_prefixes      list of prefix strings to drop from the head of the
                          group name. Default: 'Sports |' and 'Sports/'.
                          Other M3U providers use 'SP-', 'SPRT|', etc.
      add_sports_suffix   when True (default), mixed-group target names get a
                          ' Sports' suffix appended if not already present.

    Pure_sports rules (is_mixed=False):
      - 'Sports | NFL'        -> 'NFL'              (drop 'Sports |' prefix)
      - 'Sports/PPV'          -> 'PPV'              (drop 'Sports/' prefix)
      - 'Brazil | Sports'     -> 'Brazil Sports'    (collapse pipe at end)
      - 'Sports | NBA (2)'    -> 'NBA'              (drop "(N)" duplicate-feed suffix)
      - 'Sports | NBA' AND 'Sports | NBA (2)' both consolidate to target 'NBA'
      - 'UK | Sky Sports'     -> 'UK | Sky Sports'  (region info preserved, no rule matches)

    Mixed rules (is_mixed=True, add_sports_suffix=True): same as above, plus a
    final ' Sports' suffix if not already present. The suffix communicates
    "this is the filtered-sports subset of a bouquet that has non-sports
    content too." Pipes collapsed to spaces.
      - 'Sports | HBO Max US' -> 'HBO Max US Sports'
      - 'US | Peacock TV'     -> 'US Peacock TV Sports'
      - 'IE | Sky'            -> 'IE Sky Sports'
      - 'CAR | Sports'        -> 'CAR Sports'       (already ends in Sports)
      - 'Sports | Stan (2)'   -> 'Stan Sports'
    """
    name = group_name.strip()
    prefixes = strip_prefixes if strip_prefixes is not None else list(DEFAULT_STRIP_PREFIXES)

    # Step 1: strip a configured prefix off the head, case-insensitive. Each
    # prefix is escaped so users without regex literacy can list 'Sports |',
    # 'SP-', etc. as plain text.
    for prefix in prefixes:
        if not prefix:
            continue
        rx = re.compile(rf"^{re.escape(prefix)}\s*(.+)$", re.IGNORECASE)
        m = rx.match(name)
        if m:
            name = m.group(1).strip()
            break

    # Step 2: collapse "X | Sports" suffix to "X Sports"
    m = re.match(r"^(.+?)\s*\|\s*Sports$", name)
    if m:
        name = f"{m.group(1).strip()} Sports"
    # Step 3a: collapse internal whitespace, normalize trailing space-plus.
    # 'Big Ten +' -> 'Big Ten+' so it consolidates with 'Big Ten+'. Targeted to
    # trailing only so middle-of-string separators like 'Sky Sports + TNT Sports'
    # are not mangled.
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\s+\+\s*$", "+", name)
    # Step 3b: drop trailing "(N)" duplicate-feed marker
    name = re.sub(r"\s*\(\d+\)\s*$", "", name).strip()
    # Step 4 (mixed only): collapse remaining pipes to spaces, ensure Sports suffix
    if is_mixed:
        name = re.sub(r"\s*\|\s*", " ", name).strip()
        if add_sports_suffix and not re.search(r"\bSports\b", name, re.IGNORECASE):
            name = f"{name} Sports"
    return name


def _get_or_create_target_group(name: str, dry_run: bool):
    """Return ChannelGroup with given name, creating if needed (unless dry_run)."""
    from apps.channels.models import ChannelGroup
    g = ChannelGroup.objects.filter(name=name).first()
    if g:
        return g
    if dry_run:
        return None  # signal: would create
    g = ChannelGroup.objects.create(name=name)
    logger.info("[sports_filter] Created target ChannelGroup %r (id=%d)", name, g.id)
    return g


def _build_match_regex(stream_names: List[str]) -> str:
    """Build a case-insensitive iregex matching exactly any of these stream names.

    Returns "" when nothing usable is left after filtering blanks. Important:
    `^()$` is NOT a no-op — it would match the empty string and silently drop
    every real stream name from the filter, which is exactly the wrong fail-mode.
    """
    escaped = [re.escape(n) for n in stream_names or [] if n]
    if not escaped:
        return ""
    return r"^(" + "|".join(escaped) + r")$"


# ---------- Stream gathering ----------

def _gather_groups(account_id: Optional[int], samples_per_group: int):
    """Return [(group_name, [channel_sample_names])] for enabled CGM3UA rows.

    account_id=None scopes across every enabled M3U account. The same group
    name appearing in multiple providers is emitted once (the cache is keyed
    by group name, so we'd just classify it twice otherwise) with a random
    sample of streams drawn from whichever providers carry it.
    """
    from apps.channels.models import ChannelGroupM3UAccount, Stream
    rels = ChannelGroupM3UAccount.objects.filter(enabled=True).select_related("channel_group")
    if account_id is not None:
        rels = rels.filter(m3u_account_id=account_id)

    seen_names = set()
    out = []
    for r in rels:
        name = r.channel_group.name
        if name in seen_names:
            continue
        seen_names.add(name)
        stream_qs = Stream.objects.filter(channel_group=r.channel_group)
        if account_id is not None:
            stream_qs = stream_qs.filter(m3u_account_id=account_id)
        names = list(stream_qs.values_list("name", flat=True)[: samples_per_group * 3])
        if len(names) > samples_per_group:
            names = random.sample(names, samples_per_group)
        out.append((name, names))
    return out


def _gather_streams_for_group(account_id: Optional[int], group_name: str) -> List[str]:
    """Return all stream names in groups matching `group_name`.

    Uses channel_group__in= so a name shared across multiple ChannelGroup rows
    (rare but possible when several M3U accounts each create their own
    ChannelGroup row with the same display name) collects streams from all of
    them. The mixed-group name_match_regex needs the union — a stream that
    only exists in provider A still needs to match in provider A's CGM3UA
    row's regex, but a stream that only exists in provider B must not be
    excluded from B's regex either.
    """
    from apps.channels.models import ChannelGroup, Stream
    groups = list(ChannelGroup.objects.filter(name=group_name))
    if not groups:
        return []
    qs = Stream.objects.filter(channel_group__in=groups)
    if account_id is not None:
        qs = qs.filter(m3u_account_id=account_id)
    return list(qs.values_list("name", flat=True).distinct())


# ---------- Settings helpers ----------

def _apply_debug_logging(settings: Dict[str, Any]) -> bool:
    """Bump the plugin logger to DEBUG when the setting is on. Returns the
    debug flag so callers that need a local boolean for ad-hoc gating
    (`if debug or X: logger.info(...)`) don't have to re-read the setting.
    Single source of truth: read once here.
    """
    debug = bool(settings.get("debug_mode", False))
    if debug:
        logging.getLogger(LOGGER_NAME).setLevel(logging.DEBUG)
    return debug


def _resolve_account_id(settings: Dict[str, Any]) -> Optional[int]:
    """Resolve the configured M3U account scope.

    Returns None for "operate across all enabled M3U accounts" (the default
    for fresh installs — see _build_account_field for how this surfaces in
    the UI as the 'All M3U accounts' dropdown option). Returns a positive
    int when the user has explicitly scoped to a single provider.

    The empty string, None, 0, and '0' all map to None — Dispatcharr's
    select fields can produce any of these depending on the storage path.
    """
    raw = settings.get("m3u_account_id", "")
    if raw is None or raw == "" or raw == 0 or raw == "0":
        return None
    return int(raw)


def _resolve_profile_id(settings: Dict[str, Any]) -> int:
    return int(settings.get("channel_profile_id", DEFAULT_PROFILE_ID))


def _resolve_model(settings: Dict[str, Any]) -> str:
    return settings.get("model", DEFAULT_MODEL)


# ---------- Action: classify (group level) ----------

def _action_classify(settings: Dict[str, Any]) -> Dict[str, Any]:
    from . import classifier
    debug = _apply_debug_logging(settings)
    account_id = _resolve_account_id(settings)
    model = _resolve_model(settings)
    samples_per_group = int(settings.get("samples_per_group", DEFAULT_SAMPLES_PER_GROUP))

    # Provider is INFERRED from the model ID prefix; the key lookup picks the
    # matching settings field + on-disk fallback. This keeps "switch model" =
    # "switch provider" without a separate provider field that could drift.
    provider = classifier.provider_for_model(model)
    api_key = _read_api_key(settings, provider=provider)
    allow_extra_re = classifier.compile_user_terms(settings.get("extra_allow_terms", ""))
    deny_extra_re = classifier.compile_user_terms(settings.get("extra_deny_terms", ""))
    extra_hints = str(settings.get("extra_classification_hints", "") or "")
    enable_llm = bool(settings.get("enable_llm", False))
    cache = _read_group_cache()
    groups = _gather_groups(account_id, samples_per_group)
    mode = "regex+LLM" if enable_llm else "regex-only"
    logger.info("[sports_filter] Classifying %d groups in %s mode (cache has %d valid entries)", len(groups), mode, len(cache))

    results, new_only = classifier.classify_all_groups(
        api_key, model, groups, cache,
        allow_extra_re=allow_extra_re,
        deny_extra_re=deny_extra_re,
        extra_hints=extra_hints,
        enable_llm=enable_llm,
    )
    cache.update(new_only)
    _write_json(CACHE_PATH, cache)

    pure = sorted(g for g, v in results.items() if v == VERDICT_PURE_SPORTS)
    mixed = sorted(g for g, v in results.items() if v == VERDICT_MIXED)
    not_sports = sorted(g for g, v in results.items() if v == VERDICT_NOT_SPORTS)
    msg = (
        f"Classified {len(results)}: pure_sports={len(pure)}, "
        f"mixed={len(mixed)}, not_sports={len(not_sports)}. "
        f"({len(new_only)} newly classified.)"
    )
    logger.info("[sports_filter] %s", msg)
    if debug or mixed:
        logger.info("[sports_filter] mixed groups: %s", mixed)
    return {
        "status": "ok",
        "message": msg,
        "pure_sports_count": len(pure),
        "mixed_count": len(mixed),
        "not_sports_count": len(not_sports),
        "newly_classified": len(new_only),
        "sample_pure_sports": pure[:10],
        "mixed": mixed,
        "sample_not_sports": not_sports[:10],
    }


# ---------- Action: refine_mixed (stream level) ----------

def _action_refine_mixed(settings: Dict[str, Any]) -> Dict[str, Any]:
    from . import classifier
    _apply_debug_logging(settings)
    account_id = _resolve_account_id(settings)
    model = _resolve_model(settings)

    cache = _read_group_cache()
    mixed_groups = sorted(g for g, v in cache.items() if v == VERDICT_MIXED)
    if not mixed_groups:
        return {"status": "ok", "message": "No groups marked 'mixed' in cache. Run classify first."}

    enable_llm = bool(settings.get("enable_llm", False))
    if not enable_llm:
        # Regex-only mode: per-stream classification needs the LLM by design.
        # Status is "ok" not "error" because this is intentional, not a misconfig.
        msg = (
            f"LLM disabled, skipping per-stream refinement of {len(mixed_groups)} mixed group(s). "
            "Enable the 'Use LLM' setting to classify mixed bouquets per stream."
        )
        logger.info("[sports_filter] %s", msg)
        return {"status": "ok", "message": msg, "skipped": True, "mixed_groups": len(mixed_groups)}

    provider = classifier.provider_for_model(model)
    api_key = _read_api_key(settings, provider=provider)
    if not api_key:
        return {
            "status": "error",
            "message": f"No {provider} API key on disk; can't classify streams.",
        }

    stream_cache = _read_stream_cache()
    needs_llm = []  # [(stream_name, group_context)]
    seen_in_run = set()
    per_group_streams: Dict[str, List[str]] = {}

    for group_name in mixed_groups:
        streams = _gather_streams_for_group(account_id, group_name)
        per_group_streams[group_name] = streams
        for s in streams:
            if not s or s in seen_in_run:
                continue
            seen_in_run.add(s)
            if s in stream_cache:
                continue
            needs_llm.append((s, group_name))

    logger.info(
        "[sports_filter] refine_mixed: %d mixed groups, %d unique streams, %d need LLM (%d already cached)",
        len(mixed_groups), len(seen_in_run), len(needs_llm), len(seen_in_run) - len(needs_llm),
    )

    if needs_llm:
        extra_hints = str(settings.get("extra_classification_hints", "") or "")
        new_results = classifier.classify_streams_with_llm(
            api_key, model, needs_llm, extra_hints=extra_hints,
        )
        stream_cache.update(new_results)
        _write_json(STREAM_CACHE_PATH, stream_cache)

    # Build per-group summary
    summary = {}
    for group_name, streams in per_group_streams.items():
        sport_streams = [s for s in streams if stream_cache.get(s) == VERDICT_SPORTS]
        summary[group_name] = {"total": len(streams), "sports": len(sport_streams)}

    # Auto-reclassify mixed groups based on per-stream findings:
    #   0 sports streams      -> demote to not_sports (unselected on next apply)
    #   100% sports streams   -> promote to pure_sports (no regex filter needed)
    #   anything in between   -> stay mixed (regex filter applied on apply)
    # This makes refine_mixed a verification pass: the cheap group-level LLM is
    # rough; the exhaustive stream-level pass produces the final verdict.
    cache = _read_group_cache()
    demoted: List[str] = []
    promoted: List[str] = []
    for group_name, s in summary.items():
        if cache.get(group_name) != VERDICT_MIXED:
            continue
        sports, total = s["sports"], s["total"]
        if total == 0:
            continue
        if sports == 0:
            cache[group_name] = VERDICT_NOT_SPORTS
            demoted.append(group_name)
            summary[group_name]["reclassified_to"] = VERDICT_NOT_SPORTS
        elif sports == total:
            cache[group_name] = VERDICT_PURE_SPORTS
            promoted.append(group_name)
            summary[group_name]["reclassified_to"] = VERDICT_PURE_SPORTS
    if demoted or promoted:
        _write_json(CACHE_PATH, cache)
        if demoted:
            logger.info("[sports_filter] Auto-demoted to not_sports (%d): %s", len(demoted), demoted)
        if promoted:
            logger.info("[sports_filter] Auto-promoted to pure_sports (%d): %s", len(promoted), promoted)

    msg = (
        f"Refined {len(mixed_groups)} mixed groups. Stream cache: {len(stream_cache)} entries. "
        f"Reclassified -> not_sports: {len(demoted)}, -> pure_sports: {len(promoted)}, "
        f"stayed mixed: {len(mixed_groups) - len(demoted) - len(promoted)}."
    )
    logger.info("[sports_filter] %s", msg)
    for gname, s in summary.items():
        rc = s.get("reclassified_to")
        suffix = f" [-> {rc}]" if rc else ""
        logger.info("[sports_filter] %s: %d/%d streams classified as sports%s", gname, s["sports"], s["total"], suffix)
    return {
        "status": "ok",
        "message": msg,
        "mixed_groups": len(mixed_groups),
        "streams_classified_this_run": len(needs_llm),
        "stream_cache_size": len(stream_cache),
        "demoted_to_not_sports": demoted,
        "promoted_to_pure_sports": promoted,
        "per_group": summary,
    }


# ---------- Action: apply ----------

def _action_apply(settings: Dict[str, Any]) -> Dict[str, Any]:
    from apps.channels.models import ChannelGroupM3UAccount

    debug = _apply_debug_logging(settings)
    account_id = _resolve_account_id(settings)
    profile_id = _resolve_profile_id(settings)
    dry_run = bool(settings.get("dry_run", True))
    also_unselect = bool(settings.get("also_unselect_not_sports", False))
    apply_rename = bool(settings.get("apply_group_rename", True))
    strip_prefixes = _parse_strip_prefixes(settings.get("group_rename_strip_prefixes"))
    add_sports_suffix = bool(settings.get("mixed_groups_sports_suffix", True))

    cache = _read_group_cache()
    if not cache:
        return {"status": "error", "message": "Cache is empty. Run 'Classify groups' first."}

    stream_cache = _read_stream_cache()

    rels = (
        ChannelGroupM3UAccount.objects
        .filter(enabled=True)
        .select_related("channel_group")
    )
    if account_id is not None:
        rels = rels.filter(m3u_account_id=account_id)

    pure_applied: List[str] = []
    mixed_applied: List[Dict[str, Any]] = []
    sync_off: List[str] = []
    unselected: List[str] = []
    skipped_unknown: List[str] = []
    no_change_on, no_change_off = 0, 0

    for r in rels:
        name = r.channel_group.name
        verdict = cache.get(name)
        if verdict is None:
            skipped_unknown.append(name)
            continue

        if verdict in (VERDICT_PURE_SPORTS, VERDICT_MIXED):
            target_props = dict(r.custom_properties or {})
            target_props["channel_numbering_mode"] = "next_available"
            target_props["channel_profile_ids"] = [profile_id]

            # Group rename via group_override.
            # Dry-run case: the target may not exist yet, so we surface a
            # "<would-create:NAME>" marker in the output so the user can preview.
            # _get_or_create_target_group only returns None when dry_run=True and
            # the group is missing, so the placeholder branch is unreachable on a
            # real apply.
            if apply_rename:
                clean = _clean_target_name(
                    name,
                    is_mixed=(verdict == VERDICT_MIXED),
                    strip_prefixes=strip_prefixes,
                    add_sports_suffix=add_sports_suffix,
                )
                if clean != name:
                    target_group = _get_or_create_target_group(clean, dry_run)
                    if target_group:
                        target_props["group_override"] = target_group.id
                    elif dry_run:
                        target_props["group_override"] = f"<would-create:{clean}>"

            # Per-stream filter for mixed groups
            if verdict == VERDICT_MIXED:
                streams = _gather_streams_for_group(account_id, name)
                sport_streams = [s for s in streams if stream_cache.get(s) == VERDICT_SPORTS]
                if sport_streams:
                    target_props["name_match_regex"] = _build_match_regex(sport_streams)
                    mixed_applied.append({
                        "group": name,
                        "sports_streams": len(sport_streams),
                        "total_streams": len(streams),
                    })
                else:
                    # No stream classification available -> skip name_match (would create everything)
                    target_props.pop("name_match_regex", None)
                    mixed_applied.append({
                        "group": name,
                        "sports_streams": 0,
                        "total_streams": len(streams),
                        "warning": "no stream classifications; run refine_mixed",
                    })
            else:
                # pure_sports: clear any leftover name_match_regex
                target_props.pop("name_match_regex", None)

            # Idempotency check: anything actually changing?
            current_props = r.custom_properties or {}
            props_changed = target_props != current_props
            sync_changed = not r.auto_channel_sync

            if not props_changed and not sync_changed:
                no_change_on += 1
                continue

            if verdict == VERDICT_PURE_SPORTS:
                pure_applied.append(name)
            # (mixed already added above)

            if not dry_run:
                r.auto_channel_sync = True
                r.custom_properties = target_props
                r.save(update_fields=["auto_channel_sync", "custom_properties"])
        else:
            # not_sports
            update_fields = []
            if r.auto_channel_sync:
                update_fields.append("auto_channel_sync")
            if also_unselect and r.enabled:
                update_fields.append("enabled")
            if not update_fields:
                no_change_off += 1
                continue
            if "enabled" in update_fields:
                unselected.append(name)
            else:
                sync_off.append(name)
            if not dry_run:
                r.auto_channel_sync = False
                if "enabled" in update_fields:
                    r.enabled = False
                r.save(update_fields=update_fields)

    prefix = "[DRY RUN] " if dry_run else ""
    parts = [
        f"pure_sports applied: {len(pure_applied)}",
        f"mixed applied: {len(mixed_applied)}",
        f"sync OFF (not_sports): {len(sync_off)}",
    ]
    if also_unselect:
        parts.append(f"unselected: {len(unselected)}")
    parts.append(f"no change on/off: {no_change_on}/{no_change_off}")
    parts.append(f"unclassified: {len(skipped_unknown)}")
    msg = f"{prefix}{'. '.join(parts)}."
    logger.info("[sports_filter] %s", msg)
    if debug or dry_run:
        logger.info("[sports_filter] mixed details: %s", mixed_applied[:20])
        if skipped_unknown:
            logger.info("[sports_filter] unclassified: %s", skipped_unknown[:50])

    return {
        "status": "ok",
        "message": msg,
        "dry_run": dry_run,
        "apply_group_rename": apply_rename,
        "also_unselect": also_unselect,
        "pure_sports_applied": pure_applied,
        "mixed_applied": mixed_applied,
        "sync_off": sync_off,
        "unselected": unselected,
        "skipped_unknown": skipped_unknown,
    }


# ---------- Action: auto_pipeline ----------

def _action_auto_pipeline(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the full pipeline end-to-end. Used by the daily scheduler and on demand:
      classify -> refine_mixed -> apply (real) -> cleanup_orphans (real)

    The only setting we override is dry_run -> False; otherwise an automated
    pipeline that respected the dry_run field default (True) would never write.
    All other behavior (rename, also_unselect, etc.) honors the user's saved
    settings so the daily run cannot be silently more destructive than the
    Apply button.
    """
    full_settings = dict(settings)
    full_settings["dry_run"] = False

    logger.info("[sports_filter] auto_pipeline START")
    stages: Dict[str, Any] = {}
    try:
        stages["classify"] = _action_classify(full_settings)
        stages["refine_mixed"] = _action_refine_mixed(full_settings)
        stages["apply"] = _action_apply(full_settings)
        stages["cleanup_orphans"] = _action_cleanup_orphans(full_settings)
        logger.info("[sports_filter] auto_pipeline END (success)")
        return {
            "status": "ok",
            "message": "auto_pipeline completed",
            "stages": {k: v.get("message") for k, v in stages.items()},
        }
    except Exception as e:
        logger.exception("[sports_filter] auto_pipeline failed")
        return {
            "status": "error",
            "message": f"auto_pipeline failed: {type(e).__name__}: {e}",
            "stages": {k: v.get("message") for k, v in stages.items()},
        }


# ---------- Action: cleanup_orphans ----------

def _action_cleanup_orphans(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Delete orphan ChannelGroup rows that:
      - Have NO streams (Stream.channel_group)
      - Have NO channels (Channel.channel_group)
      - Are NOT pointed to by any group_override in any ChannelGroupM3UAccount
      - Are NOT linked to any M3UAccount via ChannelGroupM3UAccount

    These are leftover target groups from earlier renames that the latest apply
    no longer references. Safe to delete.

    Honors dry_run.
    """
    from apps.channels.models import ChannelGroup, ChannelGroupM3UAccount, Channel, Stream

    dry_run = bool(settings.get("dry_run", True))

    # Build set of group ids actively referenced as group_override.
    # Pull only custom_properties (one column) instead of hydrating each row -
    # cuts work on installs with thousands of CGM3UA rows.
    override_ids = set()
    for cp in ChannelGroupM3UAccount.objects.exclude(custom_properties=None).values_list("custom_properties", flat=True):
        go = (cp or {}).get("group_override")
        if isinstance(go, int):
            override_ids.add(go)

    # Build set of group ids that have any M3U-account link
    linked_ids = set(
        ChannelGroupM3UAccount.objects.values_list("channel_group_id", flat=True).distinct()
    )

    # Find candidates: zero streams, zero channels, not referenced as override, not M3U-linked
    orphans = []
    kept = 0
    for g in ChannelGroup.objects.all():
        if g.id in override_ids:
            kept += 1
            continue
        if g.id in linked_ids:
            kept += 1
            continue
        if Stream.objects.filter(channel_group=g).exists():
            kept += 1
            continue
        if Channel.objects.filter(channel_group=g).exists():
            kept += 1
            continue
        orphans.append({"id": g.id, "name": g.name})

    msg_prefix = "[DRY RUN] " if dry_run else ""
    msg = f"{msg_prefix}Found {len(orphans)} orphan ChannelGroup rows. Kept: {kept}."
    logger.info("[sports_filter] %s", msg)

    if not dry_run and orphans:
        ids = [o["id"] for o in orphans]
        deleted, _ = ChannelGroup.objects.filter(id__in=ids).delete()
        logger.info("[sports_filter] cleanup_orphans deleted %d ChannelGroup rows", deleted)
        msg = f"Deleted {deleted} orphan ChannelGroup rows."

    return {
        "status": "ok",
        "message": msg,
        "dry_run": dry_run,
        "orphans": orphans,
        "kept_count": kept,
    }


# ---------- Action: show_status ----------

def _action_show_status(settings: Dict[str, Any]) -> Dict[str, Any]:
    from apps.channels.models import ChannelGroupM3UAccount
    account_id = _resolve_account_id(settings)
    cache = _read_group_cache()
    stream_cache = _read_stream_cache()
    rels = ChannelGroupM3UAccount.objects.all()
    if account_id is not None:
        rels = rels.filter(m3u_account_id=account_id)
    enabled = rels.filter(enabled=True).count()
    syncing = rels.filter(enabled=True, auto_channel_sync=True).count()
    pure = sum(1 for v in cache.values() if v == VERDICT_PURE_SPORTS)
    mixed = sum(1 for v in cache.values() if v == VERDICT_MIXED)
    notsp = sum(1 for v in cache.values() if v == VERDICT_NOT_SPORTS)
    sport_streams = sum(1 for v in stream_cache.values() if v == VERDICT_SPORTS)
    scope = f"Acct {account_id}" if account_id is not None else "All accounts"
    msg = (
        f"{scope}: total_rels={rels.count()} enabled={enabled} syncing={syncing}. "
        f"Group cache: pure_sports={pure}, mixed={mixed}, not_sports={notsp}. "
        f"Stream cache: {len(stream_cache)} entries ({sport_streams} sports)."
    )
    logger.info("[sports_filter] %s", msg)
    return {"status": "ok", "message": msg}


# ---------- Schedule parsing ----------

def _parse_schedule(raw: Any) -> List[Tuple[int, int]]:
    """Parse comma-separated clock times into a sorted, de-duplicated list of
    (hour, minute) tuples. Generous parser, strict validator:
      - 'HHMM'   ('0300', '1830')
      - 'HH:MM'  ('03:00', '18:30')
      - 'H:MM'   ('3:00')
      - 'HH'     ('3', '03') treated as HH:00
    Whitespace anywhere is ignored. Invalid entries log a warning and are
    skipped. If nothing parses, falls back to constants.DEFAULT_SCHEDULE_TIMES.

    Returns the times sorted ascending; the scheduler walks this to find
    the next firing time after `now`.
    """
    if raw is None:
        return list(DEFAULT_SCHEDULE_TIMES)
    text = str(raw).strip()
    if not text:
        return list(DEFAULT_SCHEDULE_TIMES)

    out: List[Tuple[int, int]] = []
    for tok in re.split(r"[,\s]+", text):
        if not tok:
            continue
        clean = tok.replace(":", "").strip()
        if not clean.isdigit():
            logger.warning("[sports_filter] schedule entry %r is not numeric, skipping", tok)
            continue
        if len(clean) <= 2:
            h, m = int(clean), 0
        elif len(clean) == 3:
            h, m = int(clean[0]), int(clean[1:])
        elif len(clean) == 4:
            h, m = int(clean[:2]), int(clean[2:])
        else:
            logger.warning("[sports_filter] schedule entry %r has too many digits, skipping", tok)
            continue
        if not (0 <= h <= 23 and 0 <= m <= 59):
            logger.warning("[sports_filter] schedule entry %r out of range (00:00-23:59), skipping", tok)
            continue
        out.append((h, m))

    if not out:
        logger.warning(
            "[sports_filter] schedule %r had no valid entries, falling back to default %s",
            text, _format_schedule_times(DEFAULT_SCHEDULE_TIMES),
        )
        return list(DEFAULT_SCHEDULE_TIMES)
    return sorted(set(out))


def _format_schedule_times(times) -> str:
    """Render a list/tuple of (hour, minute) tuples as canonical comma-separated
    HHMM, used for log lines and warning messages. Inverse of _parse_schedule
    for valid input."""
    return ",".join(f"{h:02d}{m:02d}" for h, m in times)


def _next_firing(now: datetime, schedule: List[Tuple[int, int]]) -> datetime:
    """Given a sorted list of (hour, minute) tuples, return the next datetime
    strictly after `now`. Wraps to tomorrow's earliest if all of today's
    times are already past. Caller is responsible for ensuring `schedule`
    is non-empty (use _parse_schedule which guarantees it).
    """
    base = now.replace(second=0, microsecond=0)
    candidates = [base.replace(hour=h, minute=m) for h, m in schedule]
    future = [c for c in candidates if c > now]
    if future:
        return future[0]
    h, m = schedule[0]
    return (base + timedelta(days=1)).replace(hour=h, minute=m)


# ---------- Background scheduler ----------

_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_THREAD: Optional[threading.Thread] = None
_SCHEDULER_STOP = threading.Event()


def _read_persisted_settings() -> Dict[str, Any]:
    """Read the plugin's saved settings from Dispatcharr's PluginConfig."""
    try:
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.filter(key="dispatcharr_sports_filter").first()
        if cfg and cfg.enabled:
            return cfg.settings or {}
    except Exception as e:
        logger.debug("[sports_filter] read PluginConfig failed: %s", e)
    return {}


def _try_acquire_scheduler_lock() -> bool:
    """SET NX on a Redis key; returns True if THIS worker won the run."""
    try:
        from core.utils import RedisClient
        rc = RedisClient.get_client()
        return bool(rc.set(
            SCHEDULER_LOCK_KEY,
            f"pid={os.getpid()}",
            nx=True,
            ex=SCHEDULER_LOCK_TTL_S,
        ))
    except Exception as e:
        logger.warning("[sports_filter] redis lock check failed (running anyway): %s", e)
        return True


def _release_scheduler_lock() -> None:
    try:
        from core.utils import RedisClient
        rc = RedisClient.get_client()
        rc.delete(SCHEDULER_LOCK_KEY)
    except Exception:
        pass


def _scheduler_loop() -> None:
    """Wake at each configured clock time and run the auto_pipeline.

    The schedule is a comma-separated list of clock times (e.g. '0300' for
    daily 3 AM, '0000,0600,1200,1800' for every 6 hours). Each loop
    iteration computes the next firing time strictly after `now`,
    sleeps until then, then runs the pipeline.
    """
    logger.info("[sports_filter] scheduler thread started (pid=%d)", os.getpid())
    while not _SCHEDULER_STOP.is_set():
        try:
            settings = _read_persisted_settings()
            if not settings.get("auto_pipeline_enabled", False):
                # Setting flipped off — recheck in 5 minutes
                if _SCHEDULER_STOP.wait(300):
                    break
                continue
            schedule = _parse_schedule(settings.get("auto_pipeline_schedule", ""))
            now = datetime.now()
            target = _next_firing(now, schedule)
            sleep_s = (target - now).total_seconds()
            logger.info(
                "[sports_filter] scheduler sleeping %.0fs until next run (%s) — schedule=%s",
                sleep_s, target.isoformat(timespec="seconds"),
                _format_schedule_times(schedule),
            )
            if _SCHEDULER_STOP.wait(sleep_s):
                break
            # Try to grab the cross-worker lock
            if not _try_acquire_scheduler_lock():
                logger.info("[sports_filter] scheduler: lock held by another worker; skipping")
                # Sleep a minute so we don't spin near the trigger time
                if _SCHEDULER_STOP.wait(60):
                    break
                continue
            try:
                logger.info("[sports_filter] scheduler: running auto_pipeline")
                _action_auto_pipeline(settings)
            finally:
                _release_scheduler_lock()
        except Exception:
            logger.exception("[sports_filter] scheduler iteration crashed; sleeping 5min")
            if _SCHEDULER_STOP.wait(300):
                break
    logger.info("[sports_filter] scheduler thread exiting")


def _start_scheduler() -> None:
    """Start the daemon scheduler thread (idempotent across plugin reloads)."""
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return
        _SCHEDULER_STOP.clear()
        _SCHEDULER_THREAD = threading.Thread(
            target=_scheduler_loop,
            name="sports-filter-scheduler",
            daemon=True,
        )
        _SCHEDULER_THREAD.start()


def _stop_scheduler() -> None:
    _SCHEDULER_STOP.set()


# ---------- Plugin shell ----------

def _build_account_field():
    """Build the m3u_account_id field. Default '0' is the all-accounts sentinel
    (no real M3UAccount has id=0). Cannot use empty string as the sentinel
    because Dispatcharr's plugin-field serializer rejects select options with
    a blank value ('apps.plugins.loader: Invalid plugin field entry ignored').
    """
    all_option = {"value": "0", "label": "All M3U accounts"}
    try:
        from apps.m3u.models import M3UAccount
        accounts = list(M3UAccount.objects.all().order_by("id"))
        if accounts:
            options = [all_option] + [
                {"value": str(a.id), "label": f"{a.id} - {a.name}"} for a in accounts
            ]
            return {
                "id": "m3u_account_id", "type": "select", "label": "M3U Account scope",
                "default": "0", "options": options,
                "help_text": "Which M3U accounts to classify + apply against. Default 'All M3U accounts' covers every enabled provider; pick a single account only if you want to scope the plugin to one provider.",
            }
    except Exception as e:
        logger.warning("[sports_filter] Could not query M3UAccount for dropdown: %s", e)
    # Django unavailable (e.g. settings preview before db is reachable). Fall
    # back to a free-form string. '0' = all; any positive int = scoped.
    return {
        "id": "m3u_account_id", "type": "string", "label": "M3U Account scope",
        "default": "0",
        "help_text": "Database ID of the M3U account, or '0' for 'all enabled M3U accounts'.",
    }


def _build_profile_field():
    try:
        from apps.channels.models import ChannelProfile
        profiles = list(ChannelProfile.objects.all().order_by("id"))
        if profiles:
            options = [{"value": str(p.id), "label": f"{p.id} - {p.name}"} for p in profiles]
            sports = next((p for p in profiles if "sport" in p.name.lower()), None)
            default = str(sports.id) if sports else str(profiles[0].id)
            return {
                "id": "channel_profile_id", "type": "select", "label": "Channel Profile for sports",
                "default": default, "options": options,
                "help_text": "Auto-created channels for sports groups will be added to this profile.",
            }
    except Exception as e:
        logger.warning("[sports_filter] Could not query ChannelProfile for dropdown: %s", e)
    return {
        "id": "channel_profile_id", "type": "number", "label": "Channel Profile ID for sports",
        "default": DEFAULT_PROFILE_ID,
        "help_text": "Database ID of the ChannelProfile.",
    }


_REPO_URL = "https://github.com/Jacob-Lasky/dispatcharr_sports_filter"

# action id -> handler. Single source of truth used by Plugin.run dispatch.
# Keep in sync with self.actions (UI manifest).
ACTION_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "classify": _action_classify,
    "refine_mixed": _action_refine_mixed,
    "apply": _action_apply,
    "cleanup_orphans": _action_cleanup_orphans,
    "auto_pipeline": _action_auto_pipeline,
    "show_status": _action_show_status,
}


class Plugin:
    def __init__(self):
        self.name = "Sports-Only Group Filter"
        self.version = PLUGIN_VERSION
        self.description = (
            "Three-bucket M3U group filter (pure_sports / mixed / not_sports). "
            "Per-stream filtering for mixed bouquets via name_match_regex. "
            "Auto-renames source groups via group_override. Regex pre-filter, "
            "Anthropic / OpenAI / Gemini LLM for ambiguous classification."
        )
        self.author = "Jake (with Claude)"
        self.url = _REPO_URL

        # NOTE: this self.fields list intentionally restates the static fields
        # from plugin.json. Dispatcharr's plugin loader hydrates dynamic dropdown
        # options (m3u_account_id, channel_profile_id) from this Python-side
        # build because plugin.json has no DB access. The static defaults below
        # must stay in sync with plugin.json; both are sourced from constants.py
        # so the duplication is shallow.
        self.fields = [
            _build_account_field(),
            _build_profile_field(),
            {
                "id": "anthropic_api_key", "type": "string", "input_type": "password",
                "label": "Anthropic API key", "default": "",
                "help_text": "Masked in the UI. If blank, falls back to <plugin_dir>/anthropic_api_key on disk (chmod 600). Required when 'LLM model' is set to a Claude (claude-*) model.",
            },
            {
                "id": "openai_api_key", "type": "string", "input_type": "password",
                "label": "OpenAI API key", "default": "",
                "help_text": "Masked in the UI. If blank, falls back to <plugin_dir>/openai_api_key on disk (chmod 600). Required when 'LLM model' is set to an OpenAI (gpt-*, o1-*, o3-*) model.",
            },
            {
                "id": "gemini_api_key", "type": "string", "input_type": "password",
                "label": "Google Gemini API key", "default": "",
                "help_text": "Masked in the UI. If blank, falls back to <plugin_dir>/gemini_api_key on disk (chmod 600). Required when 'LLM model' is set to a Gemini (gemini-*) model.",
            },
            {
                "id": "model", "type": "select", "label": "LLM model",
                "default": DEFAULT_MODEL,
                "options": [
                    # Anthropic
                    {"value": "claude-haiku-4-5", "label": "Anthropic — Claude Haiku 4.5 (cheap, fast)"},
                    {"value": "claude-sonnet-4-6", "label": "Anthropic — Claude Sonnet 4.6"},
                    {"value": "claude-opus-4-7", "label": "Anthropic — Claude Opus 4.7"},
                    # OpenAI
                    {"value": "gpt-4o-mini", "label": "OpenAI — GPT-4o mini (cheap, fast)"},
                    {"value": "gpt-4.1-mini", "label": "OpenAI — GPT-4.1 mini"},
                    {"value": "gpt-4.1", "label": "OpenAI — GPT-4.1"},
                    {"value": "gpt-4o", "label": "OpenAI — GPT-4o"},
                    # Google Gemini
                    {"value": "gemini-2.0-flash", "label": "Google — Gemini 2.0 Flash (cheap, fast)"},
                    {"value": "gemini-2.5-flash", "label": "Google — Gemini 2.5 Flash"},
                    {"value": "gemini-2.5-pro", "label": "Google — Gemini 2.5 Pro"},
                ],
                "help_text": (
                    "Provider is inferred from the model prefix: claude-* -> Anthropic, "
                    "gpt-* / o1-* / o3-* -> OpenAI, gemini-* -> Google. Make sure the "
                    "matching API key is configured (see anthropic_api_key / "
                    "openai_api_key / gemini_api_key fields)."
                ),
            },
            {"id": "samples_per_group", "type": "number", "label": "Sample channels per group", "default": DEFAULT_SAMPLES_PER_GROUP},
            {"id": "dry_run", "type": "boolean", "label": "Dry run on Apply", "default": True},
            {
                "id": "enable_llm", "type": "boolean",
                "label": "Use LLM for ambiguous classification",
                "default": False,
                "help_text": "OFF by default (regex-only mode): no API key needed, no per-call cost, no third-party network traffic. Ambiguous group names default to 'not_sports'; tune via extra_allow_terms / extra_deny_terms. Turn ON to send ambiguous bouquets to the configured LLM (Anthropic / OpenAI / Google — pick via 'LLM model') AND get per-stream classification of mixed bouquets — requires the matching API key.",
            },
            {
                "id": "extra_allow_terms", "type": "string",
                "label": "Extra allow keywords",
                "default": "",
                "help_text": "Comma- or newline-separated extra terms that should classify as 'pure_sports' if matched. OR'd with the built-in keyword list. Each term is matched as a whole word, case-insensitive (e.g. 'lacrosse, padel').",
            },
            {
                "id": "extra_deny_terms", "type": "string",
                "label": "Extra deny keywords",
                "default": "",
                "help_text": "Comma- or newline-separated extra terms that should classify as 'not_sports' if matched. OR'd with the built-in deny list. Use this to demote things you do not want treated as sports (e.g. 'flosports, darts, snooker').",
            },
            {
                "id": "extra_classification_hints", "type": "string",
                "label": "Extra LLM classification hints",
                "default": "",
                "help_text": "Free-text instructions appended to the LLM system prompt for borderline groups/streams. Example: 'Treat motorsport documentaries as sports. Treat fishing channels as not_sports.'",
            },
            {
                "id": "auto_pipeline_enabled", "type": "boolean",
                "label": "Auto-run pipeline on schedule",
                "default": False,
                "help_text": "Off by default. When on, the plugin runs classify -> refine_mixed -> apply -> cleanup_orphans at every clock time listed in the schedule below. Enable only after you have reviewed dry-run output and trust the cache. Cache makes subsequent runs cheap; only new groups/streams hit the LLM.",
            },
            {
                "id": "auto_pipeline_schedule", "type": "string",
                "label": "Schedule (clock times, comma-separated)",
                "default": DEFAULT_SCHEDULE_STRING,
                "help_text": "Comma-separated list of clock times (server local time) when the auto-pipeline fires. Default '0300' is daily at 3 AM. Multi-times-per-day examples: '0000,0600,1200,1800' (every 6h) or '0300,1500' (every 12h). Both 'HHMM' and 'HH:MM' forms work; whitespace is ignored. Invalid entries are skipped with a warning.",
            },
            {
                "id": "apply_group_rename", "type": "boolean",
                "label": "Apply group rename (group_override) on Apply",
                "default": True,
                "help_text": "Strips configured prefixes from sports group names via Dispatcharr's built-in group_override. Auto-created channels go into the cleaner-named target group. Survives M3U refreshes.",
            },
            {
                "id": "group_rename_strip_prefixes", "type": "string",
                "label": "Group rename: prefixes to strip",
                "default": ", ".join(DEFAULT_STRIP_PREFIXES),
                "help_text": "Comma-separated list of literal prefixes to drop from the head of a group name when building the cleaner target group. AliceXC-style providers ship 'Sports | NFL', so the default is 'Sports |, Sports/'. Other providers might use 'SP-' or 'SPRT|'. Match is case-insensitive. Leave empty for no prefix stripping.",
            },
            {
                "id": "mixed_groups_sports_suffix", "type": "boolean",
                "label": "Append ' Sports' to mixed-group target names",
                "default": True,
                "help_text": "When on, mixed-bouquet target groups get a ' Sports' suffix (e.g. 'US | Peacock TV' -> 'US Peacock TV Sports'). Communicates 'this is the filtered-sports subset of a bigger bouquet'. Turn off if you prefer the bouquet name verbatim.",
            },
            {
                "id": "also_unselect_not_sports", "type": "boolean",
                "label": "Also unselect not_sports groups",
                "default": False,
                "help_text": "Stronger than auto_channel_sync=False: also flips 'enabled' off, so the M3U import skips the group entirely. Warning: orphans existing channels that pull streams only from those groups.",
            },
            {"id": "debug_mode", "type": "boolean", "label": "Debug logging", "default": False},
        ]
        self.actions = [
            {"id": "classify", "label": "Classify groups (3-bucket)",
             "description": "Classify enabled groups as pure_sports / mixed / not_sports. Writes cache.json. No DB writes."},
            {"id": "refine_mixed", "label": "Refine mixed groups (per-stream)",
             "description": "For groups marked 'mixed', classify each stream as sports/not_sports. Writes stream_cache.json. No DB writes."},
            {"id": "apply", "label": "Apply sports filter",
             "description": "Toggle auto_channel_sync, set group_override, build name_match_regex from caches. Honors dry_run."},
            {"id": "cleanup_orphans", "label": "Cleanup orphan ChannelGroups",
             "description": "Delete ChannelGroup rows with no streams, no channels, no M3U links, and not referenced by any group_override. Honors dry_run."},
            {"id": "auto_pipeline", "label": "Run full pipeline now",
             "description": "Run classify -> refine_mixed -> apply (real) -> cleanup_orphans (real) end-to-end. The daily scheduler invokes this; this button triggers it on demand."},
            {"id": "show_status", "label": "Show current state",
             "description": "Print cache + account state. No writes."},
        ]

        # Start daily scheduler thread (idempotent across reloads). Each Dispatcharr
        # worker process loads the plugin and starts its own thread; the cross-worker
        # Redis lock in _try_acquire_scheduler_lock ensures the pipeline only fires
        # in one worker per scheduled tick.
        try:
            _start_scheduler()
        except Exception as e:
            logger.warning("[sports_filter] scheduler failed to start: %s", e)

    def run(self, action: Optional[str] = None, params: Optional[Dict[str, Any]] = None, context: Optional[Dict[str, Any]] = None):
        ctx = context or {}
        settings = dict(ctx.get("settings") or {})
        if params:
            settings.update(params)
        try:
            handler = ACTION_HANDLERS.get(action or "")
            if handler is not None:
                return handler(settings)
            # Lifecycle hooks Dispatcharr calls outside the user-action surface.
            if action == "enable":
                _start_scheduler()
                return {"status": "ok", "message": "scheduler started"}
            if action == "disable":
                _stop_scheduler()
                return {"status": "ok", "message": "scheduler stopped"}
            return {"status": "error", "message": f"Unknown action: {action!r}"}
        except Exception as e:
            logger.exception("[sports_filter] Action %r failed", action)
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
