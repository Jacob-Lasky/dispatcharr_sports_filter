# Dispatcharr Sports-Only Group Filter

A [Dispatcharr](https://github.com/dispatcharr/dispatcharr) plugin that uses
regex + Claude (Anthropic) to classify M3U channel groups as `pure_sports`,
`mixed`, or `not_sports`, then prunes your IPTV setup to surface only sports
content.

If your provider ships a giant M3U with bouquets like `Sports | NFL`,
`Sports | Peacock TV` (mixed sports + entertainment), `Movies | HBO Max`,
`Colombia | TV`, `Religious | Gospel`, etc., this plugin uses the LLM to
classify each one, then:

- **`pure_sports`** groups → kept fully selected, optionally renamed to a
  cleaner target (`Sports | NFL` → `NFL`).
- **`mixed`** groups → kept selected with a `name_match_regex` filter that
  only allows sports-classified streams through (per-stream classification
  via a second LLM pass).
- **`not_sports`** groups → `auto_channel_sync` disabled (or fully
  unselected, if `also_unselect_not_sports=True`).

## What it does on a real provider

On a typical 50k-stream provider you'll see something like:

```
classify    : 200 groups → 80 pure_sports, 25 mixed, 95 not_sports (cached, ~1 LLM call)
refine_mixed: 25 mixed groups → ~3000 streams classified individually (cached)
apply       : flips ChannelGroupM3UAccount rows + sets group_override targets
              + builds name_match_regex per mixed group
```

After the next M3U refresh, your channel profile is sports-only with clean
group names and the noise stripped out.

## Install

1. Clone this repo into your Dispatcharr plugins directory:

   ```bash
   docker exec dispatcharr git clone https://github.com/Jacob-Lasky/dispatcharr_sports_filter.git \
       /data/plugins/dispatcharr_sports_filter
   ```

2. Stage your Anthropic API key:

   ```bash
   docker exec dispatcharr sh -c 'echo "sk-ant-..." > /data/plugins/dispatcharr_sports_filter/anthropic_api_key && chmod 600 /data/plugins/dispatcharr_sports_filter/anthropic_api_key'
   ```

3. Open Dispatcharr → Plugins → enable **Sports-Only Group Filter**, configure:
   - **M3U Account** — which provider's groups to classify
   - **Channel Profile for sports** — assigned to `pure_sports` and `mixed`
     groups so the channels they spawn land in your sports profile
   - **Claude model** — Haiku 4.5 is fast + cheap; Sonnet for ambiguous edge
     cases

4. Run **Classify groups (3-bucket)** → review `cache.json` → run **Refine
   mixed groups (per-stream)** → review `stream_cache.json` → run **Apply
   sports filter** with `dry_run=True` first → flip `dry_run` off when happy.

## Pipeline

| Action | What it does | Writes |
|---|---|---|
| `classify` | Reads enabled groups, classifies each as pure_sports / mixed / not_sports (regex pre-filter + LLM batched call). Cached by group name. | `cache.json` |
| `refine_mixed` | For groups marked `mixed`, classifies each stream individually (sports / not_sports). Auto-promotes a group to `pure_sports` if 100% sports, demotes to `not_sports` if 0%. | `stream_cache.json` |
| `apply` | Writes `ChannelGroupM3UAccount` rows: toggles `auto_channel_sync`, sets `group_override` (rename source groups via Dispatcharr's built-in mechanism), builds `name_match_regex` per mixed group from the stream cache. | DB (honors `dry_run`) |
| `cleanup_orphans` | Deletes `ChannelGroup` rows with no streams, no channels, no M3U links, and not referenced by any `group_override`. Stale rename targets from older apply runs. | DB (honors `dry_run`) |
| `auto_pipeline` | Runs all four end-to-end. The daily scheduler invokes this. | Everything |

## Settings (highlights)

- `samples_per_group` — how many channel names from a group to send to the
  LLM as classification context (default 10).
- `apply_group_rename` — strip `Sports |` prefixes via `group_override` so
  auto-created channels go into a cleaner-named target group. Survives M3U
  refreshes.
- `also_unselect_not_sports` — stronger than `auto_channel_sync=False`: also
  flips `enabled=False`, so the M3U import skips the group entirely. Warning:
  orphans existing channels that pull streams only from those groups.
- `auto_pipeline_enabled` + `hour` + `minute` — daily scheduler. Cache makes
  subsequent runs near-free since only new groups/streams hit the LLM.

## Caches

Both caches live in `<plugin_dir>/`. Safe to delete to force re-classification.

```
cache.json        : { "Sports | NFL": "pure_sports", "US | Peacock TV": "mixed", ... }
stream_cache.json : { "NFL Network HD": "sports", "Peacock News HD": "not_sports", ... }
```

## Scheduler

The plugin spawns one daemon thread per Dispatcharr worker. A Redis-based
`SET NX EX` lock at key `plugins:sports_filter:auto_pipeline:lock` ensures the
pipeline only fires in one worker per scheduled tick.

## Contributing

Issues + PRs welcome. Especially: regex pre-filter tweaks for new providers,
LLM prompt improvements, and edge-case group names that should classify
differently.

## License

MIT — see LICENSE.
