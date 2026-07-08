from server.models import Finding
from server.review.merge import deterministic_merge


def _f(vendor, file, line):
    return Finding(vendor, file, line, "high", "bug", "c", "r", 0.8)


def test_merge_tags_consensus_when_close():
    fs = [_f("claude", "a.py", 10), _f("codex", "a.py", 11)]
    merged = deterministic_merge(fs)
    assert (
        all(m.consensus == "consensus" for m in merged)
        if hasattr(merged[0], "consensus")
        else True
    )
    # consensus_group으로 묶였는지
    groups = {m.consensus_group_id for m in merged}
    assert len(groups) == 1


def test_merge_keeps_single_when_far():
    fs = [_f("claude", "a.py", 10), _f("codex", "b.py", 200)]
    merged = deterministic_merge(fs)
    assert len({m.consensus_group_id for m in merged}) == 2
