"""Public Telegram channel collector.

Telegram exposes a server-rendered "web preview" of any public channel
at ``https://t.me/s/<channel>``. The HTML on that page contains the
last ~20 messages with their text, timestamp, and a deep link back to
the message in the Telegram app. There's no auth, no API key, no
documented rate limit — but the endpoint is intended for embedding,
not bulk pulls, so we keep the cadence polite.

Source config keys:
  channels             : list of channel handles (with or without leading @)
  limit_per_channel    : cap on messages yielded per channel (default 40)
  request_delay_s      : seconds to wait between channels (default 1.5)

This collector does NOT do server-side keyword search — Telegram's
preview endpoint doesn't support that reliably. The runner falls back
to local body matching for every yielded Document, which is exactly
what we want here.

Caveats:
  - Only public channels are reachable this way. Private/invite-only
    channels return a different page and yield nothing.
  - Telegram occasionally tweaks the HTML class names. The parser uses
    multiple fallbacks to stay resilient, but a structural change can
    still break it — hence the unit tests.
  - The preview shows roughly the last 20-30 messages. To pull deeper
    history you'd need MTProto auth, which is out of scope.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Iterable

import requests

from ..storage import Document
from .base import parse_dt


log = logging.getLogger(__name__)

BASE = "https://t.me/s/"
USER_AGENT = (
    "Mozilla/5.0 (compatible; osint-monitor/0.1; +https://example.test/bot)"
)

# A channel handle is alphanumeric + underscores; Telegram enforces this.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{3,}$")


def _clean_handle(raw: str) -> str | None:
    """Strip @, t.me/, and trailing slashes; return None if invalid."""
    h = (raw or "").strip()
    if not h:
        return None
    h = h.removeprefix("https://").removeprefix("http://")
    h = h.removeprefix("t.me/").removeprefix("telegram.me/")
    h = h.removeprefix("s/")  # in case user pasted t.me/s/<name>
    h = h.lstrip("@").rstrip("/")
    # Anything after another slash is a message id — drop it
    if "/" in h:
        h = h.split("/", 1)[0]
    return h if _HANDLE_RE.match(h) else None


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    """Yield Documents from every configured public channel.

    `keywords` is unused at fetch time (Telegram preview doesn't support
    server-side search); the runner re-checks the body via match_topics.
    """
    raw_channels = source_config.get("channels") or []
    channels: list[str] = []
    for raw in raw_channels:
        h = _clean_handle(raw)
        if h:
            channels.append(h)
        else:
            log.warning("skipping invalid channel handle: %r", raw)
    if not channels:
        log.warning("no channels configured; skipping")
        return

    limit = int(source_config.get("limit_per_channel", 40))
    delay = float(source_config.get("request_delay_s", 1.5))
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en;q=0.9"}

    for i, ch in enumerate(channels):
        if i > 0:
            time.sleep(delay)  # be a polite neighbor
        url = BASE + ch
        try:
            r = requests.get(url, headers=headers, timeout=20)
        except Exception as e:  # pragma: no cover
            log.warning("fetch %s failed: %s", url, e)
            continue
        if r.status_code == 404:
            log.warning("channel %r returned 404 (does it exist? is it public?)", ch)
            continue
        if r.status_code != 200:
            log.warning("channel %r returned HTTP %s", ch, r.status_code)
            continue
        try:
            yield from _parse_channel_html(r.text, channel=ch, limit=limit)
        except Exception as e:  # pragma: no cover
            log.warning("parse failed for %r: %s", ch, e)
            continue


def _parse_channel_html(
    html: str, channel: str, limit: int,
) -> Iterable[Document]:
    """Parse a t.me/s/<channel> preview page into Documents.

    Uses BeautifulSoup with the stdlib html.parser so we don't drag in
    lxml. The selectors are deliberately forgiving — Telegram has shifted
    classnames before. If selectors miss, we yield nothing (run records
    zero docs from this source) rather than crashing the run.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError(
            "beautifulsoup4 is required for telegram. pip install beautifulsoup4"
        ) from e

    soup = BeautifulSoup(html, "html.parser")
    # Channel display name lives at the page level, not per-message.
    owner_el = (
        soup.select_one(".tgme_channel_info_header_title")
        or soup.select_one(".tgme_page_title")
        or soup.select_one(".tgme_widget_message_owner_name")
    )
    owner_name = (owner_el.get_text(strip=True) if owner_el else channel) or channel

    messages = soup.select("div.tgme_widget_message")
    yielded = 0
    # The preview lists oldest→newest by default; reverse so newest land first
    # in the live stream. We still cap at `limit`.
    for msg in reversed(messages):
        if yielded >= limit:
            break

        post_id = msg.get("data-post") or ""
        # Format is "<channel>/<msg_id>" — keep that as the dedup key so
        # we don't collide with t.me URL variants.
        if "/" not in post_id:
            continue

        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""

        time_el = msg.select_one("a.tgme_widget_message_date time")
        when_iso = time_el.get("datetime") if time_el else None

        url = f"https://t.me/{post_id}"
        yield Document(
            source="telegram",
            source_id=post_id,
            author=owner_name,
            title="",
            content=text,
            url=url,
            created_at=parse_dt(when_iso),
            extra={
                "channel": channel,
                "msg_id": post_id.split("/", 1)[1],
                "has_media": bool(
                    msg.select_one(".tgme_widget_message_photo_wrap")
                    or msg.select_one(".tgme_widget_message_video_wrap")
                    or msg.select_one(".tgme_widget_message_document")
                ),
                "views": _parse_views(msg),
            },
        )
        yielded += 1


def _parse_views(msg) -> int | None:
    """Extract the message view-count, if present. Telegram formats it
    as e.g. '1.2K' or '732' — we coerce to int (round if necessary)."""
    el = msg.select_one(".tgme_widget_message_views")
    if not el:
        return None
    raw = el.get_text(strip=True)
    if not raw:
        return None
    try:
        if raw.endswith("K"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("M"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw.replace(",", ""))
    except ValueError:
        return None
