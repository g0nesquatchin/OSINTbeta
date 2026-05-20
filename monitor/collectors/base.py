"""Shared helpers for collectors."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


# News URLs very commonly embed the publication date in the path. We
# trust these more than GDELT's seendate (which is when GDELT first
# *indexed* the article, not when it was published — so an old article
# re-crawled today gets seendate=today) and more than RSS published
# timestamps (often the feed's update time, not the article's).
_URL_DATE_RE = re.compile(
    r"""
    /                                 # path segment boundary
    (?P<y>\d{4})                      # year
    [/\-_]                            # separator: / - _
    (?P<m>\d{1,2})                    # month
    [/\-_]                            # separator
    (?P<d>\d{1,2})                    # day
    (?=[/\-_.?#]|$)                   # bounded so /20250115/ doesn't match
    """,
    re.VERBOSE,
)

# Less precise fallback: /YYYY/MM/ when the day isn't in the URL.
# Negative lookahead: refuse to match when the next segment looks like
# a day (1-2 digits followed by a delimiter), so an invalid date like
# /2025/02/30/ correctly returns None instead of silently degrading to
# /2025/02/01/. That fallthrough was a real bug caught by tests.
_URL_YEAR_MONTH_RE = re.compile(
    r"/(\d{4})/(\d{1,2})/(?!\d{1,2}(?:[/\-_?#]|$))(?=[A-Za-z\-_])"
)

# Loose fallback: ?date=YYYY-MM-DD or similar query-param forms.
_URL_DATE_QPARAM_RE = re.compile(
    r"[?&](?:date|published|pubdate)=(\d{4})-(\d{1,2})-(\d{1,2})",
    re.IGNORECASE,
)


def extract_date_from_url(url: str) -> Optional[datetime]:
    """Find a publication date embedded in a news article URL.

    Returns an aware UTC datetime at midnight if a plausible date pattern
    is present, else None. Validates the date so things like
    ``/2025/99/99/`` don't fool us. Range-locked to 2000..2099 so random
    four-digit numbers don't pose as years.
    """
    if not url:
        return None
    for pattern in (_URL_DATE_RE, _URL_DATE_QPARAM_RE):
        m = pattern.search(url)
        if not m:
            continue
        try:
            y = int(m.group("y") if "y" in pattern.groupindex else m.group(1))
            mo = int(m.group("m") if "m" in pattern.groupindex else m.group(2))
            d = int(m.group("d") if "d" in pattern.groupindex else m.group(3))
            if not (2000 <= y <= 2099):
                continue
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            continue
    # Year + month only (day defaults to 01). Lower confidence but better
    # than nothing for outlets that use /YYYY/MM/slug-here patterns.
    m = _URL_YEAR_MONTH_RE.search(url)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            if 2000 <= y <= 2099 and 1 <= mo <= 12:
                return datetime(y, mo, 1, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            pass
    return None


def parse_dt(value) -> Optional[datetime]:
    """Best-effort parse of common timestamp shapes into aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        # Try GDELT's compact "YYYYMMDDTHHMMSSZ" first
        try:
            if len(value) == 16 and "T" in value and value.endswith("Z"):
                return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
        except ValueError:
            pass
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(value, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None
