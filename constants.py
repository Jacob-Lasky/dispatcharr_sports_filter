"""Single source of truth for verdict strings, defaults, and shared identifiers.

Verdict strings are matched at runtime in cache files, classifier output, and
ChannelGroupM3UAccount custom_properties. A typo here is silent — the cache load
filter would just drop the entry — so they live here, not as ad-hoc literals.
"""

from __future__ import annotations


# Group-level verdicts (3-bucket ternary).
VERDICT_PURE_SPORTS = "pure_sports"
VERDICT_MIXED = "mixed"
VERDICT_NOT_SPORTS = "not_sports"
GROUP_VERDICTS = frozenset({VERDICT_PURE_SPORTS, VERDICT_MIXED, VERDICT_NOT_SPORTS})

# Stream-level verdicts (binary, used for mixed-group refinement).
VERDICT_SPORTS = "sports"
STREAM_VERDICTS = frozenset({VERDICT_SPORTS, VERDICT_NOT_SPORTS})

# Settings defaults. plugin.json carries the same values for the static manifest;
# code paths read these so a tower with no PluginConfig row still behaves.
DEFAULT_ACCOUNT_ID = 1
DEFAULT_PROFILE_ID = 1
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_SAMPLES_PER_GROUP = 6

# Default group-name prefixes to strip when rebuilding clean target names.
# Provider-specific. Public users override via the group_rename_strip_prefixes
# setting (comma-separated). The literal "Sports |" / "Sports/" forms are
# AliceXC-style; other providers use "SP-", "SPRT|", etc.
DEFAULT_STRIP_PREFIXES = ("Sports |", "Sports/")

# Auto-pipeline schedule defaults. The STRING form is what plugin.json renders
# in the UI's default field; the TIMES form is what the scheduler thread falls
# back to when parsing fails. A unit test asserts _parse_schedule of the
# STRING produces exactly the TIMES, so they cannot drift silently.
DEFAULT_SCHEDULE_STRING = "0300"
DEFAULT_SCHEDULE_TIMES = ((3, 0),)

# Cross-worker scheduler lock (Redis SET NX EX).
SCHEDULER_LOCK_KEY = "plugins:sports_filter:auto_pipeline:lock"
SCHEDULER_LOCK_TTL_S = 1800

LOGGER_NAME = "plugins.dispatcharr_sports_filter"
