from dataclasses import dataclass


@dataclass
class Finding:
    vendor: str
    file: str
    line: int
    severity: str  # critical|high|medium|low
    category: str  # bug|security|perf|style|other
    claim: str
    rationale: str
    confidence: float
    vendor_result_id: int | None = None  # ★개정: 병합 후에도 벤더 추적성 유지
    verify_status: str | None = None  # None|confirmed|refuted|contested (디베이트 패스)
    verify_rationale: str | None = None
    verify_independent: bool | None = None
    verify_evidence_status: str | None = None
    source_chunk_index: int | None = None
    owner_chunk_index: int | None = None
    scope_status: str | None = None  # owned|reassigned|would_reject|rejected
    posting_eligible: bool = True
    duplicate_group_id: int | None = None
    duplicate_suggested: bool = False
