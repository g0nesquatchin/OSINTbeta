"""SQLite storage for the Monitor.

Holds topics, source configs, documents, matches, run history, and
settings. Separate from SpiderFoot's database so they don't collide.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from .filters import MatchResult, Topic
from .url_norm import canonicalize


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    match_mode  TEXT NOT NULL DEFAULT 'word',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER NOT NULL,
    keyword     TEXT NOT NULL,
    FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    UNIQUE(topic_id, keyword)
);

CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS documents (
    dedup_key     TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    author        TEXT,
    title         TEXT,
    content       TEXT,
    url           TEXT,
    -- Cross-source dedup key: normalized form of `url` so the same
    -- article surfaced via GDELT + Google News + RSS collapses to one
    -- row at display time. Populated by save_match; backfilled by
    -- _migrate() for legacy rows.
    canonical_url TEXT,
    created_at    TEXT,
    collected_at  TEXT NOT NULL,
    extra_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_collected_at ON documents(collected_at);
CREATE INDEX IF NOT EXISTS idx_documents_canonical_url ON documents(canonical_url);

CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT NOT NULL,
    topic_id    INTEGER NOT NULL,
    keywords    TEXT NOT NULL,
    UNIQUE(dedup_key, topic_id),
    FOREIGN KEY (dedup_key) REFERENCES documents(dedup_key) ON DELETE CASCADE,
    FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    scanned             INTEGER NOT NULL DEFAULT 0,
    matched             INTEGER NOT NULL DEFAULT 0,
    new_docs            INTEGER NOT NULL DEFAULT 0,
    sources_json        TEXT,
    error               TEXT,
    -- JSON dict {source_name: [error_message, ...]} populated by the
    -- runner's logging handler so per-source crashes surface in the UI
    -- instead of getting buried in stdout.
    source_errors_json  TEXT,
    -- JSON list of non-fatal warnings (e.g. "mastodon has no hashtag-safe
    -- keywords"). Useful to explain a run that returned little/nothing.
    warnings_json       TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Document:
    source: str
    source_id: str
    author: str = ""
    title: str = ""
    content: str = ""
    url: str = ""
    created_at: Optional[datetime] = None
    extra: dict = field(default_factory=dict)

    def dedup_key(self) -> str:
        return hashlib.sha256(
            f"{self.source}::{self.source_id}".encode("utf-8")
        ).hexdigest()

    def searchable_text(self) -> str:
        return f"{self.title}\n{self.content}".strip()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class MonitorStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Apply idempotent ALTER TABLE migrations for columns added after
        the original schema shipped. Safe to run on every startup."""
        runs_cols = {
            row["name"] for row in
            self.conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "source_errors_json" not in runs_cols:
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN source_errors_json TEXT"
            )
        if "warnings_json" not in runs_cols:
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN warnings_json TEXT"
            )

        doc_cols = {
            row["name"] for row in
            self.conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "canonical_url" not in doc_cols:
            self.conn.execute(
                "ALTER TABLE documents ADD COLUMN canonical_url TEXT"
            )
            # Index missing too — CREATE INDEX IF NOT EXISTS is idempotent.
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_canonical_url "
                "ON documents(canonical_url)"
            )

        # Backfill any rows lacking canonical_url. This runs every
        # startup but the SELECT is indexed and typically zero-cost —
        # save_match() populates the column on every new insert. The
        # generality matters for two cases: legacy DBs where the column
        # was added by an earlier startup, and rows inserted via direct
        # SQL (tests, manual fixups) that skipped save_match.
        rows = self.conn.execute(
            "SELECT dedup_key, url FROM documents "
            "WHERE canonical_url IS NULL AND url IS NOT NULL AND url != ''"
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE documents SET canonical_url = ? WHERE dedup_key = ?",
                (canonicalize(r["url"]), r["dedup_key"]),
            )

    def close(self) -> None:
        self.conn.close()

    # --- topics ----------------------------------------------------

    def list_topics(self) -> list[Topic]:
        rows = self.conn.execute(
            "SELECT id, name, match_mode FROM topics ORDER BY name"
        ).fetchall()
        out = []
        for r in rows:
            kws = [
                kw["keyword"] for kw in
                self.conn.execute(
                    "SELECT keyword FROM topic_keywords WHERE topic_id=? ORDER BY keyword",
                    (r["id"],),
                ).fetchall()
            ]
            out.append(Topic(
                id=r["id"], name=r["name"],
                match_mode=r["match_mode"], keywords=kws,
            ))
        return out

    def get_topic(self, topic_id: int) -> Optional[Topic]:
        for t in self.list_topics():
            if t.id == topic_id:
                return t
        return None

    def get_topic_by_name(self, name: str) -> Optional[Topic]:
        for t in self.list_topics():
            if t.name == name:
                return t
        return None

    def create_topic(self, name: str, match_mode: str,
                     keywords: list[str]) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO topics(name, match_mode, created_at) VALUES (?,?,?)",
            (name, match_mode, _iso(datetime.now(timezone.utc))),
        )
        topic_id = cur.lastrowid
        for kw in keywords:
            kw = kw.strip()
            if kw:
                cur.execute(
                    "INSERT OR IGNORE INTO topic_keywords(topic_id, keyword) VALUES (?,?)",
                    (topic_id, kw),
                )
        self.conn.commit()
        return topic_id

    def update_topic(self, topic_id: int, name: str, match_mode: str,
                     keywords: list[str]) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE topics SET name=?, match_mode=? WHERE id=?",
            (name, match_mode, topic_id),
        )
        cur.execute("DELETE FROM topic_keywords WHERE topic_id=?", (topic_id,))
        for kw in keywords:
            kw = kw.strip()
            if kw:
                cur.execute(
                    "INSERT OR IGNORE INTO topic_keywords(topic_id, keyword) VALUES (?,?)",
                    (topic_id, kw),
                )
        self.conn.commit()

    def delete_topic(self, topic_id: int) -> None:
        self.conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        self.conn.commit()

    # --- sources ---------------------------------------------------

    def get_source(self, name: str) -> dict:
        row = self.conn.execute(
            "SELECT enabled, config_json FROM sources WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return {"enabled": False, "config": {}}
        try:
            cfg = json.loads(row["config_json"] or "{}")
        except json.JSONDecodeError:
            cfg = {}
        return {"enabled": bool(row["enabled"]), "config": cfg}

    def all_sources(self) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT name, enabled, config_json FROM sources"
        ).fetchall()
        out = {}
        for r in rows:
            try:
                cfg = json.loads(r["config_json"] or "{}")
            except json.JSONDecodeError:
                cfg = {}
            out[r["name"]] = {"enabled": bool(r["enabled"]), "config": cfg}
        return out

    def save_source(self, name: str, enabled: bool, config: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO sources(name, enabled, config_json) VALUES (?,?,?)
            ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,
                                            config_json=excluded.config_json
            """,
            (name, 1 if enabled else 0, json.dumps(config)),
        )
        self.conn.commit()

    def bootstrap_defaults(self) -> None:
        """First-run convenience: enable the free, no-auth sources so the
        Live page works without the user having to configure anything.
        Only runs if the sources table is empty.
        """
        existing = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sources"
        ).fetchone()["n"]
        if existing > 0:
            return
        self.save_source("gdelt", True, {
            "timespan": "24h", "max_records": 75, "language": "",
        })
        self.save_source("google_news", True, {
            "lang": "en-US", "country": "US", "ceid": "US:en",
        })
        self.save_source("bluesky", True, {"limit_per_term": 50})
        self.save_source("mastodon", False, {
            "instance_url": "https://mastodon.social",
            "limit_per_hashtag": 40,
        })

    # --- documents + matches --------------------------------------

    def save_match(self, doc: Document, matches: list[MatchResult]) -> bool:
        key = doc.dedup_key()
        canon = canonicalize(doc.url) if doc.url else None
        cur = self.conn.cursor()
        existed = cur.execute(
            "SELECT 1 FROM documents WHERE dedup_key=?", (key,)
        ).fetchone() is not None
        cur.execute(
            """
            INSERT OR IGNORE INTO documents
                (dedup_key, source, source_id, author, title, content,
                 url, canonical_url,
                 created_at, collected_at, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key, doc.source, doc.source_id, doc.author, doc.title,
                doc.content, doc.url, canon,
                _iso(doc.created_at),
                _iso(datetime.now(timezone.utc)),
                json.dumps(doc.extra, default=str),
            ),
        )
        for m in matches:
            if m.topic_id is None:
                continue
            cur.execute(
                """
                INSERT OR IGNORE INTO matches(dedup_key, topic_id, keywords)
                VALUES (?, ?, ?)
                """,
                (key, m.topic_id, json.dumps(m.keywords)),
            )
        self.conn.commit()
        return not existed

    def search_matches(
        self,
        query: Optional[str] = None,
        topic_id: Optional[int] = None,
        source: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
        bypass_filters: bool = False,
    ) -> list[dict]:
        """Return up to `limit` matches, cross-source-deduped.

        Rows that share a canonical_url collapse into one entry whose
        ``source`` field is a comma-separated list of every source that
        carried the article, with the earliest-published representative
        chosen for title/content. Legacy rows with NULL canonical_url
        are treated as their own group.

        Returns dicts (not sqlite3.Row) because aggregation produces
        derived fields that don't map to single columns.
        """
        sql = [
            "SELECT d.*, GROUP_CONCAT(DISTINCT t.name) AS topics "
            "FROM documents d "
            "LEFT JOIN matches m ON m.dedup_key = d.dedup_key "
            "LEFT JOIN topics t ON t.id = m.topic_id "
            "WHERE 1=1"
        ]
        args: list = []
        if query:
            sql.append("AND (d.title LIKE ? OR d.content LIKE ?)")
            like = f"%{query}%"
            args.extend([like, like])
        if topic_id is not None:
            sql.append(
                "AND d.dedup_key IN "
                "(SELECT dedup_key FROM matches WHERE topic_id=?)"
            )
            args.append(topic_id)
        if source:
            sql.append("AND d.source=?")
            args.append(source)
        if since:
            # Strict: filter by the article's publication date only.
            # Without `created_at` (some sources don't expose a date),
            # the row is excluded from date-windowed views — the
            # conservative choice when the question is "is this fresh?".
            sql.append("AND d.created_at >= ?")
            args.append(since)
        # Sort by the timestamp we actually display (article publication
        # time, falling back to collection time) so "most recent on top"
        # matches what the user sees in the When column.
        sql.append(
            "GROUP BY d.dedup_key "
            "ORDER BY COALESCE(d.created_at, d.collected_at) DESC LIMIT ?"
        )
        # Fetch ~3x the user's limit so we have headroom to collapse
        # duplicates and still hit the requested count. Capped to avoid
        # pathological scans.
        fetch_n = min(max(limit * 3, limit + 50), 5000)
        args.append(fetch_n)
        rows = self.conn.execute(" ".join(sql), args).fetchall()
        # Aggregate sources across the rows first; we'll filter at the
        # row level afterward (a row is one canonical article).
        collapsed = _collapse_by_canonical(rows, limit=fetch_n)
        if bypass_filters:
            return collapsed[:limit]
        blocklist = self.get_blocklist()
        min_title = self.get_min_title_length()
        filtered = _apply_display_filters(collapsed, blocklist, min_title)
        return filtered[:limit]

    def stats(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        m = self.conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]
        by_src = [
            (r["source"], r["n"]) for r in
            self.conn.execute(
                "SELECT source, COUNT(*) AS n FROM documents GROUP BY source"
            ).fetchall()
        ]
        by_topic = [
            (r["name"], r["n"]) for r in
            self.conn.execute(
                """
                SELECT t.name, COUNT(m.id) AS n
                FROM topics t LEFT JOIN matches m ON m.topic_id = t.id
                GROUP BY t.id ORDER BY n DESC
                """
            ).fetchall()
        ]
        return {
            "documents": n, "matches": m,
            "by_source": by_src, "by_topic": by_topic,
        }

    # --- runs ----------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO runs(started_at) VALUES (?)",
            (_iso(datetime.now(timezone.utc)),),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        scanned: int,
        matched: int,
        new_docs: int,
        sources: list[str],
        error: Optional[str] = None,
        source_errors: Optional[dict[str, list[str]]] = None,
        warnings: Optional[list[str]] = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at=?, scanned=?, matched=?, new_docs=?,
                sources_json=?, error=?,
                source_errors_json=?, warnings_json=?
            WHERE id=?
            """,
            (
                _iso(datetime.now(timezone.utc)),
                scanned, matched, new_docs,
                json.dumps(sources), error,
                json.dumps(source_errors) if source_errors else None,
                json.dumps(warnings) if warnings else None,
                run_id,
            ),
        )
        self.conn.commit()

    def list_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
        ).fetchall()

    def last_run(self) -> Optional[sqlite3.Row]:
        rows = self.list_runs(limit=1)
        return rows[0] if rows else None

    # --- settings ------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    # --- global filters ------------------------------------------
    # Display-time filters applied by search_matches() unless bypassed.
    # Stored as settings rows so they survive restarts without their own
    # table or migration.

    def get_blocklist(self) -> list[str]:
        """Return the configured domain blocklist as a list. Domains
        are lowercased and stripped of any leading "www." so user
        input doesn't have to be exact."""
        raw = self.get_setting("filter_domain_blocklist", "")
        out: list[str] = []
        for line in raw.splitlines():
            d = line.strip().lower().lstrip(".")
            if d.startswith("www."):
                d = d[4:]
            if d:
                out.append(d)
        return out

    def set_blocklist(self, domains: list[str]) -> None:
        normalized: list[str] = []
        seen: set[str] = set()
        for d in domains:
            d = (d or "").strip().lower().lstrip(".")
            if d.startswith("www."):
                d = d[4:]
            if d and d not in seen:
                seen.add(d)
                normalized.append(d)
        self.set_setting(
            "filter_domain_blocklist", "\n".join(normalized),
        )

    def get_min_title_length(self) -> int:
        try:
            return max(0, int(self.get_setting("filter_min_title_length", "0")))
        except ValueError:
            return 0

    def set_min_title_length(self, n: int) -> None:
        try:
            n = max(0, int(n))
        except (TypeError, ValueError):
            n = 0
        self.set_setting("filter_min_title_length", str(n))


def _collapse_by_canonical(
    rows: Iterable[sqlite3.Row], limit: int,
) -> list[dict]:
    """Collapse rows sharing a canonical_url into single dicts.

    Aggregation rules:
      - source: comma-separated, distinct, in order of first appearance
      - source_count: number of distinct sources carrying the article
      - title/content/url/author: from the representative (first) row
        in each group (rows arrive sorted newest-first, so the rep is
        the *latest* report of the article — usually the freshest title)
      - topics: union across the group's rows
      - extra_json: extra is from the representative row
      - dedup_key, source_id: representative's
      - collected_at / created_at: max(group) — the latest seen instance

    Groups are emitted in their first-seen order, which preserves the
    upstream ORDER BY (newest-first by created_at/collected_at).
    """
    groups: dict[str, dict] = {}
    for r in rows:
        canon = r["canonical_url"] or r["dedup_key"]
        group = groups.get(canon)
        if group is None:
            # First time we've seen this canonical key — this row is
            # the representative for display.
            d = {col: r[col] for col in r.keys()}
            d["sources"] = [r["source"]]
            d["source_count"] = 1
            groups[canon] = d
            continue
        # Append this row's source if it's new for this group.
        if r["source"] not in group["sources"]:
            group["sources"].append(r["source"])
            group["source_count"] = len(group["sources"])
        # Union topic tags across the group's rows. Topics from this row
        # may include ones the rep didn't pick up if the match was
        # per-source.
        for t in (r["topics"] or "").split(","):
            t = t.strip()
            if not t:
                continue
            existing = (group["topics"] or "").split(",")
            existing = {x.strip() for x in existing if x.strip()}
            if t not in existing:
                group["topics"] = (group["topics"] + "," + t) \
                    if group["topics"] else t
    # Truncate to the user's limit AFTER aggregation so duplicate
    # source-rows of in-window articles still contribute their source
    # tag to the rep's sources list.
    return list(groups.values())[:limit]


def _host_of(url: str) -> str:
    """Lowercase hostname from a URL, with leading 'www.' stripped.
    Returns '' on parse failure."""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""
    if ":" in host:  # strip port if present
        host = host.rsplit(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches_blocklist(host: str, blocklist: list[str]) -> bool:
    """True if the host exactly equals or is a subdomain of any
    entry in ``blocklist``. Blocklist entries are pre-normalized to
    lowercase, no 'www.', by the storage helpers."""
    if not host or not blocklist:
        return False
    for entry in blocklist:
        if host == entry or host.endswith("." + entry):
            return True
    return False


def _apply_display_filters(
    rows: list[dict],
    blocklist: list[str],
    min_title_length: int,
) -> list[dict]:
    """Drop rows whose canonical host is blocklisted or whose title is
    shorter than the minimum. Operates on collapsed rows so each
    decision is per-article rather than per-source-variant."""
    out: list[dict] = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if min_title_length and len(title) < min_title_length:
            continue
        url = row.get("canonical_url") or row.get("url") or ""
        if _host_matches_blocklist(_host_of(url), blocklist):
            continue
        out.append(row)
    return out
