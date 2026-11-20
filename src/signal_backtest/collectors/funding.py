"""Funding-news collector from free sources.

* RSS: TechCrunch venture category + Crunchbase News (both free, public feeds). Feeds only hold
  the latest ~20-50 items, so the value comes from collecting **daily** and accumulating.
* SEC EDGAR Form D "current events" Atom feed. EDGAR requires a descriptive User-Agent with a
  contact address; set ``SEC_USER_AGENT`` (never committed). In the build sandbox www.sec.gov
  answered HTTP 403, which is recorded as an observation rather than hidden.

Matching an item to an account: the account's domain appears in the item, or the company name
appears as a case-sensitive whole word in a title that also contains a funding keyword.
Name matching is low-precision for dictionary-word names; matches carry ``method`` in ``meta``.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ..http import Fetcher
from ..store import Company, Observation

COLLECTOR = "funding"
FEEDS = {
    "techcrunch_venture": "https://techcrunch.com/category/venture/feed/",
    "crunchbase_news": "https://news.crunchbase.com/feed/",
}
EDGAR_FORM_D = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&company=&dateb="
    "&owner=include&start=0&count=100&output=atom"
)
FUNDING_RX = re.compile(
    r"\b(rais(e|es|ed|ing)|funding|seed|series [a-f]\b|round|backed|investment|valuation)", re.I
)
ATOM = "{http://www.w3.org/2005/Atom}"
GENERIC_NAMES_MIN_LEN = 4


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    link: str
    published: str
    body: str


def parse_rss(source: str, xml: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        items.append(
            NewsItem(
                source=source,
                title=(it.findtext("title") or "").strip(),
                link=(it.findtext("link") or "").strip(),
                published=(it.findtext("pubDate") or "").strip(),
                body=(it.findtext("description") or "")[:5000],
            )
        )
    for e in root.iter(f"{ATOM}entry"):  # Atom (EDGAR)
        link_el = e.find(f"{ATOM}link")
        items.append(
            NewsItem(
                source=source,
                title=(e.findtext(f"{ATOM}title") or "").strip(),
                link=link_el.get("href", "") if link_el is not None else "",
                published=(e.findtext(f"{ATOM}updated") or "").strip(),
                body=(e.findtext(f"{ATOM}summary") or "")[:2000],
            )
        )
    return items


def _name_rx(name: str) -> re.Pattern[str] | None:
    core = re.sub(r",?\s+(inc\.?|ltd\.?|llc|limited|corp\.?|co\.?)$", "", name.strip(), flags=re.I)
    if len(core) < GENERIC_NAMES_MIN_LEN:
        return None
    return re.compile(r"(?<![\w-])" + re.escape(core) + r"(?![\w-])")


def match_items(companies: list[Company], items: list[NewsItem]) -> list[Observation]:
    rows: list[Observation] = []
    rxs = [(c, _name_rx(c.name)) for c in companies]
    for it in items:
        is_edgar = it.source == "sec_edgar_form_d"
        if not is_edgar and not FUNDING_RX.search(it.title):
            continue
        hay = f"{it.title} {it.body}"
        for c, rx in rxs:
            method = ""
            if c.account_id in hay.lower():
                method = "domain"
            elif rx is not None and rx.search(it.title):
                method = "name"
            if method:
                rows.append(
                    Observation(
                        c.account_id,
                        COLLECTOR,
                        "funding_news",
                        it.link[:300],
                        it.title[:300],
                        None,
                        json.dumps(
                            {"source": it.source, "published": it.published, "method": method}
                        ),
                    )
                )
    return rows


async def collect(f: Fetcher, companies: list[Company]) -> list[Observation]:
    items: list[NewsItem] = []
    rows: list[Observation] = []
    for src, url in FEEDS.items():
        res = await f.get(url)
        parsed = parse_rss(src, res.text()) if res.ok else []
        items += parsed
        rows.append(
            Observation(
                "_source",
                COLLECTOR,
                "feed_status",
                src,
                url,
                float(res.status),
                json.dumps({"items": len(parsed)}),
            )
        )
    ua = os.environ.get("SEC_USER_AGENT", "")
    res = await f.get(EDGAR_FORM_D, headers={"User-Agent": ua} if ua else None)
    parsed = parse_rss("sec_edgar_form_d", res.text()) if res.ok else []
    items += parsed
    rows.append(
        Observation(
            "_source",
            COLLECTOR,
            "feed_status",
            "sec_edgar_form_d",
            EDGAR_FORM_D,
            float(res.status),
            json.dumps({"items": len(parsed), "ua_configured": bool(ua)}),
        )
    )
    funding_items = [
        i for i in items if i.source == "sec_edgar_form_d" or FUNDING_RX.search(i.title)
    ]
    rows.append(
        Observation("_source", COLLECTOR, "funding_items_seen", "", "", float(len(funding_items)))
    )
    return rows + match_items(companies, items)
