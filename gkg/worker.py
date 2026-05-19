"""Background worker that keeps the GKG store fresh.

Pulls the latest GKG file (~every 15 min new ones drop), parses it,
ingests into SQLite, then prunes records older than retention_hours.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .fetcher import fetch_latest
from .storage import GkgStore


SETTING_RETENTION = "retention_hours"
SETTING_LAST_FETCH = "last_fetch_iso"
SETTING_LAST_ERROR = "last_error"
SETTING_VERIFY_SSL = "verify_ssl"


@dataclass
class WorkerStatus:
    running: bool = False
    fetching: bool = False
    last_fetch_at: Optional[str] = None
    last_articles: int = 0
    last_locations: int = 0
    last_error: Optional[str] = None
    next_fetch_at: Optional[str] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class GkgWorker:
    """Singleton-ish background worker.

    Default cadence: fetch every 15 minutes. Default retention: 6 hours.
    Both adjustable.
    """

    def __init__(self, store: GkgStore, fetch_interval_min: int = 15):
        self.store = store
        self.fetch_interval_min = fetch_interval_min
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status = WorkerStatus()

    def retention_hours(self) -> int:
        try:
            return max(1, int(self.store.get_meta(SETTING_RETENTION, "6")))
        except ValueError:
            return 6

    def set_retention(self, hours: int) -> None:
        self.store.set_meta(SETTING_RETENTION, str(max(1, int(hours))))

    def verify_ssl(self) -> bool:
        """Whether to verify the GDELT SSL cert when fetching files.

        Default is True. GDELT's ``data.gdeltproject.org`` cert has
        recurring hostname-mismatch issues; setting this to False
        bypasses the check at the user's request. The data is public
        and read-only, so the worst-case attacker payload is fabricated
        location mentions on the map — a real but bounded risk that
        the UI should explain when offering the toggle.
        """
        return self.store.get_meta(SETTING_VERIFY_SSL, "1") != "0"

    def set_verify_ssl(self, value: bool) -> None:
        self.store.set_meta(SETTING_VERIFY_SSL, "1" if value else "0")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.status.running = False

    def fetch_now(self) -> tuple[int, int]:
        """Pull the latest GKG file synchronously. Returns (n_articles, n_locs)."""
        self.status.fetching = True
        self.status.last_error = None
        try:
            verify = self.verify_ssl()
            if not verify:
                # Suppress the urllib3 InsecureRequestWarning that fires
                # once per call when verify=False. Bundled with urllib3 in
                # requests; only do this in the worker's narrow scope so
                # we don't accidentally hide other libraries' warnings.
                try:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                except Exception:
                    pass
            articles = list(fetch_latest(verify=verify))
            n_a, n_l = self.store.ingest(articles)
            self.status.last_articles = n_a
            self.status.last_locations = n_l
            self.status.last_fetch_at = datetime.now(timezone.utc).isoformat()
            self.store.set_meta(SETTING_LAST_FETCH, self.status.last_fetch_at)
            # Prune after fetching so we don't blow past retention
            self.store.prune_older_than(self.retention_hours())
            return n_a, n_l
        except Exception as e:  # pragma: no cover
            self.status.last_error = str(e)
            self.store.set_meta(SETTING_LAST_ERROR, str(e))
            return 0, 0
        finally:
            self.status.fetching = False

    def _loop(self) -> None:
        # Don't hammer GDELT on startup — short delay so the rest of the
        # app finishes booting first.
        if self._stop.wait(8):
            return
        while not self._stop.is_set():
            try:
                self.fetch_now()
            except Exception as e:  # pragma: no cover
                self.status.last_error = str(e)
            # Sleep until next fetch, wake fast on stop
            from datetime import timedelta
            next_at = datetime.now(timezone.utc) + timedelta(
                minutes=self.fetch_interval_min,
            )
            self.status.next_fetch_at = next_at.isoformat()
            if self._stop.wait(self.fetch_interval_min * 60):
                return
