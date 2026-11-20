# ADR 0002 - Look-ahead guard and the statistics used to judge signals

Status: accepted (2026-11-20)

## Look-ahead guard
For each account, `as_of = min(sent_at)` across all its sends. Features use only observations
with `observed_at < as_of` (strict). Accounts with no pre-send observation are **excluded and
counted** in the report rather than scored as "no signal" (that would bias towards the null).
Event features (changes between snapshots) are dated at the later snapshot's `observed_at`,
never at a date claimed by the source (e.g. a job's `updated_at`), because we can only act on
what we had seen.

## Statistics
* Per signal and outcome: rate with vs without, **Wilson 95% CIs** (well-behaved at the 1-5%
  reply rates typical of cold email), lift = ratio of rates.
* **Permutation test** (1,000 label shuffles, seeded) for the difference in rates - no normal
  approximation needed at small counts.
* **Holm-Bonferroni** across the 15 signals within each outcome: with 15 tests at alpha=0.05,
  one false "winner" is expected by chance otherwise.
* Verdicts: `positive`, `negative`, `negative (CI, not significant after Holm)`,
  `noise / inconclusive`, `insufficient data` (<10 accounts in a group). Every non-positive
  signal is listed explicitly in the report.
* Ranking: AUC with bootstrap CI for the signal model vs `icp_only` vs `random` baselines.
* Calibration: 5-fold cross-fitted 1-D logistic map score -> P(reply), reliability table by
  quintile and Brier score vs a base-rate-only forecast.
* Power: two-proportion z-test with unequal allocation (signal prevalence), alpha=0.05,
  power=0.8, for a 2x lift.

## Consequences
At realistic volumes (hundreds of sends, 2-5% reply rate) most signals will honestly come out
"inconclusive". The power table makes that expectation explicit before results arrive.
