"""Snapshot storage.

Canonical storage is append-only Parquet, one file per (snapshot_date, collector):

    data/snapshots/date=YYYY-MM-DD/<collector>.parquet

Re-running a collector on the same day overwrites that day's file (idempotent). DuckDB is used
as the query layer over the Parquet glob, so the GitHub Actions `data` branch only has to carry
small Parquet files, never a binary database.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class Observation:
    account_id: str
    collector: str
    signal_type: str
    key: str = ""
    value: str = ""
    num: float | None = None
    meta: str = ""  # small JSON string
    observed_at: str = ""  # ISO-8601 UTC; filled by write_snapshot when empty


SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.string()),
        ("observed_at", pa.string()),
        ("account_id", pa.string()),
        ("collector", pa.string()),
        ("signal_type", pa.string()),
        ("key", pa.string()),
        ("value", pa.string()),
        ("num", pa.float64()),
        ("meta", pa.string()),
    ]
)


@dataclass(frozen=True)
class Company:
    account_id: str  # registrable domain, lower-case, e.g. "acme.com"
    name: str
    website: str
    country: str
    segment: str  # "b2b_saas" | "agency"
    source: str
    team_size: int | None = None
    source_id: str = ""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def snapshot_path(data_dir: Path, snapshot_date: str, collector: str) -> Path:
    return data_dir / "snapshots" / f"date={snapshot_date}" / f"{collector}.parquet"


def write_snapshot(
    data_dir: Path,
    collector: str,
    rows: list[Observation],
    now: datetime | None = None,
) -> Path:
    now = now or utcnow()
    snap = now.date().isoformat()
    ts = now.isoformat().replace("+00:00", "Z")
    cols: dict[str, list[object]] = {f.name: [] for f in SCHEMA}
    for r in rows:
        cols["snapshot_date"].append(snap)
        cols["observed_at"].append(r.observed_at or ts)
        for f in fields(Observation):
            if f.name != "observed_at":
                cols[f.name].append(getattr(r, f.name))
    table = pa.table(cols, schema=SCHEMA)
    path = snapshot_path(data_dir, snap, collector)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)
    return path


def connect(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with an `observations` view over all snapshot Parquet files."""
    con = duckdb.connect()
    files = sorted((data_dir / "snapshots").glob("date=*/*.parquet"))
    if files:
        glob = str(data_dir / "snapshots" / "date=*" / "*.parquet")
        con.execute(
            f"CREATE VIEW observations AS SELECT * EXCLUDE (date) "
            f"FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    else:
        cols = ", ".join(
            f"CAST(NULL AS {'DOUBLE' if f.type == pa.float64() else 'VARCHAR'}) AS {f.name}"
            for f in SCHEMA
        )
        con.execute(f"CREATE VIEW observations AS SELECT {cols} WHERE FALSE")
    uni = data_dir / "universe.csv"
    if uni.exists():
        con.execute(f"CREATE VIEW companies AS SELECT * FROM read_csv_auto('{uni}', header=true)")
    return con


def write_universe(path: Path, companies: list[Company]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [f.name for f in fields(Company)]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=names)
        w.writeheader()
        for c in sorted(companies, key=lambda c: c.account_id):
            w.writerow(asdict(c))


def read_universe(path: Path) -> list[Company]:
    out: list[Company] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            ts = row.get("team_size") or ""
            out.append(
                Company(
                    account_id=row["account_id"],
                    name=row["name"],
                    website=row["website"],
                    country=row["country"],
                    segment=row["segment"],
                    source=row["source"],
                    team_size=int(float(ts)) if ts else None,
                    source_id=row.get("source_id", ""),
                )
            )
    return out
