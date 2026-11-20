"""Build the account universe (company-level data only, no people) from free public sources.

Sources
-------
* YC company directory mirror (https://yc-oss.github.io/api/companies/all.json, community
  project yc-oss/api): active B2B companies, 5-500 staff, located in US/UK/CA/AU.
  -> segment ``b2b_saas``.
* Wikidata SPARQL: instances of advertising agency / marketing agency classes with an official
  website and country in US/UK/CA/AU. -> segment ``agency``.

Wikidata is fetched with urllib (httpx is blocked there) and is aggressively rate limited
(1 req/min at times), so the builder makes exactly one query and retries once after 65 s.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .http import USER_AGENT
from .store import Company

YC_URL = "https://yc-oss.github.io/api/companies/all.json"
WDQS_URL = "https://query.wikidata.org/sparql"
TARGET_COUNTRIES = {
    "USA": "US",
    "United States": "US",
    "United Kingdom": "GB",
    "Canada": "CA",
    "Australia": "AU",
}
# Classes (P31): advertising agency, design agency, marketing agency.
# Industries (P452): marketing, advertising, web design, web development, online advertising,
# digital marketing, graphic design, content marketing, social media marketing,
# internet marketing, digital advertising. (QIDs checked against labels on 2026-11-20.)
AGENCY_CLASSES = ["wd:Q216931", "wd:Q1200676", "wd:Q9592701"]
AGENCY_INDUSTRIES = [
    "wd:Q39809",
    "wd:Q37038",
    "wd:Q190637",
    "wd:Q386275",
    "wd:Q624902",
    "wd:Q1323528",
    "wd:Q185925",
    "wd:Q558685",
    "wd:Q267114",
    "wd:Q189507",
    "wd:Q133889823",
]
MAX_EMPLOYEES = 1000  # small/medium only, when Wikidata knows the headcount
WIKIDATA_QUERY = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?c ?cLabel ?site ?country ?emp WHERE {
  VALUES ?country { wd:Q30 wd:Q145 wd:Q16 wd:Q408 }
  { VALUES ?cls { __CLASSES__ } ?c wdt:P31 ?cls . }
  UNION
  { VALUES ?ind { __INDUSTRIES__ } ?c wdt:P452 ?ind . }
  ?c wdt:P856 ?site . ?c wdt:P17 ?country .
  MINUS { ?c wdt:P576 ?dissolved }
  OPTIONAL { ?c wdt:P1128 ?emp }
  OPTIONAL { ?c rdfs:label ?cLabel . FILTER(LANG(?cLabel) = "en") }
}
LIMIT 5000
""".replace("__CLASSES__", " ".join(AGENCY_CLASSES)).replace(
    "__INDUSTRIES__", " ".join(AGENCY_INDUSTRIES)
)
# QLever (Uni Freiburg) mirrors Wikidata and is not rate-limited like WDQS, which was
# throttling to 1 req/min during the build. WDQS stays as fallback.
SPARQL_ENDPOINTS = ["https://qlever.cs.uni-freiburg.de/api/wikidata", WDQS_URL]
WD_COUNTRY = {"Q30": "US", "Q145": "GB", "Q16": "CA", "Q408": "AU"}

Getter = Callable[[str, dict[str, str]], bytes]


def _urllib_get(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with urllib.request.urlopen(req, timeout=90) as r:
        data: bytes = r.read()
        return data


def domain_of(url: str) -> str:
    """Normalise a website URL to a lower-case host without ``www.`` (our account_id)."""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    host = (urllib.parse.urlsplit(u).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def yc_companies(raw: list[dict[str, Any]]) -> list[Company]:
    out: list[Company] = []
    for x in raw:
        if x.get("status") != "Active" or x.get("industry") != "B2B":
            continue
        size = x.get("team_size") or 0
        if not (5 <= int(size) <= 500) or not x.get("website"):
            continue
        locs = str(x.get("all_locations") or "")
        country = ""
        for part in locs.replace(";", ",").split(","):
            p = part.strip()
            if p in TARGET_COUNTRIES:
                country = TARGET_COUNTRIES[p]
                break
        if not country:
            continue
        dom = domain_of(str(x["website"]))
        if not dom:
            continue
        out.append(
            Company(
                account_id=dom,
                name=str(x.get("name", "")),
                website=f"https://{dom}/",
                country=country,
                segment="b2b_saas",
                source="yc-oss",
                team_size=int(size),
                source_id=str(x.get("slug", "")),
            )
        )
    return out


def wikidata_companies(bindings: list[dict[str, Any]]) -> list[Company]:
    out: list[Company] = []
    for b in bindings:
        emp = b.get("emp", {}).get("value")
        try:
            emp_n = int(float(emp)) if emp else None
        except ValueError:
            emp_n = None
        if emp_n is not None and emp_n > MAX_EMPLOYEES:
            continue
        site = b.get("site", {}).get("value", "")
        dom = domain_of(site)
        if not dom:
            continue
        qid = b["c"]["value"].rsplit("/", 1)[-1]
        out.append(
            Company(
                account_id=dom,
                name=b.get("cLabel", {}).get("value", qid),
                website=f"https://{dom}/",
                country=WD_COUNTRY.get(b["country"]["value"].rsplit("/", 1)[-1], ""),
                segment="agency",
                source="wikidata",
                team_size=emp_n,
                source_id=qid,
            )
        )
    return out


def merge(groups: list[list[Company]], limit: int) -> list[Company]:
    """Dedupe on account_id (first source wins); agencies first because they are scarcer."""
    seen: dict[str, Company] = {}
    for g in groups:
        for c in sorted(g, key=lambda c: c.account_id):
            seen.setdefault(c.account_id, c)
    agencies = [c for c in seen.values() if c.segment == "agency"]
    saas = [c for c in seen.values() if c.segment != "agency"]
    return (agencies + saas)[:limit]


def build(
    limit: int = 1000, get: Getter = _urllib_get, log: Callable[[str], None] = print
) -> list[Company]:
    yc_raw = json.loads(get(YC_URL, {"Accept": "application/json"}))
    yc = yc_companies(yc_raw)
    log(f"yc-oss: {len(yc_raw)} companies in directory, {len(yc)} match ICP filter")
    wd: list[Company] = []
    for endpoint in SPARQL_ENDPOINTS:
        url = endpoint + "?format=json&query=" + urllib.parse.quote(WIKIDATA_QUERY)
        try:
            res = json.loads(get(url, {"Accept": "application/sparql-results+json"}))
            wd = wikidata_companies(res["results"]["bindings"])
            log(f"wikidata via {endpoint}: {len(res['results']['bindings'])} rows")
            break
        except (urllib.error.URLError, ValueError, KeyError) as e:
            log(f"wikidata via {endpoint}: failed ({e})")
    log(f"wikidata: {len(wd)} agency rows")
    return merge([wd, yc], limit)
