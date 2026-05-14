"""Orchestrates a Monitor collection pass.

Pulls each enabled source, filters by topics, persists matches, tracks
status for the live UI.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import SOURCE_NAMES
from .filters import MatchResult, match_topics
from .storage import Document, MonitorStore


# These sources do their own keyword search server-side. Their results
# are already filtered; we trust the trigger keyword stored in
# Document.extra rather than re-checking the body locally (which is
# unreliable because they don't always return the full body text).
KEYWORD_SEARCH_SOURCES = {"gdelt", "google_news", "reddit", "x_twitter", "bluesky"}


def _trust_source_match(
    doc: Document, topics: list,
) -> list[MatchResult]:
    """If a keyword-search source tagged this doc with the trigger keyword,
    map it back to the topics that contain that keyword."""
    if doc.source not in KEYWORD_SEARCH_SOURCES:
        return []
    extra = doc.extra or {}
    trigger = (
        extra.get("keyword")
        or extra.get("search_term")
        or extra.get("query")
    )
    if not trigger:
        return []
    trigger_low = trigger.lower()
    out: list[MatchResult] = []
    for t in topics:
        if any(k.lower() == trigger_low for k in t.keywords):
            out.append(MatchResult(
                topic_id=t.id, topic_name=t.name, keywords=[trigger],
            ))
    return out


@dataclass
class RunStatus:
    running: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    current_source: Optional[str] = None
    sources_done: list[str] = field(default_factory=list)
    scanned: int = 0
    matched: int = 0
    new: int = 0
    error: Optional[str] = None
    recent: list[dict] = field(default_factory=list)
    run_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_source": self.current_source,
            "sources_done": list(self.sources_done),
            "scanned": self.scanned,
            "matched": self.matched,
            "new": self.new,
            "error": self.error,
            "recent": list(self.recent),
            "run_id": self.run_id,
        }


class MonitorRunner:
    """Singleton-ish. One run at a time."""

    def __init__(self, store: MonitorStore):
        self.store = store
        self.status = RunStatus()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(
        self,
        only: Optional[list[str]] = None,
        topic_ids: Optional[list[int]] = None,
        country_codes: Optional[list[str]] = None,
        require_enabled: bool = True,
    ) -> bool:
        """Kick off a run.

        only            : restrict to these source names
        topic_ids       : restrict matching to these topics (still scans all)
        country_codes   : list of our internal Country codes (e.g. ["US","GB"])
                          — passed to GDELT/Google News as geo filters
        require_enabled : if False, run a source even when it's disabled in
                          persisted config. Used for ad-hoc Live runs.
        """
        with self._lock:
            if self.status.running:
                return False
            self.status = RunStatus(
                running=True,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        self._thread = threading.Thread(
            target=self._run,
            args=(only or SOURCE_NAMES, topic_ids or None,
                  country_codes or None, require_enabled),
            daemon=True,
        )
        self._thread.start()
        return True

    def _build_overrides(self, source_name: str,
                        country_codes: Optional[list[str]]) -> dict:
        """Inject country filters into a source config."""
        if not country_codes:
            return {}
        # Lazy import so countries module isn't required for non-geo runs
        from .countries import get
        out: dict = {}
        if source_name == "gdelt":
            out["gdelt_codes"] = [
                c.gdelt for c in (get(code) for code in country_codes) if c
            ]
        elif source_name == "google_news":
            out["locales"] = [
                {"gl": c.gnews_gl, "hl": c.gnews_hl}
                for c in (get(code) for code in country_codes) if c
            ]
        return out

    def _run(self, sources_to_use: list[str],
             topic_ids: Optional[list[int]],
             country_codes: Optional[list[str]],
             require_enabled: bool) -> None:
        run_id = self.store.start_run()
        self.status.run_id = run_id
        sources_done: list[str] = []
        error: Optional[str] = None
        try:
            all_topics = self.store.list_topics()
            if topic_ids:
                topics = [t for t in all_topics if t.id in topic_ids]
            else:
                topics = all_topics
            if not topics:
                raise RuntimeError("No topics to scan against.")
            keywords_union = sorted({k for t in topics for k in t.keywords})

            for name in SOURCE_NAMES:
                if name not in sources_to_use:
                    continue
                src = self.store.get_source(name)
                if require_enabled and not src["enabled"]:
                    continue
                # Merge persisted config with per-run geo overrides
                cfg = dict(src["config"] or {})
                cfg.update(self._build_overrides(name, country_codes))
                self.status.current_source = name
                try:
                    mod = importlib.import_module(f"monitor.collectors.{name}")
                    stream = mod.collect(cfg, keywords_union)
                    for doc in stream:
                        self.status.scanned += 1
                        # For sources that searched server-side, trust the
                        # trigger keyword rather than re-checking the body
                        # locally (which often only contains the title).
                        matches = _trust_source_match(doc, topics)
                        if not matches:
                            matches = match_topics(doc.searchable_text(), topics)
                        if not matches:
                            continue
                        self.status.matched += 1
                        is_new = self.store.save_match(doc, matches)
                        if is_new:
                            self.status.new += 1
                        # Always surface the match so the live stream shows
                        # activity even when items are duplicates of prior
                        # runs. The is_new flag distinguishes them.
                        self.status.recent.insert(0, {
                            "source": doc.source,
                            "author": doc.author,
                            "title": (doc.title or doc.content)[:160],
                            "url": doc.url,
                            "topics": [m.topic_name for m in matches],
                            "is_new": is_new,
                            "country": (doc.extra or {}).get("sourcecountry")
                                       or (doc.extra or {}).get("country") or "",
                        })
                        self.status.recent = self.status.recent[:100]
                except Exception as e:
                    self.status.error = f"{name}: {e}"
                finally:
                    sources_done.append(name)
                    self.status.sources_done = list(sources_done)
        except Exception as e:
            error = str(e)
            self.status.error = error
        finally:
            self.store.finish_run(
                run_id, self.status.scanned, self.status.matched,
                self.status.new, sources_done, error=error,
            )
            self.status.running = False
            self.status.current_source = None
            self.status.finished_at = datetime.now(timezone.utc).isoformat()
