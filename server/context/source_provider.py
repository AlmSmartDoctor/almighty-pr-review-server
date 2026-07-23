from server.context.base import ContextBlock, ContextRequest, ContextResult


_BLOCK_POLICY = {
    "review_rules": (10, False, "authorized_human", "internal", "review_history"),
    "team_feedback": (20, False, "internal", "internal", "review_history"),
    "db_schema": (30, False, "internal", "sensitive", "manifest_only"),
    "graphify": (60, True, "trusted_repo", "internal", "manifest_only"),
}


class SourceBackedProvider:
    """source(req)->str 를 주입받아 렌더하는 공통 provider(db_schema/graphify/
    team_feedback이 공유). 소스 미주입=skipped, 실패=empty. NEVER raises
    (best-effort degrade)."""

    def __init__(self, name: str, *, source=None):
        self.name = name
        self._source = source

    def fetch(self, req: ContextRequest) -> ContextResult:
        if self._source is None:
            return ContextResult(provider=self.name, status="skipped", text="")
        try:
            text = self._source(req) or ""
        except Exception:  # 소스 미도달/오류 → best-effort degrade
            return ContextResult(provider=self.name, status="empty", text="")
        status = "ok" if text.strip() else "empty"
        priority, recoverable, trust, sensitivity, retention = _BLOCK_POLICY.get(
            self.name,
            (99, False, "untrusted_external", "sensitive", "short"),
        )
        return ContextResult(
            provider=self.name,
            status=status,
            text=text,
            blocks=(
                ContextBlock(
                    source=self.name,
                    block_id=f"{self.name}:0",
                    text=text,
                    priority=priority,
                    recoverable_from_repo=recoverable,
                    trust_class=trust,
                    sensitivity=sensitivity,
                    retention=retention,
                    relevant_files=tuple(req.changed_files),
                ),
            ) if text.strip() else (),
        )
