"""Per-host throttles shared across collectors.

GDELT's API rate-limits to ~one request per 5 seconds per IP and will
return 429s (or SSL EOFs from the load balancer when it's hot) when
you exceed that. This module owns one shared throttle per host so
collectors that hit the same backend coordinate cleanly without each
one re-implementing the gap logic.

Use the module-level singletons (``GDELT_API``, ``BLUESKY``) rather
than constructing your own; the whole point is shared state.
"""

from __future__ import annotations

import threading
import time


class Throttle:
    """Polite per-host throttle.

    Calling ``wait()`` blocks the current thread until at least
    ``min_gap_s`` has elapsed since the previous successful wait. Safe
    to share across threads.
    """

    def __init__(self, min_gap_s: float):
        self.min_gap_s = float(min_gap_s)
        self._last_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            gap = (self._last_at + self.min_gap_s) - now
            if gap > 0:
                time.sleep(gap)
            self._last_at = time.time()

    def set_min_gap(self, seconds: float) -> None:
        """Bump the gap up (e.g. after a 429 we want to be even more
        polite). Lower-bound at the current configured value so we
        never accidentally tighten the spacing."""
        with self._lock:
            self.min_gap_s = max(self.min_gap_s, float(seconds))


# Module-level singletons: one per upstream host. Anything that talks
# to api.gdeltproject.org should wait on GDELT_API before each request.
GDELT_API = Throttle(min_gap_s=5.5)

# Bluesky's public endpoint is much less generous than its docs imply.
# 1.5s here is a starting point — the collector can bump it if 429s
# keep showing up.
BLUESKY = Throttle(min_gap_s=1.5)
