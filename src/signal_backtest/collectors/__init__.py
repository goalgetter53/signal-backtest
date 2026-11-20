"""Signal collectors. Each returns Observations; the store stamps them with the run time."""

from __future__ import annotations

from collections.abc import Awaitable

from ..store import Company, Observation


async def guarded(
    collector: str, c: Company, work: Awaitable[list[Observation]]
) -> list[Observation]:
    """Run one company's collection; an unexpected error becomes an observation, not a crash."""
    try:
        return await work
    except Exception as e:
        return [
            Observation(c.account_id, collector, "collector_error", type(e).__name__, str(e)[:200])
        ]
