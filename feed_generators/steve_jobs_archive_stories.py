import argparse
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pytz
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from _common import (
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

BASE_URL = "https://stevejobsarchive.com"
LISTING_URL = f"{BASE_URL}/stories"
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b"
)


def in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory() -> Path:
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


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
    return session


def setup_selenium_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    browser_path = os.environ.get("CHROME_BINARY")
    return uc.Chrome(
        options=options,
        driver_executable_path=driver_path,
        browser_executable_path=browser_path,
        user_multi_procs=True,
    )


def fetch_page_requests(url: str, session: requests.Session | None = None) -> str:
    sess = session or build_requests_session()
    logger.info(f"Fetching page (requests): {url}")
    resp = sess.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_page_selenium(url: str) -> str:
    driver = None
    try:
        logger.info(f"Fetching page (selenium): {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "script#__NEXT_DATA__"))
            )
        except Exception:
            logger.warning("Could not confirm Next.js payload, continuing...")

        return driver.page_source
    finally:
        if driver:
            driver.quit()


def fetch_page(url: str, session: requests.Session | None = None) -> str:
    try:
        return fetch_page_requests(url, session=session)
    except Exception as e:
        if in_ci():
            raise
        logger.warning(f"Requests fetch failed ({e}); falling back to Selenium...")
        return fetch_page_selenium(url)


def _extract_next_data(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return None
    payload = script.string or script.text
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse __NEXT_DATA__: {e}")
        return None


def _normalize_story_link(slug: str) -> str | None:
    if not slug:
        return None
    slug = slug.strip()
    if not slug:
        return None
    if slug.startswith("http://") or slug.startswith("https://"):
        return slug
    return urljoin(BASE_URL + "/", slug.lstrip("/"))


def _is_story_link(link: str) -> bool:
    if not link:
        return False
    base = urlparse(BASE_URL)
    parsed = urlparse(link)
    if parsed.netloc and parsed.netloc != base.netloc:
        return False
    path = parsed.path or ""
    return path == "/stories" or path.startswith("/stories/")


def _rich_text_to_text(node) -> str:
    parts: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            if value.get("nodeType") == "text":
                text = value.get("value") or ""
                if text.strip():
                    parts.append(text)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    text = " ".join(parts)
    return re.sub(r"\s+", " ", text).strip()


def _find_first_hyperlink(node) -> str | None:
    if isinstance(node, dict):
        if node.get("nodeType") == "hyperlink":
            uri = node.get("data", {}).get("uri")
            if uri:
                return uri
        for child in node.values():
            found = _find_first_hyperlink(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_first_hyperlink(child)
            if found:
                return found
    return None


def parse_listing(html: str) -> list[dict]:
    data = _extract_next_data(html)
    if not data:
        logger.warning("No Next.js data found on listing page.")
        return []

    page_data = data.get("props", {}).get("pageProps", {}).get("pageData", {})
    modules = page_data.get("modulesCollection", {}).get("items", [])
    items: list[dict] = []

    for module in modules:
        typename = module.get("__typename")
        if typename == "ModuleMediaSplitMedia":
            title = (module.get("title") or "").strip()
            link = module.get("imageLinkUrl") or _find_first_hyperlink(module.get("links"))
            description = (module.get("subtitle") or "").strip()
            if not description:
                description = _rich_text_to_text(module.get("body", {}))
            link = _normalize_story_link(link) if link else None
            if not title or not link or not _is_story_link(link):
                continue
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "category": "Stories",
                }
            )
            continue
        if typename != "ModuleMediaGrid":
            continue
        grid_items = module.get("gridItemsCollection", {}).get("items", [])
        for entry in grid_items:
            title = (entry.get("title") or "").strip()
            slug = entry.get("slug") or entry.get("url")
            description = (entry.get("description") or entry.get("subtitle") or "").strip()
            link = _normalize_story_link(slug)
            if not title or not link or not _is_story_link(link):
                continue
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "category": "Stories",
                }
            )

    seen = set()
    deduped: list[dict] = []
    for item in items:
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        deduped.append(item)

    return deduped


# Residual text of the site's custom JS video player (no <video> tag is
# server-rendered, only controls chrome inside a <figure>).
_PLAYER_TEXT_RE = re.compile(r"video controls|seconds elapsed", re.IGNORECASE)

# Trailing recommendation section headings on story pages.
_EXPLORE_MORE_MARKERS = {"explore more", "more stories"}


def _strip_video_player_ui(container) -> None:
    for text_node in list(container.find_all(string=_PLAYER_TEXT_RE)):
        # A previous decompose may have destroyed this node already.
        if getattr(text_node, "parent", None) is None:
            continue
        target = text_node.find_parent("figure") or text_node.parent
        if target is not None and target is not container:
            target.decompose()


def _truncate_explore_more(container) -> None:
    """Remove the 'Explore more' cross-promo section and everything after it."""
    marker = None
    for el in container.find_all(["h2", "h3", "h4", "p", "div", "section"]):
        if el.get_text(" ", strip=True).lower().rstrip(":") in _EXPLORE_MORE_MARKERS:
            marker = el
            break
    if marker is None:
        return
    for sibling in list(marker.next_siblings):
        sibling.extract()
    node = marker.parent
    marker.decompose()
    while node is not None and node is not container:
        for sibling in list(node.next_siblings):
            sibling.extract()
        node = node.parent


def _extract_pub_date(container) -> datetime | None:
    if container is None:
        return None
    for tag in container.find_all(["p", "span", "div", "time"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 50:
            continue
        match = DATE_RE.search(text)
        if not match:
            continue
        try:
            dt = dateparser.parse(match.group(0))
        except (TypeError, ValueError):
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt
    return None


def extract_article_content(
    html: str, page_url: str, title: str | None = None
) -> tuple[str, str, datetime | None]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="main") or soup.find("main") or soup.find("article")
    if container is None:
        logger.warning("Could not locate article container; using full body.")
        container = soup.body or soup

    pub_date = _extract_pub_date(container)
    _strip_video_player_ui(container)
    _truncate_explore_more(container)
    content_html = clean_article_html(container, page_url, title=title)
    summary = extract_summary(soup, container, title=title)
    return content_html, summary, pub_date


def generate_rss_feed(articles: list[dict], feed_name: str):
    fg = build_feed(
        title="Steve Jobs Archive Stories",
        description="Selections of video and writing drawn from moments in Steve's life.",
        site_url=LISTING_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
        author={"name": "Steve Jobs Archive"},
    )
    logger.info("RSS feed generated successfully")
    return fg


def main(feed_name: str = "steve_jobs_archive_stories", force: bool = False) -> bool:
    try:
        feeds_dir = ensure_feeds_directory()
        feed_path = feeds_dir / f"feed_{feed_name}.xml"

        # Load the previous feed as cache and fail-safe: a failed listing or
        # article fetch must never shrink the feed.
        existing_items, cache = load_cached_entries(feed_path)
        existing_by_link = {item["link"]: item for item in existing_items}

        session = build_requests_session()
        articles: list[dict] = []
        try:
            listing_html = fetch_page(LISTING_URL, session=session)
            articles = parse_listing(listing_html)
        except Exception as e:
            logger.error(f"Failed to fetch/parse listing page: {e}")
        if not articles:
            logger.warning("No stories parsed from listing page; keeping cached items.")

        for article in articles:
            link = article["link"]
            prior = existing_by_link.get(link)
            cached = cache.get(link)
            if prior and prior.get("date"):
                article["date"] = prior["date"]

            if not force and cached and cached.get("content_html"):
                article["content_html"] = cached["content_html"]
                if cached.get("description"):
                    article["description"] = cached["description"]
                continue

            try:
                article_html = fetch_page(link, session=session)
            except Exception as e:
                logger.warning(f"Could not fetch article page {link}: {e}")
                article_html = None
            if not article_html:
                # Keep whatever we had for this story rather than dropping it.
                if cached and cached.get("content_html"):
                    article["content_html"] = cached["content_html"]
                    if cached.get("description"):
                        article["description"] = cached["description"]
                continue

            content_html, summary, pub_date = extract_article_content(
                article_html, link, title=article["title"]
            )
            if content_html:
                article["content_html"] = content_html
            elif cached and cached.get("content_html"):
                article["content_html"] = cached["content_html"]
            if summary:
                article["description"] = summary
            if pub_date:
                article["date"] = pub_date

        # Merge: fresh items plus cached items whose pages disappeared from the
        # listing (or that a failed run could not re-parse).
        fresh_links = {article["link"] for article in articles}
        merged = articles + [
            item for item in existing_items if item["link"] not in fresh_links
        ]
        if not merged:
            logger.error("No articles available (fresh or cached); aborting.")
            return False

        feed = generate_rss_feed(merged, feed_name)
        save_feed(feed, feed_path)
        logger.info(f"Successfully generated RSS feed with {len(merged)} items")
        return True
    except Exception as e:
        logger.error(f"Failed to generate Steve Jobs Archive Stories RSS: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Steve Jobs Archive Stories RSS feed.")
    parser.add_argument(
        "--feed-name",
        default="steve_jobs_archive_stories",
        help="Output feed name (feed_<name>.xml)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refetch all article pages (cached items whose pages disappeared are kept).",
    )
    args = parser.parse_args()
    raise SystemExit(0 if main(feed_name=args.feed_name, force=args.force) else 1)
