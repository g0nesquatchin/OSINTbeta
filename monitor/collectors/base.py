"""Shared helpers for collectors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


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
