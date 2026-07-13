from server.context.base import ContextRequest, ContextResult, redact_secrets


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
            self.results.append(r)
        blocks = [
            f"### {r.provider}\n{r.text}"
            for r in self.results
            if r.status == "ok" and r.text
        ]
        return "\n\n".join(blocks)
