"""GDELT DOC API collector.

Free, no API key, indexes news worldwide in ~100 languages every 15
minutes. The DOC API lets us query articles by keyword over the last
N days. Reference: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""

from __future__ import annotations

import logging
from typing import Iterable

import requests

from ..storage import Document
from .base import parse_dt


log = logging.getLogger(__name__)

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    """Yield articles for each keyword.

    `keywords` is the flat union of every enabled topic's keywords so
    GDELT does the work of finding mentions; the caller filters again
    by topic for tagging.

    source_config supports:
      timespan: "24h" / "7d" etc.
      max_records: 1..250
      language: optional GDELT language filter
      gdelt_codes: optional list of FIPS country codes
                   (e.g. ["US","UK","RS"]) — limits results to articles
                   from media outlets in those countries.
    """
    timespan = source_config.get("timespan", "24h")
    max_records = int(source_config.get("max_records", 75))
    language = source_config.get("language", "")  # empty == all languages
    gdelt_codes = [c for c in (source_config.get("gdelt_codes") or []) if c]

    headers = {"User-Agent": "osint-monitor/0.1"}
    for kw in keywords:
        query = _build_query(kw, language, gdelt_codes)
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": min(max(max_records, 1), 250),
            "timespan": timespan,
            "sort": "DateDesc",
        }
        try:
            r = requests.get(ENDPOINT, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # pragma: no cover
            log.warning("query for %r failed: %s", kw, e)
            continue
        for art in data.get("articles", []) or []:
            url = art.get("url") or ""
            if not url:
                continue
            yield Document(
                source="gdelt",
                source_id=url,
                author=art.get("domain", ""),
                title=art.get("title", "") or "",
                content=art.get("seendate", ""),  # GDELT doesn't ship body
                url=url,
                created_at=parse_dt(art.get("seendate")),
                extra={
                    "keyword": kw,
                    "domain": art.get("domain"),
                    "language": art.get("language"),
                    "sourcecountry": art.get("sourcecountry"),
                    "tone": art.get("tone"),
                    "socialimage": art.get("socialimage"),
                },
            )


def _build_query(keyword: str, language: str,
                 gdelt_codes: list[str] | None = None) -> str:
    """Build a GDELT DOC query string.

    Multi-word keywords need quoting; AND/OR/- operators are passed
    through. Language and sourcecountry are filter modifiers; multiple
    countries are OR'd inside parens.
    """
    q = keyword.strip()
    if " " in q and not (q.startswith('"') and q.endswith('"')):
        q = f'"{q}"'
    if language:
        q = f"{q} sourcelang:{language}"
    if gdelt_codes:
        if len(gdelt_codes) == 1:
            q = f"{q} sourcecountry:{gdelt_codes[0]}"
        else:
            clauses = " OR ".join(f"sourcecountry:{c}" for c in gdelt_codes)
            q = f"{q} ({clauses})"
    return q
