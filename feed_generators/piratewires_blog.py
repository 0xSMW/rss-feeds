import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import pytz
import requests
import undetected_chromedriver as uc
from _common import (
    DEFAULT_MAX_ITEMS,
    build_feed,
    clean_article_html,
    extract_summary,
    feed_self_url,
    load_cached_entries,
    save_feed,
)
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.piratewires.com"
ARTICLE_API_BASE = (
    "https://piratewires.substack.com/api/v1/islands/client/posts/by-slug"
)
CATEGORY_PAGES = {
    "Home": BASE_URL,
    "Culture": f"{BASE_URL}/c/culture",
    "Politics": f"{BASE_URL}/c/politics",
    "Technology": f"{BASE_URL}/c/technology",
}


def build_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Referer": BASE_URL,
        }
    )
    # Honour server cooldowns and retry transient rate limits before using a browser.
    retries = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        # Handle 429 and its Retry-After in fetch_page_requests so challenges
        # can switch to the browser immediately instead of being retried here.
        respect_retry_after_header=False,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    return session


def in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"


def setup_selenium_driver():
    options = uc.ChromeOptions()
    if not (in_ci() and os.environ.get("DISPLAY")):
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    browser_path = os.environ.get("CHROME_BINARY")
    version_main = os.environ.get("UC_VERSION_MAIN")
    return uc.Chrome(
        options=options,
        driver_executable_path=driver_path,
        browser_executable_path=browser_path,
        version_main=int(version_main) if version_main else None,
        # An explicit workflow-installed driver does not need a pre-existing UC cache.
        user_multi_procs=not bool(driver_path),
    )


def fetch_page_requests(url: str, session: requests.Session | None = None) -> str:
    sess = session or build_requests_session()
    if getattr(sess, "_piratewires_browser_required", False):
        raise requests.HTTPError(
            "Vercel challenge requires browser fetching for this session"
        )
    logger.info(f"Fetching page (requests): {url}")
    retry_policy = Retry()
    for attempt in range(4):
        resp = sess.get(url, timeout=20)
        if resp.status_code == 429:
            # A browser challenge is not a cooldown; stop repeating blocked requests.
            if resp.headers.get("x-vercel-mitigated") == "challenge":
                sess._piratewires_browser_required = True
                resp.raise_for_status()
            if attempt < 3:
                delay = retry_policy.get_retry_after(resp)
                if delay is None:
                    delay = 0 if attempt == 0 else 2 ** (attempt + 1)
                resp.close()
                time.sleep(delay)
                continue
        resp.raise_for_status()
        return resp.text
    raise RuntimeError(f"Could not fetch {url}")


def _wait_for_article_links(driver, timeout: int = 15) -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/p/']"))
    )


def fetch_page_selenium(url: str) -> str:
    driver = None
    try:
        logger.info(f"Fetching page (selenium): {url}")
        driver = setup_selenium_driver()
        driver.get(url)
        _wait_for_article_links(driver, timeout=20)

        # Match the initial HTTP listing; archive scrolling discovers older posts
        # that cannot fit in the current feed and needlessly increases requests.
        return driver.page_source
    finally:
        if driver:
            driver.quit()


def fetch_page(url: str, session: requests.Session | None = None) -> str:
    try:
        return fetch_page_requests(url, session=session)
    except Exception as e:
        logger.warning(f"Requests fetch failed ({e}); falling back to Selenium...")
        return fetch_page_selenium(url)


def _normalize_article_link(href: str) -> str | None:
    if not href:
        return None
    if href.startswith("/p/"):
        return urljoin(BASE_URL, href)
    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if parsed.netloc.endswith("piratewires.com") and parsed.path.startswith("/p/"):
            return href.split("?")[0].split("#")[0]
    return None


def parse_listing_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for a in soup.select("a[href]"):
        link = _normalize_article_link(a.get("href", "").strip())
        if link:
            links.append(link)
    # Deduplicate while preserving order
    seen = set()
    unique_links = []
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        unique_links.append(link)
    return unique_links


def _decode_payload_text(value: str) -> str:
    if not value:
        return value
    try:
        return value.encode("utf-8").decode("unicode_escape")
    except Exception:
        return value


def parse_listing_payload(html: str, category: str) -> list[dict]:
    items: list[dict] = []
    seen_links = set()
    needle = '\\"canonical_url\\":\\"https://piratewires.substack.com/p/'
    title_key = '\\"title\\":\\"'
    subtitle_key = '\\"subtitle\\":\\"'
    date_key = '\\"post_date\\":\\"'

    idx = 0
    while True:
        idx = html.find(needle, idx)
        if idx == -1:
            break

        slug_start = idx + len(needle)
        slug_end = html.find('\\"', slug_start)
        if slug_end == -1:
            break

        window_start = max(idx - 2000, 0)
        window = html[window_start:idx]

        title_pos = window.rfind(title_key)
        subtitle_pos = window.rfind(subtitle_key)
        date_pos = window.rfind(date_key)
        if title_pos == -1 or subtitle_pos == -1 or date_pos == -1:
            idx = slug_end + 1
            continue

        title_start = title_pos + len(title_key)
        subtitle_start = subtitle_pos + len(subtitle_key)
        date_start = date_pos + len(date_key)

        title_end = window.find('\\"', title_start)
        subtitle_end = window.find('\\"', subtitle_start)
        date_end = window.find('\\"', date_start)
        if title_end == -1 or subtitle_end == -1 or date_end == -1:
            idx = slug_end + 1
            continue

        title = _decode_payload_text(window[title_start:title_end])
        subtitle = _decode_payload_text(window[subtitle_start:subtitle_end])
        post_date = _decode_payload_text(window[date_start:date_end])
        slug = html[slug_start:slug_end]
        link = f"{BASE_URL}/p/{slug}"
        if link in seen_links:
            idx = slug_end + 1
            continue
        seen_links.add(link)
        items.append(
            {
                "title": title,
                "link": link,
                "date": _parse_date(post_date),
                "category": category,
                "description": subtitle,
            }
        )
        idx = slug_end + 1

    return items


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = dateparser.parse(text.strip())
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt
    except Exception:
        return None


def _fetch_article_api_browser(api_url: str, session: requests.Session) -> str:
    """Read the publisher's JSON in a browser when the API blocks a direct client."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    driver = getattr(session, "_piratewires_article_driver", None)
    if driver is None:
        driver = setup_selenium_driver()
        session._piratewires_article_driver = driver
        driver.set_page_load_timeout(45)
    logger.info(f"Fetching article API (browser): {api_url}")
    driver.get(api_url)

    def json_document(browser):
        text = browser.find_element(By.TAG_NAME, "pre").text
        try:
            return text if isinstance(json.loads(text), dict) else False
        except ValueError:
            return False

    try:
        return WebDriverWait(driver, 20).until(json_document)
    except Exception as exc:
        try:
            detail = (
                f"{driver.title}: {driver.find_element(By.TAG_NAME, 'body').text[:250]}"
            )
        except Exception:
            detail = "No readable browser response"
        raise RuntimeError(f"Publisher returned no article JSON: {detail}") from exc


def fetch_article_api(
    url: str, session: requests.Session | None = None, *, include_body: bool = True
) -> dict:
    """Use the same metered full-article request as the publisher's web client."""
    parsed = urlparse(url)
    if parsed.hostname not in (
        "www.piratewires.com",
        "piratewires.com",
    ) or not parsed.path.startswith("/p/"):
        raise ValueError(f"Unexpected Pirate Wires article URL: {url}")
    slug = parsed.path.removeprefix("/p/").strip("/")
    api_url = f"{ARTICLE_API_BASE}/{quote(slug, safe='')}"
    if include_body:
        api_url += "?withMetering=true"
    sess = session or build_requests_session()
    owned_session = session is None
    try:
        if getattr(sess, "_piratewires_article_driver", None) is not None:
            raw = _fetch_article_api_browser(api_url, sess)
        else:
            try:
                raw = fetch_page_requests(api_url, session=sess)
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code not in (403, 429):
                    raise
                logger.warning(
                    "Direct article API request blocked; using the publisher JSON in a browser"
                )
                raw = _fetch_article_api_browser(api_url, sess)
        payload = json.loads(raw)
    finally:
        if owned_session:
            driver = getattr(sess, "_piratewires_article_driver", None)
            if driver is not None:
                driver.quit()
            sess.close()
    post = payload.get("post")
    if not isinstance(post, dict) or post.get("slug") != slug:
        raise ValueError(f"Publisher returned the wrong article for {url}")
    title = post.get("title")
    body = post.get("body_html") if include_body else ""
    if not title or (
        include_body and not body and post.get("type") not in ("podcast", "video")
    ):
        raise ValueError(f"Publisher returned no article content for {url}")
    soup = BeautifulSoup(body or "", "html.parser")
    content = clean_article_html(soup, url, title=title)
    if body and not content:
        raise ValueError(f"Publisher returned no readable article content for {url}")
    authors = post.get("authors") or []
    return {
        "title": title,
        "description": post.get("subtitle") or extract_summary(soup, soup, title=title),
        "date": _parse_date(post.get("post_date") or ""),
        "author": ", ".join(
            a["name"] for a in authors if isinstance(a, dict) and a.get("name")
        ),
        "content_html": content,
    }


def fetch_article_page_requests(
    url: str, session: requests.Session | None = None
) -> str | None:
    sess = session or build_requests_session()
    if getattr(sess, "_piratewires_browser_required", False):
        return None
    try:
        return fetch_page_requests(url, session=sess)
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url}: {e}")
        return None


def fetch_articles_selenium(urls: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    if not urls:
        return results
    driver = None
    try:
        for attempt in range(2):
            try:
                driver = setup_selenium_driver()
                break
            except Exception as e:
                logger.warning(
                    f"Selenium driver startup failed (attempt {attempt + 1}): {e}"
                )
                time.sleep(2)
        if driver is None:
            logger.warning(
                "Could not start Selenium driver; keeping requests-fetched content"
            )
            return results
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        total = len(urls)
        start_time = time.perf_counter()
        for idx, url in enumerate(urls, start=1):
            try:
                logger.info(f"Selenium fetch {idx}/{total}: {url}")
                driver.get(url)
                body_selector = "section[class*='article_postBody']"
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, f"{body_selector}, article video")
                    )
                )
                # Video-only posts are already ready; text posts may expand after hydration.
                if driver.find_elements(By.CSS_SELECTOR, body_selector):
                    try:
                        WebDriverWait(driver, 20).until(
                            lambda d: d.execute_script(
                                "const el=document.querySelector(\"section[class*='article_postBody']\");"
                                "return el ? el.innerText.length : 0;"
                            )
                            > 1500
                        )
                    except Exception:
                        pass
                page_source = driver.page_source
                results[url] = page_source
                elapsed = time.perf_counter() - start_time
                logger.info(
                    f"Selenium fetched {idx}/{total} in {elapsed:.1f}s (html {len(page_source)} chars)"
                )
            except Exception as e:
                logger.warning(f"Selenium fetch failed for {url}: {e}")
    finally:
        if driver:
            driver.quit()
    return results


def _strip_paywall_footers(container) -> None:
    """Remove the sign-up/paywall footer blocks appended to article bodies."""
    footer_markers = (
        "enjoying this story",
        "sign up for free",
        "already have an account",
        "sign in",
    )
    for text_node in list(container.find_all(string=True)):
        text = text_node.strip().lower()
        if not text:
            continue
        if any(marker in text for marker in footer_markers):
            parent = getattr(text_node, "parent", None)
            if parent is None:
                continue
            wrapper = parent.find_parent(["section", "div", "aside"]) or parent
            wrapper.decompose()


def extract_article_metadata(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str | datetime] = {}
    needs_selenium = False

    title_el = soup.find("h1")
    if title_el:
        result["title"] = title_el.get_text(strip=True)

    excerpt_el = soup.select_one("[class*='article_excerpt']")
    if excerpt_el:
        result["description"] = excerpt_el.get_text(" ", strip=True)

    author_el = soup.select_one("[class*='article_bottom'] a")
    if author_el:
        result["author"] = author_el.get_text(strip=True)

    date_el = soup.select_one("[class*='article_bottom'] p")
    if date_el:
        parsed = _parse_date(date_el.get_text(strip=True))
        if parsed:
            result["date"] = parsed

    content_container = soup.select_one("section[class*='article_postBody']")
    if content_container:
        paragraphs = [
            p.get_text(" ", strip=True)
            for p in content_container.find_all("p")
            if p.get_text(strip=True)
        ]
        if len(paragraphs) < 6 or len(" ".join(paragraphs)) < 1200:
            needs_selenium = True

        _strip_paywall_footers(content_container)
        result["content_html"] = clean_article_html(
            content_container, page_url, title=result.get("title")
        )

        if "description" not in result:
            summary = extract_summary(
                soup, content_container, title=result.get("title")
            )
            if summary:
                result["description"] = summary

    if needs_selenium:
        result["needs_selenium"] = True

    return result


def collect_listing_articles(session: requests.Session | None = None) -> list[dict]:
    articles: list[dict] = []
    seen_links = set()

    for category, url in CATEGORY_PAGES.items():
        html = fetch_page(url, session=session)
        payload_items = parse_listing_payload(html, category)
        links = parse_listing_links(html)

        if not payload_items and not links:
            logger.warning(
                f"No links found via requests for {url}; retrying with Selenium..."
            )
            html = fetch_page_selenium(url)
            payload_items = parse_listing_payload(html, category)
            links = parse_listing_links(html)

        for item in payload_items:
            link = item["link"]
            if link in seen_links:
                continue
            seen_links.add(link)
            articles.append(item)

        for link in links:
            if link in seen_links:
                continue
            seen_links.add(link)
            articles.append(
                {
                    "title": link,
                    "link": link,
                    "date": None,
                    "category": category,
                    "description": "",
                }
            )

        logger.info(
            f"Collected {len(payload_items)} payload items and {len(links)} links from {category}"
        )

    return articles


def generate_rss_feed(articles, feed_name: str = "piratewires"):
    fg = build_feed(
        title="Pirate Wires",
        description="Pirate Wires - Culture, Politics, and Technology",
        site_url=BASE_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
    )
    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "piratewires") -> Path:
    feeds_dir = Path("feeds")
    feeds_dir.mkdir(exist_ok=True)
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    save_feed(feed_generator, output_file)
    return output_file


def main(force: bool = False) -> bool:
    article_session = None
    try:
        feeds_dir = Path("feeds")
        feeds_dir.mkdir(exist_ok=True)
        feed_path = feeds_dir / "feed_piratewires.xml"

        existing_entries = []
        if not force:
            existing_entries, _cache = load_cached_entries(feed_path)

        session = build_requests_session()
        articles = collect_listing_articles(session=session)
        if not articles:
            logger.warning(
                "No Pirate Wires articles parsed. Selectors may need updating."
            )
            return False

        # Resolve undated links before ranking; old archive links must not displace
        # known recent stories. Metadata requests do not request metered full bodies.
        article_session = build_requests_session()
        article_session.headers.update(
            {"Origin": BASE_URL, "Accept": "application/json"}
        )
        for article in articles:
            if not article.get("date"):
                metadata = fetch_article_api(
                    article["link"], session=article_session, include_body=False
                )
                if not metadata.get("date"):
                    raise ValueError(
                        f"Publisher returned no publication date for {article['link']}"
                    )
                article["date"] = metadata["date"]
        # Only fetch bodies for candidates that can enter the existing 50-item feed.
        articles.sort(key=lambda article: article["date"], reverse=True)
        articles = articles[:DEFAULT_MAX_ITEMS]
        # Refresh these bodies so feeds cached from the old preview path recover too.
        new_articles = articles
        logger.info(f"Fetching full content for {len(new_articles)} articles...")

        # Keep article requests separate from a front-end session challenged by Vercel.
        for article in new_articles:
            metadata = fetch_article_api(article["link"], session=article_session)
            for key in ("title", "description", "date", "author", "content_html"):
                if metadata.get(key):
                    article[key] = metadata[key]

        combined_articles = new_articles + existing_entries
        seen_links = set()
        deduped_articles = []
        for article in combined_articles:
            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            deduped_articles.append(article)

        # build_feed sorts newest-first and caps the item count.
        feed = generate_rss_feed(deduped_articles)
        save_rss_feed(feed)
    except Exception as e:
        logger.error(f"Failed to generate Pirate Wires RSS feed: {e}")
        return False
    finally:
        if article_session is not None:
            try:
                driver = getattr(article_session, "_piratewires_article_driver", None)
                if driver is not None:
                    driver.quit()
            except Exception as exc:
                logger.warning(f"Could not close article browser: {exc}")
            article_session.close()
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Pirate Wires RSS feed.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refetch all articles and rebuild the feed.",
    )
    args = parser.parse_args()
    raise SystemExit(0 if main(force=args.force) else 1)
