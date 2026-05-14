"""Country aggregations for the world map.

Pulls documents that carry a `sourcecountry` field in their extra JSON
(currently just GDELT, but any future collector that records a country
will be picked up automatically). Counts are keyed by Natural Earth's
country names so the choropleth's TopoJSON joins cleanly.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .storage import MonitorStore


# Sources whose `extra.sourcecountry` we trust for the map.
COUNTRY_SOURCES = ("gdelt",)


# GDELT and other feeds use slightly different country names than the
# Natural Earth TopoJSON we render. Map the common cases so the join
# doesn't drop them silently.
GDELT_TO_NATURAL_EARTH = {
    "United States": "United States of America",
    "USA": "United States of America",
    "Tanzania": "United Republic of Tanzania",
    "Czech Republic": "Czechia",
    "Republic of Congo": "Republic of the Congo",
    "Democratic Republic of the Congo": "Dem. Rep. Congo",
    "Congo (Kinshasa)": "Dem. Rep. Congo",
    "Congo (Brazzaville)": "Republic of the Congo",
    "Ivory Coast": "Côte d'Ivoire",
    "Cote d'Ivoire": "Côte d'Ivoire",
    "Bosnia and Herzegovina": "Bosnia and Herz.",
    "Dominican Republic": "Dominican Rep.",
    "Central African Republic": "Central African Rep.",
    "South Sudan": "S. Sudan",
    "Equatorial Guinea": "Eq. Guinea",
    "Solomon Islands": "Solomon Is.",
    "Western Sahara": "W. Sahara",
    "Falkland Islands": "Falkland Is.",
    "Eswatini": "eSwatini",
    "Swaziland": "eSwatini",
    "North Macedonia": "Macedonia",
    "East Timor": "Timor-Leste",
    "Vatican": "Vatican",
    "Holy See": "Vatican",
    "Burma": "Myanmar",
    "Cape Verde": "Cabo Verde",
    "Brunei": "Brunei",
    "Russia": "Russia",
    "South Korea": "South Korea",
    "North Korea": "North Korea",
    "Syria": "Syria",
    "Iran": "Iran",
    "Laos": "Laos",
    "Vietnam": "Vietnam",
    "Bolivia": "Bolivia",
    "Tanzania, United Republic of": "United Republic of Tanzania",
    "Macedonia": "Macedonia",
    "Moldova": "Moldova",
    "Czechia": "Czechia",
}


def normalize_country(name: str) -> str:
    """Return the Natural Earth name for a given source country string."""
    if not name:
        return ""
    name = name.strip()
    return GDELT_TO_NATURAL_EARTH.get(name, name)


def reverse_aliases(natural_earth_name: str) -> list[str]:
    """Return every source-name that could map to this Natural Earth name."""
    out = [natural_earth_name]
    for src, dst in GDELT_TO_NATURAL_EARTH.items():
        if dst == natural_earth_name and src not in out:
            out.append(src)
    return out


def country_counts(
    store: MonitorStore,
    topic_id: Optional[int] = None,
    since: Optional[str] = None,
    sources: Optional[list[str]] = None,
) -> dict[str, int]:
    """Return {natural_earth_name: count} for matching documents."""
    sources = sources or list(COUNTRY_SOURCES)
    placeholders = ",".join("?" * len(sources))
    sql = [
        f"SELECT d.extra_json FROM documents d "
        f"WHERE d.source IN ({placeholders})"
    ]
    args: list = list(sources)
    if topic_id is not None:
        sql.append(
            "AND d.dedup_key IN (SELECT dedup_key FROM matches WHERE topic_id=?)"
        )
        args.append(topic_id)
    if since:
        sql.append("AND (d.created_at >= ? OR d.collected_at >= ?)")
        args.extend([since, since])

    rows = store.conn.execute(" ".join(sql), args).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        try:
            extra = json.loads(r["extra_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        raw = extra.get("sourcecountry") or ""
        if not raw:
            continue
        name = normalize_country(raw)
        counts[name] = counts.get(name, 0) + 1
    return counts


def articles_for_country(
    store: MonitorStore,
    country_name: str,
    topic_id: Optional[int] = None,
    since: Optional[str] = None,
    limit: int = 100,
    sources: Optional[list[str]] = None,
) -> list[sqlite3.Row]:
    """Articles whose source country matches `country_name` (Natural Earth)."""
    sources = sources or list(COUNTRY_SOURCES)
    aliases = reverse_aliases(country_name)
    # Build OR'd LIKE clauses against the JSON blob. Cheap & cheerful — for
    # the volumes this app handles it's fine.
    like_clauses = []
    args: list = []
    for alias in aliases:
        like_clauses.append("d.extra_json LIKE ?")
        args.append(f'%"sourcecountry": "{alias}"%')
    placeholders = ",".join("?" * len(sources))
    sql = [
        f"SELECT d.* FROM documents d "
        f"WHERE d.source IN ({placeholders}) "
        f"AND ({' OR '.join(like_clauses)})"
    ]
    args = list(sources) + args
    if topic_id is not None:
        sql.append(
            "AND d.dedup_key IN (SELECT dedup_key FROM matches WHERE topic_id=?)"
        )
        args.append(topic_id)
    if since:
        sql.append("AND (d.created_at >= ? OR d.collected_at >= ?)")
        args.extend([since, since])
    sql.append("ORDER BY d.collected_at DESC LIMIT ?")
    args.append(limit)
    return store.conn.execute(" ".join(sql), args).fetchall()
