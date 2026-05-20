"""URL canonicalization for cross-source article dedup.

When GDELT, Google News, and a configured RSS feed all carry the same
NYT article you currently see three rows in the matches table — same
underlying story, slightly different URLs (tracking params, AMP path,
mobile prefix, trailing slash, etc.). ``canonicalize(url)`` produces
a stable key we can group on to collapse those duplicates.

Scope:
  - Lowercase scheme + host
  - Strip leading "www."
  - Strip AMP path segments and ".amp.html" suffixes
  - Strip the fragment (#…)
  - Drop tracking query params (utm_*, fbclid, gclid, mc_*, igshid,
    ref, ref_*, source, _ga, mkt_tok)
  - Sort remaining query params alphabetically (canonical ordering so
    "?a=1&b=2" and "?b=2&a=1" hash the same)
  - Strip a single trailing slash from the path

What we do NOT do:
  - Resolve Google News redirect URLs to their underlying article. The
    encoding is an undocumented protobuf-in-base64 and changes often.
  - Strip non-tracking but normalization-fragile params (page, sort).
  - Cross-syndication dedup (same wire story published at three
    outlets under three real URLs) — that needs content-hashing.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


# Params that are purely for tracking / attribution and don't change
# the article being viewed. Match exact name or a prefix (e.g. "utm_").
_TRACKING_EXACT = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid",
    "ref", "source", "_ga", "mkt_tok", "yclid", "msclkid",
    "dclid", "cmpid", "smid",
}
_TRACKING_PREFIX = ("utm_", "ref_")


def _is_tracking(key: str) -> bool:
    k = key.lower()
    if k in _TRACKING_EXACT:
        return True
    return any(k.startswith(p) for p in _TRACKING_PREFIX)


def canonicalize(url: str) -> str:
    """Return a canonical form of `url` suitable for cross-source dedup.

    Idempotent: ``canonicalize(canonicalize(u)) == canonicalize(u)``.
    Returns the input unchanged if parsing fails or the URL has no
    scheme/host (we don't try to invent one).
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url

    if not parts.scheme or not parts.netloc:
        return url

    scheme = parts.scheme.lower()
    host = parts.netloc.lower()

    # Strip an optional :port if it's the default for the scheme.
    if (scheme == "http" and host.endswith(":80")) or \
       (scheme == "https" and host.endswith(":443")):
        host = host.rsplit(":", 1)[0]

    # Strip a single leading "www." (but only one — don't accidentally
    # mangle hostnames where "www." is part of a subdomain).
    if host.startswith("www."):
        host = host[4:]

    # Path: drop "/amp/" segments and "amp.html" suffixes, strip a
    # single trailing slash (but never the only slash on a bare host).
    path = parts.path or ""
    # Replace "/amp/" mid-path
    path = path.replace("/amp/", "/")
    # Strip "/amp" tail
    if path.endswith("/amp"):
        path = path[:-4]
    if path.endswith(".amp.html"):
        path = path[:-9] + ".html"
    if path.endswith(".amp"):
        path = path[:-4]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    # Query: drop tracking, sort the rest by (key, value).
    keep = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ]
    keep.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    query = urlencode(keep, doseq=True)

    # Drop the fragment entirely. Fragments are client-side only and
    # never identify the article.
    fragment = ""

    return urlunsplit((scheme, host, path, query, fragment))
