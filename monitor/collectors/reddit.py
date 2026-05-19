"""Reddit collector using PRAW + the official API.

Source config:
    client_id, client_secret, user_agent
    subreddits: list (omit / empty -> use Reddit-wide search)
    limit: per-subreddit or per-query
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from ..storage import Document


log = logging.getLogger(__name__)


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    try:
        import praw
    except ImportError as e:
        raise RuntimeError(
            "praw is required for reddit. pip install praw"
        ) from e

    cid = source_config.get("client_id")
    csec = source_config.get("client_secret")
    ua = source_config.get("user_agent") or "osint-monitor/0.1"
    if not cid or not csec:
        log.warning("missing credentials, skipping")
        return

    reddit = praw.Reddit(client_id=cid, client_secret=csec, user_agent=ua)
    reddit.read_only = True

    limit = int(source_config.get("limit", 25))
    subreddits = source_config.get("subreddits") or []

    queries = keywords[:]
    targets = subreddits if subreddits else ["all"]

    for sub_name in targets:
        try:
            sub = reddit.subreddit(sub_name)
            for q in queries:
                try:
                    for post in sub.search(q, limit=limit, sort="new"):
                        yield Document(
                            source="reddit",
                            source_id=f"t3_{post.id}",
                            author=str(post.author) if post.author else "[deleted]",
                            title=post.title or "",
                            content=post.selftext or "",
                            url=f"https://reddit.com{post.permalink}",
                            created_at=datetime.fromtimestamp(
                                post.created_utc, tz=timezone.utc
                            ),
                            extra={
                                "subreddit": sub_name,
                                "score": post.score,
                                "num_comments": post.num_comments,
                                "search_term": q,
                            },
                        )
                except Exception as e:  # pragma: no cover
                    log.warning("search %r in r/%s failed: %s", q, sub_name, e)
        except Exception as e:  # pragma: no cover
            log.warning("r/%s unavailable: %s", sub_name, e)
