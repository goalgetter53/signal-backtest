"""GitHub org activity via the REST API (api.github.com).

* Org handle discovered from ``github.com/<org>`` links on the homepage.
* Unauthenticated limit is 60 req/h, so without ``GITHUB_TOKEN`` at most ``budget`` orgs are
  queried per run (oldest-checked first is left to the scheduler; we take them in order).
  In GitHub Actions the built-in ``GITHUB_TOKEN`` raises this to 1,000 req/h per repository.
* In the build sandbox api.github.com answered 403 for every non-configured path (proxy
  policy); the status is recorded per org so the gap is visible in the snapshot summary.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta

from ..http import Fetcher
from ..store import Company, Observation

COLLECTOR = "github"
GH_LINK = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})(?:[/\"'?#]|$)"
)
NOT_ORGS = {
    "features",
    "about",
    "pricing",
    "login",
    "sponsors",
    "orgs",
    "topics",
    "marketplace",
    "apps",
    "enterprise",
    "security",
    "site",
    "collections",
    "explore",
    "settings",
}


def find_github_org(html: str) -> str | None:
    counts: dict[str, int] = {}
    for m in GH_LINK.finditer(html):
        org = m.group(1).lower()
        if org not in NOT_ORGS:
            counts[org] = counts.get(org, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else None


def summarise_repos(repos: list[dict[str, object]], now: datetime) -> dict[str, float]:
    cutoff = now - timedelta(days=30)
    pushed = []
    for r in repos:
        p = r.get("pushed_at")
        if isinstance(p, str) and p:
            pushed.append(datetime.fromisoformat(p.replace("Z", "+00:00")))
    return {
        "public_repos_sampled": float(len(repos)),
        "repos_pushed_30d": float(sum(1 for p in pushed if p >= cutoff)),
        "days_since_last_push": float((now - max(pushed)).days) if pushed else -1.0,
    }


async def collect(
    f: Fetcher, companies: list[Company], budget: int | None = None
) -> list[Observation]:
    token = os.environ.get("GITHUB_TOKEN", "")
    budget = budget if budget is not None else (900 if token else 50)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    now = datetime.now(UTC)

    async def org_of(c: Company) -> tuple[Company, str | None]:
        res = await f.get(c.website, check_robots=True)
        return c, find_github_org(res.text()) if res.ok else None

    pairs = await asyncio.gather(*(org_of(c) for c in companies))
    rows: list[Observation] = []
    used = 0
    for c, org in pairs:
        if not org:
            continue
        rows.append(Observation(c.account_id, COLLECTOR, "github_org", org, "homepage-link"))
        if used >= budget:
            continue
        used += 1
        url = f"https://api.github.com/orgs/{org}/repos?sort=pushed&per_page=30"
        res = await f.get(url, headers=headers)
        if res.status == 404:  # personal account rather than an org
            res = await f.get(
                f"https://api.github.com/users/{org}/repos?sort=pushed&per_page=30", headers=headers
            )
        rows.append(
            Observation(
                c.account_id,
                COLLECTOR,
                "github_api_status",
                org,
                "",
                float(res.status),
                json.dumps({"auth": bool(token), "error": res.error}),
            )
        )
        if res.ok:
            data = res.json()
            if isinstance(data, list):
                for k, v in summarise_repos(data, now).items():
                    rows.append(Observation(c.account_id, COLLECTOR, k, org, "", v))
    return rows
