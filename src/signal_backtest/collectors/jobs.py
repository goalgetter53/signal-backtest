"""Job-post collector: Greenhouse, Lever, Ashby and Workable public JSON boards.

Board discovery (per company):
1. ``link``  - ATS URLs found on the homepage or the first careers/jobs page it links to.
2. ``guess`` - the domain stem (``acme.com`` -> ``acme``) probed on Greenhouse, Lever and Ashby.
   Guesses can collide with a different company that owns the same token; they are flagged
   ``method=guess`` in ``meta`` so the backtest can exclude them.
Workable is only queried for linked boards (its API rate-limits aggressively).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from ..http import Fetcher, FetchResult
from ..store import Company, Observation
from . import guarded

COLLECTOR = "jobs"

ATS_LINK_RES: list[tuple[str, re.Pattern[str]]] = [
    (
        "greenhouse",
        re.compile(
            r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/"
            r"(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"
        ),
    ),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
]
IGNORED_TOKENS = {"embed", "api", "v1", "jobs", "j", "careers", "js", "static", "assets"}

CAREERS_HREF = re.compile(
    r"""href=["']([^"'#]*?(?:careers|jobs|join-us|work-with-us|hiring)[^"'#]*)["']""", re.I
)

ROLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "web": re.compile(
        r"front[- ]?end|web (?:developer|engineer|designer|producer)|full[- ]?stack|webflow|"
        r"wordpress|shopify (?:developer|engineer)|ui engineer|ux engineer|website",
        re.I,
    ),
    "marketing": re.compile(
        r"marketing|growth|demand gen|\bseo\b|content (?:writer|strategist|lead|manager)|brand|"
        r"communications|\bpr\b|social media",
        re.I,
    ),
    "design": re.compile(r"designer|\bux\b|\bui\b|creative director", re.I),
}


@dataclass(frozen=True)
class Job:
    ats: str
    token: str
    job_id: str
    title: str
    department: str
    location: str
    posted_at: str


def classify(title: str, department: str = "") -> list[str]:
    text = f"{title} {department}"
    return [k for k, p in ROLE_PATTERNS.items() if p.search(text)]


def find_ats_links(html: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for ats, rx in ATS_LINK_RES:
        for m in rx.finditer(html):
            tok = m.group(1).strip("/.").lower()
            if tok and tok not in IGNORED_TOKENS:
                found.add((ats, tok))
    return found


def find_careers_url(base_url: str, html: str) -> str | None:
    for m in CAREERS_HREF.finditer(html):
        href = m.group(1)
        if href.startswith(("mailto:", "javascript:")):
            continue
        return str(urljoin(base_url, href))
    return None


def domain_stem(account_id: str) -> str:
    return account_id.split(".")[0]


MAX_LINKED_BOARDS = 3


def filter_aggregator_links(
    account_id: str, links: set[tuple[str, str]]
) -> tuple[set[tuple[str, str]], bool]:
    """Pages linking many boards (job aggregators, customer showcases, VC portfolios) are not
    the company's own board. Keep only tokens that resemble the domain; else drop them all."""
    if len(links) <= MAX_LINKED_BOARDS:
        return links, False
    stem = domain_stem(account_id).replace("-", "")
    own = {(a, t) for a, t in links if stem in t.replace("-", "") or t.replace("-", "") in stem}
    return own, True


def board_url(ats: str, token: str) -> str:
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{token}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{token}",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{token}",
    }[ats]


def _s(x: object) -> str:
    return "" if x is None else str(x)


def parse_board(ats: str, token: str, payload: object) -> list[Job]:
    jobs: list[Job] = []
    if ats == "greenhouse" and isinstance(payload, dict):
        for j in payload.get("jobs", []):
            jobs.append(
                Job(
                    ats,
                    token,
                    _s(j.get("id")),
                    _s(j.get("title")),
                    _s(j["departments"][0].get("name") if j.get("departments") else ""),
                    _s((j.get("location") or {}).get("name")),
                    _s(j.get("first_published") or j.get("updated_at")),
                )
            )
    elif ats == "lever" and isinstance(payload, list):
        for j in payload:
            cat = j.get("categories") or {}
            jobs.append(
                Job(
                    ats,
                    token,
                    _s(j.get("id")),
                    _s(j.get("text")),
                    _s(cat.get("department") or cat.get("team")),
                    _s(cat.get("location")),
                    _s(j.get("createdAt")),
                )
            )
    elif ats == "ashby" and isinstance(payload, dict):
        for j in payload.get("jobs", []):
            if j.get("isListed") is False:
                continue
            jobs.append(
                Job(
                    ats,
                    token,
                    _s(j.get("id")),
                    _s(j.get("title")),
                    _s(j.get("department")),
                    _s(j.get("location")),
                    _s(j.get("publishedAt")),
                )
            )
    elif ats == "workable" and isinstance(payload, dict):
        for j in payload.get("jobs", []):
            loc = ", ".join(x for x in (_s(j.get("city")), _s(j.get("country"))) if x)
            jobs.append(
                Job(
                    ats,
                    token,
                    _s(j.get("shortcode")),
                    _s(j.get("title")),
                    _s(j.get("department")),
                    loc,
                    _s(j.get("published_on") or j.get("created_at")),
                )
            )
    return jobs


def board_observations(
    company: Company, ats: str, token: str, method: str, jobs: list[Job]
) -> list[Observation]:
    aid = company.account_id
    meta = json.dumps({"ats": ats, "token": token, "method": method})
    rows = [Observation(aid, COLLECTOR, "ats_board", f"{ats}:{token}", method, len(jobs), meta)]
    counts = {k: 0 for k in ROLE_PATTERNS}
    for j in jobs:
        cats = classify(j.title, j.department)
        for c in cats:
            counts[c] += 1
        rows.append(
            Observation(
                aid,
                COLLECTOR,
                "job_posting",
                f"{ats}:{j.job_id}",
                j.title[:200],
                None,
                json.dumps(
                    {
                        "dept": j.department[:80],
                        "loc": j.location[:80],
                        "posted_at": j.posted_at,
                        "roles": cats,
                        "method": method,
                    }
                ),
            )
        )
    rows.append(Observation(aid, COLLECTOR, "open_roles_total", ats, "", float(len(jobs)), meta))
    for k, v in counts.items():
        rows.append(Observation(aid, COLLECTOR, f"open_roles_{k}", ats, "", float(v), meta))
    return rows


async def _fetch_board(f: Fetcher, ats: str, token: str) -> tuple[FetchResult, list[Job]]:
    res = await f.get(board_url(ats, token))
    if not res.ok:
        return res, []
    try:
        return res, parse_board(ats, token, res.json())
    except (ValueError, AttributeError, TypeError):
        return res, []


async def collect_company(f: Fetcher, c: Company, guess: bool = True) -> list[Observation]:
    home = await f.get(c.website, check_robots=True)
    links: set[tuple[str, str]] = set()
    if home.ok:
        html = home.text()
        links |= find_ats_links(html)
        if not links:
            careers = find_careers_url(home.final_url, html)
            if careers:
                page = await f.get(careers, check_robots=True)
                if page.ok:
                    links |= find_ats_links(page.text())
                    links |= find_ats_links(page.final_url)
    links, aggregator = filter_aggregator_links(c.account_id, links)
    candidates: list[tuple[str, str, str]] = [(a, t, "link") for a, t in sorted(links)]
    if not candidates and guess and not aggregator:
        stem = domain_stem(c.account_id)
        candidates = [(a, stem, "guess") for a in ("greenhouse", "lever", "ashby")]
    rows: list[Observation] = []
    for ats, token, method in candidates:
        res, jobs = await _fetch_board(f, ats, token)
        if not res.ok:
            continue
        if method == "guess" and not jobs:
            continue  # empty guessed board says nothing about this company
        rows += board_observations(c, ats, token, method, jobs)
    if not rows:
        rows.append(Observation(c.account_id, COLLECTOR, "ats_board", "", "none", 0.0, ""))
    return rows


async def collect(f: Fetcher, companies: list[Company], guess: bool = True) -> list[Observation]:
    results = await asyncio.gather(
        *(guarded(COLLECTOR, c, collect_company(f, c, guess)) for c in companies)
    )
    return [r for rs in results for r in rs]
