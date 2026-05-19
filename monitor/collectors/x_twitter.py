"""X / Twitter v2 API collector. Requires paid Basic tier."""

from __future__ import annotations

import logging
from typing import Iterable

from ..storage import Document
from .base import parse_dt


log = logging.getLogger(__name__)


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    bearer = source_config.get("bearer_token") or ""
    if not bearer:
        log.warning("missing bearer_token, skipping")
        return
    try:
        import tweepy
    except ImportError as e:
        raise RuntimeError(
            "tweepy is required for x_twitter. pip install tweepy"
        ) from e

    max_results = int(source_config.get("max_results_per_query", 50))
    extra_queries = source_config.get("search_queries") or []
    queries = list(keywords) + extra_queries

    client = tweepy.Client(bearer_token=bearer, wait_on_rate_limit=True)
    for q in queries:
        try:
            resp = client.search_recent_tweets(
                query=q,
                max_results=min(max(max_results, 10), 100),
                tweet_fields=["created_at", "author_id", "public_metrics", "lang"],
                expansions=["author_id"],
                user_fields=["username"],
            )
        except Exception as e:  # pragma: no cover
            log.warning("%r failed: %s", q, e)
            continue
        users = {}
        if resp.includes and "users" in resp.includes:
            for u in resp.includes["users"]:
                users[u.id] = u.username
        for t in resp.data or []:
            username = users.get(t.author_id, str(t.author_id))
            url = f"https://x.com/{username}/status/{t.id}"
            yield Document(
                source="x_twitter",
                source_id=str(t.id),
                author=username,
                title="",
                content=t.text or "",
                url=url,
                created_at=parse_dt(t.created_at),
                extra={
                    "query": q,
                    "metrics": dict(t.public_metrics or {}),
                    "lang": t.lang,
                },
            )
