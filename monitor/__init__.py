"""Monitor: continuous keyword scraping across news + social sources.

This package complements the SpiderFoot-driven investigation workflow.
SpiderFoot is for target-driven enrichment ("tell me everything about
example.com"). Monitor is for keyword-driven watching ("tell me when
anyone mentions X anywhere").

Sources:
  - gdelt          Global news, every 15 min, ~100 languages, free
  - google_news    Google News RSS keyword search, free
  - rss            User-supplied RSS/Atom feed URLs
  - reddit         Reddit API (free key required)
  - bluesky        Bluesky public search (no key)
  - mastodon       Mastodon instance hashtag timeline
  - x_twitter      X v2 API (paid Basic tier required)

Not supported:
  - Facebook / Instagram. Meta has no public keyword-search API and
    actively litigates against scrapers. CrowdTangle (the one
    legitimate option) was shut down in August 2024.
"""

from .storage import MonitorStore, Document
from .filters import Topic, match_topics, MatchResult

__all__ = [
    "MonitorStore", "Document",
    "Topic", "match_topics", "MatchResult",
]


SOURCE_NAMES = [
    "gdelt", "google_news", "rss",
    "reddit", "bluesky", "mastodon", "x_twitter",
]
