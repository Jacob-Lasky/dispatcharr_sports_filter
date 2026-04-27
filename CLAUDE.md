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

The user (Jake) runs Dispatcharr in a Docker container on his UnRAID server
("tower"). This plugin lives in his private install at
`/data/plugins/dispatcharr_sports_filter/` inside that container.

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

## Development loop (Jake's tower)

The user keeps source in `/home/jlasky/Code/dispatcharr_sports_filter/` on his
laptop and deploys via:

```bash
# Sync source → tower /tmp
rsync -avz /home/jlasky/Code/dispatcharr_sports_filter/ \
    tower:/tmp/dispatcharr_sports_filter/

# Copy into Dispatcharr container
ssh tower 'docker cp /tmp/dispatcharr_sports_filter Dispatcharr:/data/plugins/'
```

Run an action programmatically (after edits):

```bash
ssh tower 'docker exec Dispatcharr python -c "
import django, os, sys
sys.path.insert(0, \"/app\")
os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\", \"dispatcharr.settings\")
django.setup()
from apps.plugins.loader import PluginManager
pm = PluginManager.get()
pm.discover_plugins(sync_db=False, force_reload=True, use_cache=False)
r = pm.run_action(\"dispatcharr_sports_filter\", \"show_status\", {})
print(r.get(\"message\", r))
"'
```

Read live logs:

```bash
ssh tower 'docker logs --since 5m Dispatcharr 2>&1 | grep sports_filter | tail -30'
```

## Current state (v0.5.0, 2026-04-26)

**Working:**
- 3-bucket group classification with regex pre-filter + LLM batched classify (Haiku 4.5 default)
- Per-stream classification of mixed groups
- Auto-promote / demote based on stream-level verdict (e.g., a "mixed" group classified 100% sports gets promoted to pure_sports)
- `(N)` consolidation: `NBA` and `NBA (2)` collapse to one `NBA` target via `group_override`
- Targeted spacing fix: `Big Ten +` → `Big Ten+` (trailing only)
- Mixed groups get a forced ` Sports` suffix on their clean target name
- `cleanup_orphans` removes stale rename targets
- Daily scheduler with cross-worker Redis lock
- `anthropic_api_key` available as masked UI field (preferred) or on-disk file (fallback)

**Known limitations:**
- LLM verdict not always right for ambiguous bouquets — caching makes re-runs cheap but a wrong verdict sticks until cache is manually cleared. There's no UI for editing the cache.
- The `(N)` regex assumes consistent ordering; doesn't handle `NBA (Backup)` etc.
- `cleanup_orphans` is conservative — won't delete groups that have any FK references at all, so dead groups with stale `name_match_regex` references stick around.
- The favorites system is in `dispatcharr_ranked_matchups`, not here. This plugin is purely classify+filter.

## Ideas / TODO (rough priority)

1. **Cache editor UI** — let the user override a verdict directly in the
   plugin UI (e.g., flip "Sports | FloSports" from pure_sports to not_sports)
   without manually editing `cache.json`. The user has done this twice
   already (FloSports demotion, etc.) and it's awkward.
2. **Per-group verdict reason** — alongside the verdict, store the LLM's
   reasoning so when the user sees a wrong verdict they understand why.
3. **Backwards-compat for legacy v1 binary cache** — currently legacy
   `'sports'` (binary) entries are silently dropped on load. Maybe migrate
   them rather than re-classify.
4. **Better DENY_RE for VOD-style streams** — provider VOD entries with
   "Sports" in the title (e.g., "Sports Documentaries") sometimes
   misclassify as pure_sports.
5. **Plugin-side rate limiting** — currently the LLM batched call is one
   blocking request; a slow Anthropic API can hold up apply for minutes.
   Worth chunking.
6. **Hook into cleanup_orphans for orphaned regex targets** — if a clean
   target group's regex no longer matches any streams, it's orphaned.

## User context (Jake)

- Senior at Deepgram, knows Python well, builds for himself
- Prefers terse output; will redirect if a path is wrong
- Trusts Claude to ship full features when given the lean ("knock it out")
- Tower runs Dispatcharr 0.23.0 (the latest as of late April 2026)
- M3U provider is AliceXC (XC type, account id=4); single provider
- Channel profile id=2 is "Sports" — that's the target profile the plugin assigns
- Other plugins on the same Dispatcharr install (don't disturb): `iptv_checker`,
  `stream_mapparr`, `epg_janitor`, `event_channel_managarr`,
  `dispatcharr_ranked_matchups`, `dispatcharr_timeshift`. Some of these had
  postgres-pool issues in spring 2026 — see plugin.py docstrings if you hit
  "too many clients already" errors.
- Postgres `max_connections` was bumped 100 → 200 on 2026-04-26 to handle
  multi-plugin load. Don't go below 200 if you're adding worker-heavy code.

## Companion plugin

`dispatcharr_ranked_matchups` is the user's other plugin that builds on the
sports profile this plugin produces. They share the same Anthropic key (via
the file fallback) and the same target Dispatcharr install. If you're working
on cross-plugin stuff, both repos are available locally.
