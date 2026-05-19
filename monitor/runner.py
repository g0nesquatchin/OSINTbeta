"""Orchestrates a Monitor collection pass.

Pulls each enabled source, filters by topics, persists matches, tracks
status for the live UI.
"""

from __future__ import annotations

import importlib
import logging
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

# Root logger name shared by all collectors. The runner installs a
# capture handler here for the duration of each run so per-source
# warnings/errors surface in the UI instead of getting buried in stdout.
COLLECTORS_LOG_ROOT = "monitor.collectors"


class _SourceErrorHandler(logging.Handler):
    """Logging handler that bins WARNING+ records by current source.

    The runner sets `current_source` while a collector is running; any
    records emitted with name `monitor.collectors.<source>` (or any
    descendant) get attributed to that source. Records from elsewhere
    are stored under `__other__` so we never lose information.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._by_source: dict[str, list[str]] = {}
        self._current_source: Optional[str] = None
        self._lock = threading.Lock()
        # Match a tidy, single-line format. We re-emit the level so the UI
        # can distinguish WARNING from ERROR if we ever start using ERROR.
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def bind_source(self, name: Optional[str]) -> None:
        with self._lock:
            self._current_source = name

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            # Prefer the binding the runner has set. Fall back to parsing
            # the logger name in case a collector emits before/after the
            # `current_source` window (e.g. a top-level import warning).
            bucket = self._current_source
            if bucket is None and record.name.startswith(COLLECTORS_LOG_ROOT + "."):
                bucket = record.name[len(COLLECTORS_LOG_ROOT) + 1:].split(".")[0]
            if bucket is None:
                bucket = "__other__"
            self._by_source.setdefault(bucket, []).append(msg)

    def snapshot(self) -> dict[str, list[str]]:
        with self._lock:
            return {k: list(v) for k, v in self._by_source.items() if v}


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
    source_errors: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

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
            "source_errors": {k: list(v) for k, v in self.source_errors.items()},
            "warnings": list(self.warnings),
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

        # Install the per-run log capture handler before we touch any
        # collector. Hooked at COLLECTORS_LOG_ROOT so we catch every
        # collector's logger without competing with anything else.
        handler = _SourceErrorHandler()
        collectors_logger = logging.getLogger(COLLECTORS_LOG_ROOT)
        collectors_logger.addHandler(handler)
        # Make sure WARNING actually flows through. We don't lower below
        # WARNING globally to avoid stdout spam.
        prior_level = collectors_logger.level
        if prior_level == logging.NOTSET or prior_level > logging.WARNING:
            collectors_logger.setLevel(logging.WARNING)

        try:
            all_topics = self.store.list_topics()
            if topic_ids:
                topics = [t for t in all_topics if t.id in topic_ids]
            else:
                topics = all_topics
            if not topics:
                raise RuntimeError("No topics to scan against.")

            # Pre-flight: a topic with no keywords is a silent no-op.
            # Surface that explicitly instead of running every collector
            # with an empty query.
            empty_topics = [t.name for t in topics if not t.keywords]
            if empty_topics:
                self.status.warnings.append(
                    "Topics with no keywords (skipped from query): "
                    + ", ".join(empty_topics)
                )
            keywords_union = sorted({k for t in topics for k in t.keywords})
            if not keywords_union:
                raise RuntimeError(
                    "All selected topics have no keywords; add keywords on "
                    "the Topics page before running."
                )

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
                handler.bind_source(name)
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
                    # A crash inside the collector at import or iteration
                    # time — record it as a source error and keep going.
                    self.status.source_errors.setdefault(name, []).append(
                        f"runner: {e}"
                    )
                finally:
                    handler.bind_source(None)
                    sources_done.append(name)
                    self.status.sources_done = list(sources_done)
        except Exception as e:
            error = str(e)
            self.status.error = error
        finally:
            # Merge anything the handler captured into status.source_errors
            # so the UI sees both runner-side and collector-side errors.
            captured = handler.snapshot()
            for src, msgs in captured.items():
                self.status.source_errors.setdefault(src, []).extend(msgs)
            # Pick a representative top-level error for legacy `runs.error`
            # when we don't already have one. Helps the recent-run chip on
            # /monitor stay informative.
            if error is None and self.status.source_errors:
                # Show the first source that errored — concise summary.
                first_src = next(iter(self.status.source_errors))
                first_msg = self.status.source_errors[first_src][0]
                self.status.error = f"{first_src}: {first_msg}"
            try:
                collectors_logger.removeHandler(handler)
                collectors_logger.setLevel(prior_level)
            except Exception:  # pragma: no cover
                pass
            self.store.finish_run(
                run_id, self.status.scanned, self.status.matched,
                self.status.new, sources_done,
                error=error,
                source_errors=self.status.source_errors or None,
                warnings=self.status.warnings or None,
            )
            self.status.running = False
            self.status.current_source = None
            self.status.finished_at = datetime.now(timezone.utc).isoformat()
