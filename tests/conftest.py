from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from signal_backtest.http import Fetcher, FetchResult

FIX = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


@dataclass
class FakeFetcher(Fetcher):
    """Offline fetcher: URL -> (status, body, headers). Unknown URLs return 404."""

    routes: dict[str, tuple[int, bytes, dict[str, str]]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def get(
        self, url: str, *, check_robots: bool = False, headers: dict[str, str] | None = None
    ) -> FetchResult:
        self.calls.append(url)
        status, body, hdrs = self.routes.get(url, (404, b"", {}))
        return FetchResult(url, status, body, hdrs, url)


@pytest.fixture
def fake(tmp_path: Path) -> FakeFetcher:
    return FakeFetcher(cache_dir=tmp_path / "cache")
