# Changelog

All notable changes to this plugin are documented here.

## [0.5.1] — 2026-04-27

- Fix six dead regex tokens in classifier pre-filter (`sky\s*sport`,
  `fox\s*sport`, `tnt\s*sport`, `flosport`, `documentar`, `religi`) that the
  trailing `\b` anchor silently prevented from ever matching plural / suffixed
  forms like "Sky Sports", "Documentaries", "Religious".
- Fix `_build_match_regex` to return `""` (no filter) when every input is
  blank, instead of `^()$` which would silently drop all real streams.
- Extract verdict + default constants to `constants.py` so the wire strings
  (`pure_sports` / `mixed` / `not_sports` / `sports`) and default IDs have a
  single source of truth across `plugin.py`, `classifier.py`, and tests.
- Replace `Plugin.run` if-chain with `ACTION_HANDLERS` dispatch dict; tests
  pin the action manifest against the dispatch table.
- Drop unreachable `<would-create:NAME>` placeholder cleanup in
  `_action_apply` (the path can only be entered during dry-run).
- Iterate `needs_llm` instead of LLM response keys in `classify_all_groups`
  so a hallucinated extra group name in the response can't sneak into the
  cache.
- `cleanup_orphans` now pulls only `custom_properties` via `values_list`
  instead of hydrating each `ChannelGroupM3UAccount` row.
- Stale identifiers fixed: docstring "Version 0.3.0" -> sourced from
  `PLUGIN_VERSION`; `__version__ = "0.1.0"` -> sourced from `PLUGIN_VERSION`;
  repo URL `jacob-lasky/dispatcharr-sports-filter` ->
  `Jacob-Lasky/dispatcharr_sports_filter` in `plugin.json`, `plugin.py`, and
  `README.md` (the README install command now actually clones).
- New `tests/` directory with 78 tests for pure functions (regex pre-filter,
  name cleaner, regex builder, JSON extraction, normalizers, cache filters,
  API-key resolution, action manifest contract, settings resolvers).

## [0.5.0] — 2026-04-26

- Daily auto-pipeline scheduler with cross-worker Redis lock so only one
  Dispatcharr worker fires per scheduled tick.
- `cleanup_orphans` action: deletes `ChannelGroup` rows with no streams, no
  channels, no M3U links, and not referenced by any `group_override`. These
  are stale rename targets from older apply runs.

## [0.4.x]

- Force-rename + `(N)` consolidation: `NBA` and `NBA (2)` collapse to one
  `NBA` target group via `group_override`. Mixed groups get a forced ` Sports`
  suffix on the clean target name.
- Targeted `Big Ten +` vs `Big Ten+` normalization (trailing-only).

## [0.3.x]

- Three-bucket group classification (`pure_sports` / `mixed` / `not_sports`).
- Per-stream classification for mixed bouquets via `name_match_regex`.
- `group_override` — auto-rename source groups via Dispatcharr's built-in
  override mechanism so renames survive M3U refreshes.
- Auto-promote / demote: `refine_mixed` re-classifies a group up to
  `pure_sports` or down to `not_sports` based on stream-level content.

## [0.2.x]

- Added `also_unselect_not_sports` setting (flips `enabled=False` on top of
  `auto_channel_sync=False`).
- Fixed `channel_profile_ids` storing as `["2"]` (string) instead of `[2]` (int).
- Per-process plugin cache invalidation via `pm.discover_plugins(force_reload=True, use_cache=False)`.

## [0.1.x] — initial

- Binary classifier (`sports` / `not_sports`).
- Toggles `auto_channel_sync` per `ChannelGroupM3UAccount` based on the verdict.
