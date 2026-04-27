"""Pin the verdict-string contract. A typo here is silent at runtime
(cache-load filter would just drop the entry) so test the wire shape."""

from dispatcharr_sports_filter.constants import (
    GROUP_VERDICTS,
    STREAM_VERDICTS,
    VERDICT_MIXED,
    VERDICT_NOT_SPORTS,
    VERDICT_PURE_SPORTS,
    VERDICT_SPORTS,
)


def test_group_verdict_wire_strings():
    assert VERDICT_PURE_SPORTS == "pure_sports"
    assert VERDICT_MIXED == "mixed"
    assert VERDICT_NOT_SPORTS == "not_sports"
    assert GROUP_VERDICTS == {"pure_sports", "mixed", "not_sports"}


def test_stream_verdict_wire_strings():
    assert VERDICT_SPORTS == "sports"
    assert STREAM_VERDICTS == {"sports", "not_sports"}


def test_not_sports_string_shared_across_group_and_stream():
    """The literal 'not_sports' is used both as a group-level and stream-level
    verdict. Document that overlap so a future refactor doesn't accidentally
    fork the spelling."""
    assert VERDICT_NOT_SPORTS in GROUP_VERDICTS
    assert VERDICT_NOT_SPORTS in STREAM_VERDICTS
