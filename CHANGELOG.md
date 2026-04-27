# Changelog

All notable changes to this plugin are documented here.

## [0.7.0] — 2026-04-27 — LLM-free mode (now the default)

Adds `enable_llm` boolean. **Default is now `false`** — fresh installs
run in regex-only mode out of the box, no Anthropic API key required.
Toggle the setting on (and configure an API key) to get the v0.6.0
behavior back.

In regex-only mode:

- No Anthropic API key required.
- No per-call cost, no third-party network traffic, channel names never
  leave the Dispatcharr install.
- Ambiguous group names (regex pre-filter undecided) default to
  `not_sports`. Recover the bouquets you care about via
  `extra_allow_terms`.
- `Refine mixed groups` action returns ok-with-skip rather than
  erroring out. Per-stream classification of mixed bouquets needs the
  LLM by design.
- `Apply` works as-is. With no LLM, no group ever gets classified as
  `mixed`, so the `name_match_regex` per-stream filter path is never
  exercised.
- Pre-existing `mixed` cache entries from earlier LLM-enabled runs are
  preserved (cache check runs before regex / LLM), so toggling
  `enable_llm` off is non-destructive.

The pitch for the public Dispatcharr plugin listing becomes a tiered
on-ramp: **regex tier** (free, simple) for users who want curation
without an API key, **LLM tier** (smarter, ambiguous-bouquet detection,
per-stream filtering of mixed bouquets) for users who want the full
experience.

Closes #2.

## [0.6.0] — 2026-04-27 — public-plugin generalization

### M3U account scope: 'All' is now the default

`m3u_account_id` defaults to an empty string ("All M3U accounts") instead
of forcing the user to pick a single provider before the plugin will do
anything. The dropdown gains an "All M3U accounts" option at the top.
When scoped to all, the plugin classifies + applies across every enabled
M3U account; the cache is keyed by group name so a name shared between
providers (e.g. both have 'Sports | NFL') gets one verdict applied to
both providers' relations.

Single-provider scoping is still supported for users with multiple
providers who want to limit the plugin to one of them.

The plugin does not touch EPG sources at all — EPG matching happens
downstream of channel creation, off the channel name, so there is
nothing to scope EPG-wise.

While here: fixed a pre-existing fragility in `_gather_streams_for_group`
where two `ChannelGroup` rows sharing a display name (rare but possible
across providers) would only collect streams from the first row.
Now uses `channel_group__in=` for the union.


This release decouples the plugin from Jake's specific M3U provider, taste,
and Dispatcharr install so it can be installed by anyone.

### New settings

- `extra_allow_terms` — comma- or newline-separated keywords OR'd into the
  built-in ALLOW regex. Promote niche sports the built-in list does not
  cover (e.g. `padel, lacrosse, beach volleyball`).
- `extra_deny_terms` — same shape, OR'd into the built-in DENY regex.
  Demote things you do not want treated as sports (e.g.
  `flosports, darts, snooker`). User deny terms beat the built-in allow
  via the existing "ambiguous → defer" rule, so you no longer need to
  manually edit `cache.json`.
- `extra_classification_hints` — free-text appended to the Claude system
  prompt for borderline cases the regex layer cannot resolve.
- `group_rename_strip_prefixes` — comma-separated list of prefixes to drop
  from group names when building cleaner target groups. Default
  `Sports |, Sports/` matches AliceXC; other providers can override with
  `SP-`, `SPRT|`, etc. Empty disables prefix stripping entirely.
- `mixed_groups_sports_suffix` — toggle the forced ` Sports` suffix on
  mixed-bouquet target names. Default `true` matches prior behavior.

### Behavior changes (read before upgrading)

- **`auto_pipeline_enabled` now defaults to `false`.** Existing installs
  with the field saved as `true` are unaffected; new installs must
  explicitly opt in. Rationale: a fresh public install should not run
  potentially-destructive scheduled DB writes at 3 AM before the user has
  reviewed dry-runs.
- **`_action_auto_pipeline` no longer silently sets
  `also_unselect_not_sports=True` and `apply_group_rename=True`** when the
  user has not configured them. The daily run now respects the field
  defaults exactly. If you relied on the old aggressive-by-default
  behavior, set both fields explicitly to `true` in plugin settings.
- The `2`-as-default fallback for `m3u_account_id` and `channel_profile_id`
  in `plugin.json` is now `1` — it was a stale leftover from Jake's
  install. The dynamic dropdown still picks the right account/profile at
  runtime, so this only affects the rendered form on a brand-new
  installation before the dropdown loads.

### Bug fixes

- `sec\+` was a third silently-dead built-in regex token. The trailing `\b`
  anchor never matches at end-of-string after a literal `+` because `+` is
  not a word char. Replaced with `(?!\w)`. Same trap as the
  `documentar` / `religi` / `sky\s*sport` etc. fixes in 0.5.1, just at the
  trailing end. The user-terms compiler uses the same fix so user-supplied
  terms like `sec+` work too.

### Tests

- Added `tests/test_user_extensions.py` (26 tests) covering the new
  settings end-to-end: term parsing, regex-escape behavior at metachars,
  word-boundary anchoring, prompt augmentation, custom prefix stripping,
  and the public-plugin demote-FloSports flow.

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
