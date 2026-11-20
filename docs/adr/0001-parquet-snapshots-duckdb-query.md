# ADR 0001 - Parquet snapshots as the source of truth, DuckDB as the query layer

Status: accepted (2026-11-20)

## Context
Signals must be timestamped *before* outcomes exist (6-8 weeks later) and collected daily by
a free scheduler (GitHub Actions). A binary `.duckdb` file in git would conflict and bloat.

## Decision
* Every collector run writes one zstd Parquet file per collector per UTC day:
  `data/snapshots/date=YYYY-MM-DD/<collector>.parquet`. Re-running the same day overwrites
  that file, so runs are idempotent.
* One long table schema (`account_id, collector, signal_type, key, value, num, meta,
  observed_at, snapshot_date`) so new signal types need no migration.
* DuckDB reads the Parquet glob in memory (`store.connect`) for summaries and the backtest.
* Daily snapshots go to an orphan `data` branch; `main` holds code plus the Week 0 seed.

## Consequences
+ Append-only history: nothing is ever rewritten after the day it was observed, which is what
  makes the look-ahead guard trustworthy (git history is a second, tamper-evident timestamp).
+ Small diffs; no DB server.
- Long format needs pivoting for features (done in Python, fine at <=1,000 accounts).
