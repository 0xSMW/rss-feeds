"""Exercise rate-limit recovery and full article extraction without external services."""

import json
import os
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

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
                if self.path.startswith("/posts/"):
                    cls.api_requests.append(self.path)
                    slug = urlparse(self.path).path.rsplit("/", 1)[-1]
                    post = {
                        "slug": "wrong-slug" if cls.api_mode == "wrong" else slug,
                        "title": "A current story",
                        "subtitle": "A summary",
                        "post_date": "2026-11-12T18:35:10Z",
                        "type": "newsletter",
                        "authors": [{"name": "Writer"}],
                        "body_html": "" if cls.api_mode == "missing" else ARTICLE,
                    }
                    self.send_response(403 if cls.api_mode == "denied" else 200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"post": post}).encode())
                    return
                blocked = self.path in ("/blocked", "/challenge") or (
                    self.path in ("/transient", "/cooldown") and cls.hits == 1
                )
                self.send_response(429 if blocked else 200)
                if self.path == "/challenge":
                    self.send_header("x-vercel-mitigated", "challenge")
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
        type(self).api_mode = "full"
        type(self).api_requests = []

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

    def test_vercel_challenge_switches_session_to_browser_without_retries(self):
        session = pirate.build_requests_session()
        with patch.object(
            pirate, "fetch_page_selenium", return_value=LISTING
        ) as browser:
            self.assertEqual(
                pirate.fetch_page(self.base + "/challenge", session), LISTING
            )
            self.assertEqual(
                pirate.fetch_page(self.base + "/another-listing", session), LISTING
            )
            self.assertIsNone(pirate.fetch_article_page_requests(ARTICLE_LINK, session))
        self.assertEqual(type(self).hits, 1)
        self.assertEqual(browser.call_count, 2)

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

    def test_browser_fetch_accepts_existing_video_only_posts(self):
        driver = MagicMock()
        driver.find_elements.return_value = []
        driver.page_source = "<article><video controls></video></article>"
        with patch.object(pirate, "setup_selenium_driver", return_value=driver):
            pages = pirate.fetch_articles_selenium([ARTICLE_LINK])
        self.assertEqual(pages, {ARTICLE_LINK: driver.page_source})
        driver.execute_script.assert_not_called()
        driver.quit.assert_called_once()

    def test_explicit_driver_does_not_require_uc_cache(self):
        with (
            patch.dict(
                os.environ,
                {
                    "CI": "true",
                    "DISPLAY": ":99",
                    "CHROMEDRIVER_PATH": "/workflow/chromedriver",
                    "CHROME_BINARY": "/workflow/chrome",
                },
            ),
            patch.object(pirate.uc, "Chrome") as chrome,
        ):
            pirate.setup_selenium_driver()
        self.assertFalse(chrome.call_args.kwargs["user_multi_procs"])
        arguments = chrome.call_args.kwargs["options"].arguments
        self.assertIn("--headless=new", arguments)
        self.assertEqual(
            chrome.call_args.kwargs["driver_executable_path"], "/workflow/chromedriver"
        )

    def test_full_metered_article_replaces_cached_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                Path("feeds").mkdir()
                Path("feeds/feed_piratewires.xml").write_text(
                    '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
                    "<title>Cached story</title><link>" + ARTICLE_LINK + "</link>"
                    "<content:encoded><![CDATA[<p>Cached preview only</p>]]></content:encoded>"
                    "</item></channel></rss>"
                )
                with (
                    patch.dict(
                        pirate.CATEGORY_PAGES,
                        {"Home": self.base + "/listing"},
                        clear=True,
                    ),
                    patch.object(pirate, "ARTICLE_API_BASE", self.base + "/posts"),
                    patch.object(
                        pirate,
                        "fetch_articles_selenium",
                        side_effect=AssertionError(
                            "Article API supplies the complete body"
                        ),
                    ),
                ):
                    self.assertTrue(pirate.main())
                query = parse_qs(urlparse(type(self).api_requests[-1]).query)
                self.assertEqual(query, {"withMetering": ["true"]})
                item = ET.parse("feeds/feed_piratewires.xml").find("./channel/item")
                content = item.findtext(
                    "{http://purl.org/rss/1.0/modules/content/}encoded"
                )
                self.assertIn("Full article paragraph 7.", content)
                self.assertNotIn("Listing excerpt only", content)
                self.assertEqual(item.findtext("link"), ARTICLE_LINK)
            finally:
                os.chdir(previous)

    def test_article_api_is_independent_of_challenged_frontend_session(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                with (
                    patch.dict(
                        pirate.CATEGORY_PAGES,
                        {"Home": self.base + "/challenge"},
                        clear=True,
                    ),
                    patch.object(pirate, "ARTICLE_API_BASE", self.base + "/posts"),
                    patch.object(pirate, "fetch_page_selenium", return_value=LISTING),
                ):
                    self.assertTrue(pirate.main())
                self.assertEqual(type(self).hits, 3)
            finally:
                os.chdir(previous)

    def test_source_content_errors_fail_without_overwriting_feed(self):
        for mode in ("missing", "wrong", "denied"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                previous = Path.cwd()
                os.chdir(directory)
                type(self).api_mode = mode
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
                        patch.object(pirate, "ARTICLE_API_BASE", self.base + "/posts"),
                        patch.object(
                            pirate,
                            "_fetch_article_api_browser",
                            side_effect=RuntimeError("Publisher denied access"),
                        ),
                    ):
                        self.assertFalse(pirate.main())
                    self.assertEqual(destination.read_text(), "<rss><channel/></rss>")
                finally:
                    os.chdir(previous)

    def test_only_newest_feed_candidates_fetch_article_bodies(self):
        articles = [
            {
                "title": f"Story {i}",
                "link": f"https://www.piratewires.com/p/story-{i}",
                "date": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                "category": "Home",
            }
            for i in range(61)
        ]
        metadata = {"content_html": "<p>Complete article body</p>"}
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                with (
                    patch.object(
                        pirate, "collect_listing_articles", return_value=articles
                    ),
                    patch.object(
                        pirate, "fetch_article_api", return_value=metadata
                    ) as fetch,
                ):
                    self.assertTrue(pirate.main())
                self.assertEqual(fetch.call_count, 50)
                self.assertEqual(
                    {call.args[0] for call in fetch.call_args_list},
                    {a["link"] for a in articles[:50]},
                )
                self.assertNotIn(
                    "https://www.piratewires.com/p/story-0",
                    {call.args[0] for call in fetch.call_args_list},
                )
                self.assertEqual(
                    len(
                        ET.parse("feeds/feed_piratewires.xml").findall("./channel/item")
                    ),
                    50,
                )
            finally:
                os.chdir(previous)

    def test_undated_old_links_do_not_displace_newest_stories(self):
        dated = [
            {
                "title": f"Story {i}",
                "link": f"https://www.piratewires.com/p/story-{i}",
                "date": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                "category": "Home",
            }
            for i in range(61)
        ]
        undated = [
            {
                "title": "Undated archive",
                "link": f"https://www.piratewires.com/p/archive-{i}",
                "date": None,
                "category": "Culture",
            }
            for i in range(50)
        ]
        full_requests = []
        metadata_requests = []

        def fetch(url, session=None, *, include_body=True):
            if not include_body:
                metadata_requests.append(url)
                return {"date": datetime(2025, 1, 1, tzinfo=timezone.utc)}
            full_requests.append(url)
            return {"content_html": "<p>Complete current story</p>"}

        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                with (
                    patch.object(
                        pirate, "collect_listing_articles", return_value=dated + undated
                    ),
                    patch.object(pirate, "fetch_article_api", side_effect=fetch),
                ):
                    self.assertTrue(pirate.main())
                self.assertEqual(len(metadata_requests), 50)
                self.assertEqual(set(full_requests), {a["link"] for a in dated[-50:]})
                self.assertEqual(len(full_requests), 50)
            finally:
                os.chdir(previous)

    def test_blocked_article_api_uses_browser_json(self):
        type(self).api_mode = "denied"
        payload = json.dumps(
            {
                "post": {
                    "slug": "current-story",
                    "title": "Current story",
                    "post_date": "2026-11-12T00:00:00Z",
                    "type": "newsletter",
                    "body_html": ARTICLE,
                }
            }
        )
        with (
            patch.object(pirate, "ARTICLE_API_BASE", self.base + "/posts"),
            patch.object(
                pirate, "_fetch_article_api_browser", return_value=payload
            ) as browser,
        ):
            metadata = pirate.fetch_article_api(ARTICLE_LINK)
        self.assertIn("Full article paragraph 7.", metadata["content_html"])
        self.assertIn("withMetering=true", browser.call_args.args[0])

    def test_article_json_browser_is_reused_and_closed(self):
        for allowed in (True, False):
            with (
                self.subTest(allowed=allowed),
                tempfile.TemporaryDirectory() as directory,
            ):
                previous = Path.cwd()
                os.chdir(directory)
                type(self).api_mode = "denied"
                driver = MagicMock()
                post = {
                    "slug": "current-story",
                    "title": "Current story",
                    "post_date": "2026-11-12T00:00:00Z",
                    "type": "newsletter",
                    "body_html": ARTICLE,
                }
                driver.execute_async_script.return_value = {
                    "status": 200 if allowed else 403,
                    "body": json.dumps(
                        {"post": post}
                        if allowed
                        else {"error": "Publisher denied access"}
                    ),
                }
                try:
                    with (
                        patch.dict(
                            pirate.CATEGORY_PAGES,
                            {"Home": self.base + "/listing"},
                            clear=True,
                        ),
                        patch.object(pirate, "ARTICLE_API_BASE", self.base + "/posts"),
                        patch.object(
                            pirate, "setup_selenium_driver", return_value=driver
                        ) as setup,
                    ):
                        self.assertEqual(pirate.main(), allowed)
                    setup.assert_called_once()
                    driver.get.assert_called_once_with(pirate.BASE_URL)
                    self.assertEqual(
                        driver.execute_async_script.call_count, 2 if allowed else 1
                    )
                    driver.quit.assert_called_once()
                    self.assertEqual(
                        Path("feeds/feed_piratewires.xml").exists(), allowed
                    )
                finally:
                    os.chdir(previous)

    def test_browser_listing_does_not_scroll_into_discarded_archives(self):
        driver = MagicMock()
        driver.page_source = LISTING
        with patch.object(pirate, "setup_selenium_driver", return_value=driver):
            self.assertEqual(
                pirate.fetch_page_selenium(self.base + "/listing"), LISTING
            )
        driver.execute_script.assert_not_called()
        driver.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
