"""Client for GDELT's Geo API.

Takes a keyword query and returns a GeoJSON of locations worldwide
where the keyword is being mentioned, with per-point article counts
and HTML lists of the underlying articles. Perfect for a pan/zoom
map: we get lat/lon for every place a story is being told, not just
the country of the outlet.

We route through the DOC API in PointData/GeoJSON mode — the old
standalone /geo/geo path 404s now.

GDELT rate-limits to one request per 5 seconds per IP and bans
chatty clients aggressively. We:
  - cache responses for 5 minutes in memory
  - persist that cache to disk so app restarts don't re-hit GDELT
  - self-throttle so back-to-back queries always wait the gap
  - retry once on a 429 with a longer wait
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any

import requests


ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Cache TTL and rate-limit tuning
_TTL_S = 300                  # cache lifetime
_MIN_GAP_S = 5.5              # minimum seconds between outbound requests
_RETRY_WAIT_S = 7.0           # wait after a 429 before retrying once

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

_throttle_lock = threading.Lock()
_last_request_at: float = 0.0

_CACHE_FILE = os.environ.get(
    "GDELT_CACHE_FILE",
    os.path.join(tempfile.gettempdir(), "osint_gdelt_cache.json"),
)


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


# --- disk cache --------------------------------------------------


def _load_disk_cache() -> None:
    if not os.path.exists(_CACHE_FILE):
        return
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    now = time.time()
    with _cache_lock:
        for entry in raw:
            try:
                key = tuple(entry["key"])
                ts = float(entry["ts"])
                data = entry["data"]
            except (KeyError, TypeError, ValueError):
                continue
            if now - ts < _TTL_S:
                _cache[key] = (ts, data)


def _save_disk_cache() -> None:
    with _cache_lock:
        items = [
            {"key": list(k), "ts": ts, "data": v}
            for k, (ts, v) in _cache.items()
        ]
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f)
    except OSError:
        pass


_load_disk_cache()


# --- helpers -----------------------------------------------------


def _wait_for_slot() -> None:
    """Sleep so the next outbound request respects the 5s gap."""
    global _last_request_at
    with _throttle_lock:
        now = time.time()
        wait = (_last_request_at + _MIN_GAP_S) - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


def _looks_rate_limited(status: int, body: str) -> bool:
    if status == 429:
        return True
    if "Please limit requests" in (body or "")[:200]:
        return True
    return False


def _do_request(query: str, timespan: str, maxpoints: int,
                timeout: float) -> tuple[int, str]:
    """One GET to GDELT — returns (status, body). Throttled."""
    _wait_for_slot()
    params = {
        "query": query,
        "mode": "PointData",
        "format": "GeoJSON",
        "timespan": timespan,
        "maxpoints": min(max(int(maxpoints), 10), 1000),
    }
    r = requests.get(
        ENDPOINT, params=params, timeout=timeout,
        headers={"User-Agent": "osint-monitor/0.1 (research)"},
    )
    return r.status_code, (r.text or "")


# --- main entrypoint ---------------------------------------------


def fetch_geo(
    query: str,
    timespan: str = "24h",
    maxpoints: int = 500,
    timeout: float = 25.0,
) -> dict:
    """Run the Geo API for `query`, return parsed GeoJSON.

    Cached on-disk for 5 minutes. Auto-retries once on rate-limit
    with a longer wait.
    """
    if not query:
        raise GeoApiError("Empty query.")
    key = (query, timespan, int(maxpoints))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _TTL_S:
            return hit[1]

    # First attempt
    try:
        status, body = _do_request(query, timespan, maxpoints, timeout)
    except requests.RequestException as e:
        raise GeoApiError(f"Network error: {e}") from e

    if _looks_rate_limited(status, body):
        # Wait and retry once
        time.sleep(_RETRY_WAIT_S)
        try:
            status, body = _do_request(query, timespan, maxpoints, timeout)
        except requests.RequestException as e:
            raise GeoApiError(f"Network error: {e}") from e
        if _looks_rate_limited(status, body):
            raise GeoApiError(
                "GDELT is rate-limiting this IP right now. Cached "
                "results are still used; for fresh queries, wait "
                "about 30 seconds and try again."
            )

    if status == 404:
        raise GeoApiError(
            "GDELT returned 404 — the API path may have changed."
        )
    if status != 200:
        raise GeoApiError(f"GDELT returned {status}: {body[:200]}")

    body = body.strip()
    if not body:
        data = {"type": "FeatureCollection", "features": []}
    else:
        try:
            data = json.loads(body)
        except ValueError as e:
            raise GeoApiError(f"GDELT returned non-JSON: {body[:200]}") from e
        if not isinstance(data, dict) or "features" not in data:
            data = {"type": "FeatureCollection", "features": []}

    data["features"] = data.get("features") or []
    with _cache_lock:
        _cache[key] = (time.time(), data)
    _save_disk_cache()
    return data


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
    try:
        if os.path.exists(_CACHE_FILE):
            os.unlink(_CACHE_FILE)
    except OSError:
        pass
