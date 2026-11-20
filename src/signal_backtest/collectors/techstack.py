"""Tech-stack detection on the homepage with an open-source fingerprint set.

Fingerprints: https://github.com/enthec/webappanalyzer (community fork of the formerly-MIT
Wappalyzer technology definitions; the fork is licensed GPL-3.0). To keep this repo MIT, the
JSON files are **downloaded at run time into the cache directory and never committed or
redistributed** (see docs/adr/0003-fingerprint-licence.md). Tests use a tiny hand-written
fixture instead.

Only categories relevant to "may need website help" are evaluated, which keeps the regex
workload small enough for 1,000 homepages on a 2-CPU runner.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

from ..http import Fetcher
from ..store import Company, Observation
from . import guarded

COLLECTOR = "techstack"
FP_BASE = "https://raw.githubusercontent.com/enthec/webappanalyzer/main/src"
FP_FILES = ["_", *"abcdefghijklmnopqrstuvwxyz"]
# CMS, Ecommerce, Analytics, JS frameworks, Web frameworks, Marketing automation, Tag managers,
# Page builders, Live chat, CRM, SEO, Static site generator, PaaS, A/B testing, Personalisation,
# CDP, Form builders, Hosting, Appointment scheduling, Ecommerce frontends, Blogs
RELEVANT_CATS = {1, 6, 10, 11, 12, 18, 32, 42, 51, 52, 53, 54, 57, 62, 72, 74, 76, 88, 97, 108, 110}
HTML_LIMIT = 300_000

TECH_GROUPS: dict[str, set[str]] = {
    "legacy_builder": {
        "Wix",
        "Squarespace",
        "Weebly",
        "GoDaddy Website Builder",
        "Jimdo",
        "Duda",
        "Site123",
        "Strikingly",
    },
    "modern_builder": {
        "Webflow",
        "Framer",
        "Next.js",
        "Gatsby",
        "Contentful",
        "Sanity",
        "Storyblok",
        "Prismic",
        "Nuxt.js",
        "Astro",
        "Hygraph",
    },
    "wordpress": {"WordPress"},
    "marketing_stack": {
        "HubSpot",
        "Marketo",
        "Pardot",
        "Intercom",
        "Drift",
        "Segment",
        "Google Tag Manager",
        "Hotjar",
        "Optimizely",
        "VWO",
        "Clearbit",
        "HubSpot CMS Hub",
        "Mailchimp",
        "ActiveCampaign",
        "Klaviyo",
    },
}


def _as_list(v: object) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def _compile(pattern: str) -> re.Pattern[str] | None:
    rx = pattern.split("\\;")[0]
    try:
        return re.compile(rx, re.I)
    except re.error:
        return None


@dataclass
class Tech:
    name: str
    cats: list[int]
    html: list[re.Pattern[str]] = field(default_factory=list)
    script_src: list[re.Pattern[str]] = field(default_factory=list)
    meta: dict[str, re.Pattern[str]] = field(default_factory=dict)
    headers: dict[str, re.Pattern[str]] = field(default_factory=dict)
    cookies: dict[str, re.Pattern[str]] = field(default_factory=dict)
    implies: list[str] = field(default_factory=list)


def _pats(v: object) -> list[re.Pattern[str]]:
    return [p for p in (_compile(x) for x in _as_list(v)) if p is not None]


def _pmap(v: object) -> dict[str, re.Pattern[str]]:
    out: dict[str, re.Pattern[str]] = {}
    if isinstance(v, dict):
        for k, pat in v.items():
            pats = _pats(pat)
            if pats:
                out[str(k).lower()] = pats[0]
    return out


def load_fingerprints(raw: dict[str, dict[str, object]], all_cats: bool = False) -> list[Tech]:
    techs: list[Tech] = []
    for name, d in raw.items():
        cats = [int(c) for c in _as_list(d.get("cats"))]
        if not all_cats and not RELEVANT_CATS.intersection(cats):
            continue
        techs.append(
            Tech(
                name=name,
                cats=cats,
                html=_pats(d.get("html")),
                script_src=_pats(d.get("scriptSrc")),
                meta=_pmap(d.get("meta")),
                headers=_pmap(d.get("headers")),
                cookies=_pmap(d.get("cookies")),
                implies=[x.split("\\;")[0] for x in _as_list(d.get("implies"))],
            )
        )
    return techs


async def fetch_fingerprints(f: Fetcher) -> tuple[dict[str, dict[str, object]], str]:
    """Download the fingerprint JSON (cached). Returns (definitions, sha256 of the set)."""
    raw: dict[str, dict[str, object]] = {}
    h = hashlib.sha256()
    for name in FP_FILES:
        res = await f.get(f"{FP_BASE}/technologies/{name}.json")
        if res.ok:
            h.update(res.body)
            raw.update(json.loads(res.body))
    return raw, h.hexdigest()[:12]


SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
META_TAG = re.compile(r"<meta\s[^>]*>", re.I)
META_NAME = re.compile(r"""(?:name|property)=["']([^"']+)["']""", re.I)
META_CONTENT = re.compile(r"""content=["']([^"']*)["']""", re.I)


def detect(techs: list[Tech], html: str, headers: dict[str, str]) -> list[str]:
    html = html[:HTML_LIMIT]
    scripts = SCRIPT_SRC.findall(html)
    metas: dict[str, str] = {}
    for tag in META_TAG.findall(html):
        n, c = META_NAME.search(tag), META_CONTENT.search(tag)
        if n and c:
            metas[n.group(1).lower()] = c.group(1)
    cookie_names = {
        part.split("=", 1)[0].strip().lower()
        for part in headers.get("set-cookie", "").replace("\n", ",").split(",")
        if "=" in part
    }
    found: set[str] = set()
    for t in techs:
        hit = any(p.search(s) for p in t.script_src for s in scripts)
        hit = hit or any(k in metas and p.search(metas[k]) for k, p in t.meta.items())
        hit = hit or any(k in headers and p.search(headers[k]) for k, p in t.headers.items())
        hit = hit or any(k in cookie_names for k in t.cookies)
        hit = hit or any(p.search(html) for p in t.html)
        if hit:
            found.add(t.name)
    by_name = {t.name: t for t in techs}
    queue = list(found)
    while queue:  # resolve "implies" (only within the evaluated set)
        tech = by_name.get(queue.pop())
        for imp in tech.implies if tech else []:
            if imp in by_name and imp not in found:
                found.add(imp)
                queue.append(imp)
    return sorted(found)


def groups_for(techs: list[str]) -> list[str]:
    s = set(techs)
    return sorted(g for g, names in TECH_GROUPS.items() if s & names)


async def collect(
    f: Fetcher, companies: list[Company], techs: list[Tech], fp_version: str
) -> list[Observation]:
    async def one(c: Company) -> list[Observation]:
        res = await f.get(c.website, check_robots=True)
        if not res.ok:
            return [
                Observation(
                    c.account_id,
                    COLLECTOR,
                    "tech_scan",
                    "",
                    "fail",
                    float(res.status),
                    json.dumps({"error": res.error}),
                )
            ]
        found = await asyncio.to_thread(detect, techs, res.text(), res.headers)
        cats = {t.name: t.cats for t in techs}
        rows = [
            Observation(
                c.account_id,
                COLLECTOR,
                "tech_scan",
                "",
                "ok",
                float(len(found)),
                json.dumps({"fp_version": fp_version}),
            )
        ]
        rows += [
            Observation(
                c.account_id,
                COLLECTOR,
                "tech",
                name,
                "",
                None,
                json.dumps({"cats": cats.get(name, [])}),
            )
            for name in found
        ]
        rows += [
            Observation(c.account_id, COLLECTOR, "tech_group", g, "", 1.0, "")
            for g in groups_for(found)
        ]
        return rows

    out = await asyncio.gather(*(guarded(COLLECTOR, c, one(c)) for c in companies))
    return [r for rs in out for r in rs]
