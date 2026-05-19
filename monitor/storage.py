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
    dedup_key    TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    author       TEXT,
    title        TEXT,
    content      TEXT,
    url          TEXT,
    created_at   TEXT,
    collected_at TEXT NOT NULL,
    extra_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_collected_at ON documents(collected_at);

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
        existing = {
            row["name"] for row in
            self.conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "source_errors_json" not in existing:
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN source_errors_json TEXT"
            )
        if "warnings_json" not in existing:
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN warnings_json TEXT"
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
        cur = self.conn.cursor()
        existed = cur.execute(
            "SELECT 1 FROM documents WHERE dedup_key=?", (key,)
        ).fetchone() is not None
        cur.execute(
            """
            INSERT OR IGNORE INTO documents
                (dedup_key, source, source_id, author, title, content,
                 url, created_at, collected_at, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key, doc.source, doc.source_id, doc.author, doc.title,
                doc.content, doc.url, _iso(doc.created_at),
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
    ) -> list[sqlite3.Row]:
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
            sql.append("AND (d.created_at >= ? OR d.collected_at >= ?)")
            args.extend([since, since])
        sql.append("GROUP BY d.dedup_key ORDER BY d.collected_at DESC LIMIT ?")
        args.append(limit)
        return self.conn.execute(" ".join(sql), args).fetchall()

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
