from __future__ import annotations

import json

from conftest import FakeFetcher, fixture_bytes

from signal_backtest.collectors import funding, github, jobs, techstack, website
from signal_backtest.store import Company

ACME = Company("acmestudio.com", "Acme Studio", "https://acmestudio.com/", "US", "agency", "test")


def test_find_ats_links_and_classify() -> None:
    html = (
        '<a href="https://boards.greenhouse.io/acmestudio">x</a>'
        '<a href="https://jobs.lever.co/acme/123">y</a>'
        '<a href="https://jobs.ashbyhq.com/Acme">z</a>'
        '<a href="https://apply.workable.com/acme-co/">w</a>'
        '<script src="https://boards.greenhouse.io/embed/job_board?for=acmestudio"></script>'
    )
    assert jobs.find_ats_links(html) == {
        ("greenhouse", "acmestudio"),
        ("lever", "acme"),
        ("ashby", "acme"),
        ("workable", "acme-co"),
    }
    assert jobs.classify("Senior Frontend Engineer") == ["web"]
    assert jobs.classify("Growth Marketing Manager") == ["marketing"]
    assert jobs.classify("Web Designer") == ["web", "design"]
    assert jobs.classify("Account Executive") == []


def test_aggregator_pages_do_not_count_as_own_board() -> None:
    many = {("ashby", f"client{i}") for i in range(10)}
    kept, agg = jobs.filter_aggregator_links("cargo.io", many | {("ashby", "cargo")})
    assert agg and kept == {("ashby", "cargo")}
    assert jobs.filter_aggregator_links("x.io", {("lever", "x")}) == ({("lever", "x")}, False)


def test_parse_all_boards() -> None:
    gh = jobs.parse_board("greenhouse", "t", json.loads(fixture_bytes("greenhouse.json")))
    lv = jobs.parse_board("lever", "t", json.loads(fixture_bytes("lever.json")))
    ab = jobs.parse_board("ashby", "t", json.loads(fixture_bytes("ashby.json")))
    wk = jobs.parse_board("workable", "t", json.loads(fixture_bytes("workable.json")))
    assert [len(gh), len(lv), len(ab), len(wk)] == [3, 1, 1, 1]  # unlisted ashby job dropped
    assert gh[0].department == "Engineering" and wk[0].location == "Sydney, Australia"


async def test_jobs_collect_link_beats_guess(fake: FakeFetcher) -> None:
    fake.routes = {
        ACME.website: (200, fixture_bytes("homepage_webflow.html"), {}),
        jobs.board_url("greenhouse", "acmestudio"): (200, fixture_bytes("greenhouse.json"), {}),
    }
    rows = await jobs.collect(fake, [ACME])
    by = {(r.signal_type, r.key): r for r in rows}
    assert by[("open_roles_total", "greenhouse")].num == 3
    assert by[("open_roles_web", "greenhouse")].num == 1
    assert by[("open_roles_marketing", "greenhouse")].num == 1
    assert by[("ats_board", "greenhouse:acmestudio")].value == "link"
    assert not any("lever" in c for c in fake.calls)  # no guessing once a link is found


async def test_jobs_guess_ignores_empty_boards(fake: FakeFetcher) -> None:
    fake.routes = {
        ACME.website: (200, b"<html>no links</html>", {}),
        jobs.board_url("ashby", "acmestudio"): (200, b'{"jobs": []}', {}),
    }
    rows = await jobs.collect(fake, [ACME])
    assert [(r.signal_type, r.value) for r in rows] == [("ats_board", "none")]


def test_techstack_detect_and_groups() -> None:
    raw = json.loads(fixture_bytes("fingerprints.json"))
    techs = techstack.load_fingerprints(raw, all_cats=True)
    assert "Broken" in {t.name for t in techs}  # bad regex skipped, tech kept
    wix = techstack.detect(techs, fixture_bytes("homepage_wix.html").decode(), {})
    assert wix == ["React", "Wix"]  # React via "implies"
    wf = techstack.detect(
        techs, fixture_bytes("homepage_webflow.html").decode(), {"server": "nginx/1.25"}
    )
    assert wf == ["HubSpot", "Nginx", "Webflow"]
    assert techstack.groups_for(wf) == ["marketing_stack", "modern_builder"]
    relevant = techstack.load_fingerprints(raw)
    assert "Nginx" not in {t.name for t in relevant}  # web servers not in relevant cats


def test_website_hashes() -> None:
    a = "<html><body><p>Hello 2025</p><script>x=1</script></body></html>"
    b = "<html><body><p>Hello 2026</p><script>x=2</script></body></html>"
    c = "<html><body><div><p>Hello 2026</p></div></body></html>"
    assert website.content_hash(a) == website.content_hash(b)  # digits & scripts ignored
    assert website.structure_hash(a) == website.structure_hash(b)
    assert website.structure_hash(a) != website.structure_hash(c)


async def test_website_collect(fake: FakeFetcher) -> None:
    fake.routes = {
        ACME.website: (200, fixture_bytes("homepage_wix.html"), {"last-modified": "Tue"}),
        "https://acmestudio.com/robots.txt": (
            200,
            b"User-agent: *\nSitemap: https://acmestudio.com/sitemap.xml\n",
            {},
        ),
        "https://acmestudio.com/sitemap.xml": (200, fixture_bytes("sitemap_index.xml"), {}),
        "https://acmestudio.com/sitemap-pages.xml": (200, fixture_bytes("sitemap_pages.xml"), {}),
    }
    rows = await website.collect(fake, [ACME])
    by = {(r.signal_type, r.key): r for r in rows}
    assert by[("sitemap_urls", "")].num == 3
    assert by[("has_page", "pricing")].num == 1.0
    assert by[("has_page", "careers")].num == 1.0
    assert by[("has_page", "blog")].num == 1.0  # from sitemap, not homepage links
    assert by[("has_page", "case_studies")].num == 0.0
    assert by[("page_title", "")].value == "Acme Studio | Home"


async def test_funding_matching(fake: FakeFetcher) -> None:
    fake.routes = {funding.FEEDS["techcrunch_venture"]: (200, fixture_bytes("techcrunch.xml"), {})}
    beta = Company("betacorp.io", "Beta Corp", "https://betacorp.io/", "US", "b2b_saas", "t")
    linear = Company("linear.app", "Linear", "https://linear.app/", "US", "b2b_saas", "t")
    rows = await funding.collect(fake, [ACME, beta, linear])
    matches = {
        r.account_id: json.loads(r.meta)["method"] for r in rows if r.signal_type == "funding_news"
    }
    assert matches == {"acmestudio.com": "domain", "betacorp.io": "domain"}  # Linear: no $ kw
    status = {r.key: r.num for r in rows if r.signal_type == "feed_status"}
    assert status["sec_edgar_form_d"] == 404 and status["techcrunch_venture"] == 200


async def test_github_org_discovery_and_budget(fake: FakeFetcher) -> None:
    fake.routes = {ACME.website: (200, fixture_bytes("homepage_wix.html"), {})}
    assert github.find_github_org(fixture_bytes("homepage_wix.html").decode()) == "acme-studio"
    rows = await github.collect(fake, [ACME], budget=0)
    assert [(r.signal_type, r.key) for r in rows] == [("github_org", "acme-studio")]
    rows = await github.collect(fake, [ACME], budget=5)
    assert any(r.signal_type == "github_api_status" and r.num == 404 for r in rows)
