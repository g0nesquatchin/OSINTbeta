"""Tests for the GKG store's theme/tone/since filtering."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gkg.fetcher import GkgArticle, GkgLocation
from gkg.storage import GkgStore
from gkg.themes import THEME_GROUPS, codes_for_groups


def _art(url: str, themes: list[str], tone: float,
         locations: list[GkgLocation],
         when: datetime | None = None) -> GkgArticle:
    when = when or datetime.now(timezone.utc)
    return GkgArticle(
        url=url,
        source="example.test",
        date_str=when.strftime("%Y%m%d%H%M%S"),
        themes=themes,
        tone=tone,
        locations=locations,
    )


def _loc(name: str, lat: float, lon: float) -> GkgLocation:
    return GkgLocation(
        loc_type=4, name=name, country_code="US", admin1_code="",
        lat=lat, lon=lon, feature_id="",
    )


class GkgSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="gkg-test-")
        os.close(fd)
        self.store = GkgStore(self.db_path)

        now = datetime.now(timezone.utc)
        # Article tagged KILL — distinct from SKILLED below.
        self.store.ingest([_art(
            "https://example.test/a/kill",
            ["KILL", "WB_2202_GENERAL_CRIME"],
            -5.0,
            [_loc("Atlanta", 33.7490, -84.3880)],
            when=now,
        )])
        # Article whose theme contains the literal substring "SKILLED" —
        # used to verify the boundary-aware theme matcher.
        self.store.ingest([_art(
            "https://example.test/a/skill",
            ["SKILLED_TRADES_TRAINING"],
            +2.0,
            [_loc("Boston", 42.3601, -71.0589)],
            when=now,
        )])
        # Older article tagged KILL to verify since_hours.
        self.store.ingest([_art(
            "https://example.test/a/old-kill",
            ["KILL"],
            -8.0,
            [_loc("Chicago", 41.8781, -87.6298)],
            when=now - timedelta(hours=48),
        )])
        # Positive-tone trafficking article.
        self.store.ingest([_art(
            "https://example.test/a/trafficking",
            ["HUMAN_TRAFFICKING"],
            +1.5,
            [_loc("Lagos", 6.5244, 3.3792)],
            when=now,
        )])

    def tearDown(self) -> None:
        self.store.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ---- boundary-aware theme matching ----------------------------

    def test_theme_filter_does_not_match_substring(self) -> None:
        results = self.store.search(keywords=[], themes=["KILL"])
        names = {r.name for r in results}
        # KILL must match the kill articles only, not SKILLED_TRADES_TRAINING
        self.assertIn("Atlanta", names)
        self.assertIn("Chicago", names)
        self.assertNotIn("Boston", names)

    def test_multiple_themes_are_ored(self) -> None:
        results = self.store.search(
            keywords=[], themes=["KILL", "HUMAN_TRAFFICKING"],
        )
        names = {r.name for r in results}
        self.assertIn("Atlanta", names)
        self.assertIn("Chicago", names)
        self.assertIn("Lagos", names)
        self.assertNotIn("Boston", names)

    # ---- tone bounds ----------------------------------------------

    def test_tone_max_filters_to_negative(self) -> None:
        results = self.store.search(
            keywords=[], themes=["KILL", "HUMAN_TRAFFICKING", "SKILLED_TRADES_TRAINING"],
            tone_max=0.0,
        )
        names = {r.name for r in results}
        # Only the negative-tone articles remain
        self.assertIn("Atlanta", names)   # tone -5
        self.assertIn("Chicago", names)   # tone -8
        self.assertNotIn("Boston", names)  # tone +2
        self.assertNotIn("Lagos", names)   # tone +1.5

    def test_tone_min_and_max(self) -> None:
        results = self.store.search(
            keywords=[], themes=None,
            tone_min=-6.0, tone_max=0.0,
        )
        names = {r.name for r in results}
        self.assertIn("Atlanta", names)   # -5 in range
        self.assertNotIn("Chicago", names)  # -8 below
        self.assertNotIn("Boston", names)   # +2 above
        self.assertNotIn("Lagos", names)    # +1.5 above

    # ---- since_hours ----------------------------------------------

    def test_since_hours_excludes_old(self) -> None:
        results = self.store.search(
            keywords=[], themes=["KILL"], since_hours=1,
        )
        names = {r.name for r in results}
        self.assertIn("Atlanta", names)     # fresh
        self.assertNotIn("Chicago", names)  # 48h ago

    # ---- keyword + theme combined --------------------------------

    def test_keyword_and_theme_combined_are_anded(self) -> None:
        # The keyword "kill" matches the URL slug of both Atlanta+Chicago,
        # AND theme HUMAN_TRAFFICKING is only on Lagos — intersection empty.
        results = self.store.search(
            keywords=["kill"], themes=["HUMAN_TRAFFICKING"],
        )
        self.assertEqual(results, [])

    def test_no_filters_returns_empty(self) -> None:
        # Defensive: with nothing at all, return [] (avoid hot-table dumps).
        results = self.store.search(keywords=[])
        self.assertEqual(results, [])

    # ---- top_themes -----------------------------------------------

    def test_top_themes_counts_correctly(self) -> None:
        top = dict(self.store.top_themes(limit=10))
        # KILL appears on Atlanta + Chicago articles
        self.assertEqual(top.get("KILL"), 2)
        self.assertEqual(top.get("HUMAN_TRAFFICKING"), 1)
        self.assertEqual(top.get("SKILLED_TRADES_TRAINING"), 1)

    # ---- curated catalog ------------------------------------------

    def test_catalog_groups_expand_to_codes(self) -> None:
        codes = codes_for_groups(["missing", "violence"])
        self.assertIn("MISSING_PERSON", codes)
        self.assertIn("KILL", codes)
        # de-duplication: HUMAN_TRAFFICKING_CHILD_TRAFFICKING shows up in
        # both "missing" and "trafficking" groups — but we only requested
        # one group here, so just confirm the de-dupe behavior:
        codes2 = codes_for_groups(["missing", "trafficking"])
        # de-duplicated, preserves first occurrence
        self.assertEqual(len(codes2), len(set(codes2)))

    def test_catalog_unknown_group_id_is_skipped(self) -> None:
        self.assertEqual(codes_for_groups(["nope-this-doesnt-exist"]), [])

    def test_catalog_contains_every_group(self) -> None:
        ids = {g.id for g in THEME_GROUPS}
        self.assertIn("missing", ids)
        self.assertIn("trafficking", ids)
        self.assertIn("violence", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
