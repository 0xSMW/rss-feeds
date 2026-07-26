import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

import pytz
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup

from _common import (
    DEFAULT_MAX_ITEMS,
    build_feed,
    clean_article_html,
    extract_summary,
    feed_self_url,
    load_cached_entries,
    save_feed,
)

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Site-specific junk the generic cleaner misses on openai.com article pages:
# category-chip links above the title, tag-chip links below the body,
# screen-reader-only "(opens in a new window)" spans, and the empty
# listen-to-article audio player.
STRIP_SELECTORS = (
    "a.text-meta",
    'a[href*="news/?tags="]',
    ".sr-only",
    "audio",
)


def in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"

def setup_selenium_driver():
    """Set up Selenium WebDriver with undetected-chromedriver."""
    options = uc.ChromeOptions()
    options.add_argument("--headless")  # Ensure headless mode is enabled
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    browser_path = os.environ.get("CHROME_BINARY")
    return uc.Chrome(
        options=options,
        driver_executable_path=driver_path,
        browser_executable_path=browser_path,
        user_multi_procs=True,
    )

def build_requests_session() -> requests.Session:
    """Build a requests session with headers that mimic a real browser."""
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
            "Referer": "https://openai.com/",
        }
    )
    return session


def fetch_news_content_requests(url, session: requests.Session | None = None):
    """Fetch the HTML content via requests."""
    sess = session or build_requests_session()
    logger.info(f"Fetching content via requests: {url}")
    resp = sess.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text

def fetch_news_content_selenium(url):
    """Fetch the fully loaded HTML content of a webpage using Selenium."""
    driver = None
    try:
        logger.info(f"Fetching content from URL: {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href^='/index/']")))
        except Exception:
            logger.warning("Could not confirm OpenAI research items loaded, proceeding anyway...")

        html_content = driver.page_source
        logger.info("Successfully fetched HTML content")
        return html_content

    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        raise
    finally:
        if driver:
            driver.quit()

def fetch_article_page_requests(url: str, session: requests.Session | None = None) -> str | None:
    """Fetch HTML for a single article page via requests."""
    sess = session or build_requests_session()
    try:
        resp = sess.get(url, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url} ({e})")
        return None


def fetch_articles_selenium(urls: list[str]) -> dict[str, str]:
    """Fetch article pages via a single Selenium session."""
    results: dict[str, str] = {}
    if not urls:
        return results
    driver = None
    try:
        driver = setup_selenium_driver()
        for url in urls:
            try:
                driver.get(url)
                try:
                    from selenium.webdriver.support.ui import WebDriverWait
                    WebDriverWait(driver, 20).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except Exception:
                    logger.warning(f"Timed out waiting for page load: {url}")
                results[url] = driver.page_source
            except Exception as e:
                logger.warning(f"Selenium fetch failed for {url}: {e}")
    finally:
        if driver:
            driver.quit()
    return results


def fetch_article_selenium(url: str) -> str | None:
    """Fetch a single article page via Selenium."""
    driver = None
    try:
        driver = setup_selenium_driver()
        driver.get(url)
        try:
            from selenium.webdriver.support.ui import WebDriverWait
            WebDriverWait(driver, 20).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            logger.warning(f"Timed out waiting for page load: {url}")
        return driver.page_source
    except Exception as e:
        logger.warning(f"Selenium fetch failed for {url}: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def _prepare_container(container):
    """Site-specific fixups before the shared cleaner runs.

    openai.com uses Tailwind classes whose arbitrary values embed tokens like
    ``--toc-button-h`` / ``--header-h`` (e.g. on every section heading), and a
    literal ``toc-content-heading`` class on in-body headings. Those trip the
    generic chrome-token filter and would delete real content, so strip such
    styling-only classes up front. The actual table of contents lives in a
    <nav>, which the shared cleaner removes by tag name.

    Also remove the "Keep reading" recirculation section, a marker the shared
    related-section pass does not know about.
    """
    for tag in container.find_all(True, class_=True):
        tag["class"] = [
            c
            for c in tag["class"]
            if not any(ch in c for ch in ":[(") and "toc" not in c and "header-h" not in c
        ]
    for heading in container.find_all(["h2", "h3"]):
        if heading.get_text(" ", strip=True).lower().rstrip(":") == "keep reading":
            section = heading
            while section.parent is not None and section.parent is not container:
                section = section.parent
            section.decompose()
    return container


def extract_article_content(html: str, page_url: str, title: str | None = None) -> tuple[str, str]:
    """Extract main article content HTML and a plain-text summary."""
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one("main article")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup.select_one("[class*='content']")
    )
    if container is None:
        return "", ""
    container = _prepare_container(container)
    content_html = clean_article_html(
        container, page_url, title=title, strip_selectors=STRIP_SELECTORS
    )
    content_html = _drop_empty_headings(content_html)
    summary = extract_summary(soup, container, title=title)
    return content_html, summary


def _drop_empty_headings(content_html: str) -> str:
    """Remove headings left empty after cleaning (the shared empty-element
    pruning does not cover heading tags)."""
    if not content_html or "<h" not in content_html:
        return content_html
    fragment = BeautifulSoup(content_html, "html.parser")
    changed = False
    for heading in fragment.find_all(["h2", "h3", "h4", "h5", "h6"]):
        if not heading.get_text(strip=True) and not heading.find("img"):
            heading.decompose()
            changed = True
    return str(fragment).strip() if changed else content_html

def parse_openai_news_html(html_content):
    """Parse the HTML content from OpenAI's Research News page.

    The page structure (as of 2025-09) renders each card as an <a href="/index/..."> element
    that contains:
      - title in a div with class token 'text-h5'
      - category in the first span inside p.text-meta
      - date in a <time> tag with ISO 'datetime' attribute
    """
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []

    # Find anchors that link to individual posts
    news_items = soup.select("a[href^='/index/']")

    seen_links = set()
    for item in news_items:
        try:
            # Extract link
            href = item.get("href")
            if not href:
                continue
            link = "https://openai.com" + href
            if link in seen_links:
                continue

            # Extract title: robustly match any element whose class contains 'text-h5'
            title_elem = item.select_one("div.text-h5") or item.select_one("div[class*='text-h5']")
            if title_elem and title_elem.text.strip():
                title = title_elem.text.strip()
            else:
                # Fallback: derive from aria-label (format: "Title - Category - Mon d, YYYY")
                aria = item.get("aria-label", "").strip()
                title = aria.split(" - ")[0] if aria else None
            if not title:
                continue

            # Extract category
            cat_elem = item.select_one("p.text-meta span")
            category = (cat_elem.text.strip() if cat_elem and cat_elem.text else "Research")

            # Extract date from <time datetime="...">
            date_obj = None
            time_elem = item.select_one("time")
            if time_elem:
                dt_attr = time_elem.get("datetime", "").strip()
                if dt_attr:
                    try:
                        # Handle values like '2025-08-07T10:00' (no timezone) by assuming UTC
                        date_obj = datetime.fromisoformat(dt_attr)
                        if date_obj.tzinfo is None:
                            date_obj = date_obj.replace(tzinfo=pytz.UTC)
                    except Exception:
                        pass
            if date_obj is None:
                # Fallback to now (UTC) to avoid missing items
                logger.warning(f"Date not found or unparsable for: {title}; defaulting to now")
                date_obj = datetime.now(pytz.UTC)

            articles.append(
                {
                    "title": title,
                    "link": link,
                    "date": date_obj,
                    "category": category,
                    "description": title,
                }
            )
            seen_links.add(link)
        except Exception as e:
            logger.warning(f"Skipping an article due to parsing error: {e}")
            continue

    logger.info(f"Parsed {len(articles)} articles")
    return articles

def generate_rss_feed(articles, feed_name="openai_research"):
    """Generate RSS feed from parsed articles."""
    fg = build_feed(
        title="OpenAI Research News",
        description="Latest research news and updates from OpenAI",
        site_url="https://openai.com/news/research/",
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
        author={"name": "OpenAI"},
    )
    logger.info("RSS feed generated successfully")
    return fg

def save_rss_feed(feed_generator, feed_name="openai_research"):
    """Save RSS feed to an XML file."""
    feeds_dir = Path("feeds")
    feeds_dir.mkdir(exist_ok=True)
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    save_feed(feed_generator, output_file)
    return output_file


def main(limit: int = 500, test_first: bool = False, force: bool = False) -> bool:
    """Main function to generate OpenAI Research News RSS feed."""
    url = "https://openai.com/news/research/"
    if limit:
        url = f"{url}?limit={limit}"

    try:
        feeds_dir = Path("feeds")
        feeds_dir.mkdir(exist_ok=True)
        feed_path = feeds_dir / "feed_openai_research.xml"

        # The listing page only server-renders the most recent handful of
        # posts, so the existing feed is the archive of known items. --force
        # keeps those item skeletons (title/link/date/category) but drops the
        # cached content/descriptions so every emitted item is refetched.
        existing_entries, cache = load_cached_entries(feed_path)
        if force:
            logger.info("Force mode: refetching content for all emitted items")
            cache = {}
            # load_cached_entries also attaches the old content/description to
            # the entries themselves; drop those so everything is refetched.
            for entry in existing_entries:
                entry["content_html"] = None
                entry["description"] = entry["title"]
        existing_links = {entry["link"] for entry in existing_entries}

        session = build_requests_session()
        html_content = ""
        requests_blocked = False
        try:
            html_content = fetch_news_content_requests(url, session=session)
        except requests.HTTPError as e:
            status_code = getattr(e.response, "status_code", None)
            if status_code == 403:
                requests_blocked = True
                logger.warning("Requests blocked (403). Switching to Selenium for this run.")
            else:
                logger.warning(f"Requests listing fetch failed ({e})")
        except Exception as e:
            logger.warning(f"Requests listing fetch failed ({e})")

        if (requests_blocked or not html_content) and not in_ci():
            html_content = fetch_news_content_selenium(url)

        articles = parse_openai_news_html(html_content) if html_content else []
        if not articles and not in_ci():
            html_content = fetch_news_content_selenium(url)
            articles = parse_openai_news_html(html_content)

        if not articles:
            logger.warning("No articles were parsed. Check your selectors.")
            if not existing_entries:
                return False
            logger.warning("Falling back to existing feed entries.")

        if test_first:
            if not articles:
                logger.error("No articles available for --test-first.")
                return False
            article = articles[0]
            article_html = fetch_article_page_requests(article["link"], session=session)
            if not article_html and not in_ci():
                article_html = fetch_article_selenium(article["link"])
            if not article_html:
                logger.error("Failed to fetch first article content.")
                return False

            content_html, summary = extract_article_content(
                article_html, article["link"], title=article["title"]
            )
            print("TITLE:", article["title"])
            print("LINK:", article["link"])
            print("SUMMARY:", summary)
            print("CONTENT_SNIPPET:", content_html[:800])
            return True

        new_articles = [article for article in articles if article["link"] not in existing_links]
        merged = new_articles + existing_entries

        min_dt = datetime.min.replace(tzinfo=pytz.UTC)
        merged.sort(key=lambda item: item.get("date") or min_dt, reverse=True)

        # Only the newest DEFAULT_MAX_ITEMS make it into the feed, so only
        # fetch content for those.
        to_emit = merged[:DEFAULT_MAX_ITEMS]
        for article in to_emit:
            cached = cache.get(article["link"])
            if cached and cached.get("content_html"):
                article["content_html"] = cached["content_html"]
                if cached.get("description"):
                    article["description"] = cached["description"]

        to_fetch = [article for article in to_emit if not article.get("content_html")]
        logger.info(f"Fetching full content for {len(to_fetch)} of {len(to_emit)} feed items")
        needs_selenium = []
        for article in to_fetch:
            html = None
            if not requests_blocked:
                html = fetch_article_page_requests(article["link"], session=session)
            if not html:
                needs_selenium.append(article["link"])
                continue
            content_html, summary = extract_article_content(
                html, article["link"], title=article["title"]
            )
            if content_html:
                article["content_html"] = content_html
            if summary:
                article["description"] = summary

        if needs_selenium and not in_ci():
            selenium_html = fetch_articles_selenium(needs_selenium)
            for article in to_fetch:
                html = selenium_html.get(article["link"])
                if not html:
                    continue
                content_html, summary = extract_article_content(
                    html, article["link"], title=article["title"]
                )
                if content_html:
                    article["content_html"] = content_html
                if summary:
                    article["description"] = summary

        still_missing = [a["link"] for a in to_emit if not a.get("content_html")]
        if still_missing:
            logger.warning(f"{len(still_missing)} feed items still lack full content")

        feed = generate_rss_feed(to_emit)
        save_rss_feed(feed)
    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {e}")
        return False
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate OpenAI Research News RSS feed.")
    parser.add_argument("--limit", type=int, default=500, help="Listing page limit param")
    parser.add_argument(
        "--test-first",
        action="store_true",
        help="Fetch only the first article and print a content snippet.",
    )
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    raise SystemExit(0 if main(limit=args.limit, test_first=args.test_first, force=args.force) else 1)
