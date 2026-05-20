"""Tests for the GDELT collector's OR-batched query builder + chunking.

Network access is never invoked — we just exercise the pure functions.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from monitor.collectors.gdelt import (
    _build_batched_query,
    _build_query,
    _chunk,
    _quote_term,
    _short_label,
)


class QuoteTermTest(unittest.TestCase):
    def test_single_word_unquoted(self) -> None:
        self.assertEqual(_quote_term("Uganda"), "Uganda")

    def test_multi_word_gets_quoted(self) -> None:
        self.assertEqual(_quote_term("Arua Airfield"), '"Arua Airfield"')

    def test_already_quoted_passes_through(self) -> None:
        self.assertEqual(_quote_term('"missing persons"'), '"missing persons"')

    def test_whitespace_trimmed(self) -> None:
        self.assertEqual(_quote_term("  Uganda  "), "Uganda")

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(_quote_term(""), "")
        self.assertEqual(_quote_term("   "), "")


class BuildBatchedQueryTest(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(_build_batched_query([], language=""), "")
        self.assertEqual(_build_batched_query(["", "  "], language=""), "")

    def test_single_keyword_no_parens(self) -> None:
        # One keyword shouldn't get wrapped in parens — looks ugly and
        # GDELT is happy with the bare form.
        self.assertEqual(_build_batched_query(["Uganda"], language=""), "Uganda")

    def test_single_multi_word_keyword_is_quoted(self) -> None:
        self.assertEqual(
            _build_batched_query(["Arua Airfield"], language=""),
            '"Arua Airfield"',
        )

    def test_multiple_keywords_or_combined(self) -> None:
        q = _build_batched_query(["Uganda", "M23", "Arua Airfield"], language="")
        # Order preserved, multi-word entries quoted
        self.assertEqual(q, '(Uganda OR M23 OR "Arua Airfield")')

    def test_language_appended(self) -> None:
        q = _build_batched_query(["Uganda", "M23"], language="english")
        self.assertEqual(q, "(Uganda OR M23) sourcelang:english")

    def test_single_country_appended(self) -> None:
        q = _build_batched_query(
            ["Uganda"], language="", gdelt_codes=["UG"],
        )
        self.assertEqual(q, "Uganda sourcecountry:UG")

    def test_multiple_countries_ored(self) -> None:
        q = _build_batched_query(
            ["Uganda", "M23"], language="", gdelt_codes=["UG", "RW", "CG"],
        )
        # Body is parenthesized OR; countries are also parenthesized OR
        self.assertEqual(
            q,
            "(Uganda OR M23) (sourcecountry:UG OR sourcecountry:RW OR sourcecountry:CG)",
        )

    def test_language_and_country_both(self) -> None:
        q = _build_batched_query(
            ["Uganda", "M23"], language="english", gdelt_codes=["UG"],
        )
        self.assertEqual(
            q, "(Uganda OR M23) sourcelang:english sourcecountry:UG",
        )

    def test_blank_keywords_filtered_out(self) -> None:
        q = _build_batched_query(["Uganda", "", "  ", "M23"], language="")
        self.assertEqual(q, "(Uganda OR M23)")


class ChunkTest(unittest.TestCase):
    def test_exact_division(self) -> None:
        self.assertEqual(
            list(_chunk(["a", "b", "c", "d"], 2)),
            [["a", "b"], ["c", "d"]],
        )

    def test_uneven_tail(self) -> None:
        self.assertEqual(
            list(_chunk(["a", "b", "c", "d", "e"], 2)),
            [["a", "b"], ["c", "d"], ["e"]],
        )

    def test_chunk_larger_than_input(self) -> None:
        self.assertEqual(list(_chunk(["a"], 5)), [["a"]])

    def test_empty(self) -> None:
        self.assertEqual(list(_chunk([], 5)), [])


class ShortLabelTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(_short_label([]), "(empty)")

    def test_single(self) -> None:
        self.assertEqual(_short_label(["Uganda"]), "'Uganda'")

    def test_multiple(self) -> None:
        self.assertEqual(
            _short_label(["Uganda", "M23", "Entebbe"]),
            "['Uganda' +2 more]",
        )


class BackwardCompatTest(unittest.TestCase):
    def test_old_single_keyword_function_still_works(self) -> None:
        # _build_query is the original API. We kept it as a thin wrapper
        # over _build_batched_query so any caller that imported it keeps
        # working. Verify behavioral equivalence for a single keyword.
        old = _build_query("Uganda", language="english", gdelt_codes=["UG"])
        new = _build_batched_query(
            ["Uganda"], language="english", gdelt_codes=["UG"],
        )
        self.assertEqual(old, new)


if __name__ == "__main__":
    unittest.main(verbosity=2)
