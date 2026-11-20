from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FIX

from signal_backtest import backtest as bt
from signal_backtest import demo
from signal_backtest.features import events_from_snapshots, features_as_of
from signal_backtest.stats import auc, holm, permutation_pvalue, sends_for_lift, wilson
from signal_backtest.store import Observation


def _o(
    aid: str,
    st: str,
    ts: str,
    key: str = "",
    value: str = "",
    num: float | None = None,
    collector: str = "x",
) -> Observation:
    return Observation(aid, collector, st, key, value, num, "", ts)


def test_wilson_known_values() -> None:
    lo, hi = wilson(5, 100)
    assert lo == pytest.approx(0.0215, abs=1e-3) and hi == pytest.approx(0.1118, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)


def test_holm_and_auc_and_permutation() -> None:
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert auc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5
    strong = permutation_pvalue(
        [True] * 20 + [False] * 20, [1] * 15 + [0] * 5 + [0] * 20, n_perm=500
    )
    null = permutation_pvalue([True, False] * 20, [1, 1, 0, 0] * 10, n_perm=500)
    assert strong < 0.01 and null > 0.5


def test_power_matches_textbook() -> None:
    # Equal allocation, 5% -> 10%: classic answer ~435 per arm (Fleiss without CC) => ~870 total
    n = sends_for_lift(0.05, 2.0, 0.5)
    assert 850 <= n <= 900
    assert sends_for_lift(0.02, 2.0, 0.5) > n  # rarer replies need more sends
    assert sends_for_lift(0.05, 2.0, 0.1) > n  # rare signals need more sends


def test_lookahead_guard_excludes_future_observations() -> None:
    obs = [
        _o("a.com", "open_roles_web", "2026-11-20T06:00:00Z", "greenhouse", num=0.0),
        _o("a.com", "open_roles_web", "2026-12-10T06:00:00Z", "greenhouse", num=2.0),
        _o("b.com", "open_roles_web", "2026-12-10T06:00:00Z", "greenhouse", num=2.0),
    ]
    cut = datetime(2026, 12, 1, tzinfo=UTC)
    feats = features_as_of(obs, {"a.com": cut, "b.com": cut})
    assert feats["a.com"]["hiring_web"] == 0.0  # the Dec-10 hiring signal is in the future
    assert "b.com" not in feats  # nothing observed before send -> excluded, not scored as 0


def test_events_and_decay() -> None:
    d1, d2 = "2026-11-20T06:00:00Z", "2026-11-27T06:00:00Z"
    obs = [
        _o("a.com", "tech_scan", d1, value="ok"),
        _o("a.com", "tech", d1, "Wix"),
        _o("a.com", "tech_scan", d2, value="ok"),
        _o("a.com", "tech", d2, "Webflow"),
        _o("a.com", "tech", d2, "HubSpot"),
        _o("a.com", "structure_hash", d1, value="aaa"),
        _o("a.com", "structure_hash", d2, value="bbb"),
        _o("a.com", "has_page", d1, "pricing", num=0.0),
        _o("a.com", "has_page", d2, "pricing", num=1.0),
    ]
    kinds = sorted(e.kind for e in events_from_snapshots(obs))
    assert kinds == ["cms_migration", "marketing_tool_added", "new_key_page", "redesign"]
    f = features_as_of(obs, {"a.com": datetime(2026, 12, 27, 6, tzinfo=UTC)})["a.com"]
    assert f["redesign"] == pytest.approx(0.5)  # exactly one 30-day half-life later
    assert f["cms_migration"] == pytest.approx(0.5 ** (30 / 45))


def test_load_outcomes_aggregates_per_account() -> None:
    outs = {o.account_id: o for o in bt.load_outcomes(FIX / "outcomes_sample.csv")}
    assert outs["acme.com"].sends == 2 and outs["acme.com"].replied == 1
    assert outs["acme.com"].first_sent == datetime(2026, 12, 1, 10, tzinfo=UTC)
    assert outs["beta.io"].replied == 1 and outs["beta.io"].positive == 0


def test_demo_backtest_recovers_planted_effect(tmp_path: Path) -> None:
    comps, obs, rows = demo.generate()
    path = tmp_path / "demo.csv"
    demo.write_outcomes(path, rows)
    assert path.read_text().startswith("# SYNTHETIC")
    res = bt.run_backtest(
        obs, bt.load_outcomes(path), {c.account_id: c for c in comps}, synthetic=True
    )
    assert res.n_scored == len(rows) and res.n_excluded_no_presend_obs == 0
    reply = {s.signal: s for s in res.signals if s.outcome == "replied"}
    assert reply["hiring_web"].lift > 1.5 and reply["hiring_web"].verdict == "positive"
    assert reply["legacy_builder"].verdict in {"noise / inconclusive", "insufficient data"}
    assert not math.isnan(res.auc["replied"]["signal_model"][0])
    report = bt.render_report(res)
    assert report.startswith("> **SYNTHETIC DEMO")
    assert "Negative and noise results" in report and "Power" in report
