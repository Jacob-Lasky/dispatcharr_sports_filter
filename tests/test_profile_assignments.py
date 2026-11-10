"""A scheduled apply must retain independently curated profile assignments."""

import sys
from types import SimpleNamespace

import pytest

from dispatcharr_sports_filter import plugin
from dispatcharr_sports_filter.constants import VERDICT_MIXED, VERDICT_PURE_SPORTS


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("verdict", [VERDICT_PURE_SPORTS, VERDICT_MIXED])
def test_apply_preserves_additional_group_profiles(monkeypatch, dry_run, verdict):
    class Relation:
        def __init__(self, name, profile_ids):
            self.channel_group = SimpleNamespace(name=name)
            self.auto_channel_sync = True
            self.custom_properties = {"channel_profile_ids": profile_ids}
            self.saves = 0

        def save(self, update_fields):
            self.saves += 1

    curated = Relation("Sports | NFL", [1, 21])
    other = Relation("Sports | Other", [1])

    class Relations(list):
        def filter(self, **kwargs):
            return self

        def select_related(self, *args):
            return self

    relations = Relations([curated, other])
    monkeypatch.setitem(sys.modules, "apps.channels.models", SimpleNamespace(
        ChannelGroupM3UAccount=SimpleNamespace(objects=relations),
    ))
    monkeypatch.setattr(plugin, "_read_group_cache", lambda: {
        r.channel_group.name: verdict for r in relations
    })
    monkeypatch.setattr(plugin, "_read_stream_cache", lambda: {})
    monkeypatch.setattr(plugin, "_gather_streams_for_group", lambda *args: [])
    settings = {"channel_profile_id": 1, "dry_run": dry_run, "apply_group_rename": False}

    assert plugin._action_apply(settings)["status"] == "ok"
    assert curated.custom_properties["channel_profile_ids"] == [1, 21]
    assert other.custom_properties["channel_profile_ids"] == [1]
    if dry_run:
        assert curated.custom_properties == {"channel_profile_ids": [1, 21]}
        assert curated.saves == other.saves == 0
    saved = curated.saves + other.saves
    assert plugin._action_apply(settings)["status"] == "ok"
    assert curated.custom_properties["channel_profile_ids"] == [1, 21]
    assert curated.saves + other.saves == saved


@pytest.mark.parametrize("existing,expected", [
    (None, [1]),
    ([], [1]),
    ([21, "21", 1, "2"], [1, 21, 2]),
    ("21", [1]),
    ([None, {}, "invalid", 0, -1, float("inf")], [1]),
])
def test_profile_id_compatibility(existing, expected):
    assert plugin._merge_profile_ids(1, existing) == expected
