from server.context.base import (
    ContextRequest,
    ContextResult,
    redact_secrets,
    render_context,
)


class CompositeContextProvider:
    """활성 프로바이더를 순회·집계하고 실패는 redact 후 ''로 degrade(B-INV-4).
    gather 후 self.results로 소스별 상태를 노출(B2에서 meta 조립에 사용)."""

    def __init__(self, providers, *, redactor=redact_secrets):
        self.providers = list(providers)
        self._redact = redactor
        self.results: list[ContextResult] = []

    def gather(self, *, req: ContextRequest) -> str:
        self.results = []
        for p in self.providers:
            try:
                r = p.fetch(req)
            except Exception as e:  # B-INV-4: degrade + redact
                r = ContextResult(
                    provider=getattr(p, "name", "?"),
                    status="error",
                    error=self._redact(f"{type(e).__name__}: {e}"),
                )
            r = ContextResult(
                provider=self._redact(r.provider),
                status=self._redact(r.status),
                text=self._redact(r.text or ""),
                meta=_redact_value(r.meta, self._redact),
                error=self._redact(r.error) if r.error else None,
            )
            self.results.append(r)
        return render_context(self.results)


def _redact_value(value, redactor):
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, dict):
        return {k: _redact_value(v, redactor) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, redactor) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v, redactor) for v in value)
    return value
