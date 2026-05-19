"""Smoke tests for MonitorRunner error capture + pre-flight checks.

Run from the project root with the venv activated:
    python -m unittest tests.test_runner_errors
or just:
    python tests/test_runner_errors.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import types
import unittest

# Make the project root importable when run as a script.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from monitor import runner as runner_mod
from monitor.runner import MonitorRunner
from monitor.storage import Document, MonitorStore


FAKE_NAME = "fake"


def _install_fake_collector(behavior: str) -> None:
    """Install a fake collector module that the runner can pick up.

    behavior:
      "ok"            : yield one Document per keyword, no errors
      "raise_on_2nd"  : yield for keyword 0, log a warning + raise on
                        keyword 1, yield for keyword 2+
    """
    mod_name = f"monitor.collectors.{FAKE_NAME}"
    log = logging.getLogger(mod_name)

    def collect(source_config, keywords):
        for i, kw in enumerate(keywords):
            if behavior == "raise_on_2nd" and i == 1:
                log.warning("simulated upstream failure for %r", kw)
                raise RuntimeError(f"boom: {kw}")
            yield Document(
                source=FAKE_NAME,
                source_id=f"fake-{kw}-{i}",
                author="fake",
                title=f"hit for {kw}",
                content=kw,
                url=f"https://example.test/{i}",
                extra={"keyword": kw},
            )

    mod = types.ModuleType(mod_name)
    mod.collect = collect
    sys.modules[mod_name] = mod


def _wait_idle(runner: MonitorRunner, timeout: float = 5.0) -> None:
    """Block until the runner thread reports it has finished."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not runner.status.running:
            return
        time.sleep(0.05)
    raise AssertionError("runner did not finish within timeout")


class RunnerErrorsTest(unittest.TestCase):
    def setUp(self) -> None:
        # Fresh temp DB per test
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="monitor-test-")
        os.close(fd)
        self.store = MonitorStore(self.db_path)

        # Make the runner discover our fake source by extending SOURCE_NAMES
        # for the duration of the test. We restore after each test.
        self._orig_sources = list(runner_mod.SOURCE_NAMES)
        runner_mod.SOURCE_NAMES = self._orig_sources + [FAKE_NAME]

        # Enable the fake source (so require_enabled=True picks it up)
        self.store.save_source(FAKE_NAME, True, {})

    def tearDown(self) -> None:
        runner_mod.SOURCE_NAMES = self._orig_sources
        sys.modules.pop(f"monitor.collectors.{FAKE_NAME}", None)
        self.store.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ---- pre-flight checks -----------------------------------------

    def test_no_topics_records_error_on_run(self) -> None:
        _install_fake_collector("ok")
        runner = MonitorRunner(self.store)
        started = runner.start(only=[FAKE_NAME])
        self.assertTrue(started)
        _wait_idle(runner)

        self.assertEqual(runner.status.scanned, 0)
        self.assertIsNotNone(runner.status.error)
        self.assertIn("No topics", runner.status.error)

        last = self.store.last_run()
        self.assertIsNotNone(last)
        self.assertIn("No topics", last["error"])

    def test_empty_keyword_topic_records_error(self) -> None:
        _install_fake_collector("ok")
        self.store.create_topic("Empty", "word", [])
        runner = MonitorRunner(self.store)
        runner.start(only=[FAKE_NAME])
        _wait_idle(runner)

        self.assertEqual(runner.status.scanned, 0)
        self.assertIsNotNone(runner.status.error)
        self.assertIn("no keywords", runner.status.error.lower())
        # warnings list should mention which topic was empty
        self.assertTrue(any("Empty" in w for w in runner.status.warnings),
                        f"expected 'Empty' in warnings: {runner.status.warnings}")

    # ---- per-source error capture ----------------------------------

    def test_collector_exception_recorded_per_source(self) -> None:
        _install_fake_collector("raise_on_2nd")
        # Three keywords: collector raises on the 2nd; we expect to keep the
        # first doc but the rest to be lost (the raise unwinds the generator).
        self.store.create_topic("T1", "word", ["alpha", "bravo", "charlie"])

        runner = MonitorRunner(self.store)
        runner.start(only=[FAKE_NAME])
        _wait_idle(runner)

        # First keyword's doc was yielded before the crash.
        self.assertGreaterEqual(runner.status.scanned, 1)
        self.assertGreaterEqual(runner.status.matched, 1)

        # The runner caught the raise and bucketed it under "fake".
        self.assertIn(FAKE_NAME, runner.status.source_errors,
                      f"got: {runner.status.source_errors}")
        msgs = runner.status.source_errors[FAKE_NAME]
        self.assertTrue(any("boom" in m for m in msgs),
                        f"expected 'boom' in messages: {msgs}")
        # The log.warning() before the raise should also be captured.
        self.assertTrue(any("simulated upstream failure" in m for m in msgs),
                        f"expected logged warning to be captured: {msgs}")

        # Persisted to the runs table.
        last = self.store.last_run()
        self.assertIsNotNone(last["source_errors_json"])
        persisted = json.loads(last["source_errors_json"])
        self.assertIn(FAKE_NAME, persisted)

    # ---- migration is idempotent -----------------------------------

    def test_migration_is_idempotent(self) -> None:
        # Re-opening the same DB shouldn't double-add the columns.
        self.store.close()
        store2 = MonitorStore(self.db_path)
        cols = {r["name"] for r in
                store2.conn.execute("PRAGMA table_info(runs)").fetchall()}
        self.assertIn("source_errors_json", cols)
        self.assertIn("warnings_json", cols)
        store2.close()
        # Reopen for tearDown to find a valid handle
        self.store = MonitorStore(self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
