"""Website-change collector: content hash, sitemap page count, Last-Modified, key pages.

Stores only hashes, counts and booleans (never page bodies) so snapshots stay small.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from ..http import Fetcher
from ..store import Company, Observation
from . import guarded

COLLECTOR = "website"
MAX_CHILD_SITEMAPS = 5

_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HREF = re.compile(r"""href=["']([^"'#?]+)""", re.I)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
KEY_PAGES = {
    "pricing": re.compile(r"/(pricing|plans)(/|$)", re.I),
    "careers": re.compile(r"/(careers|jobs|join-us|work-with-us)(/|$)", re.I),
    "blog": re.compile(r"/(blog|insights|resources)(/|$)", re.I),
    "case_studies": re.compile(r"/(case-studies|customers|work|portfolio)(/|$)", re.I),
}


def visible_text(html: str) -> str:
    text = _TAG.sub(" ", _SCRIPT.sub(" ", html))
    return _WS.sub(" ", text).strip()


def content_hash(html: str) -> str:
    """Hash of visible text with digits removed (dates/counters would otherwise churn)."""
    norm = _DIGITS.sub("", visible_text(html).lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def structure_hash(html: str) -> str:
    """Hash of the tag sequence only - changes on redesigns, not on copy edits."""
    tags = re.findall(r"<([a-zA-Z][a-zA-Z0-9-]*)", html[:300_000])
    return hashlib.sha256(" ".join(t.lower() for t in tags).encode()).hexdigest()[:16]


def key_pages_from_links(base: str, html: str) -> dict[str, bool]:
    host = urlsplit(base).hostname or ""
    paths = []
    for href in _HREF.findall(html):
        u = urlsplit(urljoin(base, href))
        if (u.hostname or "").removeprefix("www.") == host.removeprefix("www."):
            paths.append(u.path)
    return {k: any(rx.search(p) for p in paths) for k, rx in KEY_PAGES.items()}


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls)."""
    locs = _LOC.findall(xml)
    if "<sitemapindex" in xml[:2000].lower():
        return [], locs
    return locs, []


async def sitemap_urls(f: Fetcher, origin: str) -> tuple[int | None, list[str]]:
    robots = await f.get(origin + "/robots.txt")
    maps = []
    if robots.ok:
        maps = [
            urljoin(origin + "/", ln.split(":", 1)[1].strip())
            for ln in robots.text().splitlines()
            if ln.lower().startswith("sitemap:")
        ]
    if not maps:
        maps = [origin + "/sitemap.xml"]
    pages: list[str] = []
    seen_any = False
    for sm in maps[:2]:
        res = await f.get(sm)
        if not res.ok:
            continue
        seen_any = True
        found, children = parse_sitemap(res.text())
        pages += found
        for child in children[:MAX_CHILD_SITEMAPS]:
            cres = await f.get(child)
            if cres.ok:
                pages += parse_sitemap(cres.text())[0]
    return (len(set(pages)) if seen_any else None), pages


async def collect_company(f: Fetcher, c: Company) -> list[Observation]:
    aid = c.account_id
    res = await f.get(c.website, check_robots=True)
    rows = [
        Observation(
            aid,
            COLLECTOR,
            "homepage_status",
            "",
            res.final_url[:200],
            float(res.status),
            json.dumps({"error": res.error}) if res.error else "",
        )
    ]
    if not res.ok:
        return rows
    html = res.text()
    title = _TITLE.search(html)
    rows += [
        Observation(aid, COLLECTOR, "content_hash", "", content_hash(html)),
        Observation(aid, COLLECTOR, "structure_hash", "", structure_hash(html)),
        Observation(
            aid,
            COLLECTOR,
            "page_title",
            "",
            _WS.sub(" ", title.group(1)).strip()[:200] if title else "",
        ),
    ]
    if "last-modified" in res.headers:
        rows.append(Observation(aid, COLLECTOR, "last_modified", "", res.headers["last-modified"]))
    parts = urlsplit(res.final_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    n, pages = await sitemap_urls(f, origin)
    if n is not None:
        rows.append(Observation(aid, COLLECTOR, "sitemap_urls", "", "", float(n)))
    linked = key_pages_from_links(res.final_url, html)
    for k, rx in KEY_PAGES.items():
        present = linked[k] or any(rx.search(urlsplit(p).path) for p in pages)
        rows.append(Observation(aid, COLLECTOR, "has_page", k, "", 1.0 if present else 0.0))
    return rows


async def collect(f: Fetcher, companies: list[Company]) -> list[Observation]:
    out = await asyncio.gather(*(guarded(COLLECTOR, c, collect_company(f, c)) for c in companies))
    return [r for rs in out for r in rs]
