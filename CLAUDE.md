# Claude session handoff — dispatcharr-sports-filter

If you're a Claude session opening this repo cold, read this first. It tells
you what the plugin does, how it's structured, how to test changes, and where
the value-adds live.

## What this is

A Dispatcharr plugin that classifies M3U channel groups using regex + Claude
(Anthropic), then prunes the user's IPTV setup to surface only sports content.
It targets [Dispatcharr](https://github.com/dispatcharr/dispatcharr) — an open
source IPTV proxy that lets you map M3U streams to channels and serve them as
your own M3U + EPG to TV apps like TiviMate.

The user runs Dispatcharr in a Docker container.

## File map

| File | What it does |
|---|---|
| `plugin.json` | Plugin manifest — settings, action buttons, default values. Read by Dispatcharr's plugin loader. |
| `plugin.py` | Plugin class + 6 actions: `classify`, `refine_mixed`, `apply`, `cleanup_orphans`, `auto_pipeline`, `show_status`. Also the daemon scheduler. |
| `classifier.py` | Regex pre-filter (`ALLOW_RE` / `DENY_RE`) + Claude API calls. Two flavors: group-level (3-bucket: pure_sports/mixed/not_sports) and stream-level (binary: sports/not_sports). |
| `__init__.py` | Exports `Plugin` class. Dispatcharr loads `<plugin_dir>/__init__.py` and looks for `Plugin`. |

State that doesn't live in git:
- `cache.json` — group classification verdicts. Persists across runs.
- `stream_cache.json` — per-stream classification verdicts (only for streams in mixed groups).
- `anthropic_api_key` — file fallback if settings field is empty.
- `__pycache__/` — Python bytecode.

## How it integrates with Dispatcharr

Key Dispatcharr models the plugin reads/writes (in `apps.channels.models` +
`apps.m3u.models`):

| Model | Field this plugin uses |
|---|---|
| `ChannelGroup` | `name` (group name as it appears in M3U) |
| `ChannelGroupM3UAccount` | `enabled`, `auto_channel_sync`, `custom_properties` (where `group_override` and `name_match_regex` live) |
| `Stream` | `name`, `channel_group`, `m3u_account` |
| `M3UAccount` | account ID for filtering — typically `id=4` for the user's XC provider |
| `ChannelProfile` / `ChannelProfileMembership` | for assigning sports channels to a profile |

The plugin's mechanism: it sets `auto_channel_sync` on `ChannelGroupM3UAccount`
rows based on the verdict, plus optionally `group_override` (rename target)
and `name_match_regex` (per-stream filter for mixed groups). Dispatcharr's
own M3U-import code reads these on every refresh, so renames + filters
**survive provider M3U refreshes** without our intervention.

## Plugin loader specifics

- Plugin must be enabled via `PluginConfig.objects.update(enabled=True)`. First
  discovery creates the `PluginConfig` row but as `enabled=False`.
- Force reload: `pm.discover_plugins(force_reload=True, use_cache=False)`.
  Without this, Dispatcharr caches the loaded `Plugin` instance per worker, so
  edits to `plugin.py` aren't picked up.
- Each Dispatcharr worker (4× uwsgi + 6× celery) loads its own copy of the
  plugin. The scheduler thread spawns in each worker — a Redis `SET NX EX`
  lock at `plugins:sports_filter:auto_pipeline:lock` ensures only one fires
  per scheduled tick.

## Current state (v0.6.0, 2026-04-27)

**Public release.** Tag `v0.6.0` on `main`, GitHub release at
https://github.com/Jacob-Lasky/dispatcharr_sports_filter/releases/tag/v0.6.0.
Source of truth for version: `PLUGIN_VERSION` in `plugin.py`; mirrored in
`plugin.json`, `__init__.py.__version__`, and the Plugin class.

**Working:**
- 3-bucket group classification with regex pre-filter + LLM batched classify (Haiku 4.5 default)
- Per-stream classification of mixed groups
- Auto-promote / demote based on stream-level verdict (e.g., a "mixed" group classified 100% sports gets promoted to pure_sports)
- `(N)` consolidation: `NBA` and `NBA (2)` collapse to one `NBA` target via `group_override`
- Targeted spacing fix: `Big Ten +` → `Big Ten+` (trailing only)
- Mixed groups get a forced ` Sports` suffix on their clean target name (toggleable via `mixed_groups_sports_suffix`)
- `cleanup_orphans` removes stale rename targets
- Daily scheduler with cross-worker Redis lock (off by default in 0.6.0; opt-in)
- `anthropic_api_key` available as masked UI field (preferred) or on-disk file (fallback)
- User-extensible regex pre-filter via `extra_allow_terms` / `extra_deny_terms` (escaped, word-boundary-anchored, OR'd into the built-in tables)
- Free-text `extra_classification_hints` appended to the LLM system prompt for borderline cases
- M3U scope defaults to "All accounts" (sentinel `"0"` because Dispatcharr's plugin-field serializer rejects blank select-option values; pinned by a contract test)
- Constants module (`constants.py`) carries the verdict wire strings + defaults; do NOT spell `"pure_sports"` etc. as literals in code

**Watch out for:**
- Three silently-dead built-in regex tokens have been fixed across 0.5.1/0.6.0 (`documentar`, `religi`, `flosport`/`sky/fox/tnt sport`, `sec\+`). All shared the same root cause: `\b` cannot anchor between two word chars or between two non-word chars. If you add a new term to ALLOW_RE/DENY_RE, pick the right anchor: `(y|ies)?` style explicit suffixes for word-char tails, `(?!\w)` for terms ending in a non-word char like `+`. There's a regression test for `sec+` in `tests/test_classifier.py`; add similar pins for any new edge cases.
- The favorites system is in `dispatcharr_ranked_matchups`, not here. This plugin is purely classify+filter.

## Open work

Known limitations and TODOs are tracked as GitHub issues, not in this
file. Browse the open backlog:
https://github.com/Jacob-Lasky/dispatcharr_sports_filter/issues

## Plugin distribution

This plugin is distributed through the official Dispatcharr Plugins
repository: https://github.com/Dispatcharr/Plugins. When a plugin PR is
merged there, it gets automatically packaged, versioned, and published to
the releases branch — no separate release ceremony from the plugin author.

**Browse what is available right now:**
- Listing UI: https://dispatcharr.github.io/Dispatcharr-Docs/plugin-listing/
- Releases branch: https://github.com/Dispatcharr/Plugins/tree/releases
- Plugin forum thread (community discussion): https://discord.com/channels/1340492560220684331/1487508974457589973

**Submission flow** (already done for sports_filter v0.6.0; reference for
future major releases):

1. Develop and tag the release in this repo (Jacob-Lasky/dispatcharr_sports_filter).
2. Fork https://github.com/Dispatcharr/Plugins.
3. Add the plugin under `plugins/dispatcharr_sports_filter/` with a valid
   `plugin.json` carrying name, version, description, author, license.
4. Open a PR. Automated validation checks versioning, metadata, and code
   quality consistency. See https://github.com/Dispatcharr/Plugins/blob/main/CONTRIBUTING.md.
5. By submitting, the author grants the Dispatcharr maintainers a license
   to redistribute through the repo. Code stays the author's. Listing is
   curated; low-quality / abandoned submissions can be declined or removed.

GPG-signed manifests are coming. The bundled public key will let
Dispatcharr verify manifest integrity before installing anything.

**Future:** an in-app plugin hub for browse/install/update, plus a
published spec so third parties can host their own manifest-compatible
plugin repos.
