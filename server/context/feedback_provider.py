from server.context.base import ContextRequest, ContextResult


class FeedbackContextProvider:
    """이 레포의 과거 리뷰에서 사람이 내린 finding 판단(승인/기각/수정)을 요약해 리뷰
    프롬프트에 주입하는 provider. 학습 신호는 이미 finding 테이블에 durable하게 있으므로
    별도 저장소 없이 읽어서 렌더한다. feedback_source(req)->str 를 주입받는다.
    소스 미주입=skipped, 실패=empty. NEVER raises (best-effort degrade)."""

    name = "team_feedback"

    def __init__(self, *, feedback_source=None):
        self._source = feedback_source

    def fetch(self, req: ContextRequest) -> ContextResult:
        if self._source is None:
            return ContextResult(provider=self.name, status="skipped", text="")
        try:
            text = self._source(req) or ""
        except Exception:  # 소스 미도달/오류 → best-effort degrade
            return ContextResult(provider=self.name, status="empty", text="")
        status = "ok" if text.strip() else "empty"
        return ContextResult(provider=self.name, status=status, text=text)
