"""Snapshot summary: counts per signal type for one snapshot date, straight from the Parquet."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import connect


def _one(con: Any, sql: str, params: list[object]) -> Any:
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


def summarise(data_dir: Path, snapshot_date: str | None = None) -> dict[str, Any]:
    con = connect(data_dir)
    d = snapshot_date or _one(con, "SELECT max(snapshot_date) FROM observations", [])
    if d is None:
        return {"snapshot_date": None}
    q = "FROM observations WHERE snapshot_date = ? AND "
    p = [d]
    s: dict[str, Any] = {"snapshot_date": d}
    s["universe_companies"] = (
        _one(con, "SELECT count(*) FROM companies", [])
        if (data_dir / "universe.csv").exists()
        else None
    )
    if s["universe_companies"] is not None:
        s["universe_by_segment"] = dict(
            con.execute("SELECT segment, count(*) FROM companies GROUP BY 1 ORDER BY 1").fetchall()
        )
    s["companies_observed"] = _one(
        con, "SELECT count(DISTINCT account_id) " + q + "account_id <> '_source'", p
    )
    s["homepage_ok"] = _one(
        con,
        "SELECT count(DISTINCT account_id) "
        + q
        + "signal_type='homepage_status' AND num BETWEEN 200 AND 299",
        p,
    )
    s["homepage_status_counts"] = {
        str(int(k)) if k is not None else "none": v
        for k, v in con.execute(
            "SELECT num, count(*) " + q + "signal_type='homepage_status' GROUP BY 1 "
            "ORDER BY 2 DESC",
            p,
        ).fetchall()
    }
    s["ats_boards_by_method"] = dict(
        con.execute(
            "SELECT value, count(*) " + q + "signal_type='ats_board' AND key <> '' GROUP BY 1", p
        ).fetchall()
    )
    s["ats_boards_by_provider"] = dict(
        con.execute(
            "SELECT split_part(key, ':', 1), count(*) "
            + q
            + "signal_type='ats_board' AND key <> '' GROUP BY 1 ORDER BY 2 DESC",
            p,
        ).fetchall()
    )
    for kind in ("total", "web", "marketing", "design"):
        s[f"open_roles_{kind}"] = int(
            _one(con, "SELECT coalesce(sum(num),0) " + q + f"signal_type='open_roles_{kind}'", p)
        )
        s[f"companies_with_open_roles_{kind}"] = _one(
            con,
            "SELECT count(DISTINCT account_id) "
            + q
            + f"signal_type='open_roles_{kind}' AND num > 0",
            p,
        )
    s["companies_hiring_web_link_only"] = _one(
        con,
        "SELECT count(DISTINCT account_id) "
        + q
        + "signal_type='open_roles_web' AND num > 0 AND meta LIKE '%\"method\": \"link\"%'",
        p,
    )
    s["tech_scans_ok"] = _one(
        con, "SELECT count(*) " + q + "signal_type='tech_scan' AND value='ok'", p
    )
    s["tech_detections"] = _one(con, "SELECT count(*) " + q + "signal_type='tech'", p)
    s["companies_with_tech_detected"] = _one(
        con, "SELECT count(DISTINCT account_id) " + q + "signal_type='tech'", p
    )
    s["tech_groups"] = dict(
        con.execute(
            "SELECT key, count(*) " + q + "signal_type='tech_group' GROUP BY 1 ORDER BY 2 DESC", p
        ).fetchall()
    )
    s["top_technologies"] = dict(
        con.execute(
            "SELECT key, count(*) " + q + "signal_type='tech' GROUP BY 1 ORDER BY 2 DESC, 1 "
            "LIMIT 15",
            p,
        ).fetchall()
    )
    s["sitemaps_found"] = _one(con, "SELECT count(*) " + q + "signal_type='sitemap_urls'", p)
    s["sitemap_urls_total"] = int(
        _one(con, "SELECT coalesce(sum(num),0) " + q + "signal_type='sitemap_urls'", p)
    )
    s["companies_with_page"] = dict(
        con.execute(
            "SELECT key, count(*) "
            + q
            + "signal_type='has_page' AND num > 0 GROUP BY 1 ORDER BY 1",
            p,
        ).fetchall()
    )
    s["content_hashes"] = _one(con, "SELECT count(*) " + q + "signal_type='content_hash'", p)
    s["funding_sources"] = {
        k: {"http_status": int(v), "items": json.loads(m).get("items")}
        for k, v, m in con.execute(
            "SELECT key, num, meta " + q + "signal_type='feed_status'", p
        ).fetchall()
    }
    s["funding_items_seen"] = _one(
        con, "SELECT coalesce(sum(num),0) " + q + "signal_type='funding_items_seen'", p
    )
    s["funding_matches"] = _one(con, "SELECT count(*) " + q + "signal_type='funding_news'", p)
    s["github_orgs_found"] = _one(con, "SELECT count(*) " + q + "signal_type='github_org'", p)
    s["github_api_status_counts"] = {
        str(int(k)): v
        for k, v in con.execute(
            "SELECT num, count(*) " + q + "signal_type='github_api_status' GROUP BY 1", p
        ).fetchall()
    }
    s["rows_by_collector"] = dict(
        con.execute("SELECT collector, count(*) " + q + "TRUE GROUP BY 1 ORDER BY 1", p).fetchall()
    )
    return s


def to_markdown(s: dict[str, Any]) -> str:
    lines = [
        f"# Snapshot summary - {s.get('snapshot_date')}",
        "",
        "Generated by `signal-backtest summary` from the Parquet snapshot (real data).",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for k, v in s.items():
        if k == "snapshot_date":
            continue
        val = json.dumps(v) if isinstance(v, dict) else str(v)
        lines.append(f"| {k} | {val} |")
    return "\n".join(lines) + "\n"
