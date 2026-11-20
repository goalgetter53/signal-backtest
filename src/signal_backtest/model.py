"""Interpretable account score and its baselines.

``score = sum(weight[f] * feature[f])`` with hand-set prior weights (log-odds-ish units) that
encode the hypothesis "companies visibly investing in web/marketing are likelier to need
outside website help". The weights are **priors to be tested**, not fitted results: the
backtest reports per-signal lift so each weight can be kept, shrunk or dropped.

Baselines
* ``random`` - expected AUC 0.5; what the outbound list order is without any signal.
* ``icp_only`` - firmographic fit only (segment + team-size band), no timing signals.
"""

from __future__ import annotations

from .store import Company

PRIOR_WEIGHTS: dict[str, float] = {
    "hiring_web": 1.0,
    "hiring_marketing": 0.6,
    "hiring_design": 0.4,
    "hiring_any": 0.2,
    "legacy_builder": 0.5,
    "wordpress": 0.3,
    "no_marketing_stack": 0.1,
    "github_active": -0.2,  # strong in-house eng teams may build their own site
    "recent_funding": 0.8,
    "cms_migration": 0.7,
    "redesign": 0.3,
    "marketing_tool_added": 0.5,
    "new_key_page": 0.3,
    "new_web_role": 0.9,
    "sitemap_growth": 0.2,
}


def score(features: dict[str, float], weights: dict[str, float] | None = None) -> float:
    w = weights or PRIOR_WEIGHTS
    return sum(w.get(k, 0.0) * v for k, v in features.items())


def explain(
    features: dict[str, float], weights: dict[str, float] | None = None, top: int = 3
) -> list[tuple[str, float]]:
    w = weights or PRIOR_WEIGHTS
    contrib = [(k, w.get(k, 0.0) * v) for k, v in features.items() if v]
    return sorted(contrib, key=lambda kv: -abs(kv[1]))[:top]


def icp_score(c: Company | None) -> float:
    if c is None:
        return 0.0
    s = 1.0 if c.segment == "agency" else 0.5
    if c.team_size is not None:
        s += 0.5 if 10 <= c.team_size <= 200 else 0.0
    return s
