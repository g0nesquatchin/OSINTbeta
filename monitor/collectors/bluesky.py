"""Bluesky public search collector."""

from __future__ import annotations

import time
from typing import Iterable

import requests

from ..storage import Document
from .base import parse_dt


SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    limit = int(source_config.get("limit_per_term", 50))
    headers = {"User-Agent": "osint-monitor/0.1"}
    for term in keywords:
        try:
            r = requests.get(
                SEARCH_URL,
                params={"q": term, "limit": min(limit, 100)},
                headers=headers, timeout=15,
            )
            r.raise_for_status()
            posts = r.json().get("posts", [])
        except Exception as e:  # pragma: no cover
            print(f"[bluesky] {term!r} failed: {e}")
            posts = []
        for p in posts:
            record = p.get("record", {}) or {}
            author = p.get("author", {}) or {}
            handle = author.get("handle", "")
            uri = p.get("uri", "")
            rkey = uri.rsplit("/", 1)[-1] if uri else ""
            url = (
                f"https://bsky.app/profile/{handle}/post/{rkey}"
                if handle and rkey else uri
            )
            yield Document(
                source="bluesky",
                source_id=uri or url,
                author=handle,
                title="",
                content=record.get("text", "") or "",
                url=url,
                created_at=parse_dt(record.get("createdAt")),
                extra={
                    "search_term": term,
                    "like_count": p.get("likeCount"),
                    "repost_count": p.get("repostCount"),
                },
            )
        time.sleep(0.5)
