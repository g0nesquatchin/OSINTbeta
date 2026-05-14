"""Client for GDELT's Geo API.

The Geo API takes a keyword query and returns a GeoJSON of locations
worldwide where the keyword is being mentioned, with per-point article
counts and HTML lists of the underlying articles. Perfect for a
pan/zoom map: we get lat/lon for every place a story is being told,
not just the country of the outlet.

Endpoint reference:
  https://api.gdeltproject.org/api/v2/geo/geo

Free, no API key. We cache responses for a few minutes because each
query takes ~2-5s and we don't want to hammer GDELT.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests


# The DOC API in PointData mode returns the same GeoJSON shape that the
# old standalone geo endpoint used to. The /geo/geo path 404s now;
# everything routes through /doc/doc.
ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

_TTL_S = 300  # 5 minutes
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class GeoApiError(RuntimeError):
    pass


def build_query(keywords: list[str]) -> str:
    """OR-combine keywords, quoting any that contain spaces."""
    parts: list[str] = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        if " " in kw and not (kw.startswith('"') and kw.endswith('"')):
            kw = f'"{kw}"'
        parts.append(kw)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def fetch_geo(
    query: str,
    timespan: str = "24h",
    maxpoints: int = 500,
    timeout: float = 25.0,
) -> dict:
    """Run the Geo API for `query`, return parsed GeoJSON. Cached."""
    if not query:
        raise GeoApiError("Empty query.")
    key = (query, timespan, int(maxpoints))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _TTL_S:
            return hit[1]

    params = {
        "query": query,
        "mode": "PointData",
        "format": "GeoJSON",
        "timespan": timespan,
        "maxpoints": min(max(int(maxpoints), 10), 1000),
    }
    try:
        r = requests.get(
            ENDPOINT, params=params, timeout=timeout,
            headers={"User-Agent": "osint-monitor/0.1"},
        )
    except requests.RequestException as e:
        raise GeoApiError(f"Network error: {e}") from e
    if r.status_code == 404:
        raise GeoApiError(
            "GDELT returned 404 — the API path may have changed. "
            "Try updating the app or reporting this."
        )
    if r.status_code != 200:
        raise GeoApiError(f"GDELT returned {r.status_code}: {r.text[:200]}")
    body = r.text or ""
    # GDELT's rate-limit response is a 200 with a plain-text body, not JSON
    if "Please limit requests" in body[:200]:
        raise GeoApiError(
            "GDELT is rate-limiting this client. Wait ~5 seconds and try "
            "again — cached results will be reused."
        )
    if not body.strip():
        # Empty response means zero matches
        data = {"type": "FeatureCollection", "features": []}
        with _cache_lock:
            _cache[key] = (now, data)
        return data
    try:
        data = r.json()
    except ValueError as e:
        raise GeoApiError(f"GDELT returned non-JSON: {body[:200]}") from e

    # GDELT sometimes returns an empty body wrapped as {} when nothing matches
    features = (data.get("features") if isinstance(data, dict) else None) or []
    if not isinstance(data, dict) or "features" not in data:
        # Normalize to a valid empty FeatureCollection
        data = {"type": "FeatureCollection", "features": []}
    data["features"] = features
    with _cache_lock:
        _cache[key] = (now, data)
    return data


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
