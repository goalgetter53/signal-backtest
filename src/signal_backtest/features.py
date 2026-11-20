"""Turn timestamped observations into point-in-time features.

Look-ahead guard: :func:`features_as_of` only ever sees observations with
``observed_at < as_of``. The backtest passes each account's *first send* time as ``as_of``.

Two kinds of features, both interpretable:

* **state** - read from the latest snapshot before ``as_of`` (e.g. "has open web roles",
  "site runs on Wix/Squarespace").
* **event** - changes between consecutive snapshots (tech added, CMS migration, redesign,
  new key page, new web role) and dated news (funding). Each event contributes
  ``0.5 ** (age_days / half_life)``; the feature value is the max over events (0..1).
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .collectors.techstack import TECH_GROUPS
from .store import Observation

HALF_LIFE_DAYS: dict[str, float] = {
    "recent_funding": 60.0,
    "cms_migration": 45.0,
    "redesign": 30.0,
    "marketing_tool_added": 30.0,
    "new_key_page": 30.0,
    "new_web_role": 21.0,
    "sitemap_growth": 30.0,
}
STATE_FEATURES = [
    "hiring_web",
    "hiring_marketing",
    "hiring_design",
    "hiring_any",
    "legacy_builder",
    "wordpress",
    "no_marketing_stack",
    "github_active",
]
EVENT_FEATURES = list(HALF_LIFE_DAYS)
ALL_FEATURES = STATE_FEATURES + EVENT_FEATURES
ACTIVE_THRESHOLD = 0.25  # an event feature counts as "present" within ~2 half-lives


@dataclass(frozen=True)
class Event:
    account_id: str
    kind: str
    at: datetime
    detail: str = ""


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _meta(o: Observation) -> dict[str, object]:
    try:
        m = json.loads(o.meta) if o.meta else {}
        return m if isinstance(m, dict) else {}
    except ValueError:
        return {}


def _snapshots(obs: Iterable[Observation]) -> dict[str, dict[str, list[Observation]]]:
    """account -> observed_at (snapshot run) -> rows, for collectors that produce state."""
    out: dict[str, dict[str, list[Observation]]] = defaultdict(lambda: defaultdict(list))
    for o in obs:
        if o.collector != "funding":
            out[o.account_id][o.observed_at[:10]].append(o)
    return out


def _state(rows: list[Observation], include_guessed_boards: bool = True) -> dict[str, object]:
    st: dict[str, object] = {
        "techs": set(),
        "groups": set(),
        "roles": {},
        "pages": {},
        "web_jobs": set(),
        "content": None,
        "structure": None,
        "sitemap": None,
        "scanned": False,
        "github_push": None,
    }
    techs: set[str] = set()
    groups: set[str] = set()
    roles: dict[str, float] = defaultdict(float)
    pages: dict[str, float] = {}
    web_jobs: set[str] = set()
    for o in rows:
        if o.signal_type == "tech":
            techs.add(o.key)
        elif o.signal_type == "tech_group":
            groups.add(o.key)
        elif o.signal_type == "tech_scan" and o.value == "ok":
            st["scanned"] = True
        elif o.signal_type.startswith("open_roles_"):
            if include_guessed_boards or _meta(o).get("method") != "guess":
                roles[o.signal_type.removeprefix("open_roles_")] += o.num or 0.0
        elif o.signal_type == "job_posting":
            m = _meta(o)
            roles_of = m.get("roles")
            if (
                isinstance(roles_of, list)
                and "web" in roles_of
                and (include_guessed_boards or m.get("method") != "guess")
            ):
                web_jobs.add(o.key)
        elif o.signal_type == "has_page":
            pages[o.key] = o.num or 0.0
        elif o.signal_type == "content_hash":
            st["content"] = o.value
        elif o.signal_type == "structure_hash":
            st["structure"] = o.value
        elif o.signal_type == "sitemap_urls":
            st["sitemap"] = o.num
        elif o.signal_type == "repos_pushed_30d":
            st["github_push"] = o.num
    st.update(techs=techs, groups=groups, roles=dict(roles), pages=pages, web_jobs=web_jobs)
    return st


def events_from_snapshots(obs: Iterable[Observation]) -> list[Event]:
    obs = list(obs)
    events: list[Event] = []
    for o in obs:
        if o.signal_type == "funding_news":
            events.append(Event(o.account_id, "recent_funding", parse_ts(o.observed_at), o.value))
    marketing = TECH_GROUPS["marketing_stack"]
    modern = TECH_GROUPS["modern_builder"]
    old_cms = TECH_GROUPS["legacy_builder"] | TECH_GROUPS["wordpress"]
    for aid, snaps in _snapshots(obs).items():
        days = sorted(snaps)
        for prev_d, cur_d in itertools.pairwise(days):
            prev, cur = _state(snaps[prev_d]), _state(snaps[cur_d])
            at = parse_ts(min(o.observed_at for o in snaps[cur_d]))
            pt, ct = prev["techs"], cur["techs"]
            assert isinstance(pt, set) and isinstance(ct, set)
            if prev["scanned"] and cur["scanned"]:
                added = ct - pt
                if (pt & old_cms) and (added & modern) and not (ct & old_cms):
                    events.append(Event(aid, "cms_migration", at, ",".join(sorted(added & modern))))
                if added & marketing:
                    events.append(
                        Event(aid, "marketing_tool_added", at, ",".join(sorted(added & marketing)))
                    )
            if prev["structure"] and cur["structure"] and prev["structure"] != cur["structure"]:
                events.append(Event(aid, "redesign", at))
            pp, cp = prev["pages"], cur["pages"]
            assert isinstance(pp, dict) and isinstance(cp, dict)
            for k, v in cp.items():
                if v > 0 and pp.get(k, 1.0) == 0:
                    events.append(Event(aid, "new_key_page", at, k))
            ps, cs = prev["sitemap"], cur["sitemap"]
            if (
                isinstance(ps, float)
                and isinstance(cs, float)
                and ps > 0
                and (cs - ps >= max(10.0, 0.1 * ps))
            ):
                events.append(Event(aid, "sitemap_growth", at, f"{ps:.0f}->{cs:.0f}"))
            pw, cw = prev["web_jobs"], cur["web_jobs"]
            assert isinstance(pw, set) and isinstance(cw, set)
            if cw - pw:
                events.append(Event(aid, "new_web_role", at, str(len(cw - pw))))
    return events


def features_as_of(
    obs: Iterable[Observation],
    as_of: dict[str, datetime],
    include_guessed_boards: bool = True,
) -> dict[str, dict[str, float]]:
    """Point-in-time features per account. ``as_of`` maps account_id -> cutoff (exclusive).

    Accounts with no observation before their cutoff are omitted (cannot be scored honestly).
    """
    by_acc: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        cut = as_of.get(o.account_id)
        if cut is not None and parse_ts(o.observed_at) < cut:
            by_acc[o.account_id].append(o)
    events = events_from_snapshots(o for rows in by_acc.values() for o in rows)
    ev_by_acc: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        ev_by_acc[e.account_id].append(e)

    out: dict[str, dict[str, float]] = {}
    for aid, rows in by_acc.items():
        snaps = _snapshots(rows).get(aid, {})
        latest = _state(snaps[max(snaps)], include_guessed_boards) if snaps else _state([])
        roles = latest["roles"]
        groups = latest["groups"]
        assert isinstance(roles, dict) and isinstance(groups, set)
        f: dict[str, float] = {
            "hiring_web": float(roles.get("web", 0) > 0),
            "hiring_marketing": float(roles.get("marketing", 0) > 0),
            "hiring_design": float(roles.get("design", 0) > 0),
            "hiring_any": float(roles.get("total", 0) > 0),
            "legacy_builder": float("legacy_builder" in groups),
            "wordpress": float("wordpress" in groups),
            "no_marketing_stack": float(
                bool(latest["scanned"]) and "marketing_stack" not in groups
            ),
            "github_active": float((latest["github_push"] or 0) > 0),  # type: ignore[operator]
        }
        cut = as_of[aid]
        for kind, hl in HALF_LIFE_DAYS.items():
            vals = [
                0.5 ** (max((cut - e.at).total_seconds(), 0) / 86400 / hl)
                for e in ev_by_acc.get(aid, [])
                if e.kind == kind and e.at < cut
            ]
            f[kind] = max(vals, default=0.0)
        out[aid] = f
    return out


def is_active(name: str, value: float) -> bool:
    return value >= ACTIVE_THRESHOLD if name in HALF_LIFE_DAYS else value > 0
