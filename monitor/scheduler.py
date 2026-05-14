"""Background scheduler for the Monitor.

A simple thread that wakes every minute, checks whether the interval
has elapsed since the last run, and triggers another if so. Persists
the state via MonitorStore settings so it survives process restarts.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .runner import MonitorRunner
from .storage import MonitorStore


SETTING_INTERVAL = "scheduler_interval_min"
SETTING_ENABLED = "scheduler_enabled"
SETTING_LAST_TRIGGER = "scheduler_last_trigger_iso"


class Scheduler:
    def __init__(self, store: MonitorStore, runner: MonitorRunner):
        self.store = store
        self.runner = runner
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- settings ----------------------------------------------

    def enabled(self) -> bool:
        return self.store.get_setting(SETTING_ENABLED, "0") == "1"

    def interval_min(self) -> int:
        try:
            return max(5, int(self.store.get_setting(SETTING_INTERVAL, "60")))
        except ValueError:
            return 60

    def last_trigger(self) -> Optional[datetime]:
        v = self.store.get_setting(SETTING_LAST_TRIGGER, "")
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None

    def next_trigger(self) -> Optional[datetime]:
        last = self.last_trigger()
        if last is None:
            return None
        from datetime import timedelta
        return last + timedelta(minutes=self.interval_min())

    def configure(self, enabled: bool, interval_min: int) -> None:
        self.store.set_setting(SETTING_ENABLED, "1" if enabled else "0")
        self.store.set_setting(SETTING_INTERVAL, str(max(5, int(interval_min))))

    # --- lifecycle ----------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # --- internals ----------------------------------------------

    def _loop(self) -> None:
        # Pause briefly before the first check so the app finishes booting.
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            try:
                if self.enabled() and not self.runner.status.running:
                    last = self.last_trigger()
                    now = datetime.now(timezone.utc)
                    due = (
                        last is None or
                        (now - last).total_seconds() >= self.interval_min() * 60
                    )
                    if due:
                        if self.runner.start():
                            self.store.set_setting(
                                SETTING_LAST_TRIGGER, now.isoformat()
                            )
            except Exception as e:  # pragma: no cover
                print(f"[scheduler] tick error: {e}")
            # Sleep ~60s but wake fast on stop
            if self._stop.wait(60):
                return
