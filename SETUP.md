# SETUP - what the owner has to do

Everything here is free. Nothing needs a paid account.

## 1. Push the repo and make it public (Actions minutes are free for public repos)

1. Create an empty **public** repo `goalgetter53/signal-backtest` on github.com (no README).
2. From this folder:
   ```bash
   git remote add origin https://github.com/goalgetter53/signal-backtest.git
   git push -u origin main
   ```
3. On github.com open the repo -> **Settings -> Actions -> General -> Workflow permissions** ->
   choose **Read and write permissions** -> Save. (The daily job commits snapshots to the
   `data` branch.)

## 2. Turn on the daily collector

1. Open the **Actions** tab -> enable workflows if asked.
2. Click **collect** -> **Run workflow** once by hand. After ~10-20 minutes a `data` branch
   appears with `data/snapshots/date=YYYY-MM-DD/*.parquet` and `reports/snapshot_summary.md`.
3. From then on it runs every day at 05:17 UTC. Nothing else to do.

The built-in `GITHUB_TOKEN` is used automatically for the GitHub-org collector (1,000 req/h).

## 3. Optional: SEC EDGAR Form D (US funding filings)

SEC requires a User-Agent with a contact. Use a role address, not a personal one.
1. Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**.
2. Name `SEC_USER_AGENT`, value e.g. `signal-backtest research ops@your-agency-domain.com`.
Without it the collector still runs and records the EDGAR HTTP status in the snapshot.

## 4. Make sure every outbound prospect is in the universe (do this before the first send)

Signals can only be collected for accounts in `data/universe.csv`. Before outbound-engine sends
its first email:
1. Export the prospect list from outbound-engine (company domain is enough).
2. Append any missing companies to `data/universe.csv` with the same columns
   (`account_id` = domain without `www.`, `segment` = `agency` or `b2b_saas`, `source` =
   `outbound-engine`). Commit to `main`.
3. Let the collector run for **at least one day** before those accounts are emailed - accounts
   with no snapshot before their first send are excluded from the backtest by design.

## 5. Run the real backtest (after 6-8 weeks of sends)

1. Export outcomes from outbound-engine as CSV with exactly these columns:
   `account_id,sent_at,replied,positive,meeting` (one row per send; `sent_at` ISO-8601 UTC,
   flags 0/1). No names or email addresses are needed or wanted.
2. Get the snapshots: `git fetch origin data && git worktree add ../sb-data origin/data`
3. Run:
   ```bash
   make install
   .venv/bin/python -m signal_backtest --data-dir ../sb-data/data backtest --outcomes outcomes.csv
   ```
4. Read `reports/backtest_report.md`. Commit it to `main` and update the README results.

## Running on your own machine instead of GitHub Actions

```bash
make install && make collect && make summary
```
Schedule it daily (Windows Task Scheduler or `cron`: `17 5 * * * cd /path && make collect`).
Keep the machine's `data/snapshots/` folder - it is the history the backtest needs.
