import time
from concurrent.futures import ThreadPoolExecutor, wait

from server import config
from server.context.base import (
    ContextRequest,
    ContextResult,
    redact_secrets,
    render_context,
)


# timeout 난 fetch가 즉시 중단될 수 없는 Python thread 특성상 호출별 executor를 만들면
# 반복 timeout 때 thread가 누적된다. 프로세스 공용 상한으로 격리한다.
_CONTEXT_EXECUTOR = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="review-context"
)


class CompositeContextProvider:
    """활성 프로바이더를 순회·집계하고 실패는 redact 후 ''로 degrade(B-INV-4).
    gather 후 self.results로 소스별 상태를 노출(B2에서 meta 조립에 사용)."""

    def __init__(
        self, providers, *, redactor=redact_secrets, provider_timeout: float | None = None
    ):
        self.providers = list(providers)
        self._redact = redactor
        # pipeline의 바깥 timeout보다 먼저 반환해 완료된 소스 결과를 저장할 여유를 둔다.
        self._provider_timeout = (
            provider_timeout
            if provider_timeout is not None
            else max(0.1, config.CONTEXT_GATHER_TIMEOUT_SEC - 1)
        )
        self.results: list[ContextResult] = []

    def gather(self, *, req: ContextRequest) -> str:
        if not self.providers:
            self.results = []
            return ""

        # 독립 소스를 병렬 수집한다. 느린 원격 소스 하나 때문에 static 등 이미 끝난
        # 배경지식까지 바깥 timeout에서 통째로 버려지지 않게 source 단위로 degrade한다.
        futures = [
            _CONTEXT_EXECUTOR.submit(self._fetch, provider, req)
            for provider in self.providers
        ]
        done, unfinished = wait(futures, timeout=self._provider_timeout)
        results = []
        for provider, future in zip(self.providers, futures):
            if future not in done:
                result = ContextResult(
                    provider=getattr(provider, "name", "?"),
                    status="error",
                    error="context source timed out",
                    meta={"duration_ms": int(self._provider_timeout * 1000)},
                )
            else:
                result = future.result()
            results.append(self._sanitize(result))
        # 아직 queue에서 시작하지 않은 fetch는 제거한다. 실행 중 fetch는 local result만
        # 반환하고 공용 executor 상한 안에 남으므로 self.results나 thread 수를 늘리지 못한다.
        for future in unfinished:
            future.cancel()
        self.results = results
        return render_context(results, max_total_chars=req.max_context_chars)

    @staticmethod
    def _fetch(provider, req: ContextRequest) -> ContextResult:
        started_at = time.monotonic()
        try:
            result = provider.fetch(req)
            if not isinstance(result, ContextResult):
                raise TypeError("context provider must return ContextResult")
            meta = dict(result.meta or {})
            meta["duration_ms"] = int((time.monotonic() - started_at) * 1000)
            return ContextResult(
                provider=result.provider,
                status=result.status,
                text=result.text,
                meta=meta,
                error=result.error,
                blocks=result.blocks,
            )
        except Exception as exc:  # B-INV-4: malformed result도 source 하나만 degrade
            return ContextResult(
                provider=getattr(provider, "name", "?"),
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                meta={
                    "duration_ms": int((time.monotonic() - started_at) * 1000)
                },
            )

    def _sanitize(self, result: ContextResult) -> ContextResult:
        return ContextResult(
            provider=self._redact(result.provider),
            status=self._redact(result.status),
            text=self._redact(result.text or ""),
            meta=_redact_value(result.meta or {}, self._redact),
            error=self._redact(result.error) if result.error else None,
            blocks=tuple(
                type(block)(
                    source=self._redact(block.source),
                    block_id=self._redact(block.block_id),
                    text=self._redact(block.text),
                    priority=block.priority,
                    recoverable_from_repo=block.recoverable_from_repo,
                    trust_class=self._redact(block.trust_class),
                    sensitivity=self._redact(block.sensitivity),
                    retention=self._redact(block.retention),
                    relevant_files=tuple(
                        self._redact(path) for path in block.relevant_files
                    ),
                )
                for block in result.blocks
            ),
        )


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
