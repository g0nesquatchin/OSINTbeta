"""Tests for monitor.collectors.base.extract_date_from_url.

This is the helper that fixes the GDELT "seendate=today on a 2024 article"
problem: we trust dates embedded in the URL path more than dates that
sources hand us.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from monitor.collectors.base import extract_date_from_url


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


class ExtractDateFromUrlTest(unittest.TestCase):
    # ---- common news outlet patterns ------------------------------

    def test_slash_separated_yyyy_mm_dd(self) -> None:
        cases = [
            ("https://nytimes.com/2025/01/15/world/article-slug.html", _utc(2025, 1, 15)),
            ("https://bbc.co.uk/news/2024/03/22/story-here", _utc(2024, 3, 22)),
            ("https://example.com/articles/2026/05/01/title", _utc(2026, 5, 1)),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(extract_date_from_url(url), expected)

    def test_dash_separated_yyyy_mm_dd(self) -> None:
        self.assertEqual(
            extract_date_from_url("https://example.com/2025-01-15-some-title"),
            _utc(2025, 1, 15),
        )

    def test_query_param_date(self) -> None:
        cases = [
            "https://example.com/article?date=2025-01-15",
            "https://example.com/article?id=42&published=2025-1-15",
            "https://example.com/article?pubdate=2025-01-15&id=42",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(extract_date_from_url(url), _utc(2025, 1, 15))

    def test_year_month_only_falls_back_to_day_1(self) -> None:
        # Lower confidence — we use day=01 when no day is in the path.
        self.assertEqual(
            extract_date_from_url("https://example.com/2025/03/some-slug-here"),
            _utc(2025, 3, 1),
        )

    # ---- robustness ------------------------------------------------

    def test_no_date_in_url_returns_none(self) -> None:
        self.assertIsNone(extract_date_from_url("https://example.com/some-article"))
        self.assertIsNone(extract_date_from_url("https://example.com/"))
        self.assertIsNone(extract_date_from_url(""))
        self.assertIsNone(extract_date_from_url(None))

    def test_rejects_implausible_year(self) -> None:
        # Random four-digit number in a slug shouldn't pose as a year.
        self.assertIsNone(extract_date_from_url("https://example.com/1899/01/15/x"))
        self.assertIsNone(extract_date_from_url("https://example.com/3000/01/15/x"))

    def test_rejects_invalid_month_day(self) -> None:
        # /2025/13/01/ — month 13 should not validate
        self.assertIsNone(extract_date_from_url("https://example.com/2025/13/01/x"))
        # /2025/02/30/ — day 30 in Feb should not validate
        self.assertIsNone(extract_date_from_url("https://example.com/2025/02/30/x"))

    def test_does_not_match_concatenated_yyyymmdd(self) -> None:
        # /20250115/ without separators is ambiguous (could be an id or a
        # date); we deliberately only match clearly-delimited forms.
        self.assertIsNone(extract_date_from_url("https://example.com/article/20250115/x"))

    def test_returns_utc_midnight(self) -> None:
        dt = extract_date_from_url("https://example.com/2025/01/15/x")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual((dt.hour, dt.minute, dt.second), (0, 0, 0))

    def test_picks_first_date_when_multiple(self) -> None:
        # If a URL contains multiple plausible dates (e.g. archived view),
        # we currently take the first match. Document that explicitly.
        url = "https://example.com/2025/01/15/archive/2020/06/01/old"
        self.assertEqual(extract_date_from_url(url), _utc(2025, 1, 15))


if __name__ == "__main__":
    unittest.main(verbosity=2)
