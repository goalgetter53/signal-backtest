"""Small, dependency-free statistics used by the backtest (stdlib only, deterministic)."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from statistics import NormalDist

_N = NormalDist()


def wilson(successes: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z = _N.inv_cdf(1 - (1 - conf) / 2)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def permutation_pvalue(
    flags: Sequence[bool], outcomes: Sequence[int], n_perm: int = 1000, seed: int = 7
) -> float:
    """Two-sided p-value for difference in outcome rate between flagged and unflagged groups."""
    n = len(flags)
    k = sum(flags)
    if k == 0 or k == n:
        return 1.0
    total = sum(outcomes)

    def diff(sel_sum: int) -> float:
        return sel_sum / k - (total - sel_sum) / (n - k)

    observed = abs(diff(sum(o for f, o in zip(flags, outcomes, strict=True) if f)))
    rng = random.Random(seed)
    idx = list(range(n))
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(idx)
        s = sum(outcomes[i] for i in idx[:k])
        if abs(diff(s)) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (family-wise error control across signals)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adj[i] = running
    return adj


def auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """ROC AUC via the Mann-Whitney statistic (ties count 0.5)."""
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return float("nan")
    ranked = sorted((s, i) for i, s in enumerate(list(pos) + list(neg)))
    ranks = [0.0] * len(ranked)
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[ranked[k][1]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = sum(ranks[: len(pos)])
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def bootstrap_auc_ci(
    scores: Sequence[float], labels: Sequence[int], n_boot: int = 500, seed: int = 11
) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(scores)
    vals: list[float] = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        a = auc([scores[i] for i in idx], [labels[i] for i in idx])
        if not math.isnan(a):
            vals.append(a)
    if not vals:
        return (float("nan"), float("nan"))
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[min(len(vals) - 1, int(0.975 * len(vals)))])


def logistic_1d(x: Sequence[float], y: Sequence[int], iters: int = 50) -> tuple[float, float]:
    """Fit p = sigmoid(a + b x) by Newton-Raphson with a tiny ridge for stability."""
    a, b = 0.0, 0.0
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for xi, yi in zip(x, y, strict=True):
            p = 1 / (1 + math.exp(-max(-30.0, min(30.0, a + b * xi))))
            w = p * (1 - p)
            g0 += yi - p
            g1 += (yi - p) * xi
            h00 += w
            h01 += w * xi
            h11 += w * xi * xi
        h00 += 1e-6
        h11 += 1e-3
        g1 -= 1e-3 * b
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            break
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a, b = a + da, b + db
        if abs(da) + abs(db) < 1e-8:
            break
    return a, b


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def sends_for_lift(
    base_rate: float,
    lift: float = 2.0,
    prevalence: float = 0.5,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Total sends N so a two-sided two-proportion z-test detects ``lift`` x ``base_rate``.

    ``base_rate`` is the reply rate *without* the signal, ``prevalence`` the share of sends
    that carry the signal (unequal allocation). Returns the total across both groups.
    """
    p0, p1 = base_rate, min(base_rate * lift, 0.999)
    pi = prevalence
    pbar = pi * p1 + (1 - pi) * p0
    za = _N.inv_cdf(1 - alpha / 2)
    zb = _N.inv_cdf(power)
    num = za * math.sqrt(pbar * (1 - pbar) * (1 / pi + 1 / (1 - pi))) + zb * math.sqrt(
        p1 * (1 - p1) / pi + p0 * (1 - p0) / (1 - pi)
    )
    return math.ceil((num / (p1 - p0)) ** 2)
