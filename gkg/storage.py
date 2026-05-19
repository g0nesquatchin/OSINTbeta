"""SQLite storage for GKG articles + locations."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from .fetcher import GkgArticle


SCHEMA = """
CREATE TABLE IF NOT EXISTS gkg_articles (
    url          TEXT PRIMARY KEY,
    source       TEXT,
    date_str     TEXT NOT NULL,
    themes       TEXT,            -- semicolon-separated theme tags
    tone         REAL DEFAULT 0,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gkg_articles_date  ON gkg_articles(date_str);
CREATE INDEX IF NOT EXISTS idx_gkg_articles_url   ON gkg_articles(url);

CREATE TABLE IF NOT EXISTS gkg_locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_url   TEXT NOT NULL,
    loc_type      INTEGER,
    name          TEXT NOT NULL,
    country_code  TEXT,
    admin1_code   TEXT,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    FOREIGN KEY (article_url) REFERENCES gkg_articles(url) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gkg_locations_article  ON gkg_locations(article_url);
CREATE INDEX IF NOT EXISTS idx_gkg_locations_name     ON gkg_locations(name);
CREATE INDEX IF NOT EXISTS idx_gkg_locations_coords   ON gkg_locations(lat, lon);

CREATE TABLE IF NOT EXISTS gkg_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class GeoResult:
    """One aggregated map pin: a location with the articles tied to it."""
    name: str
    lat: float
    lon: float
    country_code: str
    admin1_code: str
    loc_type: int
    count: int
    article_urls: list[str]
    sources: list[str]


class GkgStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- ingest -----------------------------------------------------

    def ingest(self, articles: Iterable[GkgArticle]) -> tuple[int, int]:
        """Insert articles + their locations. Returns (article_count, loc_count)."""
        n_articles = 0
        n_locations = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self.conn.cursor()
            for art in articles:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO gkg_articles
                        (url, source, date_str, themes, tone, collected_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        art.url, art.source, art.date_str,
                        ";".join(art.themes), art.tone, now_iso,
                    ),
                )
                # Replace locations for this article (idempotent)
                cur.execute("DELETE FROM gkg_locations WHERE article_url = ?",
                            (art.url,))
                for loc in art.locations:
                    cur.execute(
                        """
                        INSERT INTO gkg_locations
                            (article_url, loc_type, name, country_code,
                             admin1_code, lat, lon)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            art.url, loc.loc_type, loc.name, loc.country_code,
                            loc.admin1_code, loc.lat, loc.lon,
                        ),
                    )
                    n_locations += 1
                n_articles += 1
            self.conn.commit()
        return n_articles, n_locations

    # --- pruning ----------------------------------------------------

    def prune_older_than(self, hours: int) -> int:
        """Delete articles older than `hours` based on GKG DATE field.
        Returns rows deleted."""
        if hours <= 0:
            return 0
        from datetime import timedelta
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        cutoff = cutoff_dt.strftime("%Y%m%d%H%M%S")
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM gkg_articles WHERE date_str < ?", (cutoff,))
            deleted = cur.rowcount
            self.conn.commit()
        return deleted

    # --- stats ------------------------------------------------------

    def stats(self) -> dict:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM gkg_articles")
        articles = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM gkg_locations")
        locations = cur.fetchone()["n"]
        cur.execute("SELECT MIN(date_str) AS lo, MAX(date_str) AS hi FROM gkg_articles")
        row = cur.fetchone()
        return {
            "articles": articles,
            "locations": locations,
            "earliest": row["lo"] if row else None,
            "latest": row["hi"] if row else None,
        }

    def top_themes(self, limit: int = 30) -> list[tuple[str, int]]:
        """Return the most-common GKG theme codes currently in the DB.

        Themes are stored semicolon-joined per article. We unfold them in
        Python rather than via SQL because SQLite's string-splitting
        toolkit is limited — and theme counts are bounded by retention,
        so the unfold is cheap.
        """
        counts: dict[str, int] = {}
        for r in self.conn.execute("SELECT themes FROM gkg_articles"):
            for t in (r["themes"] or "").split(";"):
                t = t.strip()
                if t:
                    counts[t] = counts.get(t, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        return ranked[:limit]

    def get_meta(self, key: str, default: str = "") -> str:
        cur = self.conn.execute("SELECT value FROM gkg_meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO gkg_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.conn.commit()

    # --- search -----------------------------------------------------

    def search(
        self,
        keywords: list[str],
        limit: int = 5000,
        themes: Optional[list[str]] = None,
        tone_min: Optional[float] = None,
        tone_max: Optional[float] = None,
        since_hours: Optional[int] = None,
    ) -> list[GeoResult]:
        """Return locations aggregated across articles matching the filters.

        Keyword matching: case-insensitive substring against URL, themes,
        and location name. Each keyword is split on spaces; every token
        must appear in at least one of those fields. Keywords are OR'd.

        themes      : OR'd against the article's V2Themes list with
                      ``;TOKEN;`` boundary matching, so "KILL" doesn't
                      match "SKILLED". If empty/None, no theme filter.
        tone_min/max: clamp the article's average tone (typically -10..+10).
        since_hours : only include articles whose GKG DATE is within the
                      last N hours.

        For phrase queries like "missing persons" we require both words
        to appear in at least one of those fields.
        """
        keywords = [k.strip().lower() for k in keywords if k.strip()]

        # Bail only when NO filter at all is applied. Theme-only queries
        # are legitimate (e.g. "show me everywhere KILL articles point to").
        if not keywords and not themes \
                and tone_min is None and tone_max is None \
                and not since_hours:
            return []

        # For each keyword, split on spaces — each token must appear somewhere
        # in (url OR themes OR location name). We OR keywords together.
        clauses: list[str] = []
        args: list = []
        for kw in keywords:
            tokens = [t for t in kw.split() if t]
            if not tokens:
                continue
            per_token = []
            for t in tokens:
                like = f"%{t}%"
                per_token.append("(lower(a.url) LIKE ? OR lower(a.themes) LIKE ? OR lower(l.name) LIKE ?)")
                args.extend([like, like, like])
            clauses.append("(" + " AND ".join(per_token) + ")")

        where_parts: list[str] = []
        if clauses:
            where_parts.append("(" + " OR ".join(clauses) + ")")

        # Theme filter — boundary-aware so 'KILL' doesn't match 'SKILLED'.
        # We pad themes with ';' on each side and look for ';TOKEN;'.
        if themes:
            theme_tokens = [t.strip() for t in themes if t and t.strip()]
            if theme_tokens:
                theme_clauses = []
                for t in theme_tokens:
                    theme_clauses.append("(';' || a.themes || ';') LIKE ?")
                    args.append(f"%;{t};%")
                where_parts.append("(" + " OR ".join(theme_clauses) + ")")

        # Tone bounds.
        if tone_min is not None:
            where_parts.append("a.tone >= ?")
            args.append(float(tone_min))
        if tone_max is not None:
            where_parts.append("a.tone <= ?")
            args.append(float(tone_max))

        # Within-retention recency filter. GKG DATE is YYYYMMDDHHMMSS, a
        # lexicographically-sortable string, so a plain >= works.
        if since_hours is not None and since_hours > 0:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
            where_parts.append("a.date_str >= ?")
            args.append(cutoff.strftime("%Y%m%d%H%M%S"))

        where = " AND ".join(where_parts) if where_parts else "1=1"
        sql = f"""
            SELECT l.name, l.lat, l.lon, l.country_code, l.admin1_code,
                   l.loc_type, a.url, a.source
            FROM gkg_locations l
            JOIN gkg_articles a ON a.url = l.article_url
            WHERE {where}
            LIMIT ?
        """
        args.append(limit)

        rows = self.conn.execute(sql, args).fetchall()

        # Aggregate by (lat, lon) rounded — multiple articles at same spot
        # collapse to a single map pin with count.
        agg: dict[tuple, GeoResult] = {}
        for r in rows:
            # Bucket by 2 decimal degrees (~1km) so we don't fragment exact
            # duplicates into many pins.
            key = (round(r["lat"], 2), round(r["lon"], 2), r["name"])
            if key not in agg:
                agg[key] = GeoResult(
                    name=r["name"], lat=r["lat"], lon=r["lon"],
                    country_code=r["country_code"] or "",
                    admin1_code=r["admin1_code"] or "",
                    loc_type=r["loc_type"] or 0,
                    count=0, article_urls=[], sources=[],
                )
            entry = agg[key]
            entry.count += 1
            if r["url"] not in entry.article_urls and len(entry.article_urls) < 20:
                entry.article_urls.append(r["url"])
            if r["source"] and r["source"] not in entry.sources \
                    and len(entry.sources) < 10:
                entry.sources.append(r["source"])

        results = sorted(agg.values(), key=lambda x: -x.count)
        return results
