"""End-to-end tests for cross-source URL dedup at the storage layer.

We seed a MonitorStore with documents from multiple sources that share
a canonical URL and verify search_matches() collapses them with the
expected source aggregation.
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

from monitor.filters import MatchResult
from monitor.storage import Document, MonitorStore


def _doc(source: str, url: str, title: str = "Article", when=None) -> Document:
    return Document(
        source=source,
        # source_id is whatever the collector uses for uniqueness within
        # that source — typically the URL itself.
        source_id=url,
        author=source,
        title=title,
        content="",
        url=url,
        created_at=when or datetime.now(timezone.utc),
        extra={},
    )


class CrossSourceDedupTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="dedup-test-")
        os.close(fd)
        self.store = MonitorStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ---- the core promise ------------------------------------------

    def test_same_article_three_sources_collapses_to_one_row(self) -> None:
        url_a = "https://www.nytimes.com/2025/01/15/article.html?utm_source=feed"
        url_b = "https://nytimes.com/2025/01/15/article.html#section-1"
        url_c = "https://nytimes.com/2025/01/15/article.html/"

        self.store.save_match(_doc("gdelt",       url_a), [])
        self.store.save_match(_doc("google_news", url_b), [])
        self.store.save_match(_doc("rss",         url_c), [])

        rows = self.store.search_matches(limit=10)
        self.assertEqual(len(rows), 1, f"expected 1 collapsed row, got {len(rows)}")
        row = rows[0]
        self.assertEqual(row["source_count"], 3)
        # Sources should be all three. Order is insertion order so the
        # first-seen source is the rep.
        self.assertEqual(set(row["sources"]), {"gdelt", "google_news", "rss"})

    def test_different_articles_stay_separate(self) -> None:
        self.store.save_match(_doc("gdelt",
            "https://example.com/2025/01/15/article-A", title="A"), [])
        self.store.save_match(_doc("gdelt",
            "https://example.com/2025/01/15/article-B", title="B"), [])
        rows = self.store.search_matches(limit=10)
        titles = sorted(r["title"] for r in rows)
        self.assertEqual(titles, ["A", "B"])
        for r in rows:
            self.assertEqual(r["source_count"], 1)

    def test_same_source_same_url_does_not_double_count(self) -> None:
        # Two ingests of the same article from the same source (e.g.
        # two collection passes) should not show source_count=2.
        url = "https://example.com/2025/01/15/article"
        self.store.save_match(_doc("gdelt", url), [])
        self.store.save_match(_doc("gdelt", url), [])
        rows = self.store.search_matches(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_count"], 1)
        self.assertEqual(rows[0]["sources"], ["gdelt"])

    def test_legacy_rows_without_canonical_get_backfilled(self) -> None:
        # Simulate a pre-migration row: insert directly with NULL
        # canonical_url, then reopen the store so _migrate() runs again
        # (it's idempotent) and backfills.
        self.store.conn.execute(
            """INSERT INTO documents
               (dedup_key, source, source_id, url, canonical_url,
                collected_at)
               VALUES ('legacy', 'gdelt', 'http://x/a',
                       'http://x/a', NULL, '2026-01-01T00:00:00+00:00')"""
        )
        self.store.conn.execute(
            """UPDATE documents SET canonical_url = NULL
               WHERE dedup_key = 'legacy'"""
        )
        self.store.conn.commit()
        self.store.close()
        store2 = MonitorStore(self.db_path)
        try:
            row = store2.conn.execute(
                "SELECT canonical_url FROM documents WHERE dedup_key='legacy'"
            ).fetchone()
            self.assertIsNotNone(row["canonical_url"])
            self.assertTrue(row["canonical_url"].startswith("http"))
        finally:
            store2.close()
            self.store = MonitorStore(self.db_path)  # for tearDown

    def test_dedup_respects_limit_after_aggregation(self) -> None:
        # 5 distinct canonical articles, the first one (newest) appears in
        # 3 sources. Asking for limit=3 should give us 3 distinct articles
        # (not 3 row variants of one) AND preserve the 3-source aggregation.
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        url1 = "https://example.com/2025/01/15/A"
        # Three sources, all NEWEST so url1 ranks at the top.
        self.store.save_match(_doc("gdelt",       url1, when=now), [])
        self.store.save_match(_doc("google_news", url1, when=now), [])
        self.store.save_match(_doc("rss",         url1, when=now), [])
        # Four older single-source articles.
        for i in range(2, 6):
            self.store.save_match(_doc(
                "gdelt", f"https://example.com/2025/01/15/{i}",
                when=now - timedelta(hours=i),
            ), [])

        rows = self.store.search_matches(limit=3)
        self.assertEqual(len(rows), 3)
        # The 3-source article should be the first row (newest) with all
        # three sources aggregated.
        self.assertEqual(rows[0]["url"], url1)
        self.assertEqual(rows[0]["source_count"], 3)
        self.assertEqual(set(rows[0]["sources"]),
                         {"gdelt", "google_news", "rss"})

    def test_topic_tags_union_across_sources(self) -> None:
        url = "https://example.com/2025/01/15/article"
        # Create two topics
        t1 = self.store.create_topic("Africa", "word", ["Uganda"])
        t2 = self.store.create_topic("Conflict", "word", ["M23"])
        # Same article surfaces via two sources, matched by different topics
        self.store.save_match(_doc("gdelt", url),
            [MatchResult(topic_id=t1, topic_name="Africa", keywords=["Uganda"])])
        self.store.save_match(_doc("google_news", url),
            [MatchResult(topic_id=t2, topic_name="Conflict", keywords=["M23"])])

        rows = self.store.search_matches(limit=10)
        self.assertEqual(len(rows), 1)
        topics_in_row = set((rows[0]["topics"] or "").split(","))
        self.assertIn("Africa", topics_in_row)
        self.assertIn("Conflict", topics_in_row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
