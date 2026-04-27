# Changelog

All notable changes to this plugin are documented here.

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
