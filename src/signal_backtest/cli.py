"""Command-line entry point: ``signal-backtest <command>`` (or ``python -m signal_backtest``)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from . import backtest as bt
from . import demo, summary, universe
from .collectors import funding, github, jobs, techstack, website
from .http import Fetcher
from .store import Observation, connect, read_universe, write_snapshot, write_universe

COLLECTORS = ["jobs", "techstack", "website", "funding", "github"]
API_HOSTS = {
    "boards-api.greenhouse.io": (6, 0.1),
    "api.lever.co": (4, 0.2),
    "api.ashbyhq.com": (6, 0.1),
    "apply.workable.com": (1, 2.0),
    "raw.githubusercontent.com": (4, 0.0),
    "api.github.com": (2, 0.5),
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def cmd_universe(a: argparse.Namespace) -> int:
    companies = universe.build(limit=a.limit, log=_log)
    write_universe(a.data_dir / "universe.csv", companies)
    seg: dict[str, int] = {}
    for c in companies:
        seg[c.segment] = seg.get(c.segment, 0) + 1
    _log(f"universe: {len(companies)} companies written ({seg})")
    return 0


async def _collect(a: argparse.Namespace) -> dict[str, int]:
    comps = read_universe(a.data_dir / "universe.csv")
    if a.limit:
        comps = comps[: a.limit]
    counts: dict[str, int] = {}
    async with Fetcher(
        cache_dir=a.data_dir / "cache", host_limits=API_HOSTS, offline=a.offline
    ) as f:
        for name in a.collectors:
            t = time.monotonic()
            rows: list[Observation]
            if name == "jobs":
                rows = await jobs.collect(f, comps, guess=not a.no_guess)
            elif name == "techstack":
                raw, ver = await techstack.fetch_fingerprints(f)
                techs = techstack.load_fingerprints(raw)
                _log(
                    f"techstack: {len(raw)} fingerprints loaded, {len(techs)} in relevant "
                    f"categories (set {ver})"
                )
                rows = await techstack.collect(f, comps, techs, ver)
            elif name == "website":
                rows = await website.collect(f, comps)
            elif name == "funding":
                rows = await funding.collect(f, comps)
            elif name == "github":
                rows = await github.collect(f, comps)
            else:
                raise SystemExit(f"unknown collector {name}")
            path = write_snapshot(a.data_dir, name, rows)
            counts[name] = len(rows)
            _log(f"{name}: {len(rows)} observations -> {path} ({time.monotonic() - t:.0f}s)")
    return counts


def cmd_collect(a: argparse.Namespace) -> int:
    asyncio.run(_collect(a))
    return 0


def cmd_summary(a: argparse.Namespace) -> int:
    s = summary.summarise(a.data_dir, a.date)
    out = a.reports_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "snapshot_summary.json").write_text(json.dumps(s, indent=2, default=str) + "\n")
    (out / "snapshot_summary.md").write_text(summary.to_markdown(s))
    _log(json.dumps(s, indent=2, default=str))
    return 0


def _load_obs(data_dir: Path) -> list[Observation]:
    con = connect(data_dir)
    rows = con.execute(
        "SELECT account_id, collector, signal_type, key, value, num, meta, observed_at "
        "FROM observations"
    ).fetchall()
    return [Observation(*r) for r in rows]


def cmd_backtest(a: argparse.Namespace) -> int:
    outcomes = bt.load_outcomes(a.outcomes)
    if any(o.account_id.endswith(demo.DEMO_SUFFIX) for o in outcomes):
        raise SystemExit("refusing: outcomes file contains synthetic demo accounts")
    uni = {c.account_id: c for c in read_universe(a.data_dir / "universe.csv")}
    res = bt.run_backtest(_load_obs(a.data_dir), outcomes, uni)
    a.reports_dir.mkdir(parents=True, exist_ok=True)
    path = a.reports_dir / "backtest_report.md"
    path.write_text(bt.render_report(res))
    _log(f"wrote {path}")
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    comps, obs, outs = demo.generate()
    out_csv = Path("examples/DEMO_synthetic_outcomes.csv")
    demo.write_outcomes(out_csv, outs)
    res = bt.run_backtest(
        obs, bt.load_outcomes(out_csv), {c.account_id: c for c in comps}, synthetic=True
    )
    a.reports_dir.mkdir(parents=True, exist_ok=True)
    path = a.reports_dir / "DEMO_backtest_report.md"
    path.write_text(bt.render_report(res, "Signal backtest report (SYNTHETIC DEMO)"))
    _log(f"wrote {out_csv} and {path}")
    return 0


def cmd_power(a: argparse.Namespace) -> int:
    _log(bt.power_table())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="signal-backtest")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--reports-dir", type=Path, default=Path("reports"))
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("universe", help="build account universe from public sources")
    u.add_argument("--limit", type=int, default=1000)
    c = sub.add_parser("collect", help="run collectors and write today's snapshot")
    c.add_argument("--collectors", nargs="+", default=COLLECTORS, choices=COLLECTORS)
    c.add_argument("--limit", type=int, default=0, help="only first N companies (0 = all)")
    c.add_argument("--no-guess", action="store_true", help="skip guessed ATS board tokens")
    c.add_argument("--offline", action="store_true", help="cache only, no network")
    s = sub.add_parser("summary", help="counts per signal type for a snapshot date")
    s.add_argument("--date", default=None)
    b = sub.add_parser("backtest", help="lift/calibration report from real outcomes")
    b.add_argument("--outcomes", type=Path, required=True)
    sub.add_parser("demo", help="SYNTHETIC demo report (format only)")
    sub.add_parser("power", help="sends needed to detect a 2x lift")
    a = p.parse_args(argv)
    handlers = {
        "universe": cmd_universe,
        "collect": cmd_collect,
        "summary": cmd_summary,
        "backtest": cmd_backtest,
        "demo": cmd_demo,
        "power": cmd_power,
    }
    return handlers[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
