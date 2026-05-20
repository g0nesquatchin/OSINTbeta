"""Tests for the display-time noise filters on /monitor.

Both the domain blocklist (exact + subdomain match) and the minimum
title length operate on collapsed rows after cross-source dedup.
``bypass_filters=True`` is honored as a transparency escape hatch.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from monitor.storage import (
    Document, MonitorStore,
    _host_matches_blocklist, _host_of,
)


def _doc(source: str, url: str, title: str = "Article", when=None) -> Document:
    return Document(
        source=source,
        source_id=url,
        author=source,
        title=title,
        content="",
        url=url,
        created_at=when or datetime.now(timezone.utc),
        extra={},
    )


class HostMatcherTest(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertTrue(_host_matches_blocklist("msn.com", ["msn.com"]))

    def test_subdomain_match(self) -> None:
        self.assertTrue(_host_matches_blocklist("news.msn.com", ["msn.com"]))
        self.assertTrue(_host_matches_blocklist(
            "deep.nested.msn.com", ["msn.com"],
        ))

    def test_unrelated_host_does_not_match(self) -> None:
        self.assertFalse(_host_matches_blocklist("nytimes.com", ["msn.com"]))
        # "nytimes-msn.com" must NOT match "msn.com" — the boundary is
        # the literal dot before the entry.
        self.assertFalse(_host_matches_blocklist(
            "nytimes-msn.com", ["msn.com"],
        ))

    def test_empty_blocklist(self) -> None:
        self.assertFalse(_host_matches_blocklist("msn.com", []))

    def test_empty_host(self) -> None:
        self.assertFalse(_host_matches_blocklist("", ["msn.com"]))

    def test_multiple_entries(self) -> None:
        bl = ["msn.com", "yahoo.com"]
        self.assertTrue(_host_matches_blocklist("news.yahoo.com", bl))
        self.assertTrue(_host_matches_blocklist("msn.com", bl))
        self.assertFalse(_host_matches_blocklist("reuters.com", bl))


class HostOfTest(unittest.TestCase):
    def test_strips_www_and_lowercases(self) -> None:
        self.assertEqual(_host_of("HTTPS://www.MSN.com/article"), "msn.com")

    def test_strips_port(self) -> None:
        self.assertEqual(_host_of("http://example.com:8080/x"), "example.com")

    def test_empty(self) -> None:
        self.assertEqual(_host_of(""), "")
        self.assertEqual(_host_of("not-a-url"), "")


class StorageBlocklistTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="filter-test-")
        os.close(fd)
        self.store = MonitorStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ---- persistence helpers --------------------------------------

    def test_blocklist_normalization(self) -> None:
        # Inputs get lowercased, www-stripped, deduped, and trimmed.
        self.store.set_blocklist([
            "  MSN.com  ",
            "www.yahoo.com",
            "MSN.COM",         # duplicate
            "",
            ".prnewswire.com", # leading dot
        ])
        self.assertEqual(
            self.store.get_blocklist(),
            ["msn.com", "yahoo.com", "prnewswire.com"],
        )

    def test_min_title_length_persists(self) -> None:
        self.store.set_min_title_length(15)
        self.assertEqual(self.store.get_min_title_length(), 15)
        # Invalid input clamps to 0
        self.store.set_min_title_length(-5)
        self.assertEqual(self.store.get_min_title_length(), 0)

    # ---- end-to-end filtering -------------------------------------

    def test_blocklist_drops_matching_rows(self) -> None:
        self.store.save_match(_doc(
            "gdelt", "https://news.msn.com/2025/01/15/A", title="Junk"), [])
        self.store.save_match(_doc(
            "gdelt", "https://nytimes.com/2025/01/15/B", title="Real"), [])
        self.store.set_blocklist(["msn.com"])

        rows = self.store.search_matches(limit=10)
        titles = [r["title"] for r in rows]
        self.assertEqual(titles, ["Real"])

    def test_min_title_drops_short_titles(self) -> None:
        self.store.save_match(_doc(
            "gdelt", "https://example.com/2025/01/15/A", title="A"), [])
        self.store.save_match(_doc(
            "gdelt", "https://example.com/2025/01/15/B",
            title="A longer headline here"), [])
        self.store.set_min_title_length(10)

        rows = self.store.search_matches(limit=10)
        titles = [r["title"] for r in rows]
        self.assertEqual(titles, ["A longer headline here"])

    def test_bypass_filters_returns_everything(self) -> None:
        self.store.save_match(_doc(
            "gdelt", "https://news.msn.com/2025/01/15/A", title="Junk"), [])
        self.store.save_match(_doc(
            "gdelt", "https://nytimes.com/2025/01/15/B", title="Real"), [])
        self.store.set_blocklist(["msn.com"])

        rows = self.store.search_matches(limit=10, bypass_filters=True)
        titles = sorted(r["title"] for r in rows)
        self.assertEqual(titles, ["Junk", "Real"])

    def test_blocklist_runs_after_cross_source_collapse(self) -> None:
        # The same article surfaced via two sources — once with a
        # blocked host, once with a clean host. Because dedup picks ONE
        # canonical_url per group (and both URLs canonicalize to the
        # same string only if the host matches), this exercises the
        # "blocklist applies to the rep's canonical host" assumption.
        # Here url_msn and url_real have DIFFERENT canonical_urls so
        # they stay as two articles — the msn one gets filtered out.
        url_msn = "https://news.msn.com/2025/01/15/article"
        url_real = "https://reuters.com/2025/01/15/article"
        self.store.save_match(_doc("gdelt", url_msn,   title="X"), [])
        self.store.save_match(_doc("gdelt", url_real,  title="X"), [])
        self.store.set_blocklist(["msn.com"])

        rows = self.store.search_matches(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertIn("reuters.com", rows[0]["canonical_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
