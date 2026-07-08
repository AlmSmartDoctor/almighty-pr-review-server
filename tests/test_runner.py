import asyncio

from server.review.runner import RunnerPool


def test_semaphore_limits_concurrency():
    pool = RunnerPool(limit=2)
    active, peak = 0, 0

    async def job():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    async def main():
        return await asyncio.gather(*(pool.run(job) for _ in range(6)))

    results = asyncio.run(main())
    assert results == ["ok"] * 6
    assert (
        peak == 2
    )  # limit=2 → exactly two jobs overlap (proves the cap AND real parallelism)
