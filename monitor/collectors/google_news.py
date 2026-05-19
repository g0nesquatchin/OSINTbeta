"""Google News keyword search via its public RSS endpoint.

Free, no key. Format:
  https://news.google.com/rss/search?q=QUERY&hl=LANG&gl=COUNTRY&ceid=COUNTRY:LANG
"""

from __future__ import annotations

import logging
from typing import Iterable
from urllib.parse import quote_plus

from ..storage import Document
from .base import parse_dt


log = logging.getLogger(__name__)


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    """Iterate every (keyword, locale) pair.

    source_config supports:
      lang, country, ceid : default locale if no `locales` provided
      locales: optional list of {"gl": "GB", "hl": "en-GB"} dicts.
               When present, run the search once per locale and tag the
               results with that locale.
    """
    try:
        import feedparser
    except ImportError as e:
        raise RuntimeError(
            "feedparser is required for google_news. pip install feedparser"
        ) from e

    locales = source_config.get("locales") or []
    if not locales:
        locales = [{
            "gl": source_config.get("country", "US"),
            "hl": source_config.get("lang", "en-US"),
        }]

    for loc in locales:
        gl = loc.get("gl") or "US"
        hl = loc.get("hl") or "en-US"
        ceid = loc.get("ceid") or f"{gl}:{hl.split('-')[0]}"
        for kw in keywords:
            q = quote_plus(kw)
            url = (
                f"https://news.google.com/rss/search?q={q}"
                f"&hl={hl}&gl={gl}&ceid={ceid}"
            )
            try:
                parsed = feedparser.parse(url)
            except Exception as e:  # pragma: no cover
                log.warning("%r (%s) failed: %s", kw, gl, e)
                continue
            for entry in parsed.entries:
                link = entry.get("link") or ""
                if not link:
                    continue
                src = entry.get("source")
                if isinstance(src, dict):
                    author = src.get("title", "Google News")
                else:
                    author = src or "Google News"
                yield Document(
                    source="google_news",
                    source_id=link,
                    author=author,
                    title=entry.get("title", "") or "",
                    content=entry.get("summary", "") or "",
                    url=link,
                    created_at=parse_dt(entry.get("published") or entry.get("updated")),
                    extra={
                        "keyword": kw,
                        "country": gl,
                        "lang": hl,
                        # surface as sourcecountry too so the map picks it up
                        "sourcecountry": gl,
                    },
                )
