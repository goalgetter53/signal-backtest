from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import fixture_bytes

from signal_backtest import universe
from signal_backtest.store import (
    Company,
    Observation,
    connect,
    read_universe,
    write_snapshot,
    write_universe,
)
from signal_backtest.summary import summarise


def test_domain_normalisation() -> None:
    assert universe.domain_of("https://WWW.Acme.com/about") == "acme.com"
    assert universe.domain_of("maple.ca") == "maple.ca"


def test_yc_filter() -> None:
    comps = universe.yc_companies(json.loads(fixture_bytes("yc_sample.json")))
    assert {(c.account_id, c.country) for c in comps} == {("acme.com", "US"), ("maple.ca", "CA")}


def test_wikidata_filter_and_merge() -> None:
    b = [
        {
            "c": {"value": "http://www.wikidata.org/entity/Q1"},
            "cLabel": {"value": "Big Agency"},
            "site": {"value": "https://big.com"},
            "country": {"value": "x/Q30"},
            "emp": {"value": "50000"},
        },
        {
            "c": {"value": "http://www.wikidata.org/entity/Q2"},
            "cLabel": {"value": "Small"},
            "site": {"value": "https://www.small.co.uk/"},
            "country": {"value": "x/Q145"},
        },
    ]
    wd = universe.wikidata_companies(b)
    assert [(c.account_id, c.country, c.segment) for c in wd] == [("small.co.uk", "GB", "agency")]
    dup = Company("small.co.uk", "Dup", "https://small.co.uk/", "GB", "b2b_saas", "yc-oss")
    merged = universe.merge([wd, [dup]], limit=10)
    assert len(merged) == 1 and merged[0].source == "wikidata"


def test_snapshot_idempotent_and_queryable(tmp_path: Path) -> None:
    write_universe(
        tmp_path / "universe.csv",
        [Company("a.com", "A, Inc", "https://a.com/", "US", "agency", "t", 12)],
    )
    assert read_universe(tmp_path / "universe.csv")[0].name == "A, Inc"
    now = datetime(2026, 11, 20, 6, tzinfo=UTC)
    rows = [
        Observation(
            "a.com", "jobs", "open_roles_web", "greenhouse", "", 2.0, json.dumps({"method": "link"})
        )
    ]
    write_snapshot(tmp_path, "jobs", rows, now)
    write_snapshot(tmp_path, "jobs", rows, now)  # same day again: overwrite, not duplicate
    con = connect(tmp_path)
    assert con.execute("SELECT count(*), max(observed_at) FROM observations").fetchone() == (
        1,
        "2026-11-20T06:00:00Z",
    )
    s = summarise(tmp_path)
    assert s["open_roles_web"] == 2 and s["companies_hiring_web_link_only"] == 1
    assert s["universe_companies"] == 1


def test_empty_store(tmp_path: Path) -> None:
    con = connect(tmp_path)
    assert con.execute("SELECT count(*) FROM observations").fetchone() == (0,)
    with pytest.raises(KeyError):
        _ = summarise(tmp_path)["open_roles_web"]
