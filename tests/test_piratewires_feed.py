"""Exercise rate-limit recovery and full article extraction without external services."""

import os
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "feed_generators"))
import piratewires_blog as pirate

ARTICLE_LINK = "https://www.piratewires.com/p/current-story"
LISTING = '<a href="/p/current-story">Listing excerpt only</a>'
ARTICLE = (
    '<h1>A current story</h1><section class="article_postBody">'
    + "".join(
        "<p>Full article paragraph %d. %s</p>" % (i, "Article body text. " * 20)
        for i in range(8)
    )
    + "</section>"
)


class PirateWiresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hits = 0

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits += 1
                blocked = self.path == "/blocked" or (
                    self.path in ("/transient", "/cooldown") and cls.hits == 1
                )
                self.send_response(429 if blocked else 200)
                if blocked:
                    self.send_header(
                        "Retry-After", "7" if self.path == "/cooldown" else "0"
                    )
                self.end_headers()
                self.wfile.write(("Vercel challenge" if blocked else LISTING).encode())

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).hits = 0

    def test_transient_429_retries_requests(self):
        with patch.object(pirate.time, "sleep"):
            self.assertEqual(
                pirate.fetch_page_requests(self.base + "/transient"), LISTING
            )
        self.assertEqual(type(self).hits, 2)

    def test_rate_limit_honours_retry_after(self):
        with patch("urllib3.util.retry.time.sleep") as sleep:
            self.assertEqual(
                pirate.fetch_page_requests(self.base + "/cooldown"), LISTING
            )
        sleep.assert_called_once_with(7)
        self.assertEqual(type(self).hits, 2)

    def test_persistent_429_uses_browser_in_ci(self):
        with (
            patch.dict(os.environ, {"CI": "true"}),
            patch("urllib3.util.retry.time.sleep"),
            patch.object(
                pirate, "fetch_page_selenium", return_value=LISTING
            ) as browser,
        ):
            articles = self.collect("/blocked")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["link"], ARTICLE_LINK)
        browser.assert_called_once_with(self.base + "/blocked")
        self.assertEqual(type(self).hits, 4)

    def collect(self, endpoint):
        with patch.dict(
            pirate.CATEGORY_PAGES, {"Home": self.base + endpoint}, clear=True
        ):
            return pirate.collect_listing_articles()

    def test_browser_failure_propagates(self):
        with (
            patch.dict(os.environ, {"CI": "true"}),
            patch("urllib3.util.retry.time.sleep"),
            patch.object(
                pirate,
                "fetch_page_selenium",
                side_effect=RuntimeError("browser blocked"),
            ),
            self.assertRaisesRegex(RuntimeError, "browser blocked"),
        ):
            self.collect("/blocked")

    def test_explicit_driver_does_not_require_uc_cache(self):
        with (
            patch.dict(
                os.environ,
                {
                    "CHROMEDRIVER_PATH": "/workflow/chromedriver",
                    "CHROME_BINARY": "/workflow/chrome",
                },
            ),
            patch.object(pirate.uc, "Chrome") as chrome,
        ):
            pirate.setup_selenium_driver()
        self.assertFalse(chrome.call_args.kwargs["user_multi_procs"])
        self.assertEqual(
            chrome.call_args.kwargs["driver_executable_path"], "/workflow/chromedriver"
        )

    def test_writes_article_body_not_listing_excerpt(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                with (
                    patch.dict(
                        pirate.CATEGORY_PAGES,
                        {"Home": self.base + "/listing"},
                        clear=True,
                    ),
                    patch.object(
                        pirate, "fetch_article_page_requests", return_value=ARTICLE
                    ) as article_fetch,
                    patch.object(
                        pirate,
                        "fetch_articles_selenium",
                        side_effect=AssertionError("Complete article needs no browser"),
                    ),
                ):
                    self.assertTrue(pirate.main())
                article_fetch.assert_called_once()
                self.assertEqual(article_fetch.call_args.args[0], ARTICLE_LINK)
                item = ET.parse("feeds/feed_piratewires.xml").find("./channel/item")
                content = item.findtext(
                    "{http://purl.org/rss/1.0/modules/content/}encoded"
                )
                self.assertIn("Full article paragraph 7.", content)
                self.assertNotIn("Listing excerpt only", content)
            finally:
                os.chdir(previous)

    def test_missing_article_content_fails_without_overwriting_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                Path("feeds").mkdir()
                destination = Path("feeds/feed_piratewires.xml")
                destination.write_text("<rss><channel/></rss>")
                with (
                    patch.dict(
                        pirate.CATEGORY_PAGES,
                        {"Home": self.base + "/listing"},
                        clear=True,
                    ),
                    patch.object(
                        pirate, "fetch_article_page_requests", return_value=None
                    ),
                    patch.object(pirate, "fetch_articles_selenium", return_value={}),
                ):
                    self.assertFalse(pirate.main())
                self.assertEqual(destination.read_text(), "<rss><channel/></rss>")
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
