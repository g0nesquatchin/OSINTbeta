"""Mastodon hashtag-timeline collector.

Source config:
    instance_url: e.g. https://mastodon.social
    access_token: optional, raises rate limits
    hashtags: list (without leading #). If empty, uses the topic keywords
              that are valid hashtag tokens.
    limit_per_hashtag
"""

from __future__ import annotations

import re
from typing import Iterable

import requests

from ..storage import Document
from .base import parse_dt


_TAG_RE = re.compile(r"<[^>]+>")
_HASHTAG_OK = re.compile(r"^[A-Za-z0-9_]+$")


def _strip_html(s: str) -> str:
    return _TAG_RE.sub("", s or "").strip()


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    base = (source_config.get("instance_url") or "").rstrip("/")
    if not base:
        print("[mastodon] no instance_url, skipping")
        return
    token = source_config.get("access_token") or ""
    hashtags = source_config.get("hashtags") or []
    if not hashtags:
        # Fall back to topic keywords that look like hashtag-safe tokens
        hashtags = [k for k in keywords if _HASHTAG_OK.match(k.replace("#", ""))]
    limit = int(source_config.get("limit_per_hashtag", 40))

    headers = {"User-Agent": "osint-monitor/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for tag in hashtags:
        clean = tag.lstrip("#")
        try:
            r = requests.get(
                f"{base}/api/v1/timelines/tag/{clean}",
                params={"limit": min(limit, 40)},
                headers=headers, timeout=15,
            )
            r.raise_for_status()
            items = r.json()
        except Exception as e:  # pragma: no cover
            print(f"[mastodon] #{clean} failed: {e}")
            continue
        for item in items:
            account = item.get("account", {}) or {}
            yield Document(
                source="mastodon",
                source_id=item.get("uri") or item.get("url") or str(item.get("id")),
                author=account.get("acct", ""),
                title="",
                content=_strip_html(item.get("content", "")),
                url=item.get("url", ""),
                created_at=parse_dt(item.get("created_at")),
                extra={
                    "hashtag": clean, "instance": base,
                    "reblogs": item.get("reblogs_count"),
                    "favourites": item.get("favourites_count"),
                },
            )
