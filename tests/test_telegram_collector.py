"""Parser tests for the Telegram public-channel collector.

We feed a synthetic ``t.me/s/<channel>`` HTML fixture to
``_parse_channel_html`` and assert the yielded Documents have the right
fields. No network access required.

If beautifulsoup4 isn't installed, all tests in this module skip.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import bs4  # noqa: F401
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

from monitor.collectors.telegram import _parse_channel_html, _clean_handle


# Stripped-down version of the public preview HTML. Class names match the
# selectors currently used by ``t.me/s/<channel>`` (verified against
# production HTML, though Telegram has changed these before).
FIXTURE_HTML = """
<!DOCTYPE html>
<html><body>
  <div class="tgme_channel_info_header_title">Some Channel · Long Name</div>

  <div class="tgme_channel_history">
    <!-- oldest first in the actual preview, newest last -->
    <div class="tgme_widget_message" data-post="somechannel/100">
      <div class="tgme_widget_message_text">First message body</div>
      <a class="tgme_widget_message_date" href="https://t.me/somechannel/100">
        <time datetime="2026-05-01T10:00:00+00:00">May 1</time>
      </a>
      <span class="tgme_widget_message_views">1.2K</span>
    </div>

    <div class="tgme_widget_message" data-post="somechannel/101">
      <div class="tgme_widget_message_text">
        Second message — mentions <a href="...">a missing person</a> in Lagos
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/somechannel/101">
        <time datetime="2026-05-01T11:30:00+00:00">May 1</time>
      </a>
      <a class="tgme_widget_message_photo_wrap" href="..."></a>
      <span class="tgme_widget_message_views">732</span>
    </div>

    <div class="tgme_widget_message" data-post="somechannel/102">
      <div class="tgme_widget_message_text">Latest update on the situation</div>
      <a class="tgme_widget_message_date" href="https://t.me/somechannel/102">
        <time datetime="2026-05-01T12:45:00+00:00">May 1</time>
      </a>
      <span class="tgme_widget_message_views">2.5M</span>
    </div>

    <!-- A malformed entry without data-post — should be skipped. -->
    <div class="tgme_widget_message">
      <div class="tgme_widget_message_text">orphaned message</div>
    </div>
  </div>
</body></html>
"""


@unittest.skipUnless(BS4_AVAILABLE, "beautifulsoup4 not installed")
class TelegramParserTest(unittest.TestCase):
    def test_yields_documents_in_newest_first_order(self) -> None:
        docs = list(_parse_channel_html(FIXTURE_HTML, channel="somechannel", limit=10))
        self.assertEqual(len(docs), 3, f"expected 3 docs, got {len(docs)}")
        # Newest first
        self.assertEqual(docs[0].source_id, "somechannel/102")
        self.assertEqual(docs[1].source_id, "somechannel/101")
        self.assertEqual(docs[2].source_id, "somechannel/100")

    def test_document_fields(self) -> None:
        docs = list(_parse_channel_html(FIXTURE_HTML, channel="somechannel", limit=10))
        d = docs[1]  # the middle message with the link inside
        self.assertEqual(d.source, "telegram")
        self.assertEqual(d.source_id, "somechannel/101")
        self.assertEqual(d.url, "https://t.me/somechannel/101")
        self.assertIn("missing person", d.content)
        self.assertEqual(d.author, "Some Channel · Long Name")
        self.assertIsNotNone(d.created_at)
        self.assertEqual(d.extra["channel"], "somechannel")
        self.assertEqual(d.extra["msg_id"], "101")
        self.assertTrue(d.extra["has_media"])  # we added a photo wrap

    def test_views_parsing_handles_k_m_and_plain(self) -> None:
        docs = list(_parse_channel_html(FIXTURE_HTML, channel="somechannel", limit=10))
        # Order is newest first, so docs[0] is msg 102 (2.5M), [1]=101 (732), [2]=100 (1.2K)
        self.assertEqual(docs[0].extra["views"], 2_500_000)
        self.assertEqual(docs[1].extra["views"], 732)
        self.assertEqual(docs[2].extra["views"], 1_200)

    def test_limit_caps_yielded(self) -> None:
        docs = list(_parse_channel_html(FIXTURE_HTML, channel="somechannel", limit=2))
        self.assertEqual(len(docs), 2)
        # Newest-first still — so we keep the freshest two
        self.assertEqual(docs[0].source_id, "somechannel/102")
        self.assertEqual(docs[1].source_id, "somechannel/101")


class CleanHandleTest(unittest.TestCase):
    def test_strips_at_and_protocol(self) -> None:
        self.assertEqual(_clean_handle("@durov"), "durov")
        self.assertEqual(_clean_handle("durov"), "durov")
        self.assertEqual(_clean_handle("https://t.me/durov"), "durov")
        self.assertEqual(_clean_handle("https://t.me/s/durov"), "durov")
        self.assertEqual(_clean_handle("t.me/durov/12345"), "durov")
        self.assertEqual(_clean_handle("  @durov  "), "durov")

    def test_rejects_invalid_handles(self) -> None:
        self.assertIsNone(_clean_handle(""))
        self.assertIsNone(_clean_handle("ab"))  # too short
        self.assertIsNone(_clean_handle("has spaces"))
        self.assertIsNone(_clean_handle("has-dash"))
        self.assertIsNone(_clean_handle("has.dot"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
