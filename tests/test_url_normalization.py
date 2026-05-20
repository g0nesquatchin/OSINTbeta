"""Tests for monitor.url_norm.canonicalize.

This is the helper that powers cross-source URL dedup. It's pure
string-manipulation, so tests are fast and exhaustive.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from monitor.url_norm import canonicalize


class CanonicalizeTest(unittest.TestCase):
    def assertSame(self, a: str, b: str) -> None:
        """Both URLs canonicalize to the same thing — the assertion
        cross-source dedup actually depends on."""
        self.assertEqual(canonicalize(a), canonicalize(b),
                         f"\n  {a}\n  vs\n  {b}")

    def assertDifferent(self, a: str, b: str) -> None:
        self.assertNotEqual(canonicalize(a), canonicalize(b),
                            f"\n  {a}\n  vs\n  {b}")

    # ---- the headline case ----------------------------------------

    def test_strips_utm_params(self) -> None:
        self.assertSame(
            "https://nytimes.com/2025/01/15/world/article.html",
            "https://nytimes.com/2025/01/15/world/article.html?utm_source=feed&utm_medium=email",
        )

    def test_strips_fbclid_gclid(self) -> None:
        self.assertSame(
            "https://example.com/article",
            "https://example.com/article?fbclid=abc&gclid=xyz",
        )

    def test_www_prefix_stripped(self) -> None:
        self.assertSame(
            "https://www.bbc.com/news/world-12345",
            "https://bbc.com/news/world-12345",
        )

    def test_scheme_case_normalized(self) -> None:
        self.assertSame(
            "HTTPS://Example.Com/Path",
            "https://example.com/Path",
        )

    def test_fragment_stripped(self) -> None:
        self.assertSame(
            "https://example.com/article",
            "https://example.com/article#section-2",
        )

    def test_trailing_slash_dropped(self) -> None:
        self.assertSame(
            "https://example.com/path",
            "https://example.com/path/",
        )

    # ---- AMP variants -------------------------------------------

    def test_amp_path_segment_stripped(self) -> None:
        self.assertSame(
            "https://example.com/news/world-story",
            "https://example.com/amp/news/world-story",
        )

    def test_amp_html_suffix_stripped(self) -> None:
        self.assertSame(
            "https://example.com/article.html",
            "https://example.com/article.amp.html",
        )

    def test_amp_tail_stripped(self) -> None:
        self.assertSame(
            "https://example.com/article",
            "https://example.com/article/amp",
        )

    # ---- query parameter handling -------------------------------

    def test_param_ordering_normalized(self) -> None:
        self.assertSame(
            "https://example.com/article?a=1&b=2",
            "https://example.com/article?b=2&a=1",
        )

    def test_non_tracking_params_preserved(self) -> None:
        # Real content params (like id or page) must survive.
        self.assertEqual(
            canonicalize("https://example.com/story?id=42&utm_source=x"),
            "https://example.com/story?id=42",
        )

    def test_multiple_tracking_prefixes_stripped(self) -> None:
        self.assertSame(
            "https://example.com/article",
            "https://example.com/article?utm_campaign=x&utm_term=y&ref_url=z",
        )

    # ---- things we DON'T want to collapse -----------------------

    def test_different_hosts_stay_different(self) -> None:
        # Same article syndicated to two different outlets has two
        # genuinely different URLs — we can't dedup that with URL alone.
        self.assertDifferent(
            "https://reuters.com/article-1",
            "https://msn.com/article-1",
        )

    def test_different_paths_stay_different(self) -> None:
        self.assertDifferent(
            "https://example.com/article-1",
            "https://example.com/article-2",
        )

    def test_different_content_params_stay_different(self) -> None:
        # If `id` distinguishes articles, two ids must canonicalize
        # differently (we only strip *tracking* params).
        self.assertDifferent(
            "https://example.com/story?id=42",
            "https://example.com/story?id=43",
        )

    # ---- robustness --------------------------------------------

    def test_empty_url(self) -> None:
        self.assertEqual(canonicalize(""), "")

    def test_malformed_url(self) -> None:
        # No scheme + host — return unchanged rather than mangle.
        self.assertEqual(canonicalize("not a url"), "not a url")

    def test_idempotent(self) -> None:
        urls = [
            "https://www.nytimes.com/2025/01/15/article?utm_source=feed#top",
            "https://example.com/amp/news/x?a=1&b=2",
            "https://reuters.com/article/",
        ]
        for u in urls:
            with self.subTest(u=u):
                once = canonicalize(u)
                twice = canonicalize(once)
                self.assertEqual(once, twice,
                                 f"not idempotent: {u} -> {once} -> {twice}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
