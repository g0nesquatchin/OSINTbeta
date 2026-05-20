"""Bluesky public search collector."""

from __future__ import annotations

import logging
import time
from typing import Iterable

import requests

from ..storage import Document
from ._throttle import BLUESKY as _throttle
from .base import parse_dt


log = logging.getLogger(__name__)


SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
# A more browser-like UA. Bluesky's public endpoint has gotten stricter
# about anonymous traffic and seems to be flagging obvious-bot UAs with
# 403. This isn't a guarantee — sustained use really wants an App
# Password auth header — but it's the cheapest improvement.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 osint-monitor/0.1"
)
_RETRY_WAIT_S = 4.0


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    limit = int(source_config.get("limit_per_term", 50))
    # Optional App-Password JWT for sustained use. See README for the
    # auth flow; without it the public endpoint will 403 you under load.
    access_jwt = (source_config.get("access_jwt") or "").strip()

    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if access_jwt:
        headers["Authorization"] = f"Bearer {access_jwt}"

    forbidden_seen = 0
    for term in keywords:
        posts = []
        for attempt in (1, 2):
            _throttle.wait()
            try:
                r = requests.get(
                    SEARCH_URL,
                    params={"q": term, "limit": min(limit, 100)},
                    headers=headers, timeout=15,
                )
            except requests.RequestException as e:  # pragma: no cover
                log.warning("%r failed: %s", term, e)
                break
            if r.status_code in (429, 403):
                if attempt == 1:
                    # Bump the throttle and retry once.
                    _throttle.set_min_gap(_throttle.min_gap_s + 0.5)
                    log.info("%s for %r, backing off %ss",
                             r.status_code, term, _RETRY_WAIT_S)
                    time.sleep(_RETRY_WAIT_S)
                    continue
                log.warning("%r got %s twice; skipping", term, r.status_code)
                forbidden_seen += 1
                break
            if r.status_code != 200:
                log.warning("%r returned HTTP %s", term, r.status_code)
                break
            try:
                posts = r.json().get("posts", []) or []
            except ValueError as e:
                log.warning("%r returned non-JSON: %s", term, e)
            break
        # If Bluesky has 403'd us several times in a row, bail on the
        # rest of the keywords this run — we're clearly being shaped.
        if forbidden_seen >= 3 and not access_jwt:
            log.warning(
                "bluesky 403'd %d keywords; bailing this run. "
                "Configure access_jwt (App Password) for sustained use.",
                forbidden_seen,
            )
            return
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
        # No explicit sleep here — _throttle.wait() at the top of the
        # next iteration already enforces the gap.
