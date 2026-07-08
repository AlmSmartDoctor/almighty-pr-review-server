from dataclasses import dataclass

from server.models import Finding

LINE_PROXIMITY = 5


@dataclass
class MergedFinding:
    finding: Finding
    consensus: str  # single|consensus
    consensus_group_id: int

    # 편의 위임
    def __getattr__(self, k):
        return getattr(self.finding, k)


def deterministic_merge(findings: list[Finding]) -> list[MergedFinding]:
    """(파일·라인근접·카테고리)로 CONSENSUS/SINGLE 태깅. LLM 미사용."""
    out: list[MergedFinding] = []
    groups: list[list[Finding]] = []
    for f in findings:
        placed = False
        for g in groups:
            h = g[0]
            if (
                h.file == f.file
                and h.category == f.category
                and abs(h.line - f.line) <= LINE_PROXIMITY
            ):
                g.append(f)
                placed = True
                break
        if not placed:
            groups.append([f])
    for gid, g in enumerate(groups):
        vendors = {x.vendor for x in g}
        tag = "consensus" if len(vendors) > 1 else "single"
        for f in g:
            out.append(MergedFinding(f, tag, gid))
    return out
