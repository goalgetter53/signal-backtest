"""Backtest: do signals observed *before* the first send predict reply / positive / meeting?

Input outcomes CSV (from outbound-engine), one row per send:
    account_id,sent_at,replied,positive,meeting
Rows are aggregated per account: ``first_sent = min(sent_at)``, outcomes = any().

Look-ahead guard: features are computed with ``as_of = first_sent`` and only observations
with ``observed_at < first_sent`` (see features.features_as_of). Accounts without any
pre-send observation are excluded and counted in the report.
"""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .features import ALL_FEATURES, features_as_of, is_active, parse_ts
from .model import PRIOR_WEIGHTS, icp_score, score
from .stats import (
    auc,
    bootstrap_auc_ci,
    holm,
    logistic_1d,
    permutation_pvalue,
    sends_for_lift,
    sigmoid,
    wilson,
)
from .store import Company, Observation

OUTCOMES = ("replied", "positive", "meeting")
MIN_GROUP = 10


@dataclass(frozen=True)
class AccountOutcome:
    account_id: str
    first_sent: datetime
    sends: int
    replied: int
    positive: int
    meeting: int

    def get(self, name: str) -> int:
        return int(getattr(self, name))


def _flag(v: str) -> int:
    return 1 if v.strip().lower() in {"1", "true", "yes", "y", "t"} else 0


def load_outcomes(path: Path) -> list[AccountOutcome]:
    first: dict[str, datetime] = {}
    sends: dict[str, int] = {}
    flags: dict[str, dict[str, int]] = {}
    with path.open(newline="") as fh:
        for r in csv.DictReader(line for line in fh if not line.startswith("#")):
            aid = r["account_id"].strip().lower()
            ts = parse_ts(r["sent_at"].strip())
            first[aid] = min(ts, first.get(aid, ts))
            sends[aid] = sends.get(aid, 0) + 1
            f = flags.setdefault(aid, dict.fromkeys(OUTCOMES, 0))
            for k in OUTCOMES:
                f[k] = max(f[k], _flag(r.get(k) or "0"))
    return [
        AccountOutcome(
            aid,
            first[aid],
            sends[aid],
            flags[aid]["replied"],
            flags[aid]["positive"],
            flags[aid]["meeting"],
        )
        for aid in sorted(first)
    ]


@dataclass
class SignalResult:
    signal: str
    outcome: str
    n_with: int
    k_with: int
    n_without: int
    k_without: int
    p_value: float
    p_holm: float = 1.0

    @property
    def rate_with(self) -> float:
        return self.k_with / self.n_with if self.n_with else float("nan")

    @property
    def rate_without(self) -> float:
        return self.k_without / self.n_without if self.n_without else float("nan")

    @property
    def lift(self) -> float:
        if not self.n_with or not self.n_without or self.k_without == 0:
            return float("nan")
        return self.rate_with / self.rate_without

    @property
    def verdict(self) -> str:
        if self.n_with < MIN_GROUP or self.n_without < MIN_GROUP:
            return "insufficient data"
        _, hi_w = wilson(self.k_with, self.n_with)
        lo_o, _ = wilson(self.k_without, self.n_without)
        if self.p_holm < 0.05 and self.rate_with > self.rate_without:
            return "positive"
        if self.p_holm < 0.05 and self.rate_with < self.rate_without:
            return "negative"
        if hi_w < lo_o:
            return "negative (CI, not significant after Holm)"
        return "noise / inconclusive"


@dataclass
class BacktestResult:
    n_outcome_accounts: int
    n_scored: int
    n_excluded_no_presend_obs: int
    base_rates: dict[str, float]
    signals: list[SignalResult]
    auc: dict[str, dict[str, tuple[float, float, float]]]  # outcome -> model -> (auc, lo, hi)
    calibration: list[tuple[int, int, float, float, float, float]]  # bin, n, pred, obs, lo, hi
    brier: tuple[float, float]  # (model, base-rate)
    prevalence: dict[str, float]
    synthetic: bool = False
    notes: list[str] = field(default_factory=list)


def run_backtest(
    observations: Iterable[Observation],
    outcomes: list[AccountOutcome],
    universe: dict[str, Company],
    synthetic: bool = False,
    seed: int = 3,
) -> BacktestResult:
    as_of = {o.account_id: o.first_sent for o in outcomes}
    feats = features_as_of(observations, as_of)
    scored = [o for o in outcomes if o.account_id in feats]
    n = len(scored)
    base = {k: (sum(o.get(k) for o in scored) / n if n else float("nan")) for k in OUTCOMES}

    results: list[SignalResult] = []
    prevalence: dict[str, float] = {}
    for s in ALL_FEATURES:
        flags = [is_active(s, feats[o.account_id].get(s, 0.0)) for o in scored]
        prevalence[s] = sum(flags) / n if n else 0.0
        for k in OUTCOMES:
            ys = [o.get(k) for o in scored]
            kw = sum(y for f, y in zip(flags, ys, strict=True) if f)
            nw = sum(flags)
            results.append(
                SignalResult(s, k, nw, kw, n - nw, sum(ys) - kw, permutation_pvalue(flags, ys))
            )
    for k in OUTCOMES:
        fam = [r for r in results if r.outcome == k]
        for r, p in zip(fam, holm([r.p_value for r in fam]), strict=True):
            r.p_holm = p

    rng = random.Random(seed)
    model_scores = [score(feats[o.account_id], PRIOR_WEIGHTS) for o in scored]
    icp = [icp_score(universe.get(o.account_id)) + 1e-6 * rng.random() for o in scored]
    rnd = [rng.random() for _ in scored]
    aucs: dict[str, dict[str, tuple[float, float, float]]] = {}
    for k in OUTCOMES:
        ys = [o.get(k) for o in scored]
        aucs[k] = {}
        for name, sc in (("signal_model", model_scores), ("icp_only", icp), ("random", rnd)):
            lo, hi = bootstrap_auc_ci(sc, ys)
            aucs[k][name] = (auc(sc, ys), lo, hi)

    calib, brier = _calibration(model_scores, [o.replied for o in scored], seed)
    return BacktestResult(
        len(outcomes),
        n,
        len(outcomes) - n,
        base,
        results,
        aucs,
        calib,
        brier,
        prevalence,
        synthetic,
    )


def _calibration(
    scores: list[float], ys: list[int], seed: int, folds: int = 5, bins: int = 5
) -> tuple[list[tuple[int, int, float, float, float, float]], tuple[float, float]]:
    """Cross-fitted 1-D logistic map score -> P(reply); reliability table by predicted quintile."""
    n = len(scores)
    if n < folds * 2 or sum(ys) == 0:
        return [], (float("nan"), float("nan"))
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    pred = [0.0] * n
    for f in range(folds):
        test = set(idx[f::folds])
        a, b = logistic_1d(
            [scores[i] for i in range(n) if i not in test],
            [ys[i] for i in range(n) if i not in test],
        )
        for i in test:
            pred[i] = sigmoid(a + b * scores[i])
    order = sorted(range(n), key=lambda i: pred[i])
    table = []
    for bi in range(bins):
        members = order[bi * n // bins : (bi + 1) * n // bins]
        if not members:
            continue
        k = sum(ys[i] for i in members)
        lo, hi = wilson(k, len(members))
        table.append(
            (
                bi + 1,
                len(members),
                sum(pred[i] for i in members) / len(members),
                k / len(members),
                lo,
                hi,
            )
        )
    base = sum(ys) / n
    brier_model = sum((p - y) ** 2 for p, y in zip(pred, ys, strict=True)) / n
    brier_base = sum((base - y) ** 2 for y in ys) / n
    return table, (brier_model, brier_base)


def _pct(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{100 * x:.1f}%"


def power_table(
    base_rates: Iterable[float] = (0.01, 0.02, 0.03, 0.05, 0.08),
    prevalences: Iterable[float] = (0.1, 0.2, 0.3, 0.5),
) -> str:
    prevalences = list(prevalences)
    lines = [
        "| baseline reply rate | "
        + " | ".join(f"signal on {int(p * 100)}% of sends" for p in prevalences)
        + " |",
        "|---|" + "---|" * len(prevalences),
    ]
    for b in base_rates:
        cells = [f"{sends_for_lift(b, 2.0, p):,}" for p in prevalences]
        lines.append(f"| {b * 100:.0f}% | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_report(r: BacktestResult, title: str = "Signal backtest report") -> str:
    out: list[str] = []
    if r.synthetic:
        out += [
            "> **SYNTHETIC DEMO - NOT REAL RESULTS.** Outcomes and signals below were",
            "> generated by `signal-backtest demo` to show the report format. Nothing here",
            "> says anything about real prospects.",
            "",
        ]
    out += [
        f"# {title}",
        "",
        f"- Accounts with outcomes: **{r.n_outcome_accounts}**",
        f"- Scored (had >=1 observation strictly before first send): **{r.n_scored}**",
        f"- Excluded, no pre-send observation (look-ahead guard): "
        f"**{r.n_excluded_no_presend_obs}**",
        "- Base rates: " + ", ".join(f"{k} {_pct(v)}" for k, v in r.base_rates.items()),
        "",
    ]

    out += [
        "## Ranking quality (AUC, 95% bootstrap CI)",
        "",
        "| outcome | signal_model | icp_only | random |",
        "|---|---|---|---|",
    ]
    for k, models in r.auc.items():
        cells = [
            f"{a:.3f} ({lo:.3f}-{hi:.3f})" if not math.isnan(a) else "n/a"
            for a, lo, hi in (models[m] for m in ("signal_model", "icp_only", "random"))
        ]
        out.append(f"| {k} | " + " | ".join(cells) + " |")

    out += [
        "",
        "## Lift per signal (reply rate with vs without; Wilson 95% CI; permutation "
        "p, Holm-adjusted within outcome)",
        "",
    ]
    for k in OUTCOMES:
        out += [
            f"### {k}",
            "",
            "| signal | prevalence | with: rate (CI) n | without: rate (CI) n | lift | p | "
            "p_holm | verdict |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for s in sorted(
            (s for s in r.signals if s.outcome == k), key=lambda s: (s.p_holm, s.signal)
        ):
            lo_w, hi_w = wilson(s.k_with, s.n_with)
            lo_o, hi_o = wilson(s.k_without, s.n_without)
            lift = "n/a" if math.isnan(s.lift) else f"{s.lift:.2f}x"
            out.append(
                f"| {s.signal} | {_pct(r.prevalence[s.signal])} | {_pct(s.rate_with)} "
                f"({_pct(lo_w)}-{_pct(hi_w)}) n={s.n_with} | {_pct(s.rate_without)} "
                f"({_pct(lo_o)}-{_pct(hi_o)}) n={s.n_without} | {lift} | {s.p_value:.3f} | "
                f"{s.p_holm:.3f} | {s.verdict} |"
            )
        out.append("")

    reply = [s for s in r.signals if s.outcome == "replied"]
    bad = [s for s in reply if s.verdict != "positive"]
    out += ["## Negative and noise results (reply outcome) - listed explicitly", ""]
    if bad:
        out += [
            f"- **{s.signal}**: {s.verdict} (lift "
            f"{'n/a' if math.isnan(s.lift) else f'{s.lift:.2f}x'}, n_with={s.n_with}, "
            f"p_holm={s.p_holm:.3f})"
            for s in bad
        ]
    else:
        out.append("- none")
    out += ["", "## Calibration (cross-fitted logistic map of score -> P(reply))", ""]
    if r.calibration:
        out += ["| quintile | n | mean predicted | observed (Wilson CI) |", "|---|---|---|---|"]
        out += [
            f"| {b} | {n} | {_pct(p)} | {_pct(o)} ({_pct(lo)}-{_pct(hi)}) |"
            for b, n, p, o, lo, hi in r.calibration
        ]
        out += [
            "",
            f"Brier score: model {r.brier[0]:.4f} vs base-rate-only {r.brier[1]:.4f} "
            "(lower is better).",
        ]
    else:
        out.append("Not enough data for calibration.")
    out += [
        "",
        "## Power: total sends needed to detect a 2x reply-rate lift",
        "(two-sided alpha=0.05, power=0.80, two-proportion z-test, unequal allocation)",
        "",
        power_table(),
        "",
    ]
    out += r.notes
    return "\n".join(out) + "\n"
