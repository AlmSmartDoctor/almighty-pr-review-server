import asyncio


class RunnerPool:
    """동시성 = asyncio 세마포어 N. seam: 추후 분산큐로 교체 가능."""

    def __init__(self, limit: int = 2):
        self._sem = asyncio.Semaphore(limit)

    async def run(self, coro_factory):
        async with self._sem:
            return await coro_factory()
