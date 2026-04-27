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

# LLM providers. The active provider is INFERRED from the chosen model's prefix
# rather than a separate select field — the model ID already determines which
# wire shape to speak, so a separate provider field would just create a way
# for the two to drift. The MODEL_PREFIX_PROVIDER tuple is ordered
# longest-prefix-first so a future shorter prefix can't shadow a longer one.
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_GEMINI = "gemini"
PROVIDERS = frozenset({PROVIDER_ANTHROPIC, PROVIDER_OPENAI, PROVIDER_GEMINI})

MODEL_PREFIX_PROVIDER = (
    ("claude-", PROVIDER_ANTHROPIC),
    ("gpt-", PROVIDER_OPENAI),
    ("o1-", PROVIDER_OPENAI),
    ("o3-", PROVIDER_OPENAI),
    ("o4-", PROVIDER_OPENAI),
    ("gemini-", PROVIDER_GEMINI),
)

# Per-provider settings field names (UI password input) and on-disk fallback
# filenames. The on-disk pattern existed before multi-provider support landed
# (anthropic_api_key file alongside the plugin); we extend it symmetrically so
# users who already use the file-fallback workflow get the same affordance for
# OpenAI / Gemini.
PROVIDER_SETTINGS_FIELD = {
    PROVIDER_ANTHROPIC: "anthropic_api_key",
    PROVIDER_OPENAI: "openai_api_key",
    PROVIDER_GEMINI: "gemini_api_key",
}
PROVIDER_KEY_FILE = {
    PROVIDER_ANTHROPIC: "anthropic_api_key",
    PROVIDER_OPENAI: "openai_api_key",
    PROVIDER_GEMINI: "gemini_api_key",
}

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
