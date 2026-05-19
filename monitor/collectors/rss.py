"""Generic RSS / Atom feed collector.

Source config:
    feeds: list of URLs
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..storage import Document
from .base import parse_dt


log = logging.getLogger(__name__)


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    try:
        import feedparser
    except ImportError as e:
        raise RuntimeError(
            "feedparser is required for rss. pip install feedparser"
        ) from e

    for url in source_config.get("feeds", []) or []:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:  # pragma: no cover
            log.warning("fetch failed for %s: %s", url, e)
            continue
        feed_title = (parsed.feed.get("title") or url) if parsed.feed else url
        for entry in parsed.entries:
            link = entry.get("link") or entry.get("id") or ""
            if not link:
                continue
            yield Document(
                source="rss",
                source_id=link,
                author=feed_title,
                title=entry.get("title", "") or "",
                content=entry.get("summary") or entry.get("description") or "",
                url=link,
                created_at=parse_dt(entry.get("published") or entry.get("updated")),
                extra={"feed_url": url},
            )
