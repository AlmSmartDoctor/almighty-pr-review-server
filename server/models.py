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
    verify_status: str | None = None  # None|confirmed|refuted (반박 패스)
    verify_rationale: str | None = None
