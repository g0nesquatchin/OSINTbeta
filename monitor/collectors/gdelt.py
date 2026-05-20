"""GDELT DOC API collector.

Free, no API key, indexes news worldwide in ~100 languages every 15
minutes. The DOC API lets us query articles by keyword over the last
N days. Reference: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/

This collector OR-batches your topic keywords into a single GDELT
query — instead of N requests at 5s/each, we issue ~ceil(N/25)
requests. The runner's local match_topics() then re-attributes each
returned article to the originating topic via title-text match.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

import requests

from ..storage import Document
from ._throttle import GDELT_API as _throttle
from .base import extract_date_from_url, parse_dt


log = logging.getLogger(__name__)

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT recommends one request per 5 seconds. We use the shared
# GDELT_API throttle (which the Geo API client also uses) so multiple
# callers coordinate. On 429 we wait this much before one retry.
_RETRY_WAIT_S = 7.0

# Max keywords per batched OR query. GDELT permits long queries but
# the API URL has practical limits (~2 KB) and overly-long OR clauses
# degrade precision. 25 is comfortable headroom; users can lower it via
# source_config.batch_size if needed.
_DEFAULT_BATCH_SIZE = 25


def collect(source_config: dict, keywords: list[str]) -> Iterable[Document]:
    """Yield articles from GDELT across the topic keyword union.

    Keywords are OR-batched (default 25 per batch) into a single query
    per batch — far gentler on GDELT's rate-limit than firing one
    request per keyword. The runner's local match_topics() then maps
    each article back to the originating topic via title-text match.

    source_config supports:
      timespan: "24h" / "7d" etc.
      max_records: 1..250 per batch (default 75)
      language: optional GDELT language filter (e.g. "english")
      gdelt_codes: optional list of FIPS country codes
                   (e.g. ["US","UK","RS"]) — limits results to articles
                   from media outlets in those countries.
      max_age_days: drop articles whose detected publication date is
                    older than now - max_age_days. Default 30. Set to
                    0 to disable.
      batch_size:   max keywords per OR'd query. Default 25.

    GDELT periodically re-crawls old articles. Without `max_age_days`
    your DB will accumulate articles published months/years ago whose
    GDELT seendate is recent. The guardrail prevents that.
    """
    timespan = source_config.get("timespan", "24h")
    # 250 is GDELT's hard ceiling per request, and with OR-batching we
    # only spend one request per ~25 keywords, so it's safe to default
    # high. User can lower via source_config.max_records.
    max_records = int(source_config.get("max_records", 250))
    language = source_config.get("language", "")  # empty == all languages
    gdelt_codes = [c for c in (source_config.get("gdelt_codes") or []) if c]
    try:
        max_age_days = int(source_config.get("max_age_days", 30))
    except (TypeError, ValueError):
        max_age_days = 30
    try:
        batch_size = max(1, int(source_config.get("batch_size", _DEFAULT_BATCH_SIZE)))
    except (TypeError, ValueError):
        batch_size = _DEFAULT_BATCH_SIZE

    age_cutoff: datetime | None = None
    if max_age_days > 0:
        age_cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    headers = {"User-Agent": "osint-monitor/0.1 (research)"}
    batches = list(_chunk(keywords, batch_size))
    if len(batches) > 1:
        log.info(
            "splitting %d keywords into %d OR-batched queries (batch_size=%d)",
            len(keywords), len(batches), batch_size,
        )

    for batch in batches:
        query = _build_batched_query(batch, language, gdelt_codes)
        if not query:
            continue
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": min(max(max_records, 1), 250),
            "timespan": timespan,
            "sort": "DateDesc",
        }
        # Use a short label for logging instead of the full OR'd body.
        label = _short_label(batch)
        data = _fetch_with_retry(label, params, headers)
        if data is None:
            continue
        for art in data.get("articles", []) or []:
            url = art.get("url") or ""
            if not url:
                continue
            # GDELT's `seendate` is when GDELT first *indexed* the URL, not
            # the article's actual publication date. Re-crawled old
            # articles will show today's seendate even when they're years
            # old. Prefer a date embedded in the URL itself — that's a
            # near-perfect ground truth for most news outlets.
            seendate = parse_dt(art.get("seendate"))
            url_date = extract_date_from_url(url)
            if url_date is not None:
                created_at = url_date
                date_source = "url"
            else:
                created_at = seendate
                date_source = "gdelt_seendate"
            # Guardrail: drop articles whose detected publication date
            # predates the age cutoff. Without this, GDELT's re-crawl
            # behavior would let December-2024 articles into a "fresh
            # news" view because their seendate is today.
            if age_cutoff is not None and created_at is not None \
                    and created_at < age_cutoff:
                continue
            yield Document(
                source="gdelt",
                source_id=url,
                author=art.get("domain", ""),
                title=art.get("title", "") or "",
                content=art.get("seendate", ""),  # GDELT doesn't ship body
                url=url,
                created_at=created_at,
                extra={
                    # NB: with OR-batching we no longer know which exact
                    # keyword GDELT matched. The runner's match_topics
                    # re-derives topic membership from the title text,
                    # which is GDELT's primary search field anyway.
                    "batch_keywords": batch,
                    "domain": art.get("domain"),
                    "language": art.get("language"),
                    "sourcecountry": art.get("sourcecountry"),
                    "tone": art.get("tone"),
                    "socialimage": art.get("socialimage"),
                    "date_source": date_source,
                    "gdelt_seendate": art.get("seendate"),
                },
            )


def _chunk(seq: list[str], n: int) -> Iterable[list[str]]:
    """Yield successive n-sized chunks from seq."""
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _short_label(batch: list[str]) -> str:
    """A compact, log-friendly summary of a keyword batch."""
    if not batch:
        return "(empty)"
    if len(batch) == 1:
        return repr(batch[0])
    return f"[{batch[0]!r} +{len(batch) - 1} more]"


def _fetch_with_retry(label: str, params: dict, headers: dict) -> dict | None:
    """Run one GDELT query with throttle + a single retry on 429/SSL/timeout.

    `label` is a short human label used only in log messages.
    Returns the parsed JSON, or None if both attempts failed.
    Logs at WARNING so the runner's per-source capture handler
    surfaces the issue in the runs table.
    """
    for attempt in (1, 2):
        _throttle.wait()
        try:
            r = requests.get(ENDPOINT, params=params, headers=headers, timeout=25)
        except requests.exceptions.SSLError as e:
            if attempt == 1:
                log.info("SSL error for %s, retrying after %ss", label, _RETRY_WAIT_S)
                time.sleep(_RETRY_WAIT_S)
                continue
            log.warning("query for %s failed after retry: %s", label, e)
            return None
        except requests.exceptions.Timeout as e:
            if attempt == 1:
                log.info("timeout for %s, retrying after %ss", label, _RETRY_WAIT_S)
                time.sleep(_RETRY_WAIT_S)
                continue
            log.warning("query for %s timed out twice: %s", label, e)
            return None
        except requests.RequestException as e:  # pragma: no cover
            log.warning("query for %s failed: %s", label, e)
            return None

        if r.status_code == 429:
            if attempt == 1:
                _throttle.set_min_gap(_throttle.min_gap_s + 1.0)
                log.info("429 for %s, backing off %ss + bumping gap", label, _RETRY_WAIT_S)
                time.sleep(_RETRY_WAIT_S)
                continue
            log.warning("query for %s rate-limited twice; giving up on this batch", label)
            return None
        if r.status_code != 200:
            log.warning("query for %s returned %s", label, r.status_code)
            return None
        try:
            return r.json()
        except ValueError as e:
            log.warning("query for %s returned non-JSON: %s", label, e)
            return None
    return None  # pragma: no cover


def _quote_term(kw: str) -> str:
    """Quote a multi-word keyword so GDELT treats it as a phrase. Existing
    explicit quotes are passed through unchanged."""
    kw = (kw or "").strip()
    if not kw:
        return ""
    if " " in kw and not (kw.startswith('"') and kw.endswith('"')):
        return f'"{kw}"'
    return kw


def _build_batched_query(
    keywords: list[str],
    language: str,
    gdelt_codes: list[str] | None = None,
) -> str:
    """Build a GDELT DOC query that ORs all keywords together.

    Multi-word keywords are quoted as phrases. Filters (sourcelang,
    sourcecountry) are appended after the OR clause. Returns "" if no
    valid keywords are present.

    Example output:
        ("Uganda" OR "M23" OR "Arua Airfield") sourcelang:english
        (sourcecountry:US OR sourcecountry:UK)
    """
    quoted = [t for t in (_quote_term(kw) for kw in keywords) if t]
    if not quoted:
        return ""
    body = quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"
    if language:
        body = f"{body} sourcelang:{language}"
    if gdelt_codes:
        if len(gdelt_codes) == 1:
            body = f"{body} sourcecountry:{gdelt_codes[0]}"
        else:
            clauses = " OR ".join(f"sourcecountry:{c}" for c in gdelt_codes)
            body = f"{body} ({clauses})"
    return body


# Kept for backward compat with anything that imported it. New code
# should use _build_batched_query (which handles 1..N keywords).
def _build_query(keyword: str, language: str,
                 gdelt_codes: list[str] | None = None) -> str:
    return _build_batched_query([keyword], language, gdelt_codes)
